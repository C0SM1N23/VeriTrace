"""Step 4 of §8.20: the efficiency metrics, as time series.

*Toate afisate ca serie temporala, nu doar ca numar total — asa vezi cand se
degradeaza.* Every metric here is therefore a fold over the command stream and
the bank segments `banks.py` already built, windowed the same way
`perf.metrics` windows its series, so the frontend's chart code is one
component for both tabs.

Row hit/miss/conflict is deliberately the simple, well-defined reading rather
than the fullest possible one: every `ACTIVATE` is classified from what
happened right before it *on that bank*, not from a request queue this layer
cannot see (nothing here observes what the controller was asked for, only what
it did). That is enough to answer the question that actually matters — *is
this bank thrashing* — without pretending to a precision the command stream
does not carry.
"""

from __future__ import annotations

from typing import Sequence

from veritrace.memory.model import (
    ACTIVATING,
    ACTIVE,
    PRECHARGING,
    BankSegment,
    CmdEvent,
    Efficiency,
    RowStats,
    Series,
)

#: Windows in a time series — enough to see the shape, few enough to draw.
SERIES_POINTS = 120


def _windows(n: int, points: int = SERIES_POINTS) -> int:
    return max(1, -(-n // max(1, points)))


def _classify(commands: Sequence[CmdEvent], n_banks: int) -> list[tuple[int, int, str]]:
    """One pass over the whole run: every ACTIVATE and every "reused an
    already-open row" READ/WRITE, as `(time, bank, "hit"|"miss"|"conflict")`.

    The single source both `row_hits` and `row_hit_series` fold — classifying
    each window on its own would forget whether a bank was already open when
    the window started, turning every window's first access into a spurious
    compulsory miss.
    """
    out: list[tuple[int, int, str]] = []
    last_cmd: dict[int, str] = {}
    awaiting_first_access: dict[int, bool] = {}

    for c in sorted(commands, key=lambda c: c.time):
        b = c.fields.get("bank")
        if not isinstance(b, int) or not 0 <= b < n_banks:
            continue
        if c.name == "ACTIVATE":
            kind = "conflict" if last_cmd.get(b) == "PRECHARGE" else "miss"
            out.append((c.time, b, kind))
            awaiting_first_access[b] = True
        elif c.name in ("READ", "WRITE"):
            if not awaiting_first_access.get(b, True):
                out.append((c.time, b, "hit"))
            awaiting_first_access[b] = False
        last_cmd[b] = c.name
    return out


def row_hits(commands: Sequence[CmdEvent], n_banks: int) -> tuple[RowStats, dict[int, RowStats]]:
    """Totals, overall and per bank."""
    events = _classify(commands, n_banks)
    per_bank: dict[int, RowStats] = {b: RowStats() for b in range(n_banks)}
    for _, b, kind in events:
        s = per_bank[b]
        if kind == "hit":
            s.hits += 1
        elif kind == "miss":
            s.misses += 1
        else:
            s.conflicts += 1
    total = RowStats(
        hits=sum(s.hits for s in per_bank.values()),
        misses=sum(s.misses for s in per_bank.values()),
        conflicts=sum(s.conflicts for s in per_bank.values()),
    )
    return total, per_bank


def row_hit_series(events: Sequence[tuple[int, int, str]], run_start: int, run_end: int) -> Series:
    """Hit rate per window, from the same classified events `row_hits` used —
    *when* it degrades, not just that it did."""
    width = _windows(max(1, run_end - run_start))
    series = Series(name="row_hit_rate", unit="ratio", window=width)
    ordered = sorted(events, key=lambda e: e[0])
    lo = run_start
    i = 0
    while lo < run_end:
        hi = min(lo + width, run_end)
        hits = misses = conflicts = 0
        while i < len(ordered) and ordered[i][0] < hi:
            kind = ordered[i][2]
            if kind == "hit":
                hits += 1
            elif kind == "miss":
                misses += 1
            else:
                conflicts += 1
            i += 1
        total = hits + misses + conflicts
        series.points.append((lo, hits / total if total else 0.0))
        lo = hi
    return series


def bus_utilization(
    commands: Sequence[CmdEvent], run_start: int, run_end: int, period: int | None
) -> tuple[float, Series]:
    """Share of *clock cycles* carrying a READ or WRITE, overall and per window.

    Per cycle, not per trace unit: a dump whose timescale is picoseconds would
    otherwise report a utilisation a thousand times lower than the same design
    dumped in nanoseconds, which is a units bug wearing a metric's clothes.
    """
    rw = sorted((c for c in commands if c.name in ("READ", "WRITE")), key=lambda c: c.time)
    span = (run_end - run_start) or 1
    total_cycles = max(1, _in_cycles(span, period))
    overall = len(rw) / total_cycles

    width = _windows(span)
    series = Series(name="bus_utilization", unit="ratio", window=width)
    lo = run_start
    i = 0
    while lo < run_end:
        hi = min(lo + width, run_end)
        n = 0
        while i < len(rw) and rw[i].time < hi:
            n += 1
            i += 1
        series.points.append((lo, n / max(1, _in_cycles(hi - lo, period))))
        lo = hi
    return overall, series


def _in_cycles(units: int, period: int | None) -> int:
    """Trace time units as a count of clock cycles."""
    return round(units / period) if period else units


def refresh_overhead(
    commands: Sequence[CmdEvent], rfc_units: int, run_start: int, run_end: int
) -> float:
    """Share of the run spent unavailable because of REFRESH.

    A ratio of two spans in the same units, so it needs no clock of its own.
    """
    n_refresh = sum(1 for c in commands if c.name == "REFRESH")
    total = (run_end - run_start) or 1
    return min(1.0, (n_refresh * rfc_units) / total)


def turnaround(commands: Sequence[CmdEvent], period: int | None) -> tuple[int, int]:
    """Cycles spent, and times it happened, switching between READ and WRITE.

    Every direction change costs the gap between the two commands that
    changed it — the bus is not moving useful data for either party while it
    turns around, whatever the exact protocol-level reason.
    """
    rw = sorted((c for c in commands if c.name in ("READ", "WRITE")), key=lambda c: c.time)
    units = 0
    events = 0
    for prev, cur in zip(rw, rw[1:]):
        if prev.name != cur.name:
            units += cur.time - prev.time
            events += 1
    return _in_cycles(units, period), events


def bank_parallelism(
    segments: Sequence[BankSegment], run_start: int, run_end: int
) -> tuple[float, Series]:
    """Average number of banks simultaneously unavailable for a fresh
    ACTIVATE — `ACTIVATING`, `ACTIVE` or `PRECHARGING`, i.e. everything but
    `idle`."""
    busy = [s for s in segments if s.state in (ACTIVATING, ACTIVE, PRECHARGING)]
    total = (run_end - run_start) or 1
    area = sum(min(s.t1, run_end) - max(s.t0, run_start) for s in busy)
    overall = area / total

    width = _windows(total)
    series = Series(name="bank_parallelism", unit="banks", window=width)
    lo = run_start
    while lo < run_end:
        hi = min(lo + width, run_end)
        overlap = sum(max(0, min(s.t1, hi) - max(s.t0, lo)) for s in busy)
        series.points.append((lo, overlap / max(1, hi - lo)))
        lo = hi
    return overall, series


def measure(
    commands: Sequence[CmdEvent],
    segments: Sequence[BankSegment],
    n_banks: int,
    rfc_units: int,
    run_start: int,
    run_end: int,
    period: int | None = None,
) -> Efficiency:
    """Every §8.20 efficiency metric for one interface."""
    out = Efficiency()
    events = _classify(commands, n_banks)
    out.row_hits, out.per_bank = row_hits(commands, n_banks)
    out.row_hit_series = row_hit_series(events, run_start, run_end)
    out.bus_utilization, out.bus_utilization_series = bus_utilization(
        commands, run_start, run_end, period
    )
    out.refresh_overhead = refresh_overhead(commands, rfc_units, run_start, run_end)
    out.turnaround_cycles, out.turnaround_events = turnaround(commands, period)
    out.bank_parallelism, out.bank_parallelism_series = bank_parallelism(segments, run_start, run_end)
    return out
