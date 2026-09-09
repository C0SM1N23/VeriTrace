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
#: §8.4's one exception, as its own check name. When the design is no longer
#: being clocked every signal is frozen, and the finding worth having is that
#: fact rather than the four hundred downstream of it — so it is reported
#: instead of them, and `--fail-on` and `checks.disable` can name it apart from
#: the per-signal rows it replaces.
CHECK_CLOCK_STOPPED = "clock_stopped"

#: The default threshold of §8.4: *"THRESHOLD default = 100 de cicluri de
#: clock, configurabil."* Not scaled to the run — a threshold that moved with
#: the trace would mean two runs of the same design disagreeing about what is
#: stuck. It is reported instead, by `too_short` below.
DEFAULT_CYCLES = 100


def _threshold(config: Any, threshold_cycles: int | None) -> int:
    if threshold_cycles is not None:
        return threshold_cycles
    return getattr(config, "stuck_cycles", DEFAULT_CYCLES)


def too_short(
    store: Any,
    clock: Clock | None,
    config: Any = None,
    threshold_cycles: int | None = None,
    start_time: int | None = None,
) -> str | None:
    """Why the scan cannot report anything, or `None` when it can.

    A threshold wider than the run makes the check structurally unable to fire:
    the longest freeze a trace can contain is the trace itself. Printing
    "nothing has been frozen" there reads as a verdict on the design when it is
    a fact about the window — and a 50-cycle testbench against §8.4's 100-cycle
    default is the ordinary case, not a corner one. P7: a check that could not
    run says so.
    """
    if clock is None or clock.period is None:
        return None
    cycles = _threshold(config, threshold_cycles)
    t0, t1 = store.time_range
    window_start = max(t0, start_time) if start_time is not None else t0
    # A stopped clock is reported whatever the window is — it is a fact about
    # the run, not a signal measured against the threshold — so the scan has
    # something to say here and must not be recorded as unable to run.
    if not clock.is_toggling_at(t1):
        return None
    # The same comparison `scan` makes, against the widest gap the run allows.
    if t1 - window_start > cycles * clock.period:
        return None
    ran = max(0, clock.cycles_between(window_start - 1, t1))
    window = (
        f"the whole run is {ran}"
        if start_time is None or window_start == t0
        else f"the requested window is {ran}"
    )
    return (
        f"the threshold is {cycles} cycles and {window} — nothing "
        f"can have been frozen that long. Lower it with --cycles, choose an earlier start, or set "
        f"checks.stuck_cycles in .veritrace.toml."
    )


def scan(
    store: Any,
    clock: Clock | None,
    graph: Any = None,
    config: Any = None,
    threshold_cycles: int | None = None,
    start_time: int | None = None,
) -> Iterator[Finding]:
    """Yield one finding per signal frozen for more than the threshold.

    Without a clock there is no cycle to count, so the check reports nothing
    rather than inventing a time unit — the caller records that as a skip.
    """
    if clock is None or clock.period is None:
        return

    cycles = _threshold(config, threshold_cycles)
    t0, t1 = store.time_range
    window_start = max(t0, start_time) if start_time is not None else t0
    if window_start > t1:
        return
    if not clock.is_toggling_at(t1):
        # §8.4: *"daca design-ul nu mai e ceasuit, totul e inghetat si singurul
        # finding util e acel fapt"*. Reporting nothing at all was the same
        # silence as a clean run — the one thing this check must never look
        # like — so the fact is now the finding.
        stopped = clock.edges[-1] if clock.edges else t1
        yield Finding(
            group=Group.STUCK,
            severity=Severity.ERROR,
            check=CHECK_CLOCK_STOPPED,
            title=f"the clock stopped at c{clock.cycle_of(stopped)}",
            signal=clock.path,
            time=stopped,
            detail=(
                f"no edge for the last {t1 - stopped} time unit(s); every signal is "
                "frozen from here, so the per-signal scan would only list the design"
            ),
            # A clock that stops is a fact about the stimulus, not something the
            # causal walk explains — an honest empty `[why]` beats one that
            # errors on a testbench the graph does not contain (P7).
            why=None,
        )
        return

    threshold_t = cycles * clock.period
    # Two parallel scans for the whole trace, and no per-signal Python loop
    # between them (§4.1: rayon is here for exactly this shape of work).
    #
    # The values used to be fetched one at a time, inside the loop below. That
    # measured at 74% of the whole scan — a cold Parquet decode and a PyO3
    # crossing per frozen signal — and put §8.4 three times over §4.2's 400 ms
    # budget on a 5000-signal trace. Selecting first and fetching the survivors
    # in one pass is the same answer for a fraction of the wall time.
    last = store.last_change_all(t1 + 1)

    frozen_signals = []
    for meta in canonical(store, graph, config):
        h = meta.handle
        t_last = last[h] if h < len(last) else None
        if t_last is None:
            continue  # nothing dumped at all: nothing to say
        # VTQ's `after=` starts the observation window. A signal already frozen
        # at that point only gets credit for the part of its freeze we actually
        # asked to observe; otherwise `after=c600, min_duration=c100` could
        # report a 500-cycle-old value from outside a 50-cycle window.
        if t1 - max(t_last, window_start) <= threshold_t:
            continue
        sig = graph.get(meta.path) if graph is not None else None
        # §8.4 excludes design constants: a parameter or an `assign x = 1'b0`
        # is not frozen, it was never able to move.
        if sig is not None and sig.is_tie_off:
            continue
        frozen_signals.append((meta, t_last, sig))

    values = store.value_at_all([m.handle for m, _, _ in frozen_signals], t1)

    for (meta, t_last, sig), value in zip(frozen_signals, values):
        frozen = clock.cycles_between(t_last, t1)
        observed = clock.cycles_between(max(t_last, window_start), t1)
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
            detail=(
                f"{frozen} cycles without a transition"
                if window_start <= t_last
                else f"{frozen} cycles without a transition; {observed} inside the requested window"
            ),
            # Ask about the value it is stuck at, at the moment it is stuck —
            # the question the engineer was about to type anyway.
            why=f"why({meta.path} @ {t1})",
        )
