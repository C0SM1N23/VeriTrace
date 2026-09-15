"""FastAPI server: REST routes of §10.1 and the waveform WebSocket of §10.2.

Per §3 the layers do not know about each other. This one owns HTTP, sessions and
message framing; it asks the trace store for values and never interprets them.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import shutil
import tempfile
import time
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt
from starlette.background import BackgroundTask

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


def _is_plain_why(text: str) -> bool:
    """Only ``why(``, not the separate ``why_not(`` command."""
    return re.match(r"^why\s*\(", text, re.IGNORECASE) is not None


class CreateSession(BaseModel):
    trace_path: str
    # Omitted means retain an existing session's RTL; [] explicitly requests
    # waveform-only mode. Do not erase this distinction at the JSON boundary.
    rtl_paths: list[str] | None = None
    top: str | None = None


class QueryBody(BaseModel):
    vtq: str


class ValuesBody(BaseModel):
    """Exact values for Source/Inspector, never downsampled display buckets."""

    handles: list[int] = Field(max_length=5000)
    time: int
    #: Negative browser-local handles map to an elaborated signal path.  They
    #: are never accepted without this map, so an arbitrary integer cannot be
    #: mistaken for a native trace handle.
    derived: dict[int, str] = Field(default_factory=dict)


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


def _causal_target(pipeline: "vtq.Pipeline") -> str:
    """The single causal question inside ``subtrace()`` or ``repro()``."""
    call = pipeline.source
    if pipeline.stages:
        raise vtq.QueryError(f"`{call.name}()` produces a report and cannot be piped")
    if call.kwargs or len(call.args) != 1:
        raise vtq.QueryError(f"`{call.name}()` needs exactly one signal/time question")
    target = str(call.args[0]).strip()
    if not target:
        raise vtq.QueryError(f"`{call.name}()` needs a signal/time question")
    return target if target.lower().startswith("why(") else f"why({target})"


def _why_not_of(
    session: Session,
    pipeline: "vtq.Pipeline",
    on_node: Any = None,
) -> dict[str, Any]:
    """Run §11.4's counterfactual through the same graph and trace as why()."""
    call = pipeline.source
    if pipeline.stages or call.kwargs or len(call.args) != 1:
        raise vtq.QueryError(
            "`why_not()` needs one comparison, for example "
            "why_not(top.ctrl.ready == 1 @ c1247), and cannot be piped"
        )
    target = str(call.args[0]).strip()
    wrapped = target if target.lower().startswith("why(") else f"why({target})"
    question = vtq.parse(wrapped)
    if question.txn is not None:
        raise vtq.QueryError("`why_not()` asks about a signal value, not a transaction")
    if question.value is None or question.op != "==":
        raise vtq.QueryError("`why_not()` needs the value that was wanted, using `signal == value`")

    signal, at, _headline = _question(session, wrapped)
    sig = session.graph.get(signal) if session.graph is not None else None
    if sig is None:
        raise vtq.QueryError(f"unknown signal: {signal}")
    bits = vtq.literal_bits(question.value, sig.width)
    if bits is None:
        raise vtq.QueryError(f"cannot read {question.value!r} as a {sig.width}-bit value")

    from veritrace.analysis.whytrace import bv_from_bits

    result = session.why_not(signal, at, bv_from_bits(bits, sig.width), on_node=on_node)
    return {
        "kind": "why_not",
        "signal": signal,
        "time": at,
        "expected": bits,
        **result.to_dict(),
    }


def _repro_of(
    session: Session,
    text: str,
    *,
    validate_it: bool,
    mode: str | None = None,
    timeout: float = 120.0,
):
    """Build §8.3 once for the dedicated route and the global VTQ command."""
    from veritrace.repro import testbench

    signal, at, headline, result, sub = _subtrace_of(session, text)
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
            validate_it=validate_it and bool(sources),
            timeout=timeout,
            mode=mode,
        )
    except testbench.ReproError as e:
        raise vtq.QueryError(str(e)) from e
    return signal, at, headline, built


def _diff_report(
    session: Session,
    other: Session,
    strategy: str,
    anchor: str | None,
    ignore: list[str],
    times_a: list[int] | None = None,
    times_b: list[int] | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    """One first-divergence implementation for the route and VTQ dispatcher."""
    from veritrace import diff as diff_mod

    if other.session_id == session.session_id:
        raise vtq.QueryError("that is the same trace")
    try:
        alignment = diff_mod.align(
            diff_mod.side(session.trace_path.name, session.store, session.clock),
            diff_mod.side(other.trace_path.name, other.store, other.clock),
            strategy,
            protocol_a=session.protocol,
            protocol_b=other.protocol,
            signal=anchor,
            times_a=times_a or [],
            times_b=times_b or [],
        )
    except diff_mod.AlignError as e:
        raise vtq.QueryError(str(e)) from e
    patterns = list(ignore) + list(getattr(session.config, "ignore", []) or [])
    try:
        report = diff_mod.compare(alignment, patterns)
        if focus is not None:
            diff_mod.select_focus(report, focus)
    except diff_mod.AlignError as e:
        raise vtq.QueryError(str(e)) from e
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


def _run_pipeline(
    session: Session,
    pipeline: "vtq.Pipeline",
    registry: SessionRegistry | None = None,
) -> dict[str, Any]:
    """Execute any non-``why`` VTQ command against one production session.

    The global query bar and the transaction endpoint used to have different
    dispatch tables.  As a result ``txn()``, ``stalls()``, ``cmds()`` and half
    the language worked in their tabs but failed in the bar §9.4 calls the
    application's spine.  One dispatcher keeps REST, WebSocket and the domain
    tabs on the same runtime path.
    """
    from veritrace.analysis import signalq
    from veritrace.coverage import query as cov_query
    from veritrace.memory import query as mem_query
    from veritrace.perf import query as perf_query
    from veritrace.protocol import query as txn_query

    if pipeline.name == "why_not":
        return _why_not_of(session, pipeline)

    if pipeline.name == "diff":
        call = pipeline.source
        if registry is None:
            raise vtq.QueryError("`diff()` needs the session registry")
        if pipeline.stages or len(call.args) != 1:
            raise vtq.QueryError("`diff()` needs exactly one other trace and cannot be piped")
        unknown = set(call.kwargs) - {"align", "anchor", "marks"}
        if unknown:
            raise vtq.QueryError(f"unknown diff option(s): {', '.join(sorted(unknown))}")
        try:
            other = registry.open(str(call.args[0]), session.rtl_paths, session.top)
        except (FileNotFoundError, ValueError, OSError) as e:
            raise vtq.QueryError(f"cannot open other trace: {e}") from e
        strategy = str(call.kwargs.get("align", "cycle"))
        times_a, times_b = [], []
        if "marks" in call.kwargs:
            from veritrace.diff.align import AlignError, parse_marks

            if strategy != "manual":
                raise vtq.QueryError("marks require align=manual")
            try:
                times_a, times_b = parse_marks(call.kwargs["marks"])
            except AlignError as exc:
                raise vtq.QueryError(str(exc)) from exc
        if strategy not in {"cycle", "handshake", "retire", "manual"}:
            raise vtq.QueryError(
                "diff align must be cycle, handshake, retire or manual"
            )
        return {
            "kind": "diff",
            **_diff_report(
                session,
                other,
                strategy,
                str(call.kwargs["anchor"]) if "anchor" in call.kwargs else None,
                [],
                times_a,
                times_b,
            ),
        }

    if pipeline.name == "subtrace":
        from veritrace.repro import narrate

        target = _causal_target(pipeline)
        signal, at, headline, _result, sub = _subtrace_of(session, target)
        return {
            "kind": "subtrace",
            "query": target,
            "signal": signal,
            "time": at,
            "headline": headline,
            "title": narrate.headline(sub),
            "symptom": narrate.symptom_paragraph(sub, target),
            "steps": narrate.steps(sub),
            **sub.to_dict(),
        }
    if pipeline.name == "repro":
        target = _causal_target(pipeline)
        signal, at, headline, built = _repro_of(session, target, validate_it=False)
        return {
            "kind": "repro",
            "query": target,
            "signal": signal,
            "time": at,
            "headline": headline,
            **built.to_dict(),
        }

    if pipeline.name in signalq.COMMANDS:
        return signalq.run(session, pipeline)
    if pipeline.name in cov_query.COMMANDS:
        return cov_query.run(session.integrity, session.coverage, pipeline)
    if pipeline.name in perf_query.COMMANDS:
        return perf_query.run(
            session.performance, session.protocol, session.wait_for, pipeline
        )
    if pipeline.name in mem_query.COMMANDS:
        root = session.config.root if session.config is not None else session.trace_path.parent
        return mem_query.run(session.memory, pipeline, session.store, session.clock, root)
    if pipeline.name == "protocol":
        if session.protocol is None:
            raise vtq.QueryError("protocol extraction did not run for this session")
        return txn_query.protocol(session.protocol, pipeline)
    if pipeline.name == "txn":
        if session.protocol is None:
            raise vtq.QueryError("protocol extraction did not run for this session")
        return {"kind": "txn", **txn_query.run(session.protocol, pipeline).to_dict()}
    known = sorted(
        set(signalq.COMMANDS)
        | set(cov_query.COMMANDS)
        | set(perf_query.COMMANDS)
        | set(mem_query.COMMANDS)
        | {"protocol", "txn", "subtrace", "repro", "diff", "why_not"}
    )
    raise vtq.QueryError(
        f"unknown command `{pipeline.name}()`; try {', '.join(known)}"
    )


class ReproBody(BaseModel):
    # `validate` is the word the API wants and a method pydantic already owns on
    # BaseModel, so the field is named apart and aliased back.
    model_config = {"populate_by_name": True}

    vtq: str
    #: `auto` lets §8.3's table decide, which is what the button does.
    mode: str = Field(default="auto", pattern="^(auto|minimal|focused)$")
    #: Compiling and running takes seconds, so the UI asks for it explicitly.
    run_validation: bool = Field(default=True, alias="validate")


class ExportBody(BaseModel):
    """The public §10.1 export contract.

    ``vtq`` remains accepted for clients released before the REST schema was
    completed; new clients use ``target``.  For the built-in formats the target
    is a VTQ question, except that SVG also accepts ``fsm(signal)`` or a bare
    FSM signal name.
    """

    model_config = {"populate_by_name": True}

    kind: str = "html"
    target: str | None = None
    vtq: str | None = None
    mode: str = Field(default="auto", pattern="^(auto|minimal|focused)$")
    run_validation: bool = Field(default=True, alias="validate")

    def query(self) -> str | None:
        return (self.target or self.vtq or "").strip() or None


class DiffBody(BaseModel):
    """§8.7. The second trace is named by path; the first is the session's own."""

    trace: str
    strategy: str = Field(default="cycle", pattern="^(cycle|handshake|retire|manual)$")
    anchor: str | None = None
    times_a: list[StrictInt] = Field(default_factory=list)
    times_b: list[StrictInt] = Field(default_factory=list)
    focus: str | None = None
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


class MemoryTimingBody(BaseModel):
    model_config = {"extra": "forbid"}
    iface: str
    chip: str | None = None
    toml: str | None = Field(default=None, max_length=64_000)


def _machines(session: Session) -> list[Any]:
    """Extract and overlay FSMs through the one production implementation."""
    from veritrace.analysis import fsm as fsm_mod

    if session.graph is None:
        raise HTTPException(
            status_code=409,
            detail="No RTL loaded. FSM extraction reads the design graph — start with --rtl.",
        )
    machines = fsm_mod.extract(session.graph, session.elaboration, session.store)
    for machine in machines:
        fsm_mod.overlay(machine, session.store, session.graph, session.clock)
    return machines


def _simulation_command(trace_path: Path) -> str:
    """Recall the exact command persisted by ``veritrace run``, if available."""
    try:
        return (Path(trace_path).parent / "sim.command").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        return ""


def _session_annotations(session: Session) -> list[str]:
    """Project text annotations included in the standalone report appendix."""
    from veritrace import notes

    root = Path(getattr(session.config, "root", None) or session.trace_path.parent)
    out: list[str] = []
    for path in notes.discover(root):
        try:
            out.extend(str(note) for note in notes.read(path))
        except OSError:
            # One optional notes file becoming unreadable after session open
            # must not turn a valid bug report into a fake successful download.
            continue
    return out


def _plugin_export(session: Session, kind: str, target: str | None) -> Response:
    """Run a custom exporter with the same production data as the CLI path."""
    from veritrace import plugin as plugin_mod

    root = Path(getattr(session.config, "root", None) or session.trace_path.parent)
    plugin_mod.clear()
    _analyses, errors = plugin_mod.discover(root)
    matches = [exporter for exporter in plugin_mod.exporters() if exporter.name == kind]
    if not matches:
        available = ", ".join(["html", "json", "svg", *(e.name for e in plugin_mod.exporters())])
        failed = f"; plugin load errors: {errors}" if errors else ""
        raise HTTPException(
            status_code=400,
            detail=f"unknown export format {kind!r}; available: {available}{failed}",
        )

    causal = None
    rendered_target = target
    if target is not None:
        _signal, _time, headline, causal, _sub = _subtrace_of(session, target)
        rendered_target = headline or target
    try:
        artifact = plugin_mod.run_exporter(
            matches[0],
            store=session.store,
            graph=session.graph,
            elaboration=session.elaboration,
            clock=session.clock,
            config=session.config,
            protocol=session.protocol,
            coverage=session.coverage,
            memory=session.memory,
            performance=session.performance,
            query=rendered_target,
            causal=causal,
            findings=session.report,
            metadata={
                "trace_path": str(session.trace_path),
                "trace_sha256": getattr(session.store, "source_sha256", ""),
                "top": getattr(session.graph, "top", "") if session.graph is not None else "",
            },
        )
    except plugin_mod.PluginError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    suffix = matches[0].extension.strip()
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        suffix = ".bin"
    media_types = {
        ".csv": "text/csv; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".txt": "text/plain; charset=utf-8",
    }
    name = _IDENT.sub("_", matches[0].name) or "veritrace_export"
    return Response(
        artifact,
        media_type=media_types.get(suffix.lower(), "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{name}{suffix}"'},
    )


def _derived_signal(session: Session, path: str) -> Any:
    """Resolve one reconstructed RTL signal, refusing guesses and dumped rows."""
    if session.graph is None:
        raise ValueError("derived waveform needs RTL")
    from veritrace.correlate.resolver import resolve_path

    signal, candidates = resolve_path(session.graph, path)
    if signal is None:
        if candidates:
            raise ValueError(f"{path} is ambiguous: {', '.join(candidates[:8])}")
        raise ValueError(f"unknown RTL signal: {path}")
    sig = session.graph.get(signal)
    if sig is None or not sig.is_reconstructible:
        raise ValueError(f"{signal} is not an undumped reconstructible signal")
    return sig


def _derived_wave(
    session: Session, path: str, t0: int, t1: int, px_width: int
) -> dict[str, Any]:
    """Evaluate an undumped combinational signal over a real trace window.

    Every dumped leaf read by every possible driver is used as an event source.
    The expression is then evaluated at the settled value of each unique event
    timestamp.  This is the waveform counterpart of why-trace's §7.3
    reconstruction, not interpolation from the one causal sample the user
    happened to click.
    """
    from veritrace.analysis.whytrace import TraceView
    from veritrace.graph.model import refs

    sig = _derived_signal(session, path)
    leaves: set[int] = set()
    visiting: set[str] = set()

    def collect(node: Any) -> None:
        if node.path in visiting:
            return
        if node.trace_handle is not None:
            leaves.add(node.trace_handle)
            return
        visiting.add(node.path)
        try:
            for driver in node.drivers:
                for sid in (*refs(driver.guard), *refs(driver.value)):
                    dep = session.graph.get(sid.path())
                    if dep is not None:
                        collect(dep)
        finally:
            visiting.discard(node.path)

    collect(sig)
    times = {t0}
    for handle in leaves:
        times.update(t for t, _value in session.store.transitions(handle, t0, t1))

    view = TraceView(session.graph, session.store)

    def value_at(t: int) -> str:
        value = view.value(sig.id, t)
        # A hold on an undumped sequential register cannot be reconstructed;
        # paint X rather than extending the last inferred assignment as fact.
        out = str(value) if value is not None else "x"
        view._cache.clear()  # bound memory to one timestamp on huge windows
        return out

    initial = value_at(t0)
    points: list[tuple[int, str]] = []
    previous = initial
    for t in sorted(times):
        value = value_at(t)
        if value != previous:
            points.append((t, value))
            previous = value

    if len(points) <= px_width:
        return {"mode": "exact", "initial": initial, "transitions": points}

    # Pixel-bounded min/max buckets, matching the native wire shape. Unknown
    # reconstructed values carry the X flag; known values retain numeric order.
    span = max(t1 - t0, 1)
    buckets: dict[int, list[tuple[int, str]]] = {}
    for t, value in points:
        at = min(px_width - 1, max(0, ((t - t0) * px_width) // span))
        buckets.setdefault(at, []).append((t, value))
    reduced: list[tuple[int, str, str, int, int]] = []
    for rows in buckets.values():
        known = [(int(v, 2), v) for _t, v in rows if v and set(v) <= {"0", "1"}]
        flags = 1 if len(known) != len(rows) else 0
        if known:
            lo = min(known)[1]
            hi = max(known)[1]
        else:
            lo = hi = "x"
        reduced.append((rows[0][0], lo, hi, len(rows), flags))
    return {"mode": "minmax", "initial": initial, "transitions": reduced}


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
            pending = registry.status(session_id)
            if pending is not None:
                detail = (
                    pending.get("error")
                    if pending.get("phase") == "error"
                    else f"session is still {pending.get('phase', 'opening')}"
                )
                raise HTTPException(status_code=409, detail=detail)
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
            session_id, status = registry.start(body.trace_path, body.rtl_paths, body.top)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"cannot open trace: {e}") from e
        return {"session_id": session_id, **status}

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
        value = registry.status(session_id)
        if value is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return value

    @api.post("/session/{session_id}/diff")
    def diff_route(session_id: str, body: DiffBody) -> dict[str, Any]:
        """§8.7 — first divergence against another run, from this one's point of view.

        The other trace is opened as a session of its own, so it is analysed by
        exactly the same code as the first and stays open for the next question
        about it. It inherits this session's RTL: two runs of one design are the
        case §8.7 is for, and asking the user to name the sources twice would be
        asking them to get it wrong once.
        """
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
            return _diff_report(
                session,
                other,
                body.strategy,
                body.anchor,
                body.ignore,
                body.times_a,
                body.times_b,
                body.focus,
            )
        except vtq.QueryError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

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

    @api.post("/session/{session_id}/values")
    def values(session_id: str, body: ValuesBody) -> dict[str, Any]:
        """Settled, exact values at one time for Source and Inspector.

        Waveform chunks are deliberately reduced to pixel resolution.  They
        are therefore drawing data, not an authority for value inlays.  This
        small batched endpoint keeps the UI on the trace store's exact
        ``value_at`` semantics without one HTTP request per signal.
        """
        session = require(session_id)
        rows: list[dict[str, Any]] = []
        for handle in dict.fromkeys(body.handles):
            try:
                if handle < 0:
                    path = body.derived.get(handle)
                    if path is None:
                        raise ValueError("negative handle needs a derived signal path")
                    from veritrace.analysis.whytrace import TraceView

                    sig = _derived_signal(session, path)
                    inferred = TraceView(session.graph, session.store).value(sig.id, body.time)
                    bits = str(inferred) if inferred is not None else "x"
                else:
                    value = session.store.value_at(handle, body.time)
                    bits = value.bits if value is not None else None
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(status_code=404, detail=f"signal {handle}: {e}") from e
            rows.append(
                {
                    "handle": handle,
                    "value": bits,
                }
            )
        return {"time": body.time, "values": rows}

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
        requested = file.replace("\\", "/").removeprefix("./")
        known = sorted({p.resolve() for p in [*session.rtl_files(),
                       *getattr(session.elaboration, "sources", [])] if p.is_file()})
        root = Path(getattr(session.config, "root", session.trace_path.parent)).resolve()

        def names(path: Path) -> set[str]:
            out = {path.name, path.as_posix()}
            try:
                out.add(path.relative_to(root).as_posix())
            except ValueError:
                pass
            return out

        matches = [p for p in known if requested in names(p)]
        if not matches:
            raise HTTPException(status_code=404, detail=f"no loaded RTL file named {requested!r}")
        if len(matches) > 1:
            choices = []
            for path in matches:
                try:
                    choices.append(path.relative_to(root).as_posix())
                except ValueError:
                    choices.append(path.as_posix())
            raise HTTPException(
                status_code=409,
                detail=f"RTL file name {requested!r} is ambiguous; use one of: {', '.join(choices)}",
            )
        match = matches[0]
        target = (match.as_posix() if sum(p.name == match.name for p in known) > 1
                  else match.name)

        by_line: dict[int, list[dict[str, Any]]] = {}
        for sig in session.graph:
            for loc, role in [(sig.decl_loc, "decl")] + [(d.loc, "driver") for d in sig.drivers]:
                if loc and loc.file.replace("\\", "/") in names(match):
                    by_line.setdefault(loc.line, []).append(
                        {"path": sig.path, "handle": sig.trace_handle, "role": role}
                    )
        return {
            "file": target,
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
        session = require(session_id)
        text = (body.vtq or "").strip()
        if not _is_plain_why(text):
            try:
                pipeline = vtq.parse_pipeline(text)
                return {"query": body.vtq, **_run_pipeline(session, pipeline, registry)}
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
        session = require(session_id)
        try:
            _signal, _time, _headline, built = _repro_of(
                session,
                body.vtq,
                validate_it=body.run_validation,
                mode=None if body.mode == "auto" else body.mode,
            )
        except vtq.QueryError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return {"query": body.vtq, **built.to_dict()}

    @api.post("/session/{session_id}/export")
    def export_route(session_id: str, body: ExportBody) -> Response:
        """Export a real session artifact through the §10.1 format contract.

        HTML and causal SVG reuse the exact cached why-tree behind the Causal
        tab. JSON executes the same VTQ dispatcher as ``/query``. Custom kinds
        are exporter plugins discovered from the session's project, rather
        than an API-only imitation of the CLI exporter path.
        """
        from veritrace.api.sessions import sha256_files
        from veritrace.export import report as report_mod
        from veritrace.repro import testbench

        session = require(session_id)
        kind = body.kind.strip().lower()
        target = body.query()

        if kind in {"csv", "parquet"}:
            # TAB 8 exports the same transaction table that extraction wrote,
            # not a UI-shaped summary.  Accept the visible interface name and
            # the query-language spelling so API and browser clients need no
            # private target convention.
            if target is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{kind.upper()} export needs a transaction interface target",
                )
            requested = target
            match = re.fullmatch(r"\s*txn\s*\(\s*([^,()]+?)\s*\)\s*", target, re.I)
            if match is not None:
                requested = match.group(1).strip().strip("\"'")
            elif "(" in target or ")" in target:
                raise HTTPException(
                    status_code=400,
                    detail="transaction export accepts an interface name or txn(interface)",
                )
            if session.protocol is None:
                raise HTTPException(status_code=409, detail="protocol extraction did not run")
            extraction = session.protocol.get(requested)
            if extraction is None:
                known = ", ".join(
                    e.interface.name for e in session.protocol.extractions
                ) or "none"
                raise HTTPException(
                    status_code=404,
                    detail=f"no interface named `{requested}`; detected: {known}",
                )

            from veritrace import clocks
            from veritrace.protocol import persist

            iface_clock = (
                clocks.clock_at(session.store, extraction.interface.clock)
                if extraction.interface.clock
                else None
            ) or session.clock
            columns, _origin = persist.to_columns(extraction, iface_clock)
            safe_name = _IDENT.sub("_", extraction.interface.name) or "transactions"

            if kind == "csv":
                out = io.StringIO(newline="")
                writer = csv.writer(out, lineterminator="\n")
                writer.writerow(name for name, _values in columns)
                writer.writerows(zip(*(values for _name, values in columns), strict=True))
                return Response(
                    out.getvalue(),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f'attachment; filename="{safe_name}.csv"'
                    },
                )

            # Normally this is exactly the immutable table produced while the
            # session opened.  A read-only trace directory can prevent that
            # cache write; exporting must still work, so materialise the same
            # columns in a disposable file and remove it after the response.
            persisted = Path(extraction.parquet) if extraction.parquet else None
            if persisted is not None and persisted.is_file():
                return FileResponse(
                    persisted,
                    media_type="application/vnd.apache.parquet",
                    filename=f"{safe_name}.parquet",
                )
            from veritrace._native import write_txn_table

            temp_dir = Path(tempfile.mkdtemp(prefix="veritrace-export-"))
            generated = temp_dir / f"{safe_name}.parquet"
            try:
                write_txn_table(str(generated), columns)
            except Exception:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise
            return FileResponse(
                generated,
                media_type="application/vnd.apache.parquet",
                filename=generated.name,
                background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
            )

        if kind == "json":
            if target is None:
                raise HTTPException(status_code=400, detail="JSON export needs a VTQ target")
            command = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\(", target)
            if command is not None and command.group(1).lower() != "why":
                try:
                    pipeline = vtq.parse_pipeline(target)
                    payload = {"query": target, **_run_pipeline(session, pipeline, registry)}
                except vtq.QueryError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                except KeyError as e:
                    raise HTTPException(status_code=404, detail=str(e)) from e
            else:
                signal, t, headline, result, sub = _subtrace_of(session, target)
                payload = {
                    "query": target,
                    "signal": signal,
                    "time": t,
                    "headline": headline,
                    **result.to_dict(),
                    "subtrace": sub.to_dict(),
                }
            return JSONResponse(
                payload,
                headers={"Content-Disposition": 'attachment; filename="veritrace.json"'},
            )

        if kind == "svg":
            if target is None:
                raise HTTPException(status_code=400, detail="SVG export needs a target")
            from veritrace.analysis import fsm as fsm_mod
            from veritrace.export import fsmsvg

            requested = target
            match = re.fullmatch(r"\s*fsm\s*\(\s*([^()]+?)\s*\)\s*", target, re.I)
            if match is not None:
                requested = match.group(1)
            machine = fsm_mod.find(_machines(session), requested)
            if match is not None or machine is not None:
                if machine is None:
                    raise HTTPException(status_code=404, detail=f"no state machine on {requested}")
                svg = fsmsvg.render(machine)
                name = _IDENT.sub("_", machine.signal) or "fsm"
            else:
                signal, _t, _headline, _result, sub = _subtrace_of(session, target)
                svg = report_mod.waveform_svg(session.store, sub, session.clock)
                name = _IDENT.sub("_", signal) or "causal_waveform"
            return Response(
                svg,
                media_type="image/svg+xml",
                headers={"Content-Disposition": f'attachment; filename="{name}.svg"'},
            )

        if kind != "html":
            return _plugin_export(session, kind, target)

        if target is None:
            raise HTTPException(status_code=400, detail="HTML export needs a causal target")
        _signal, _t, headline, result, sub = _subtrace_of(session, target)
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
            query=headline or target,
            sources={p.resolve().as_posix(): p for p in
                     [*sources, *getattr(session.elaboration, "sources", [])]},
            repro=built,
            findings=session.report,
            trace_path=session.trace_path,
            rtl_sha256=session.rtl_sha256 or (sha256_files(sources) if sources else None),
            top=getattr(session.graph, "top", "") or "",
            command=_simulation_command(session.trace_path),
            annotations=_session_annotations(session),
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
        session = require(session_id)
        try:
            pipeline = vtq.parse_pipeline(body.vtq)
            return {"query": body.vtq, **_run_pipeline(session, pipeline, registry)}
        except vtq.QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @api.get("/session/{session_id}/performance")
    def performance(
        session_id: str, t0: int | None = None, t1: int | None = None
    ) -> dict[str, Any]:
        """§8.17's metrics and §8.18's wait-for graph — TAB 9 (§11.4b).

        Computed when the session opened, so this is a read. The wait-for graph
        travels alongside the report rather than inside it: TAB 9 draws the
        findings, and the graph is what the deadlock rows expand into.
        """
        session = require(session_id)
        if (t0 is None) != (t1 is None) or (t0 is not None and t1 is not None and t1 < t0):
            raise HTTPException(status_code=400, detail="performance window needs t0 <= t1")
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
        report = session.performance.to_dict()
        if t0 is not None and t1 is not None and session.protocol is not None:
            from veritrace.perf import metrics

            extractions = [metrics.in_window(ex, t0, t1) for ex in session.protocol.extractions]
            report["interfaces"] = [
                metrics.measure(ex, session.store, session.clock).to_dict() for ex in extractions
            ]
            report["fairness"] = metrics.fairness(extractions).to_dict()
        return {
            **report,
            "window": {"t0": t0, "t1": t1} if t0 is not None else None,
            "liveness_scope": "whole run",
            "wait_for": [e.to_dict() for e in session.wait_for],
            "errors": [session.performance_error] if session.performance_error else [],
        }

    @api.get("/session/{session_id}/performance/history")
    def performance_history(
        session_id: str,
        iface: str = Query(min_length=1),
        metric: str = "p99_latency",
        limit: int = Query(default=40, ge=2, le=500),
    ) -> dict[str, Any]:
        """The selected metric over recorded runs — §11.4 and §13.6.

        Reads the exact DuckDB written by ``veritrace record``.  Missing or
        damaged history is an explicit empty state, never a flat zero series.
        """
        session = require(session_id)
        from veritrace import regress

        root = getattr(session.config, "root", None) or Path.cwd()
        path = Path(root) / regress.DEFAULT_DB
        try:
            return regress.performance_history(path, iface, metric, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/session/{session_id}/memory")
    def memory(session_id: str) -> dict[str, Any]:
        """§8.20's bank timeline, command stream and timing violations — TAB 10
        (§11.4b).

        Computed when the session opened, so this is a read, same as
        `/performance`. `with_commands=False` keeps the summary cheap; the full
        decoded command stream is what `cmds(iface)` is for.
        """
        from veritrace.memory import timing
        from veritrace.memory.arrays import inventory

        session = require(session_id)
        timing_errors: list[str] = []
        chips = timing.discover(getattr(session.config, "root", None) or session.trace_path.parent,
                                errors=timing_errors)
        selection = session.layout_file.load().get("memoryTiming", {})
        catalog = {"chips": [c.to_dict() for c in chips], "timing_errors": timing_errors,
                   "selection": selection, "arrays": inventory(session.graph),
                   "rtl_error": session.rtl_error}
        if not session.memory:
            return {
                **catalog,
                "interfaces": [],
                "errors": [e for e in (session.memory_error, session.protocol_error) if e],
            }
        return {
            **catalog,
            "interfaces": [r.to_dict(with_commands=False) for r in session.memory],
            "errors": [session.memory_error] if session.memory_error else [],
        }

    @api.get("/session/{session_id}/memory/array")
    def memory_array(session_id: str, path: str, time: int = Query(ge=0),
                     offset: int = Query(default=0, ge=0), count: int = Query(default=64, ge=1, le=256)):
        from veritrace.memory.arrays import sample

        session = require(session_id)
        try:
            return sample(session.graph, session.store, path, time, offset, count)
        except KeyError as exc:
            raise HTTPException(404, detail=f"Unknown RTL memory: {path}") from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @api.post("/session/{session_id}/memory/timing")
    def memory_timing(session_id: str, body: MemoryTimingBody) -> dict[str, Any]:
        from veritrace.memory.timing import TimingError

        choice = body.model_dump(exclude_none=True, exclude={"iface"})
        try:
            require(session_id).set_memory_timing(body.iface, choice)
        except TimingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=f"could not persist timing selection: {exc}") from exc
        return memory(session_id)

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
        session = require(session_id)
        machines = _machines(session)
        return {"machines": [m.to_dict() for m in machines]}

    @api.get("/session/{session_id}/fsm/{signal}")
    def fsm_signal(session_id: str, signal: str) -> dict[str, Any]:
        """The §10.1 per-signal FSM resource (nodes, edges and trace overlay)."""
        from veritrace.analysis import fsm as fsm_mod

        machine = fsm_mod.find(_machines(require(session_id)), signal)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"no state machine on {signal}")
        return machine.to_dict()

    @api.get("/session/{session_id}/fsm/{signal}/svg")
    def fsm_svg(session_id: str, signal: str) -> HTMLResponse:
        """"Export SVG" from the FSM mode (§8.8) — for documentation and READMEs."""
        from veritrace.analysis import fsm as fsm_mod
        from veritrace.export import fsmsvg

        session = require(session_id)
        machine = fsm_mod.find(_machines(session), signal)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"no state machine on {signal}")
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
        try:
            return session.save_layout(body.model_dump())
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Could not save layout: {exc}. Check storage permissions and available space, then retry.",
            ) from exc

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
                await dispatch(websocket, session, msg, registry)
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


async def dispatch(
    websocket: WebSocket,
    session: Session,
    msg: dict[str, Any],
    registry: SessionRegistry | None = None,
) -> None:
    op = msg.get("op")
    if op == "wave":
        await handle_wave(websocket, session, msg)
    elif op == "query":
        await handle_query(websocket, session, msg, registry)
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
    raw_derived = msg.get("derived") or {}
    if not isinstance(raw_derived, dict):
        await send(websocket, {"op": "error", "message": "derived must be a map"})
        return
    try:
        derived = {int(handle): str(path) for handle, path in raw_derived.items()}
    except (TypeError, ValueError):
        await send(websocket, {"op": "error", "message": "derived handles must be integers"})
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
            handle = int(h)
            if handle < 0:
                path = derived.get(handle)
                if path is None:
                    raise ValueError("negative handle needs a derived signal path")
                chunk = await asyncio.to_thread(
                    _derived_wave, session, path, t0, t1, min(px_width, MAX_PX)
                )
            else:
                chunk = session.store.wave(handle, t0, t1, px_width)
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
                "h": handle,
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


async def handle_query(
    websocket: WebSocket,
    session: Session,
    msg: dict[str, Any],
    registry: SessionRegistry | None = None,
) -> None:
    """Run VTQ without blocking the socket and stream causal nodes as built.

    ``WhyTracer`` is synchronous CPU work.  Running it on the event loop made
    the nominal streaming endpoint mute until the entire tree was finished.
    A thread performs the analysis while a queue carries each completed node
    back to the socket in deterministic post-order.  Non-causal commands use
    the same dispatcher as REST and are sent as one partial result followed by
    the mandatory completion statistics.
    """
    text = msg.get("vtq")
    if not isinstance(text, str) or not text.strip():
        await send(websocket, {"op": "error", "message": "query needs a non-empty vtq string", "done": True})
        return

    text = text.strip()
    started = time.perf_counter()
    if not _is_plain_why(text):
        try:
            pipeline = vtq.parse_pipeline(text)
            task = asyncio.create_task(
                asyncio.to_thread(_run_pipeline, session, pipeline, registry)
            )
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            except TimeoutError:
                # The command has crossed P3's 200 ms boundary. Its analyses do
                # not all expose a meaningful denominator, so report a live
                # phase/elapsed heartbeat instead of fabricating a percentage.
                while not task.done():
                    await send(
                        websocket,
                        {
                            "op": "progress",
                            "phase": pipeline.name,
                            "pct": None,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                        },
                    )
                    try:
                        result = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
                    except TimeoutError:
                        continue
                    break
                else:
                    result = await task
        except HTTPException as e:
            await send(websocket, {"op": "error", "message": str(e.detail), "done": True})
            return
        except (vtq.QueryError, KeyError, ValueError) as e:
            await send(websocket, {"op": "error", "message": str(e), "done": True})
            return
        await send(websocket, {"op": "partial", "result": {"query": text, **result}})
        await send(
            websocket,
            {
                "op": "done",
                "stats": {"ms": round((time.perf_counter() - started) * 1000, 3), "nodes": 0},
            },
        )
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def observe(node: Any) -> None:
        # Called from the worker thread. ``call_soon_threadsafe`` is the only
        # supported way to touch an asyncio queue owned by the server loop.
        loop.call_soon_threadsafe(queue.put_nowait, node.to_dict())

    def analyse() -> dict[str, Any]:
        signal, at, headline = _question(session, text)
        result = session.why(signal, at, on_node=observe)
        return {"query": text, "signal": signal, "time": at, "headline": headline, **result.to_dict()}

    task = asyncio.create_task(asyncio.to_thread(analyse))
    nodes = 0
    next_progress = started + 0.2
    try:
        while not task.done() or not queue.empty():
            try:
                node = await asyncio.wait_for(queue.get(), timeout=0.02)
            except TimeoutError:
                now = time.perf_counter()
                if now >= next_progress:
                    await send(
                        websocket,
                        {
                            "op": "progress",
                            "phase": "causal",
                            "pct": None,
                            "nodes": nodes,
                            "elapsed_ms": round((now - started) * 1000),
                        },
                    )
                    next_progress = now + 0.5
                continue
            nodes += 1
            await send(websocket, {"op": "partial", "node": node})
        # Propagate errors only after draining nodes already completed.  They
        # remain valid progress even if a later branch failed.
        result = await task
    except HTTPException as e:
        await send(websocket, {"op": "error", "message": str(e.detail), "done": True})
        return
    except (vtq.QueryError, KeyError, ValueError) as e:
        await send(websocket, {"op": "error", "message": str(e), "done": True})
        return

    await send(
        websocket,
        {
            "op": "done",
            "result": result,
            "stats": {"ms": round((time.perf_counter() - started) * 1000, 3), "nodes": nodes},
        },
    )
