"""FastAPI server: REST routes of §10.1 and the waveform WebSocket of §10.2.

Per §3 the layers do not know about each other. This one owns HTTP, sessions and
message framing; it asks the trace store for values and never interprets them.
"""

from __future__ import annotations

import re
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from veritrace import __version__
from veritrace._native import MAX_PX
from veritrace.analysis import cone, params, vtq
from veritrace.api.search import hierarchy_level, search_signals, signal_json, subtree_signals
from veritrace.api.sessions import Session, SessionRegistry
from veritrace.config import WORK_DIR

#: Anything not safe in a downloaded filename.
_IDENT = re.compile(r"[^A-Za-z0-9_.-]+")

#: Emit a progress message every this many signals on a large wave request (P3:
#: no mute spinner).
PROGRESS_EVERY = 16

#: Headers for the one URL that serves two things: `/` answers a browser with
#: the application and everything else with JSON.
#:
#: `Vary: Accept` is the whole reason this constant exists. Without it a browser
#: caches whichever representation it saw first *under the URL alone* and hands
#: it back to any later request for the same URL — so a navigation to `/` filled
#: the cache with HTML, and the app's own `fetch("/")` was then answered out of
#: that cache, with the server never asked. The symptom is
#: `Unexpected token '<', "<!doctype "...` and no entry in the access log, which
#: is what makes it so confusing: the request that failed was never made.
#:
#: `no-store` on top, because the two representations are cheap to produce and
#: an index.html cached across a rebuild points at a bundle that no longer
#: exists. Hashed assets under `/assets/` are immutable and cache normally.
_NEGOTIATED = {"Vary": "Accept", "Cache-Control": "no-store"}


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


def _question(session: Session, text: str) -> tuple[str, int, str]:
    """Reduce a query to `(signal, time, headline)` — §8.16's hop included.

    `/query`, `/subtrace`, `/repro` and `/export` all ask the same thing of the
    same session, and a second copy of this would be a second opinion about what
    `@c1247` means or which signal a transaction question is really about.
    """
    if session.graph is None:
        raise HTTPException(
            status_code=409,
            detail="No RTL loaded. why() needs the design graph — start with --rtl.",
        )
    try:
        q = vtq.parse(text)
    except vtq.QueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if q.txn is not None:
        from veritrace.protocol import link as txn_link

        if session.protocol is None or not session.protocol.extractions:
            raise HTTPException(
                status_code=409, detail="No protocol interfaces were detected in this trace."
            )
        try:
            question = txn_link.question(
                session.protocol, session.txn_index, session.store, session.clock, q.txn
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return question.signal, question.time, question.headline

    # A partial name resolves the way §7.2 resolves one — unique suffix, or the
    # candidates. The query bar is typed into by hand like the terminal is, and
    # a `why(dut.full)` that worked in only one of the two would be a rule the
    # user has to remember rather than a rule the tool has.
    from veritrace.correlate.resolver import resolve_path

    signal, candidates = resolve_path(session.graph, q.signal)
    if signal is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{q.signal} could be any of: {', '.join(candidates[:8])}"
                if candidates
                else f"unknown signal: {q.signal}"
            ),
        )
    return signal, _resolve_time(session, q), ""


def _subtrace_of(session: Session, text: str):
    """§8.2's minimised, narrated chain for a question — the input to §11.5 and §12."""
    from veritrace.repro import narrate, subtrace

    signal, t, headline = _question(session, text)
    result = session.why(signal, t)
    sub = narrate.narrate(subtrace.minimise(result.root, session.store, session.clock))
    return signal, t, headline, result, sub


class ReproBody(BaseModel):
    # `validate` is the word the API wants and a method pydantic already owns on
    # BaseModel, so the field is named apart and aliased back.
    model_config = {"populate_by_name": True}

    vtq: str
    #: `auto` lets §8.3's table decide, which is what the button does.
    mode: str = Field(default="auto", pattern="^(auto|minimal|focused)$")
    #: Compiling and running takes seconds, so the UI asks for it explicitly.
    run_validation: bool = Field(default=True, alias="validate")


class DiffBody(BaseModel):
    """§8.7. The second trace is named by path; the first is the session's own."""

    trace: str
    strategy: str = Field(default="cycle", pattern="^(cycle|handshake|retire|manual)$")
    anchor: str | None = None
    #: §11.4's "ignore this signal", as globs. Counters and timestamps
    #: legitimately differ between two runs.
    ignore: list[str] = Field(default_factory=list)


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
        a browser navigating here gets the app, a client asking for JSON gets
        the JSON. No path is moved, and no second port or prefix is invented.

        **An explicit `application/json` wins over `text/html`.** A browser
        navigating here sends both — `text/html` first, `*/*` last — and so does
        `fetch` on some engines, which is how the app once asked for its own
        configuration and was handed its own index page back. Ranking the
        explicit request above the vague one makes the answer depend on what
        the caller asked for rather than on which browser it is.
        """
        payload = {
            "name": "veritrace",
            "version": __version__,
            "default_session": app.state.default_session_id,
            "max_px": MAX_PX,
        }
        index = _ui_dir() / "index.html"
        wants_html = "application/json" not in accept and "text/html" in accept
        if wants_html and index.is_file():
            return FileResponse(index, headers=_NEGOTIATED)
        return JSONResponse(payload, headers=_NEGOTIATED)

    @api.post("/session")
    def create_session(body: CreateSession) -> dict[str, Any]:
        try:
            session = registry.open(body.trace_path, body.rtl_paths, body.top)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"cannot open trace: {e}") from e
        return {"session_id": session.session_id, **session.status()}

    @api.get("/sessions")
    def sessions() -> dict[str, Any]:
        """Every trace this server has open — the trace picker of TAB 5 (§11.4).

        One server can hold several sessions (§10.1); without a way to list them
        the Diff tab would have no way to offer the second run except by making
        the user retype a path they already opened.
        """
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "trace": str(s.trace_path),
                    "name": s.trace_path.name,
                    "has_rtl": s.graph is not None,
                    "n_signals": s.store.n_signals,
                    "default": s.session_id == app.state.default_session_id,
                }
                for s in registry.all()
            ]
        }

    @api.get("/session/{session_id}/status")
    def status(session_id: str) -> dict[str, Any]:
        return require(session_id).status()

    @api.post("/session/{session_id}/diff")
    def diff_route(session_id: str, body: DiffBody) -> dict[str, Any]:
        """§8.7 — first divergence against another run, from this one's point of view.

        The other trace is opened as a session of its own, so it is analysed by
        exactly the same code as the first and stays open for the next question
        about it. It inherits this session's RTL: two runs of one design are the
        case §8.7 is for, and asking the user to name the sources twice would be
        asking them to get it wrong once.
        """
        from veritrace import diff as diff_mod

        session = require(session_id)
        try:
            other = registry.open(body.trace, session.rtl_paths, session.top)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"cannot open trace: {e}") from e
        if other.session_id == session.session_id:
            raise HTTPException(status_code=400, detail="that is the same trace")

        try:
            alignment = diff_mod.align(
                diff_mod.side(session.trace_path.name, session.store, session.clock),
                diff_mod.side(other.trace_path.name, other.store, other.clock),
                body.strategy,
                protocol_a=session.protocol,
                protocol_b=other.protocol,
                signal=body.anchor,
            )
        except diff_mod.AlignError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

        patterns = list(body.ignore) + list(getattr(session.config, "ignore", []) or [])
        report = diff_mod.compare(alignment, patterns)
        report.txn_divergences = diff_mod.compare_transactions(
            alignment, session.protocol, other.protocol
        )
        if report.first is not None and session.graph is not None:
            diff_mod.explain(report, session.graph, other.graph)
        return {
            "a": {"session_id": session.session_id, "name": session.trace_path.name},
            "b": {"session_id": other.session_id, "name": other.trace_path.name},
            **report.to_dict(),
        }

    @api.get("/session/{session_id}/hierarchy")
    def hierarchy(session_id: str, path: str | None = None) -> dict[str, Any]:
        """§11.3's left column: the design tree, one level at a time."""
        return hierarchy_level(require(session_id).store, path)

    @api.get("/session/{session_id}/hierarchy/signals")
    def hierarchy_signals(
        session_id: str,
        path: str | None = None,
        limit: int = Query(default=5000, ge=1, le=50_000),
    ) -> dict[str, Any]:
        """Every signal at or below one scope — ModelSim's `add wave -r`.

        The tree is lazy per level (§10.1), so the client cannot assemble this
        itself without one request per scope; and a viewer where a design is
        put on screen one signal at a time is not a viewer anybody uses.
        """
        rows = subtree_signals(require(session_id).store, path, limit)
        return {"path": path or "", "count": len(rows), "signals": rows}

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
        match = next((p for p in session.rtl_files() if p.name == target), None)
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
        """§10.1's signal-level commands: `why(...)` and the rest of the table.

        `why` is the one with its own grammar and its own result shape, so it
        keeps its own branch; everything else goes to the dispatch table in
        `analysis.signalq`, which calls the same modules the CLI and the REST
        routes call. Before this, the parser accepted all eighteen commands of
        §10.1 and the executor answered one — so `cone(top.ctrl.ready)` typed
        into the query bar came back "not supported" for an analysis the same
        session already had a button for.
        """
        from veritrace.analysis import signalq

        session = require(session_id)
        text = (body.vtq or "").strip()
        if not text.startswith("why"):
            try:
                pipeline = vtq.parse_pipeline(text)
            except vtq.QueryError:
                pipeline = None
            if pipeline is not None and pipeline.name in signalq.COMMANDS:
                try:
                    return {"query": body.vtq, **signalq.run(session, pipeline)}
                except vtq.QueryError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                except KeyError as e:
                    raise HTTPException(status_code=404, detail=str(e)) from e

        signal, t, headline = _question(session, body.vtq)
        return {
            "query": body.vtq,
            "signal": signal,
            "time": t,
            "headline": headline,
            **session.why(signal, t).to_dict(),
        }

    @api.post("/session/{session_id}/subtrace")
    def subtrace_route(session_id: str, body: QueryBody) -> dict[str, Any]:
        """§8.2's minimal subtrace, narrated — the steps Replay mode walks (§11.5).

        Separate from `/query` rather than folded into it: the tree is what the
        Causal tab draws and the subtrace is what the story is told from, and
        most `why()` calls never ask for the second.
        """
        from veritrace.repro import narrate

        session = require(session_id)
        signal, t, headline, _result, sub = _subtrace_of(session, body.vtq)
        return {
            "query": body.vtq,
            "signal": signal,
            "time": t,
            "headline": headline,
            "title": narrate.headline(sub),
            "symptom": narrate.symptom_paragraph(sub, body.vtq),
            "steps": narrate.steps(sub),
            **sub.to_dict(),
        }

    @api.post("/session/{session_id}/repro")
    def repro_route(session_id: str, body: ReproBody) -> dict[str, Any]:
        """§8.3's testbench, and — unless asked not to — the run that proves it."""
        from veritrace.repro import testbench

        session = require(session_id)
        _signal, _t, _headline, result, sub = _subtrace_of(session, body.vtq)
        sources = session.rtl_files()
        try:
            built = testbench.build(
                sub,
                session.graph,
                session.store,
                session.clock,
                session.elaboration,
                root=result.root,
                sources=sources,
                work=session.trace_path.parent / WORK_DIR / "repro",
                validate_it=body.run_validation and bool(sources),
                mode=None if body.mode == "auto" else body.mode,
            )
        except testbench.ReproError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return {"query": body.vtq, **built.to_dict()}

    @api.post("/session/{session_id}/export")
    def export_route(session_id: str, body: ReproBody) -> HTMLResponse:
        """§12's report, which §11.5 makes the export button of Replay mode."""
        from veritrace.api.sessions import sha256_files
        from veritrace.export import report as report_mod
        from veritrace.repro import testbench

        session = require(session_id)
        _signal, _t, headline, result, sub = _subtrace_of(session, body.vtq)
        sources = session.rtl_files()
        built = None
        try:
            built = testbench.build(
                sub,
                session.graph,
                session.store,
                session.clock,
                session.elaboration,
                root=result.root,
                sources=sources,
                work=session.trace_path.parent / WORK_DIR / "repro",
                validate_it=body.run_validation and bool(sources),
                mode=None if body.mode == "auto" else body.mode,
            )
        except testbench.ReproError:
            # One of §12's seven sections. The other six still stand.
            pass

        page = report_mod.build(
            result.root,
            sub,
            session.store,
            session.clock,
            query=headline or body.vtq,
            sources={p.name: p for p in sources},
            repro=built,
            findings=session.report,
            trace_path=session.trace_path,
            rtl_sha256=session.rtl_sha256 or (sha256_files(sources) if sources else None),
            top=getattr(session.graph, "top", "") or "",
        )
        name = _IDENT.sub("_", page.title) or "bug_report"
        return HTMLResponse(
            page.html,
            headers={"Content-Disposition": f'attachment; filename="{name}.html"'},
        )

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
        from veritrace.coverage import query as cov_query
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
            if pipeline.name in cov_query.COMMANDS:
                got = cov_query.run(session.integrity, session.coverage, pipeline)
                return {"query": body.vtq, **got}
            if pipeline.name in perf_query.COMMANDS:
                got = perf_query.run(
                    session.performance, session.protocol, session.wait_for, pipeline
                )
                return {"query": body.vtq, **got}
            if pipeline.name in mem_query.COMMANDS:
                root = session.config.root if session.config is not None else session.trace_path.parent
                got = mem_query.run(session.memory, pipeline, session.store, session.clock, root)
                return {"query": body.vtq, **got}
            if pipeline.name == "protocol":
                return {"query": body.vtq, **txn_query.protocol(session.protocol, pipeline)}
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

    @api.get("/session/{session_id}/coverage")
    def coverage(session_id: str) -> dict[str, Any]:
        """TAB 7 — §8.21's functional matrix, §8.12's imported code coverage, and
        the derived conditions for what neither of them reached.

        Computed on open like the other tabs, so this is a read. Both halves and
        the reasons a half is missing come back together: an empty section with
        no explanation is exactly the thing §11.4 says a coverage tool must not
        do.
        """
        session = require(session_id)
        if session.coverage is None:
            return {
                "functional": [],
                "code": None,
                "holes": [],
                "skipped": {},
                "errors": [e for e in (session.coverage_error, session.protocol_error) if e],
            }
        return {
            **session.coverage.to_dict(),
            # §8.37 renders in this tab, so it arrives with it. A second endpoint
            # would be a second thing to keep in step with the session.
            "plan": session.plan.to_dict() if session.plan is not None else None,
            "errors": [session.coverage_error] if session.coverage_error else [],
        }

    @api.get("/session/{session_id}/integrity")
    def integrity(session_id: str) -> dict[str, Any]:
        """§8.19's scoreboard: what was compared, and every mismatch."""
        session = require(session_id)
        if session.integrity is None:
            return {
                "interfaces": [],
                "mismatches": [],
                "compared_paths": [],
                "skipped": {},
                "errors": [e for e in (session.integrity_error, session.protocol_error) if e],
            }
        return {
            **session.integrity.to_dict(),
            "errors": [session.integrity_error] if session.integrity_error else [],
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

    @api.get("/session/{session_id}/stuck")
    def stuck_route(
        session_id: str,
        cycles: int = Query(default=..., ge=1, le=1_000_000),
    ) -> dict[str, Any]:
        """§8.4 at a threshold other than the configured one.

        The good answer depends on the run — three of the four reference
        designs are shorter than §8.4's 100-cycle default, so on those the
        check cannot fire at all. Until now the only way to try a narrower
        window was to edit `.veritrace.toml` and restart the server, which is
        the wrong shape for a knob whose right value is discovered by turning
        it. Only the stuck scan runs: it is one parallel pass over an already
        warm time cache, not the whole check suite.
        """
        from veritrace.analysis import stuck as stuck_mod

        session = require(session_id)
        if session.clock is None:
            return {
                "cycles": cycles,
                "findings": [],
                "unreachable": "no clock could be identified in this trace",
            }
        found = sorted(
            stuck_mod.scan(session.store, session.clock, session.graph, session.config, cycles),
            key=lambda f: f.sort_key,
        )
        suppressed = set(session.suppressions())
        return {
            "cycles": cycles,
            "n_cycles": session.clock.n_cycles,
            "unreachable": stuck_mod.too_short(
                session.store, session.clock, session.config, cycles
            ),
            "findings": [f.to_dict() for f in found if f.id not in suppressed],
        }

    @api.get("/session/{session_id}/correlation")
    def correlation_route(session_id: str) -> dict[str, Any]:
        """§7.2's rate, with the names behind it.

        The status bar has shown the percentage since the first prompt; what it
        could not do was answer the question the percentage provokes. A rate is
        only actionable next to the list it summarises — and §7.2's own remedy,
        the simulator dump flags of §4.0, is chosen by looking at *which*
        signals are missing.
        """
        session = require(session_id)
        if session.correlation is None:
            raise HTTPException(status_code=404, detail="this session has no RTL")
        return session.correlation.to_dict()

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

    @api.get("/session/{session_id}/fsm")
    def fsm_route(session_id: str) -> dict[str, Any]:
        """§8.8's machines, with the overlay when there is a trace — the FSM mode.

        A mode of the Source tab (§11.4), not a tab: the diagram is a view of the
        structure of the code, so it is served beside the code rather than as a
        domain of its own.
        """
        from veritrace.analysis import fsm as fsm_mod

        session = require(session_id)
        if session.graph is None:
            raise HTTPException(
                status_code=409,
                detail="No RTL loaded. FSM extraction reads the design graph — start with --rtl.",
            )
        machines = fsm_mod.extract(session.graph, session.elaboration, session.store)
        for m in machines:
            fsm_mod.overlay(m, session.store, session.graph, session.clock)
        return {"machines": [m.to_dict() for m in machines]}

    @api.get("/session/{session_id}/fsm/{signal}/svg")
    def fsm_svg(session_id: str, signal: str) -> HTMLResponse:
        """"Export SVG" from the FSM mode (§8.8) — for documentation and READMEs."""
        from veritrace.analysis import fsm as fsm_mod
        from veritrace.export import fsmsvg

        session = require(session_id)
        if session.graph is None:
            raise HTTPException(status_code=409, detail="No RTL loaded.")
        machines = fsm_mod.extract(session.graph, session.elaboration, session.store)
        machine = fsm_mod.find(machines, signal)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"no state machine on {signal}")
        fsm_mod.overlay(machine, session.store, session.graph, session.clock)
        name = _IDENT.sub("_", machine.signal)
        return HTMLResponse(
            fsmsvg.render(machine),
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'attachment; filename="{name}.svg"'},
        )

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
        # `no-store` on the shell for the same reason as `/`: it names the
        # hashed bundle, so a cached copy that survives a rebuild asks for a
        # file that is no longer there — and a hard reload becomes the only way
        # to start the app, which nobody should have to know.
        return FileResponse(index, headers=_NEGOTIATED)


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
