"""X-propagation tracer — §8.5.

The same backward slicing as why-trace, stopped at the specialised terminals of
§8.5 (`whytrace.classify_x` implements the table; this module runs it over a
whole trace and aggregates).

The aggregation is the point. §8.5: *"often 200 X's have 2 causes"*. A list of
every signal that is X at some point is unreadable and tells you nothing you
could not see in the waveform; a list of the two registers nobody reset is a
morning's work. So each X-carrying signal is traced back to its origin and the
report is keyed on the origin, carrying the count and the first appearance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity
from veritrace.analysis.signals import canonical
from veritrace.analysis.whytrace import (
    X_TERMINAL_DETAIL,
    X_TERMINALS,
    CausalNode,
    Reason,
    WhyTracer,
)
from veritrace.clocks import Clock
from veritrace.graph.model import SourceLoc

CHECK = "x_source"

#: An X in the reset window is normal — the design has not been initialised yet.
#: Anything still X afterwards is a real finding. Expressed in clock cycles so
#: it is independent of the timescale.
SETTLE_CYCLES = 2

#: Reporting every X-carrying signal individually is what §8.5 exists to avoid.
#: Beyond this many, the tail is summarised rather than listed.
MAX_VICTIMS_LISTED = 5


@dataclass(slots=True)
class XSource:
    """One root cause, and everything downstream of it that went X."""

    signal: str
    reason: Reason
    loc: SourceLoc | None
    first_seen: int
    #: Signals whose X traced back to here, first-appearance order.
    victims: list[str] = field(default_factory=list)

    @property
    def n_victims(self) -> int:
        return len(self.victims)


def _origin(node: CausalNode) -> CausalNode | None:
    """The deepest X terminal in the tree — where this X came from.

    Deliberately *not* restricted to `is_primary_path`. That flag ranks by
    oldest last transition (§8.1), which is the right heuristic for "why does
    this signal hold this value" and the wrong one here: a reset that has been
    stable since time zero outranks the uninitialised register next to it, and
    the walk misses the X entirely. The question this module asks is where the
    X came from, so it follows the X.

    Ties are broken by depth, then the primary flag, then the path, so two runs
    on the same trace agree (P1).
    """
    best: tuple[int, int, str] | None = None
    found: CausalNode | None = None
    stack: list[tuple[CausalNode, int]] = [(node, 0)]
    while stack:
        n, d = stack.pop()
        if n.reason in X_TERMINALS:
            key = (d, int(n.is_primary_path), n.signal.path())
            if best is None or key > best:
                best, found = key, n
        stack.extend((c, d + 1) for c in n.children)
    return found


def _survives_reset(store: Any, handle: int, first_x: int, settle: int) -> bool:
    """Whether an X is still there once reset has been released.

    Every register is X before its first write, so filtering on *first*
    appearance would drop nearly every real finding along with the noise. What
    separates the two is whether the X is still present after the reset window:
    an X that clears is the design initialising, an X that persists is a bug.
    The reported time stays the first appearance, which is what §8.5 asks for.

    "Still present" means *anywhere* after the window, not at one sampled
    instant. Sampling only `settle` loses every X that clears during reset and
    comes back later — a driver conflict or an out-of-range index does not wait
    for reset to be over, and those were silently absent from the report.
    """
    if first_x >= settle:
        return True
    return store.first_x_from(handle, settle) is not None


def scan(
    store: Any,
    graph: Any,
    clock: Clock | None = None,
    config: Any = None,
) -> Iterator[Finding]:
    """Yield one finding per X root cause, ordered by how much it explains."""
    settle = 0
    if clock is not None and clock.n_cycles > SETTLE_CYCLES:
        settle = clock.edges[SETTLE_CYCLES]

    # One canonical name per event stream: the aliases of a port connection are
    # X together, and only the driven name has anywhere for the walk to go.
    by_handle = {s.handle: s.path for s in canonical(store, graph, config)}
    # One parallel Rust pass over the whole trace; the graph walk is the only
    # part that needs Python.
    victims = sorted(
        (t, by_handle[h])
        for h, t in store.first_x_all()
        if h in by_handle and _survives_reset(store, h, t, settle)
    )
    if not victims:
        return

    tracer = WhyTracer(graph, store)
    sources: dict[tuple[str, Reason], XSource] = {}

    for t, path in victims:
        if graph.get(path) is None:
            continue  # in the dump but not in the RTL; correlation reports that
        origin = _origin(tracer.why(path, t).root)
        if origin is None:
            continue
        key = (origin.signal.path(), origin.reason)
        src = sources.get(key)
        if src is None:
            src = sources[key] = XSource(
                signal=origin.signal.path(),
                reason=origin.reason,
                loc=origin.loc,
                first_seen=origin.time,
            )
        src.victims.append(path)
        src.first_seen = min(src.first_seen, origin.time)

    # Most explanatory first — the same ordering §8.10b uses for triage, and for
    # the same reason: the cause that accounts for the most is where to start.
    for src in sorted(sources.values(), key=lambda s: (-s.n_victims, s.first_seen, s.signal)):
        # `trace.ignore` has to cover the root cause as well as the victims:
        # the finding is *about* the source, so ignoring the source silences it.
        if config is not None and config.is_ignored(src.signal):
            continue
        shown = src.victims[:MAX_VICTIMS_LISTED]
        more = src.n_victims - len(shown)
        notes = [f"reaches {', '.join(shown)}" + (f" and {more} more" if more else "")]
        yield Finding(
            group=Group.X_SOURCES,
            severity=Severity.ERROR if src.reason is not Reason.UNKNOWN_X else Severity.WARN,
            check=CHECK,
            title=X_TERMINAL_DETAIL[src.reason],
            signal=src.signal,
            loc=src.loc,
            time=src.first_seen,
            detail=(
                f"{src.n_victims} signal goes X because of this"
                if src.n_victims == 1
                else f"{src.n_victims} signals go X because of this"
            ),
            why=f"why({src.signal} @ {src.first_seen})",
            notes=tuple(notes),
            related=tuple(src.victims),
        )
