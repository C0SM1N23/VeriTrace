"""Deadlock, livelock and starvation as automatic findings — §8.18, §11.4.

*All three are scans over the transaction table, so they run automatically when
a session opens, like the stuck detector.*

Nothing is computed here that §8.18 has not already computed: this is the
adapter that puts a wait-for cycle in the Checks tab next to the stuck signals
and the protocol violations, because from the user's side they are the same kind
of news — something the tool noticed without being asked.

The `[why]` on each row aims at the wire, not at the abstraction. A deadlock is
a statement about a `ready` that stayed low, so it opens in Causal like any
other finding, and §8.18's *"[Why on each link]"* is then the same query run per
edge rather than a separate feature.
"""

from __future__ import annotations

from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity, location_of

CHECK_DEADLOCK = "deadlock"
CHECK_LIVELOCK = "livelock"
CHECK_STARVATION = "starvation"

#: Every check this module can produce, for `--fail-on` and `checks.disable`.
CHECKS: dict[str, str] = {
    CHECK_DEADLOCK: "a cycle in the wait-for graph that persisted (§8.18)",
    CHECK_LIVELOCK: "a state machine oscillating with nothing completing (§8.18)",
    CHECK_STARVATION: "an agent that asked and was not served while others were (§8.18)",
}


def _cycles(clock: Any, t: int | None) -> str:
    return "?" if t is None else (f"c{clock.cycle_of(t)}" if clock is not None else str(t))


def scan(
    liveness: Any,
    clock: Any = None,
    config: Any = None,
    graph: Any = None,
) -> Iterator[Finding]:
    """Turn a §8.18 scan into findings."""
    if liveness is None:
        return

    for d in liveness.deadlocks:
        chain = " -> ".join(d.agents)
        first = d.first_txn or d.first_blocked or "nothing had been issued yet"
        # The chain reads as prose because that is how §8.18 writes it, and a
        # deadlock is one of the few findings where the *shape* is the finding:
        # who waits on whom, in order, is the whole diagnosis.
        lines = [
            f"{e.agent} waits on `{e.resource}` held by {e.holder}" for e in d.edges
        ]
        yield Finding(
            group=Group.LIVENESS,
            severity=Severity.ERROR,
            check=CHECK_DEADLOCK,
            title=f"deadlock: {len(d.agents)} agents in a wait-for cycle ({chain})",
            signal=d.edges[0].resource if d.edges else None,
            loc=location_of(graph, d.edges[0].resource if d.edges else None),
            time=d.at,
            detail=(
                f"persisted {d.cycles} cycles from {_cycles(clock, d.at)}; "
                f"first blocked: {first}"
            ),
            why=f"why({d.edges[0].resource} @ {d.at})" if d.edges else None,
            notes=tuple(lines),
            # The agent set is the identity: the same cycle found again after a
            # re-run is the same finding, and a suppression has to survive that.
            related=tuple(sorted(d.agents)),
        )

    for lv in liveness.livelocks:
        yield Finding(
            group=Group.LIVENESS,
            severity=Severity.WARN,
            check=CHECK_LIVELOCK,
            title=(
                f"livelock: `{lv.signal}` changed {lv.toggles} times between "
                f"{len(lv.states)} states with nothing completing"
            ),
            signal=lv.signal,
            loc=location_of(graph, lv.signal),
            time=lv.since,
            detail=(
                f"{_cycles(clock, lv.since)}-{_cycles(clock, lv.until)}, "
                f"states {', '.join(lv.states)}"
            ),
            why=f"why({lv.signal} @ {lv.since})",
            related=(lv.signal,),
        )

    for s in liveness.starvation:
        yield Finding(
            group=Group.LIVENESS,
            severity=Severity.WARN,
            check=CHECK_STARVATION,
            title=(
                f"{s.agent} asked for {s.cycles} cycles without being served, "
                f"while {', '.join(s.served)} progressed"
            ),
            signal=None,
            time=s.since,
            detail=f"{_cycles(clock, s.since)}-{_cycles(clock, s.until)}",
            why=None,
            related=(s.agent,),
        )
