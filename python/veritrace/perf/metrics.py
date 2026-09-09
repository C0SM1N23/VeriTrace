"""The §8.17 metric table, as folds over what extraction already produced.

*Once you have transactions, the metrics are aggregations.* Everything here is
therefore arithmetic over `Extraction.transactions` and `Extraction.perf` — no
second pass over the trace, and nothing that knows a protocol name.

Two definitions are worth stating plainly, because a metric whose definition is
ambiguous is a metric people argue about instead of using:

* **Throughput** is bytes moved per cycle, and the theoretical peak it is
  compared against is one full beat every cycle on the pack's transfer
  channels. When the pack declares a `strobe`, the bytes are the ones the beat
  actually enabled, so a burst writing one byte per beat does not read as a
  saturated bus.
* **Burst efficiency** is data beats divided by the cycles the transaction held
  the bus. §8.17 asks for "useful beats / total beats" and names its purpose:
  *detects short bursts that waste the bus*. A ratio of beats to beats cannot do
  that — it is 1.0 for every burst length. Measured against occupancy it falls
  as the per-transaction overhead grows, which is the thing being looked for.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from veritrace.perf.model import (
    AgentShare,
    Fairness,
    InterfacePerf,
    Series,
    StallProfile,
    histogram,
    jain,
)
from veritrace.protocol.model import Extraction, Transaction

#: Windows in a time series. Enough to see structure, few enough to send and to
#: draw; the window width follows from the run length rather than being fixed,
#: so a 500-cycle trace and a 5-million-cycle one both come back readable.
SERIES_POINTS = 120


def in_window(ex: Extraction, t0: int, t1: int) -> Extraction:
    """Metric view of [t0, t1], without mutating the stored extraction.

    Transfers are counted inside the window. Latency measures transactions
    completing there, retaining their actual issue-to-completion durations.
    """
    perf = ex.perf
    if perf is not None:
        lo, hi = perf.window(t0, t1)
        perf = replace(perf, **{
            name: getattr(perf, name)[lo:hi]
            for name in ("edges", "labels", "requesting", "transferring", "outstanding", "blocked_on")
        })
    transactions = [
        replace(
            txn,
            end_time=txn.end_time if txn.end_time is not None and txn.end_time <= t1 else None,
            events=[event for event in txn.events if t0 <= event.time <= t1],
        )
        for txn in ex.transactions
        if txn.start_time <= t1 and (txn.end_time is None or txn.end_time >= t0)
    ]
    return replace(
        ex, perf=perf, transactions=transactions,
        sampled_cycles=len(perf) if perf is not None else 0,
        beats=[beat for beat in ex.beats if t0 <= beat.time <= t1],
    )


def _windows(n_cycles: int, points: int = SERIES_POINTS) -> int:
    return max(1, -(-n_cycles // max(1, points)))


def stall_profile(ex: Extraction) -> StallProfile | None:
    """Fold `CyclePerf.labels` into §8.17's headline plus its time series."""
    perf = ex.perf
    if perf is None or not len(perf):
        return None
    counts = perf.counts()
    prof = StallProfile(
        iface=ex.interface.name,
        buckets=list(perf.buckets),
        counts=counts,
        total=len(perf),
        reasons={r.name: r.msg for r in ex.interface.pack.stall_reasons if r.msg},
        notes=dict(perf.notes),
    )
    width = _windows(len(perf))
    for lo in range(0, len(perf), width):
        chunk = perf.labels[lo : lo + width]
        window = {b: 0 for b in perf.buckets}
        for label in chunk:
            window[perf.buckets[label]] += 1
        prof.series.append(
            {
                "t0": perf.edges[lo],
                "t1": perf.edges[min(len(perf) - 1, lo + width - 1)],
                "cycles": len(chunk),
                "counts": window,
            }
        )
    return prof


def _latency_of(t: Transaction) -> int | None:
    """Cycles from issue to completion.

    Prefers the pack's own `latency` metric so a protocol that measures it
    differently is believed, and falls back to the duration in raw time units
    only when there is none. An unfinished transaction has no latency: it has
    not finished, and calling the rest of the run its latency would put a
    fabricated point in the p99.
    """
    if not t.closed:
        return None
    v = t.metrics.get("latency")
    if isinstance(v, int):
        return v
    d = t.duration()
    return d if d is not None else None


def latency(ex: Extraction, by: str | None = None) -> dict[str, Any]:
    """Histogram overall, and grouped when §10.1's `by=` asks for it."""
    pairs = [(t.ref, v) for t in ex.transactions if (v := _latency_of(t)) is not None]
    out: dict[str, Any] = {"all": histogram(pairs)}
    if not by:
        return out
    groups: dict[str, list[tuple[str, int]]] = {}
    for t in ex.transactions:
        v = _latency_of(t)
        if v is None:
            continue
        groups.setdefault(_group_of(t, by), []).append((t.ref, v))
    out["groups"] = {k: histogram(v) for k, v in sorted(groups.items())}
    return out


def _group_of(t: Transaction, by: str) -> str:
    match by:
        case "master" | "iface" | "interface":
            return t.iface
        case "id":
            return "—" if t.id is None else str(t.id)
        case "type" | "kind":
            return t.kind
    return str(t.fields.get(by) if by in t.fields else t.metrics.get(by, "—"))


def _bytes_per_beat(ex: Extraction, store: Any) -> int | None:
    """Width of one beat in bytes, from the pack's declared data field."""
    field = ex.interface.pack.perf.data
    path = ex.interface.signals.get(field) if field else None
    if path is None:
        return None
    h = store.find(path) if store is not None else None
    if h is None:
        return None
    return max(1, store.signal(h).width // 8)


def _popcount(v: Any) -> int | None:
    if isinstance(v, int):
        return int(v).bit_count()
    return None


def bytes_moved(ex: Extraction, store: Any) -> tuple[int, int | None]:
    """`(bytes, bytes per beat)` over the transfer channels.

    The strobe wins when the pack declares one and the design carries it: a
    beat with `wstrb=0x1` moved one byte, and counting it as four is how a
    throughput chart ends up flattering the design.
    """
    perf = ex.interface.pack.perf
    per_beat = _bytes_per_beat(ex, store)
    channels = set(perf.channels(ex.interface.pack))
    total = 0
    for t in ex.transactions:
        for e in t.events:
            if e.channel not in channels:
                continue
            n = _popcount(e.fields.get(perf.strobe)) if perf.strobe else None
            total += n if n is not None else (per_beat or 0)
    return total, per_beat


def throughput(ex: Extraction, store: Any, clock: Any = None) -> tuple[Series | None, int, float | None]:
    """Bytes per cycle over time, plus the total and the theoretical peak."""
    perf = ex.perf
    total, per_beat = bytes_moved(ex, store)
    if perf is None or not len(perf):
        return None, total, float(per_beat) if per_beat else None

    channels = set(ex.interface.pack.perf.channels(ex.interface.pack))
    at = {t: i for i, t in enumerate(perf.edges)}
    per_cycle = [0] * len(perf)
    for t in ex.transactions:
        for e in t.events:
            i = at.get(e.time)
            if i is None or e.channel not in channels:
                continue
            n = _popcount(e.fields.get(ex.interface.pack.perf.strobe)) if ex.interface.pack.perf.strobe else None
            per_cycle[i] += n if n is not None else (per_beat or 0)

    width = _windows(len(perf))
    series = Series(name="throughput", unit="bytes/cycle", window=width)
    for lo in range(0, len(perf), width):
        chunk = per_cycle[lo : lo + width]
        series.points.append((perf.edges[lo], sum(chunk) / len(chunk)))
    return series, total, float(per_beat) if per_beat else None


def outstanding(ex: Extraction) -> tuple[Series | None, int, bool]:
    """Open transactions over time, with §8.17's plateau test.

    *A graph that flattens at a fixed value means you have found the limit that
    is holding you.* So the plateau is computed rather than left to the eye: the
    peak is a plateau when the series sits at it for a large share of the time
    it is non-zero at all.
    """
    perf = ex.perf
    if perf is None or not len(perf) or not perf.outstanding:
        return None, 0, False
    vals = perf.outstanding
    peak = max(vals)
    width = _windows(len(perf))
    series = Series(name="outstanding", unit="transactions", window=width)
    for lo in range(0, len(vals), width):
        chunk = vals[lo : lo + width]
        series.points.append((perf.edges[lo], max(chunk)))
    busy = sum(1 for v in vals if v)
    # A peak of one is not a ceiling anyone hit: a protocol that permits a single
    # transaction in flight sits at one whenever it is doing anything, and
    # calling that "the limit holding you" on every AXI4-Lite interface would
    # make the real plateaus invisible.
    plateau = peak >= 2 and busy > 0 and sum(1 for v in vals if v == peak) >= 0.5 * busy
    return series, peak, plateau


def burst_efficiency(ex: Extraction) -> float | None:
    """Data beats per cycle of bus occupancy, averaged over transactions."""
    channels = set(ex.interface.pack.perf.channels(ex.interface.pack))
    beats = 0
    occupied = 0
    for t in ex.transactions:
        if not t.closed:
            continue
        n = sum(1 for e in t.events if e.channel in channels)
        span = t.duration()
        if not n or span is None:
            continue
        beats += n
        # In cycles when there is a clock to count them in; the ratio is a
        # ratio either way, and mixing units inside one sum is the bug this
        # avoids.
        occupied += max(n, _span_cycles(ex, t))
    return (beats / occupied) if occupied else None


def _span_cycles(ex: Extraction, t: Transaction) -> int:
    perf = ex.perf
    if perf is None or not len(perf):
        return 1
    lo, hi = perf.window(t.start_time, t.end_time if t.end_time is not None else t.start_time)
    return max(1, hi - lo)


def fairness(extractions: Sequence[Extraction]) -> Fairness:
    """Jain's index across agents, from cycles asked for versus cycles served.

    Deliberately built from `requesting`/`transferring` rather than from the
    stall labels: a detector that changes its answer when someone renames a rung
    of the cascade would be measuring the pack, not the design.
    """
    out = Fairness()
    live = [ex for ex in extractions if ex.perf is not None and len(ex.perf)]
    if not live:
        return out

    # Which agents moved in each window, for starvation's "while others were
    # progressing" clause. Indexed by edge time so interfaces on different
    # clocks still line up.
    served_at: dict[int, set[str]] = {}
    for ex in live:
        perf = ex.perf
        for i, moved in enumerate(perf.transferring):
            if moved:
                served_at.setdefault(perf.edges[i], set()).add(ex.interface.name)

    for ex in live:
        perf = ex.perf
        share = AgentShare(agent=ex.interface.name)
        share.requested = sum(perf.requesting)
        share.granted = sum(perf.transferring)
        run = 0
        run_from: int | None = None
        for i in range(len(perf)):
            asking = perf.requesting[i] and not perf.transferring[i]
            if asking:
                run += 1
                if run_from is None:
                    run_from = perf.edges[i]
                if run > share.starved:
                    share.starved, share.starved_at = run, run_from
            else:
                run, run_from = 0, None
        out.agents.append(share)

    out.index = jain([a.granted for a in out.agents])
    worst = max(out.agents, key=lambda a: a.starved, default=None)
    if worst is not None and worst.starved:
        out.worst, out.worst_cycles = worst.agent, worst.starved
    return out


def measure(ex: Extraction, store: Any = None, clock: Any = None) -> InterfacePerf:
    """Every §8.17 metric for one interface."""
    out = InterfacePerf(iface=ex.interface.name)
    out.stalls = stall_profile(ex)
    out.latency = latency(ex)["all"]
    tp, total, peak = throughput(ex, store, clock)
    out.throughput, out.bytes_moved, out.peak_bytes_per_cycle = tp, total, peak
    out.outstanding, out.outstanding_peak, out.outstanding_plateau = outstanding(ex)
    out.burst_efficiency = burst_efficiency(ex)
    if ex.perf is None:
        out.notes["perf"] = (
            "no per-cycle profile for this interface; "
            + (ex.skipped.get("clock") or "the extraction was restored from a cache without one")
        )
    elif not ex.interface.pack.stall_reasons:
        out.notes["stalls"] = (
            f"{ex.interface.pack.name} declares no [[stall_reason]], so every "
            "non-transfer cycle is `other`"
        )
    return out
