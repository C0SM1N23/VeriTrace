"""Stuck detector — §8.4.

Runs on session open without being asked. For a deadlock it is often not a
clue but the answer: the thing that stopped moving is the thing that broke.

The whole scan is one parallel pass in Rust (`last_change_all`), because
`last_change_before` on every signal is exactly the shape rayon exists for, and
the pass warms the time cache the rest of the session then reads for free. The
Python here is only the policy: what counts as stuck, and what does not.

The exclusions in §8.4 are what make the output readable rather than a list of
every tie-off in the design:

* **Design constants** — parameters, `assign x = 1'b0`, and signals that never
  transition at all. A constant is not frozen, it was never moving.
* **Untraced signals** — nothing observed, nothing to say.
* **`trace.ignore` globs** from `.veritrace.toml` (§4.3).
* **A clock that has stopped.** If the design is no longer being clocked,
  everything is frozen and the one useful finding is that fact, not the four
  hundred signals downstream of it.
"""

from __future__ import annotations

from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity
from veritrace.analysis.signals import canonical
from veritrace.clocks import Clock

CHECK = "stuck"


def scan(
    store: Any,
    clock: Clock | None,
    graph: Any = None,
    config: Any = None,
    threshold_cycles: int | None = None,
) -> Iterator[Finding]:
    """Yield one finding per signal frozen for more than the threshold.

    Without a clock there is no cycle to count, so the check reports nothing
    rather than inventing a time unit — the caller records that as a skip.
    """
    if clock is None or clock.period is None:
        return

    cycles = threshold_cycles if threshold_cycles is not None else getattr(
        config, "stuck_cycles", 100
    )
    t0, t1 = store.time_range
    if not clock.is_toggling_at(t1):
        return

    threshold_t = cycles * clock.period
    # One parallel scan for the whole trace (§4.1: rayon for whole-trace work).
    last = store.last_change_all(t1 + 1)

    for meta in canonical(store, graph, config):
        h = meta.handle
        t_last = last[h] if h < len(last) else None
        if t_last is None:
            continue  # nothing dumped at all: nothing to say
        if t1 - t_last <= threshold_t:
            continue
        sig = graph.get(meta.path) if graph is not None else None
        # §8.4 excludes design constants: a parameter or an `assign x = 1'b0`
        # is not frozen, it was never able to move.
        if sig is not None and sig.is_tie_off:
            continue

        value = store.value_at(h, t1)
        frozen = clock.cycles_between(t_last, t1)
        # A signal whose only event is the opening dump never transitioned: it
        # is a constant in this run rather than something that froze. §8.4 wants
        # both, and separating them keeps the tie-offs out of the way of the
        # deadlock that is actually being looked for.
        moved = meta.n_events > 1 and t_last > t0
        yield Finding(
            group=Group.STUCK,
            severity=Severity.WARN if moved else Severity.INFO,
            check=CHECK,
            title=(
                f"frozen at {value} since c{clock.cycle_of(t_last)}"
                if moved
                else f"never changed from {value} in this run"
            ),
            signal=meta.path,
            loc=sig.decl_loc if sig is not None else None,
            time=t_last,
            detail=f"{frozen} cycles without a transition",
            # Ask about the value it is stuck at, at the moment it is stuck —
            # the question the engineer was about to type anyway.
            why=f"why({meta.path} @ {t1})",
        )
