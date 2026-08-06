"""FastAPI server: REST routes of §10.1 and the waveform WebSocket of §10.2.

Per §3 the layers do not know about each other. This one owns HTTP, sessions and
message framing; it asks the trace store for values and never interprets them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgpack
from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from veritrace import __version__
from veritrace._native import MAX_PX
from veritrace.analysis import cone, params, vtq
from veritrace.analysis.whytrace import WhyTracer
from veritrace.api.search import hierarchy_level, search_signals, signal_json
from veritrace.api.sessions import Session, SessionRegistry

#: Emit a progress message every this many signals on a large wave request (P3:
#: no mute spinner).
PROGRESS_EVERY = 16


class CreateSession(BaseModel):
    trace_path: str
    rtl_paths: list[str] = Field(default_factory=list)
    top: str | None = None


class QueryBody(BaseModel):
    vtq: str


class SuppressBody(BaseModel):
    #: Mandatory. §11.4: a suppression without a reason turns the findings list
    #: into a graveyard nobody dares to empty.
    reason: str = Field(min_length=1)


def _rtl_files(session: Session) -> list[str]:
    """Every RTL file behind a session, expanded from what was passed in."""
    from veritrace.graph.elaborate import discover

    out: list[str] = []
    for p in session.rtl_paths:
        out.extend(str(f) for f in discover(p))
    return out


def _resolve_time(session: Session, q: "vtq.WhyQuery") -> int:
    """`@1247` is a timestamp; `@c1247` is the 1247th edge of the primary clock.

    §5.5, problem 3: a cycle number only means something against a named clock.
    The clock is resolved once when the session opens, so every part of the tool
    numbers cycles against the same signal.
    """
    _t0, t1 = session.store.time_range
    if q.time is None:
        return t1
    if not q.is_cycle:
        return q.time
    if session.clock is None:
        raise HTTPException(
            status_code=400,
            detail="Cycle times need a primary clock; none was found in this trace.",
        )
    at = session.clock.time_of(q.time)
    if at is None:
        raise HTTPException(status_code=400, detail="the primary clock never rises")
    return at


class LayoutBody(BaseModel):
    model_config = {"extra": "allow"}

    signals: list[Any] = Field(default_factory=list)
    groups: list[Any] = Field(default_factory=list)
    radix: dict[str, Any] = Field(default_factory=dict)
    bookmarks: list[Any] = Field(default_factory=list)
    cursors: list[Any] = Field(default_factory=list)
    zoom: Any = None


def create_app(
    default_trace: str | Path | None = None,
    rtl: list[str] | None = None,
    top: str | None = None,
) -> FastAPI:
    app = FastAPI(title="VeriTrace", version=__version__)
    registry = SessionRegistry()
    app.state.registry = registry

    default_session_id: str | None = None
    if default_trace is not None:
        default_session_id = registry.open(default_trace, rtl, top).session_id
    app.state.default_session_id = default_session_id

    def require(session_id: str) -> Session:
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return session

    api = APIRouter()

    @api.get("/")
    def root(accept: str = Header(default="")) -> Any:
        """The API root, and — for a browser — the application itself.

        §10.1 fixes the API paths, and a packaged install has to answer a bare
        URL with something a person can use. Content negotiation gives both:
        a browser navigating here sends `Accept: text/html` and gets the app;
        `fetch` sends `*/*` and gets the JSON. No path is moved, and no second
        port or prefix is invented.
        """
        payload = {
            "name": "veritrace",
            "version": __version__,
            "default_session": app.state.default_session_id,
            "max_px": MAX_PX,
        }
        index = _ui_dir() / "index.html"
        if "text/html" in accept and index.is_file():
            return FileResponse(index)
        return payload

    @api.post("/session")
    def create_session(body: CreateSession) -> dict[str, Any]:
        try:
            session = registry.open(body.trace_path, body.rtl_paths, body.top)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"cannot open trace: {e}") from e
        return {"session_id": session.session_id, **session.status()}

    @api.get("/session/{session_id}/status")
    def status(session_id: str) -> dict[str, Any]:
        return require(session_id).status()

    @api.get("/session/{session_id}/hierarchy")
    def hierarchy(session_id: str, path: str | None = None) -> dict[str, Any]:
        return hierarchy_level(require(session_id).store, path)

    @api.get("/session/{session_id}/signals")
    def signals(
        session_id: str,
        q: str = Query(default=""),
        limit: int = Query(default=200, ge=1, le=5000),
    ) -> dict[str, Any]:
        session = require(session_id)
        all_signals = session.store.signals()
        if q:
            rows = search_signals(all_signals, q, limit)
        else:
            rows = [signal_json(s) for s in all_signals[:limit]]
        return {"query": q, "count": len(rows), "total": session.store.n_signals, "signals": rows}

    @api.get("/session/{session_id}/source/{file:path}")
    def source(session_id: str, file: str) -> dict[str, Any]:
        """RTL text plus the signals declared or driven on each line.

        The decorations let the Source tab put a value inlay next to a line
        without the client having to parse SystemVerilog.
        """
        session = require(session_id)
        if session.graph is None:
            raise HTTPException(
                status_code=404,
                detail="No RTL loaded. Start the server with --rtl to read source.",
            )
        target = Path(file).name
        match = next((Path(p) for p in _rtl_files(session) if Path(p).name == target), None)
        if match is None or not match.is_file():
            raise HTTPException(status_code=404, detail=f"no RTL file named {target!r}")

        by_line: dict[int, list[dict[str, Any]]] = {}
        for sig in session.graph:
            for loc, role in [(sig.decl_loc, "decl")] + [(d.loc, "driver") for d in sig.drivers]:
                if loc and loc.file == match.name:
                    by_line.setdefault(loc.line, []).append(
                        {"path": sig.path, "handle": sig.trace_handle, "role": role}
                    )
        return {
            "file": match.name,
            "text": match.read_text(encoding="utf-8", errors="replace"),
            "signals": {str(k): v for k, v in sorted(by_line.items())},
        }

    @api.post("/session/{session_id}/query")
    def query(session_id: str, body: QueryBody) -> dict[str, Any]:
        """§10.1: `why(...)`, at signal or transaction level (§8.16)."""
        session = require(session_id)
        if session.graph is None:
            raise HTTPException(
                status_code=409,
                detail="No RTL loaded. why() needs the design graph — start with --rtl.",
            )
        try:
            q = vtq.parse(body.vtq)
        except vtq.QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        headline = ""
        if q.txn is not None:
            from veritrace.protocol import link as txn_link

            if session.protocol is None or not session.protocol.extractions:
                raise HTTPException(
                    status_code=409,
                    detail="No protocol interfaces were detected in this trace.",
                )
            try:
                question = txn_link.question(
                    session.protocol, session.txn_index, session.store, session.clock, q.txn
                )
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            signal, t, headline = question.signal, question.time, question.headline
        else:
            signal = q.signal
            if session.graph.get(signal) is None:
                raise HTTPException(status_code=404, detail=f"unknown signal: {signal}")
            t = _resolve_time(session, q)

        tracer = WhyTracer(session.graph, session.store, txn_index=session.txn_index)
        result = tracer.why(signal, t)
        return {
            "query": body.vtq,
            "signal": signal,
            "time": t,
            "headline": headline,
            **result.to_dict(),
        }

    @api.get("/session/{session_id}/transactions")
    def transactions(session_id: str) -> dict[str, Any]:
        """Detected interfaces and what each produced — TAB 8 (§11.4b)."""
        session = require(session_id)
        if session.protocol is None:
            return {
                "interfaces": [],
                "packs": [],
                "errors": [session.protocol_error] if session.protocol_error else [],
                "ms": 0,
            }
        return session.protocol.to_dict(with_transactions=False)

    @api.post("/session/{session_id}/transactions/query")
    def transactions_query(session_id: str, body: QueryBody) -> dict[str, Any]:
        """`txn(iface, type=WRITE) | slowest(10)` and the §8.17 commands — §10.1.

        One endpoint for both because they are one language: `txn(m0)` and
        `stalls(m0)` differ in what they return, not in how they are written, and
        splitting them would make the query bar need to know which is which.
        """
        from veritrace.memory import query as mem_query
        from veritrace.perf import query as perf_query
        from veritrace.protocol import query as txn_query

        session = require(session_id)
        if session.protocol is None:
            raise HTTPException(
                status_code=409, detail="protocol extraction did not run for this session"
            )
        try:
            pipeline = vtq.parse_pipeline(body.vtq)
            if pipeline.name in perf_query.COMMANDS:
                got = perf_query.run(
                    session.performance, session.protocol, session.wait_for, pipeline
                )
                return {"query": body.vtq, **got}
            if pipeline.name in mem_query.COMMANDS:
                root = session.config.root if session.config is not None else session.trace_path.parent
                got = mem_query.run(session.memory, pipeline, session.store, session.clock, root)
                return {"query": body.vtq, **got}
            result = txn_query.run(session.protocol, pipeline)
        except vtq.QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"query": body.vtq, "kind": "txn", **result.to_dict()}

    @api.get("/session/{session_id}/performance")
    def performance(session_id: str) -> dict[str, Any]:
        """§8.17's metrics and §8.18's wait-for graph — TAB 9 (§11.4b).

        Computed when the session opened, so this is a read. The wait-for graph
        travels alongside the report rather than inside it: TAB 9 draws the
        findings, and the graph is what the deadlock rows expand into.
        """
        session = require(session_id)
        if session.performance is None:
            return {
                "interfaces": [],
                "fairness": None,
                "liveness": {"deadlocks": [], "livelocks": [], "starvation": [], "skipped": {}},
                "wait_for": [],
                "errors": [
                    e
                    for e in (session.performance_error, session.protocol_error)
                    if e
                ],
                "ms": 0,
            }
        return {
            **session.performance.to_dict(),
            "wait_for": [e.to_dict() for e in session.wait_for],
            "errors": [session.performance_error] if session.performance_error else [],
        }

    @api.get("/session/{session_id}/memory")
    def memory(session_id: str) -> dict[str, Any]:
        """§8.20's bank timeline, command stream and timing violations — TAB 10
        (§11.4b).

        Computed when the session opened, so this is a read, same as
        `/performance`. `with_commands=False` keeps the summary cheap; the full
        decoded command stream is what `cmds(iface)` is for.
        """
        session = require(session_id)
        if not session.memory:
            return {
                "interfaces": [],
                "errors": [e for e in (session.memory_error, session.protocol_error) if e],
            }
        return {
            "interfaces": [r.to_dict(with_commands=False) for r in session.memory],
            "errors": [session.memory_error] if session.memory_error else [],
        }

    @api.get("/session/{session_id}/checks")
    def checks(session_id: str) -> dict[str, Any]:
        """Everything the session found on its own — TAB 6 (§11.4).

        Computed when the session opened, so this is a read, not a scan: §1.4
        wants the findings already there when the tab is first looked at.
        """
        session = require(session_id)
        report = session.findings()
        return {
            **report.to_dict(),
            "suppressed": session.suppressions(),
            "parameters": (
                params.tree(session.elaboration).to_dict()
                if session.elaboration is not None
                and params.tree(session.elaboration) is not None
                else None
            ),
        }

    @api.post("/session/{session_id}/checks/{finding_id}/suppress")
    def suppress(session_id: str, finding_id: str, body: SuppressBody) -> dict[str, Any]:
        session = require(session_id)
        try:
            suppressions = session.suppress(finding_id, body.reason)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"suppressed": suppressions}

    @api.delete("/session/{session_id}/checks/{finding_id}/suppress")
    def unsuppress(session_id: str, finding_id: str) -> dict[str, Any]:
        return {"suppressed": require(session_id).unsuppress(finding_id)}

    @api.get("/session/{session_id}/cone")
    def cone_route(
        session_id: str,
        signal: str,
        depth: int = Query(default=cone.DEFAULT_DEPTH, ge=1, le=32),
        direction: str = Query(default="fanin", pattern="^(fanin|fanout|both)$"),
        active_only: bool = Query(default=False),
        t0: int | None = None,
        t1: int | None = None,
    ) -> dict[str, Any]:
        """§8.6. `active_only` intersects the cone with the visible window."""
        session = require(session_id)
        if session.graph is None:
            raise HTTPException(
                status_code=409, detail="No RTL loaded. cone() needs the design graph."
            )
        lo, hi = session.store.time_range
        window = (t0 if t0 is not None else lo, t1 if t1 is not None else hi + 1)
        try:
            result = cone.cone(
                session.graph,
                signal,
                depth=depth,
                direction=direction,  # type: ignore[arg-type]
                store=session.store if active_only else None,
                window=window if active_only else None,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"unknown signal: {signal}") from e
        return result.to_dict()

    @api.get("/session/{session_id}/layout")
    def get_layout(session_id: str) -> dict[str, Any]:
        return require(session_id).load_layout()

    @api.put("/session/{session_id}/layout")
    def put_layout(session_id: str, body: LayoutBody) -> dict[str, Any]:
        session = require(session_id)
        return session.save_layout(body.model_dump())

    @api.websocket("/session/{session_id}/ws")
    async def ws(websocket: WebSocket, session_id: str) -> None:
        session = registry.get(session_id)
        await websocket.accept()
        if session is None:
            await websocket.send_bytes(
                msgpack.packb({"op": "error", "message": f"unknown session: {session_id}"})
            )
            await websocket.close()
            return
        try:
            while True:
                raw = await websocket.receive_bytes()
                try:
                    msg = msgpack.unpackb(raw, raw=False)
                except Exception as e:  # noqa: BLE001 - any decode failure is client error
                    await send(websocket, {"op": "error", "message": f"bad msgpack: {e}"})
                    continue
                if not isinstance(msg, dict):
                    await send(websocket, {"op": "error", "message": "expected a map"})
                    continue
                await dispatch(websocket, session, msg)
        except WebSocketDisconnect:
            return

    app.include_router(api)
    _mount_ui(app)
    return app


def _ui_dir() -> Path:
    """Where the built frontend lives inside the installed package.

    Absent in a source checkout until `make web-build` runs; present in every
    wheel, which is what makes `pip install veritrace && veritrace serve` a
    complete tool rather than a bare API (§13.4).
    """
    return Path(__file__).resolve().parent.parent / "web"


def _mount_ui(app: FastAPI) -> None:
    """Serve the built app, if this install has one.

    Mounted *after* the API router, so no static file can ever shadow a route
    of §10.1. The catch-all returns `index.html` for unknown paths, which is
    what a single-page app needs for a reload on a deep link to work.
    """
    ui = _ui_dir()
    index = ui / "index.html"
    if not index.is_file():
        return
    assets = ui / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        candidate = ui / path
        if path and candidate.is_file() and ui in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(index)


async def send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_bytes(msgpack.packb(payload, use_bin_type=True))


async def dispatch(websocket: WebSocket, session: Session, msg: dict[str, Any]) -> None:
    op = msg.get("op")
    if op == "wave":
        await handle_wave(websocket, session, msg)
    elif op == "ping":
        await send(websocket, {"op": "pong"})
    else:
        await send(websocket, {"op": "error", "message": f"unknown op: {op!r}"})


async def handle_wave(websocket: WebSocket, session: Session, msg: dict[str, Any]) -> None:
    """Stream one `wave_chunk` per requested signal.

    The pixel cap of §10.2 is enforced by the store itself, so no code path here
    can accidentally exceed it.
    """
    handles = msg.get("signals") or []
    if not isinstance(handles, list):
        await send(websocket, {"op": "error", "message": "signals must be a list"})
        return

    t_lo, t_hi = session.store.time_range
    try:
        t0 = int(msg.get("t0", t_lo))
        t1 = int(msg.get("t1", t_hi))
        px_width = int(msg.get("px_width", 1200))
    except (TypeError, ValueError):
        await send(websocket, {"op": "error", "message": "t0, t1 and px_width must be integers"})
        return

    if px_width < 1:
        await send(websocket, {"op": "error", "message": "px_width must be >= 1"})
        return

    total = len(handles)
    if total == 0:
        await send(websocket, {"op": "wave_chunk", "h": None, "transitions": [], "done": True})
        return

    for i, h in enumerate(handles):
        last = i == total - 1
        try:
            chunk = session.store.wave(int(h), t0, t1, px_width)
        except (KeyError, ValueError, TypeError) as e:
            await send(
                websocket,
                {"op": "error", "message": f"signal {h!r}: {e}", "h": h, "done": last},
            )
            continue

        await send(
            websocket,
            {
                "op": "wave_chunk",
                "h": int(h),
                "t0": t0,
                "t1": t1,
                "px_width": px_width,
                "mode": chunk["mode"],
                "initial": chunk["initial"],
                "transitions": chunk["transitions"],
                "done": last,
            },
        )

        if total > PROGRESS_EVERY and (i + 1) % PROGRESS_EVERY == 0 and not last:
            await send(
                websocket,
                {"op": "progress", "phase": "wave", "pct": round(100 * (i + 1) / total)},
            )
