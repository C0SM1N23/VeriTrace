"""What §8.17 and §8.18 produce.

Deliberately free of any dependency on the protocol engine: these are the
*answers*, and keeping them plain makes them equally reachable from the CLI,
the API, the Parquet export and a test that builds one by hand.

The one type that is not an aggregate is `CyclePerf`. It is the per-clock-edge
record produced during extraction step 2, and everything else in this module is
a fold over it. That direction matters: §8.17's promise is that *every cycle
gets exactly one label and the shares add to 100%*, which is only checkable if
there is a single array where each cycle appears once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: Bucket every cycle falls into when no rung of the cascade claimed it. Named
#: rather than dropped: see `pack.RESERVED_BUCKETS`.
OTHER = "other"
RESET = "reset"
TRANSFER = "transfer"

#: Ceiling `CyclePerf.outstanding` is clamped to, so one cycle is one byte. Well
#: past any real interconnect's depth, and recorded here so a series that sits at
#: it is read as saturation of the *counter* rather than as a design limit.
OUTSTANDING_CAP = 255


@dataclass(slots=True)
class CyclePerf:
    """One byte per clock edge per interface — the raw material of TAB 9.

    `labels` indexes `buckets`. `requesting` and `transferring` are kept beside
    it rather than being read back out of the labels, because §8.18's starvation
    and §8.17's fairness must not depend on what a pack chose to *call* its
    buckets: a detector that changes its mind when someone renames a rung is not
    a detector.
    """

    #: Bucket name per label value. Index 0 is always `reset`, 1 `transfer`.
    buckets: list[str]
    #: Clock edges these arrays are indexed by, ascending.
    edges: list[int]
    #: Bucket index per edge.
    labels: bytes = b""
    #: 1 where a transfer channel had `valid` high — the agent wanted the bus.
    requesting: bytes = b""
    #: 1 where a beat was accepted on a transfer channel.
    transferring: bytes = b""
    #: Open transactions at each edge, capped at 255 for storage. The cap is
    #: recorded so a plateau at 255 is never read as a real ceiling.
    outstanding: bytes = b""
    #: 0 where nothing was refused, else 1 + the index into `channels` of the
    #: first channel offering a beat that was not accepted. This is what §8.18
    #: needs and the labels cannot give: *which wire* an agent is stuck on.
    blocked_on: bytes = b""
    #: Transfer channels, in the order `blocked_on` indexes them.
    channels: list[str] = field(default_factory=list)
    #: Stall reasons that could not be evaluated, and why. Their cycles fall
    #: through to a later rung, so the total is unaffected — but a rung that
    #: never fires must say so rather than look like a cause that never happened.
    notes: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.labels)

    def counts(self) -> dict[str, int]:
        """Cycles per bucket. Sums to `len(self)` by construction."""
        out = {b: 0 for b in self.buckets}
        for label in self.labels:
            out[self.buckets[label]] += 1
        return out

    def time_of(self, i: int) -> int:
        return self.edges[i]

    def window(self, t0: int, t1: int) -> tuple[int, int]:
        """Half-open index range covering [t0, t1]."""
        from bisect import bisect_left, bisect_right

        return bisect_left(self.edges, t0), bisect_right(self.edges, t1)


@dataclass(slots=True)
class StallProfile:
    """§8.17's headline: *out of 10.000 cycles, 3.200 were lost, and here is
    what to*."""

    iface: str
    buckets: list[str]
    counts: dict[str, int]
    total: int
    #: `[{"t0":…, "t1":…, "counts": {...}}]` — the stacked area of TAB 9.
    series: list[dict[str, Any]] = field(default_factory=list)
    #: Bucket -> the pack's own sentence about it.
    reasons: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def lost(self) -> int:
        """Cycles that moved nothing, ignoring reset — §8.17's "3.200 pierdute"."""
        return self.total - self.counts.get(TRANSFER, 0) - self.counts.get(RESET, 0)

    def shares(self, digits: int = 1) -> dict[str, float]:
        """Percentages that add to exactly 100.

        Largest-remainder rather than naive rounding: a stacked chart whose
        labels add to 99.9% looks like a bug in the tool, and §8.17 makes "the
        sum is 100%, verifiable" part of the claim. With no cycles at all there
        is nothing to apportion, and every share is 0 — not 100 spread over
        buckets that never happened.
        """
        if not self.total:
            return {b: 0.0 for b in self.buckets}
        scale = 10**digits
        exact = {b: self.counts.get(b, 0) * 100 * scale / self.total for b in self.buckets}
        floors = {b: int(v) for b, v in exact.items()}
        left = 100 * scale - sum(floors.values())
        # Ties broken by bucket order, so two runs of the same trace agree (P1).
        order = sorted(self.buckets, key=lambda b: (-(exact[b] - floors[b]), self.buckets.index(b)))
        for b in order[:left]:
            floors[b] += 1
        return {b: floors[b] / scale for b in self.buckets}

    def to_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "buckets": list(self.buckets),
            "counts": dict(self.counts),
            "shares": self.shares(),
            "total": self.total,
            "lost": self.lost,
            "series": self.series,
            "reasons": dict(self.reasons),
            "notes": dict(self.notes),
        }


@dataclass(slots=True)
class Histogram:
    """Latency, as §8.17 wants it: the whole distribution, not an average.

    *An average latency hides the tail, and the tail is the bug.* So the
    percentiles are carried explicitly and the outliers keep their transaction
    refs, which is what makes them clickable in TAB 9.
    """

    n: int = 0
    min: int = 0
    max: int = 0
    mean: float = 0.0
    p50: int = 0
    p95: int = 0
    p99: int = 0
    #: `[(lo, hi, count)]`, contiguous and covering [min, max].
    bins: list[tuple[int, int, int]] = field(default_factory=list)
    #: `[(ref, latency)]` for the slowest few — the clickable tail.
    outliers: list[tuple[str, int]] = field(default_factory=list)
    unit: str = "cycles"

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "min": self.min,
            "max": self.max,
            "mean": round(self.mean, 2),
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "bins": [{"lo": lo, "hi": hi, "n": n} for lo, hi, n in self.bins],
            "outliers": [{"ref": r, "value": v} for r, v in self.outliers],
            "unit": self.unit,
        }


@dataclass(slots=True)
class Series:
    """A time series, sampled on windows of the interface's own clock."""

    name: str
    unit: str
    #: `[(t, value)]`, one point per window, at the window's start.
    points: list[tuple[int, float]] = field(default_factory=list)
    #: Window width in clock cycles.
    window: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "window": self.window,
            "points": [{"t": t, "v": round(v, 4)} for t, v in self.points],
        }


@dataclass(slots=True)
class AgentShare:
    """One master's slice of a shared resource — the row behind §8.17's
    fairness bar."""

    agent: str
    #: Cycles this agent asked for the bus.
    requested: int = 0
    #: Cycles it got it.
    granted: int = 0
    #: Bytes moved, when the pack says how wide a beat is.
    bytes: int = 0
    #: Longest run of asking without being served, in cycles.
    starved: int = 0
    #: When that run started, so the finding can point at it.
    starved_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "requested": self.requested,
            "granted": self.granted,
            "bytes": self.bytes,
            "starved": self.starved,
            "starved_at": self.starved_at,
        }


@dataclass(slots=True)
class Fairness:
    """Jain's index over what the agents actually got.

    Jain rather than a bare min/max because it is the standard the literature
    and every interconnect datasheet use: 1.0 is perfectly equal, 1/n is one
    agent taking everything, and the value in between is interpretable without
    knowing how many agents there are.
    """

    agents: list[AgentShare] = field(default_factory=list)
    index: float = 1.0
    #: The agent with the longest unserved run, and how long it was.
    worst: str | None = None
    worst_cycles: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": [a.to_dict() for a in self.agents],
            "index": round(self.index, 4),
            "worst": self.worst,
            "worst_cycles": self.worst_cycles,
        }


def jain(values: Sequence[float]) -> float:
    """Jain's fairness index. 1.0 for an empty or all-zero set: nobody got
    anything, which is not unfair, it is idle."""
    vals = [float(v) for v in values]
    total = sum(vals)
    if not vals or total == 0:
        return 1.0
    return total * total / (len(vals) * sum(v * v for v in vals))


@dataclass(slots=True)
class InterfacePerf:
    """Everything TAB 9 shows for one interface."""

    iface: str
    stalls: StallProfile | None = None
    latency: Histogram | None = None
    #: `by=master` / `by=id` groupings, as §10.1's `latency(iface, by=master)`.
    latency_by: dict[str, Histogram] = field(default_factory=dict)
    throughput: Series | None = None
    outstanding: Series | None = None
    #: Peak outstanding, and whether the series sat there — §8.17's "plateauing
    #: at a fixed value means you have found the limit".
    outstanding_peak: int = 0
    outstanding_plateau: bool = False
    #: Useful beats / bus-occupied cycles.
    burst_efficiency: float | None = None
    bytes_moved: int = 0
    #: Bytes per cycle if every cycle carried a full beat.
    peak_bytes_per_cycle: float | None = None
    notes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "stalls": self.stalls.to_dict() if self.stalls else None,
            "latency": self.latency.to_dict() if self.latency else None,
            "latency_by": {k: v.to_dict() for k, v in self.latency_by.items()},
            "throughput": self.throughput.to_dict() if self.throughput else None,
            "outstanding": self.outstanding.to_dict() if self.outstanding else None,
            "outstanding_peak": self.outstanding_peak,
            "outstanding_plateau": self.outstanding_plateau,
            "burst_efficiency": (
                None if self.burst_efficiency is None else round(self.burst_efficiency, 4)
            ),
            "bytes_moved": self.bytes_moved,
            "peak_bytes_per_cycle": self.peak_bytes_per_cycle,
            "notes": dict(self.notes),
        }


# --- §8.18 ------------------------------------------------------------------


@dataclass(slots=True)
class WaitEdge:
    """`A waits for R, held by B` — one arrow of §8.18's wait-for graph."""

    agent: str
    resource: str
    holder: str | None
    since: int
    until: int
    #: The transaction of `agent` that is blocked, e.g. `m0.WRITE[3]`.
    txn: str | None = None
    #: Pack channel the blocked handshake is on, e.g. `AW`.
    channel: str = ""
    #: What the agent was trying to do, in words. §8.18 reports the first
    #: blocked transaction; when the blockage stopped one from even starting
    #: there is no row in the table to point at, and naming the one that would
    #: have been next is more use than an empty field.
    blocked: str = ""
    #: The transaction of `holder` occupying the resource.
    holder_txn: str | None = None
    #: Cycles the edge was live, for "> N cycles" and for the report.
    cycles: int = 0
    #: How the holder was identified. Shown, because "the RTL says so" and
    #: "nothing said so" must not look alike.
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "resource": self.resource,
            "holder": self.holder,
            "since": self.since,
            "until": self.until,
            "txn": self.txn,
            "channel": self.channel,
            "blocked": self.blocked,
            "holder_txn": self.holder_txn,
            "cycles": self.cycles,
            "evidence": self.evidence,
            # §8.18's "[Why pe fiecare veriga]": every link is a question the
            # causal engine can answer on its own.
            "why": f"why({self.resource} @ {self.since})",
        }


@dataclass(slots=True)
class Deadlock:
    """A cycle in the wait-for graph that persisted."""

    #: Agents in wait order; `agents[i]` waits on `edges[i]`.
    agents: list[str]
    edges: list[WaitEdge]
    at: int
    cycles: int
    #: The first transaction stuck by it — §8.18 reports this by name.
    first_txn: str | None = None
    first_txn_at: int | None = None
    #: What the first blocked agent was trying to do, when no transaction of its
    #: own exists to name.
    first_blocked: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": list(self.agents),
            "edges": [e.to_dict() for e in self.edges],
            "at": self.at,
            "cycles": self.cycles,
            "first_txn": self.first_txn,
            "first_txn_at": self.first_txn_at,
            "first_blocked": self.first_blocked,
        }


@dataclass(slots=True)
class Livelock:
    """Motion without progress: a state signal oscillating while nothing
    completes."""

    signal: str
    since: int
    until: int
    toggles: int
    states: list[str]
    #: Transactions that completed in the window. Zero is what makes it a
    #: livelock rather than a busy design.
    completed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "since": self.since,
            "until": self.until,
            "toggles": self.toggles,
            "states": list(self.states),
            "completed": self.completed,
        }


@dataclass(slots=True)
class Starvation:
    """An agent that asked and was not served while others progressed."""

    agent: str
    since: int
    until: int
    cycles: int
    #: Agents that did move in that window — the "in timp ce altii progreseaza"
    #: half, without which a quiet bus would read as starvation.
    served: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "since": self.since,
            "until": self.until,
            "cycles": self.cycles,
            "served": list(self.served),
        }


@dataclass(slots=True)
class Liveness:
    """The §8.18 scan, whole."""

    deadlocks: list[Deadlock] = field(default_factory=list)
    livelocks: list[Livelock] = field(default_factory=list)
    starvation: list[Starvation] = field(default_factory=list)
    #: Why a part of the scan could not run — no RTL, no clock, no transactions.
    skipped: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.deadlocks or self.livelocks or self.starvation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadlocks": [d.to_dict() for d in self.deadlocks],
            "livelocks": [x.to_dict() for x in self.livelocks],
            "starvation": [s.to_dict() for s in self.starvation],
            "skipped": dict(self.skipped),
        }


@dataclass(slots=True)
class PerfReport:
    """TAB 9, whole."""

    interfaces: list[InterfacePerf] = field(default_factory=list)
    fairness: Fairness | None = None
    liveness: Liveness = field(default_factory=Liveness)
    elapsed_ms: float = 0.0

    def get(self, iface: str) -> InterfacePerf | None:
        return next((i for i in self.interfaces if i.iface == iface), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interfaces": [i.to_dict() for i in self.interfaces],
            "fairness": self.fairness.to_dict() if self.fairness else None,
            "liveness": self.liveness.to_dict(),
            "ms": round(self.elapsed_ms, 2),
        }


def percentile(sorted_values: Sequence[int], q: float) -> int:
    """Nearest-rank percentile.

    Nearest-rank and not interpolated on purpose: a latency is a whole number
    of cycles that some transaction actually had, and "p99 = 41.5 cycles" names
    a transaction that never existed.
    """
    if not sorted_values:
        return 0
    k = max(1, min(len(sorted_values), int(-(-q * len(sorted_values) // 1))))
    return sorted_values[k - 1]


def histogram(
    values: Iterable[tuple[str, int]], n_bins: int = 20, n_outliers: int = 10
) -> Histogram:
    """Build a `Histogram` from `(ref, value)` pairs."""
    pairs = [(r, int(v)) for r, v in values if v is not None]
    if not pairs:
        return Histogram()
    vals = sorted(v for _, v in pairs)
    lo, hi = vals[0], vals[-1]
    h = Histogram(
        n=len(vals),
        min=lo,
        max=hi,
        mean=sum(vals) / len(vals),
        p50=percentile(vals, 0.50),
        p95=percentile(vals, 0.95),
        p99=percentile(vals, 0.99),
        outliers=sorted(pairs, key=lambda p: -p[1])[:n_outliers],
    )
    # One bin per distinct value while there are few of them: binning 1..6
    # cycles into 20 ranges invents a spread the data does not have.
    span = hi - lo + 1
    k = min(n_bins, span)
    width = -(-span // k)
    edges = [(lo + i * width, min(hi, lo + (i + 1) * width - 1)) for i in range(k)]
    counts = [0] * k
    for v in vals:
        counts[min(k - 1, (v - lo) // width)] += 1
    h.bins = [(a, b, c) for (a, b), c in zip(edges, counts)]
    return h
