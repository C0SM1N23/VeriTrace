"""Attributing a timing path to the RTL, and to the run — §8.30.

Two joins, and the second is the one that does not exist anywhere else.

**Path endpoint → source line.** Vivado names a netlist resource
(`top/u_ctrl/state_reg[3]`); the design graph names a signal (`top.ctrl.state`)
with a declaration line. Matching them turns "-0.417ns on some flop" into a line
of code — usually a 32-bit comparator written on one line, which is §8.30's own
example.

**Path endpoint → switching activity.** A critical path that the simulation never
exercises is a false priority. Vivado cannot know that; it has never run the
design. This is the join that makes the ranking useful rather than alphabetical.
"""

from __future__ import annotations

import re
from typing import Any

from veritrace.timing.model import Hop, TimingReport
from veritrace.timing.vivado import pin

#: `u_ctrl/state_reg[3]` -> `u_ctrl.state`. Synthesis renames a flop to
#: `<name>_reg`, splits a vector into per-bit instances, and uses `/` for
#: hierarchy — all three are undone here so the name can be looked up in the
#: graph, which knows the design as it was written.
_BIT = re.compile(r"\[\d+\]$")
_REG = re.compile(r"_reg(?:\[\d+\])?$")


def rtl_name(resource: str) -> str:
    """A netlist resource as the RTL spells it, best effort.

    The trailing pin goes first (`.../Q`, `.../CO[3]`): a hop names a *pin* of a
    cell, and leaving it on means no hop ever matches a signal — which is why the
    per-hop source lines §8.30 asks for were coming back empty.
    """
    name = pin(resource.strip().split()[0]) if resource.strip() else ""
    name = _BIT.sub("", name)
    name = _REG.sub("", name)
    return name.replace("/", ".")


def _resolve(graph: Any, store: Any, name: str) -> str:
    """The signal `name` refers to, matched from the most specific end.

    Synthesis flattens and renames, so an exact match is the exception. Matching
    on the *suffix* is what survives that: `u_ctrl.state` finds `top.u_ctrl.state`
    however many levels the report omitted.
    """
    if not name:
        return ""
    for source in (graph, store):
        if source is None:
            continue
        paths = (
            list(source.signals) if hasattr(source, "signals") and isinstance(getattr(source, "signals"), dict)
            else [s.path for s in source.signals()]
        )
        if name in paths:
            return name
        tail = "." + name
        hits = [p for p in paths if p.endswith(tail)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            # Several candidates means the report's name is ambiguous in this
            # design; naming one of them at random would be worse than naming none.
            return ""
    return ""


def correlate(
    report: TimingReport, graph: Any = None, store: Any = None, clock: Any = None
) -> TimingReport:
    """Fill in source locations and switching activity — §8.30's two joins."""
    report.correlated = store is not None
    for path in report.paths:
        path.source_signal = _resolve(graph, store, rtl_name(path.source))
        path.dest_signal = _resolve(graph, store, rtl_name(path.destination))
        path.source_loc = _loc(graph, path.source_signal)
        path.dest_loc = _loc(graph, path.dest_signal)

        if graph is not None:
            path.hops = [
                Hop(h.delay_ns, h.cumulative_ns, h.resource, _loc(graph, rtl_name(h.resource)))
                for h in path.hops
            ]

        if store is None:
            continue
        target = path.dest_signal or path.source_signal
        handle = store.find(target) if target else None
        if handle is None:
            path.note = (
                "no signal in the trace matches this endpoint — synthesis may have "
                "renamed or merged it"
            )
            continue
        t0, t1 = store.time_range
        path.toggles = max(0, len(store.transitions(handle, t0, t1)) - 1)
        if path.toggles == 0 and path.violated:
            path.note = (
                "this path never switched in the run, so closing it buys nothing "
                "the simulation can show — check the stimulus before the logic"
            )
    return report


def _loc(graph: Any, name: str) -> str:
    sig = graph.get(name) if name else None
    if sig is None:
        return ""
    loc = getattr(sig, "decl_loc", None)
    return f"{loc.file}:{loc.line}" if loc and getattr(loc, "file", "") else ""
