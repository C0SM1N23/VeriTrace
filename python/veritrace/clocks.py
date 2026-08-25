"""The primary clock, and the cycle numbering that hangs off it — §5.5.

§5.5 problem 3: *a cycle number means nothing without a named clock*. `c1247`
is the 1247th rising edge of one specific signal, and every part of the tool
that prints or accepts a cycle has to agree on which one. So the resolution
happens once, here, and the resulting `Clock` is cached on the session.

Resolution order, most explicit first:

1. `clocks.primary` in `.veritrace.toml` (§4.3) — the user said so.
2. The clock the most sequential drivers are synchronised to, from the design
   graph. This is the right answer whenever RTL is loaded.
3. A trace-only heuristic: the busiest one-bit signal whose name looks like a
   clock. Needed because §7.4 makes "no RTL" a supported mode.

Every step is reported in `Clock.method`, so a wrong cycle number can be traced
to the decision that produced it instead of being a mystery (P1).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any

#: Names a clock plausibly has. Deliberately narrow: a false positive here
#: renumbers every cycle in the UI.
CLOCK_NAME_RE = re.compile(r"(^|[._])(clk|clock)([._\d]|$)|clk", re.IGNORECASE)

#: SI multipliers into femtoseconds — the finest unit VCD uses, so converting
#: between a human unit (a log timestamp, a datasheet's "18ns") and the
#: trace's own `$timescale` stays integer arithmetic throughout.
UNIT_FS = {"s": 10**15, "ms": 10**12, "us": 10**9, "ns": 10**6, "ps": 10**3, "fs": 1}


def to_trace_units(magnitude: float, unit: str, timescale: str) -> int | None:
    """A value given in `unit` (e.g. a datasheet's `18` `"ns"`), in the trace's
    own time units.

    `None` on an unrecognised unit or timescale — a wrong scale silently
    misplaces every comparison made against it, so refusing to guess (P1)
    matters more here than almost anywhere else in the tool.
    """
    unit_fs = UNIT_FS.get(unit.lower())
    scale_fs = UNIT_FS.get(timescale.lstrip("0123456789 ").lower())
    if unit_fs is None or scale_fs is None:
        return None
    digits = re.match(r"\s*(\d+)", timescale)
    per_unit = int(digits.group(1)) if digits else 1
    return int(magnitude * unit_fs / (scale_fs * per_unit))


@dataclass(slots=True)
class Clock:
    """A clock plus its rising edges, which is what cycle numbering needs."""

    path: str
    handle: int
    #: Rising-edge timestamps, ascending. Computed once per session.
    edges: list[int] = field(default_factory=list)
    #: How this clock was chosen: "config", "graph" or "heuristic".
    method: str = "heuristic"

    @property
    def n_cycles(self) -> int:
        return len(self.edges)

    @property
    def period(self) -> int | None:
        """Median edge-to-edge distance, or `None` for fewer than two edges.

        Median rather than mean so a long idle tail or a gated stretch does not
        stretch the period that thresholds are measured in.
        """
        if len(self.edges) < 2:
            return None
        gaps = sorted(b - a for a, b in zip(self.edges, self.edges[1:]))
        return gaps[len(gaps) // 2] or None

    def cycle_of(self, t: int) -> int:
        """Cycle number in force at `t` — the count of edges at or before it."""
        return max(0, bisect_right(self.edges, t) - 1)

    def time_of(self, cycle: int) -> int | None:
        """Timestamp of edge `cycle`, or `None` when the run is shorter."""
        if not self.edges:
            return None
        return self.edges[min(max(cycle, 0), len(self.edges) - 1)]

    def cycles_between(self, t0: int, t1: int) -> int:
        """Edges in `(t0, t1]` — how long something has been frozen, in cycles."""
        return max(0, bisect_right(self.edges, t1) - bisect_right(self.edges, t0))

    def is_toggling_at(self, t: int, window: int = 4) -> bool:
        """§8.4 guards the stuck report with "and the clock is still toggling":
        a design that simply stopped being clocked is one finding, not four
        hundred."""
        i = bisect_left(self.edges, t)
        return len(self.edges) - i >= window or self.cycles_between(
            t - (self.period or 0) * window, t
        ) >= min(window, len(self.edges))


def _edges_of(store: Any, handle: int) -> list[int]:
    """Rising-edge timestamps of a one-bit signal.

    Delegated to the store, which computes them on settled values and without
    building a Python object per event — a 10 MHz clock over a millisecond is
    ten thousand edges, and every cycle number in the session is counted
    against this list.
    """
    return store.rising_edges(handle)


def clock_at(store: Any, path: str, method: str = "interface") -> Clock | None:
    """A `Clock` for a specific signal path, or `None` if it is not usable.

    §8.14 needs this: an interface names its own clock, and a design with two
    clock domains must not have one of them numbered against the other's edges.
    """
    handle = store.find(path)
    if handle is None:
        return None
    edges = _edges_of(store, handle)
    if len(edges) < 2:
        return None
    return Clock(path=path, handle=handle, edges=edges, method=method)


def _from_graph(graph: Any) -> list[str]:
    """Clock paths ordered by how many sequential drivers use them."""
    counts: dict[str, int] = {}
    for sig in graph:
        for d in sig.drivers:
            if d.clock is not None:
                counts[d.clock.path()] = counts.get(d.clock.path(), 0) + 1
    return [p for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _from_trace(store: Any) -> list[str]:
    """One-bit signals that toggle, clock-looking names first, busiest first."""
    ones = [s for s in store.signals() if s.width == 1 and s.n_events > 2]
    named = [s for s in ones if CLOCK_NAME_RE.search(s.name)]
    pool = named or ones
    return [s.path for s in sorted(pool, key=lambda s: (-s.n_events, s.path))]


def resolve(store: Any, graph: Any = None, config: Any = None) -> Clock | None:
    """The primary clock for this session, or `None` if nothing looks like one."""
    candidates: list[tuple[str, str]] = []
    configured = getattr(config, "primary_clock", None)
    if configured:
        candidates.append((configured, "config"))
    if graph is not None:
        candidates += [(p, "graph") for p in _from_graph(graph)]
    candidates += [(p, "heuristic") for p in _from_trace(store)]

    for path, method in candidates:
        handle = store.find(path)
        if handle is None and graph is not None:
            # A graph clock may be spelled differently in the dump; the
            # correlation layer already recorded which handle it landed on.
            sig = graph.get(path)
            handle = sig.trace_handle if sig is not None else None
        if handle is None:
            continue
        edges = _edges_of(store, handle)
        if len(edges) < 2:
            continue
        return Clock(path=path, handle=handle, edges=edges, method=method)
    return None


def domains(
    store: Any,
    graph: Any = None,
    config: Any = None,
    primary: Clock | None = None,
    aliases: Any = None,
) -> list[dict[str, Any]]:
    """Every clock domain in the design, and which scopes each one drives.

    §5.5, problem 3: *"nu exista 'ciclul' in general — exista fronturi pe un
    anumit clock"*, and a signal in a second domain has to be shown with its own
    cycle number, named — `axi: c891 (aclk)`. The Inspector is where the spec
    puts that, so it needs to know which clock counts a given signal rather than
    guessing from the name.

    Membership is per *signal*, not per scope, because two clocks routinely
    drive registers in one module — `designs/checks` has exactly that, and a
    scope prefix cannot tell them apart. Each non-primary domain therefore
    carries the signals it actually clocks, read off the graph; everything else
    belongs to the primary clock. With no graph there is only the primary,
    which is the honest answer: nothing in a bare dump says which net clocks
    which flop.
    """
    if primary is None:
        primary = resolve(store, graph, config)
    if primary is None:
        return []

    wanted: list[str] = [primary.path]
    for path in list(getattr(config, "other_clocks", []) or []) + (
        _from_graph(graph) if graph is not None else []
    ):
        if path not in wanted:
            wanted.append(path)

    # Signals each clock drives, from the drivers that name it.
    driven: dict[str, set[str]] = {}
    if graph is not None:
        for sig in graph:
            for d in sig.drivers:
                if d.clock is not None:
                    driven.setdefault(d.clock.path(), set()).add(sig.path)

    # One wire wears several names (§7.1): a register inside a module is also a
    # port net one level up, and the dump carries all of them. A domain that
    # listed only the inner name would fail to recognise the row a user clicked,
    # so each member brings its whole equivalence class with it.
    same = _alias_classes(aliases)
    for paths in driven.values():
        for path in list(paths):
            paths |= same.get(path, set())

    out: list[dict[str, Any]] = []
    for path in wanted:
        clock = primary if path == primary.path else clock_at(store, path, "graph")
        if clock is None or clock.period is None:
            continue
        out.append(
            {
                "path": path,
                "name": path.rsplit(".", 1)[-1],
                "primary": path == primary.path,
                "period": clock.period,
                "origin": clock.edges[0] if clock.edges else 0,
                "n_cycles": clock.n_cycles,
                # Only for the secondary domains: the primary is the default for
                # every signal, and listing a whole design here would put the
                # hierarchy in a status payload for nothing.
                "signals": [] if path == primary.path else sorted(driven.get(path, ())),
            }
        )
    return out


def _alias_classes(aliases: Any) -> dict[str, set[str]]:
    """Path -> every other path naming the same wire, from port connections."""
    if not aliases:
        return {}
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in aliases:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    out: dict[str, set[str]] = {}
    for path in parent:
        out.setdefault(find(path), set()).add(path)
    return {p: out[find(p)] for p in parent}


def format_time(t: int, clock: Clock | None) -> str:
    """`c1247` when a clock is known, the raw timestamp otherwise.

    Cycles are what an engineer reads a waveform in, but inventing one without
    a clock would be a lie, so the fallback is explicit rather than a guess.
    """
    return f"c{clock.cycle_of(t)}" if clock is not None else str(t)
