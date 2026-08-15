"""The §10.1 signal-level commands, other than `why()`.

    find(pattern)              cone(sig, depth=3, active=true)
    fanout(sig, depth=2)       stuck(after=c100, min_duration=c50)
    edges(sig, c100:c500)      hold(sig, c100:c500)
    xtrace(sig) / xtrace()     fsm(sig)
    lint(scope, checks=[cdc,latch])

§10.1 lists eighteen commands and calls VTQ *the* query language. The parser has
accepted all of them since it was written; the executor answered one, so typing
`cone(top.ctrl.ready)` into the query bar — the thing §11.3 calls the spine of
the tool — came back with "only `why(...)` is supported" for an analysis the
same session already had a button for.

Nothing here is a new analysis. Every entry calls the module that already
answers the question for the CLI and the REST route, which is the point: a
command is a dispatch-table line, and the one implementation cannot drift into
two behaviours.

The dispatch-table shape is `perf.query`'s and `memory.query`'s, so all three
read the same way.
"""

from __future__ import annotations

from typing import Any, Callable

from veritrace.analysis.vtq import Call, Pipeline, QueryError

#: Commands answered here. `why` is not one: it has its own grammar, its own
#: result shape and its own route (§8.1).
COMMANDS = (
    "find",
    "cone",
    "fanout",
    "stuck",
    "edges",
    "hold",
    "xtrace",
    "fsm",
    "lint",
)

#: Commands that read the RTL graph rather than only the dump (§7.4 makes a
#: dump with no RTL a supported mode, so these have to say why they cannot run).
NEEDS_RTL = ("cone", "fanout", "xtrace", "fsm", "lint")


class Session:
    """What a command needs, however the caller happens to hold it.

    The REST route has a `Session`, the CLI has a `Context`, and neither should
    have to become the other. Duck-typed on purpose: five attributes, all of
    which both already have.
    """

    store: Any
    graph: Any
    clock: Any
    config: Any


def _at(session: Any, when: Any, default: int) -> int:
    """A time argument: a raw timestamp, or `cN` for a clock cycle."""
    if when is None:
        return default
    if isinstance(when, int):
        return when
    text = str(when).strip()
    if text[:1].lower() == "c":
        clock = getattr(session, "clock", None)
        if clock is None:
            raise QueryError("cycle times need a primary clock; none was found in this trace")
        at = clock.time_of(int(text[1:]))
        if at is None:
            raise QueryError("the primary clock never rises")
        return at
    try:
        return int(text, 0)
    except ValueError:
        raise QueryError(f"cannot read a time from {when!r}") from None


def _window(session: Any, call: Call, pos: int = 1) -> tuple[int, int]:
    """`c100:c500` as a positional argument, or the whole trace."""
    t0, t1 = session.store.time_range
    span = call.args[pos] if len(call.args) > pos else call.kwargs.get("range")
    if span is None:
        return t0, t1 + 1
    if isinstance(span, tuple):
        return _at(session, span[0], t0), _at(session, span[1], t1) + 1
    raise QueryError(f"`{call.name}()` wants a range like c100:c500, not {span!r}")


def _signal(session: Any, call: Call) -> str:
    """The signal argument, resolved the way §7.2 resolves a partial name."""
    from veritrace.correlate.resolver import resolve_path

    if not call.args:
        raise QueryError(f"`{call.name}()` needs a signal")
    name = str(call.args[0])
    graph = getattr(session, "graph", None)
    if graph is None:
        # No RTL: the trace's own names are all there is, and they are exact.
        if session.store.find(name) is None:
            raise QueryError(f"unknown signal: {name}")
        return name
    path, candidates = resolve_path(graph, name)
    if path is not None:
        return path
    if candidates:
        raise QueryError(
            f"`{name}` is ambiguous — {len(candidates)} signals end with it: "
            + ", ".join(candidates[:8])
        )
    raise QueryError(f"unknown signal: {name}")


# --- the commands -----------------------------------------------------------


def _find(session: Any, call: Call) -> dict[str, Any]:
    """§10.1's fuzzy hierarchy search — the palette's ranking, as a query."""
    from veritrace.api.search import search_signals

    pattern = str(call.args[0]) if call.args else ""
    limit = int(call.kwargs.get("limit", 200))
    rows = search_signals(session.store.signals(), pattern, limit=limit)
    return {"pattern": pattern, "total": session.store.n_signals, "signals": rows}


def _cone(session: Any, call: Call) -> dict[str, Any]:
    from veritrace.analysis import cone as cone_mod

    signal = _signal(session, call)
    depth = int(call.kwargs.get("depth", 4))
    direction = "fanout" if call.name == "fanout" else str(call.kwargs.get("direction", "fanin"))
    active = call.kwargs.get("active") in (True, "true", "True", 1)
    window = _window(session, call) if active else None
    result = cone_mod.cone(
        session.graph,
        signal,
        depth=depth,
        direction=direction,
        store=session.store if active else None,
        window=window,
    )
    return {
        "signal": signal,
        "direction": direction,
        "depth": depth,
        "n_inactive": result.n_inactive,
        "nodes": [{"path": n.path, "depth": n.depth, "role": n.role} for n in result.nodes],
    }


def _stuck(session: Any, call: Call) -> dict[str, Any]:
    """§8.4 at a threshold the question chose.

    §10.1 spells the arguments `after=` and `min_duration=`; the detector has
    one threshold, and `min_duration` is the one that names it. `after=` narrows
    nothing the scan can express — it always measures back from the end of the
    run — so it is refused rather than accepted and ignored.
    """
    from veritrace.analysis import stuck as stuck_mod

    if "after" in call.kwargs:
        raise QueryError(
            "`stuck(after=...)` is not supported: the detector measures back from "
            "the end of the run, so a start time would be accepted and ignored. "
            "Use `stuck(min_duration=cN)`."
        )
    raw = call.kwargs.get("min_duration") or (call.args[0] if call.args else None)
    cycles = None
    if raw is not None:
        text = str(raw)
        cycles = int(text[1:]) if text[:1].lower() == "c" else int(text, 0)
    if session.clock is None:
        raise QueryError("no clock could be identified, so cycles have no meaning")
    found = sorted(
        stuck_mod.scan(session.store, session.clock, session.graph, session.config, cycles),
        key=lambda f: f.sort_key,
    )
    return {
        "cycles": cycles if cycles is not None else getattr(
            session.config, "stuck_cycles", stuck_mod.DEFAULT_CYCLES
        ),
        "n_cycles": session.clock.n_cycles,
        "unreachable": stuck_mod.too_short(
            session.store, session.clock, session.config, cycles
        ),
        "findings": [f.to_dict() for f in found],
    }


def _edges(session: Any, call: Call) -> dict[str, Any]:
    """Every transition in a window — the raw event list, glitches included."""
    signal = _signal(session, call)
    handle = session.store.find(signal)
    if handle is None:
        raise QueryError(f"{signal} is in the RTL but not in this trace")
    lo, hi = _window(session, call)
    rows = session.store.transitions(handle, lo, hi)
    clock = getattr(session, "clock", None)
    return {
        "signal": signal,
        "from": lo,
        "to": hi,
        "edges": [
            {"time": t, "cycle": clock.cycle_of(t) if clock else None, "value": str(v)}
            for t, v in rows
        ],
    }


def _hold(session: Any, call: Call) -> dict[str, Any]:
    """The intervals in which a signal was constant (§10.1's `hold`).

    Built from the transitions rather than from `is_constant`: the question is
    *which* stretches held, and a boolean per window cannot answer that.
    """
    signal = _signal(session, call)
    handle = session.store.find(signal)
    if handle is None:
        raise QueryError(f"{signal} is in the RTL but not in this trace")
    lo, hi = _window(session, call)
    clock = getattr(session, "clock", None)
    start = session.store.value_at(handle, lo)
    spans, prev_t, prev_v = [], lo, start
    for t, v in session.store.transitions(handle, lo + 1, hi):
        if str(v) == str(prev_v):
            continue  # a write that did not change the value is not a boundary
        spans.append((prev_t, t, prev_v))
        prev_t, prev_v = t, v
    spans.append((prev_t, hi - 1, prev_v))
    return {
        "signal": signal,
        "spans": [
            {
                "from": a,
                "to": b,
                "value": str(v),
                "cycles": clock.cycles_between(a, b) if clock else None,
            }
            for a, b, v in spans
        ],
    }


def _xtrace(session: Any, call: Call) -> dict[str, Any]:
    """§8.5 — every X in the design grouped by root cause, or one signal's."""
    from veritrace.analysis import xprop

    found = list(xprop.scan(session.store, session.graph, session.clock, session.config))
    if call.args:
        signal = _signal(session, call)
        found = [f for f in found if f.signal == signal or signal in f.related]
    return {"findings": [f.to_dict() for f in found]}


def _fsm(session: Any, call: Call) -> dict[str, Any]:
    """§8.8's machines, with the trace overlay when there is one."""
    from veritrace.analysis import fsm as fsm_mod

    el = getattr(session, "elaboration", None)
    machines = fsm_mod.extract(session.graph, el, session.store)
    if call.args:
        signal = _signal(session, call)
        machines = [m for m in machines if m.signal == signal]
        if not machines:
            raise QueryError(f"{signal} is not a state register this design declares")
    for m in machines:
        fsm_mod.overlay(m, session.store, session.graph, session.clock)
    return {"machines": [m.to_dict() for m in machines]}


def _lint(session: Any, call: Call) -> dict[str, Any]:
    """§8.11's static checks, optionally narrowed to a scope or a check list."""
    from veritrace.analysis import checks as checks_mod
    from veritrace.analysis import lint as lint_mod

    el = getattr(session, "elaboration", None)
    found = list(
        lint_mod.scan(
            session.graph,
            el.diagnostics if el is not None else (),
            session.store,
            session.clock,
            session.config,
        )
    )
    scope = str(call.args[0]) if call.args else None
    if scope:
        found = [f for f in found if f.signal and f.signal.startswith(scope)]
    wanted = call.kwargs.get("checks")
    if wanted:
        names = checks_mod.expand_checks(
            wanted if isinstance(wanted, (list, tuple)) else str(wanted).split(",")
        )
        found = [f for f in found if f.check in names]
    return {"findings": [f.to_dict() for f in sorted(found, key=lambda f: f.sort_key)]}


_DISPATCH: dict[str, Callable[[Any, Call], dict[str, Any]]] = {
    "find": _find,
    "cone": _cone,
    "fanout": _cone,
    "stuck": _stuck,
    "edges": _edges,
    "hold": _hold,
    "xtrace": _xtrace,
    "fsm": _fsm,
    "lint": _lint,
}


def run(session: Any, pipeline: Pipeline) -> dict[str, Any]:
    """Execute one parsed signal-level command."""
    call = pipeline.source
    if pipeline.stages:
        raise QueryError(f"`{call.name}()` does not take a `| stage`")
    fn = _DISPATCH.get(call.name)
    if fn is None:
        raise QueryError(f"`{call.name}()` is not a signal command; try {', '.join(COMMANDS)}")
    if call.name in NEEDS_RTL and getattr(session, "graph", None) is None:
        # §7.4: a dump with no RTL is a supported mode, and saying which half is
        # missing beats a stack trace about a `None`.
        raise QueryError(
            f"`{call.name}()` needs the RTL. Start the server with --rtl, or set "
            "design.rtl in .veritrace.toml."
        )
    return {"kind": call.name, **fn(session, call)}
