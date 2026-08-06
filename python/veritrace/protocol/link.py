"""Why-trace at transaction level — §8.16.

*Here the two pillars meet, and this is the capability nobody has open source.*

The mechanism is deliberately small, exactly as §8.16 describes it: *when
why-trace reaches a signal belonging to a known interface, check whether a
transaction involving it is open at that moment. If so, insert a `TXN_LINK` node
and carry on from there. One lookup in the transaction table, indexed on
(signal, interval).*

Everything interesting follows from that one hop. A chain that would otherwise
end at `grant != 0` — true, useless — instead reads:

    m0.awvalid stayed 0
    └─ grant = 1
       └─ m1.awvalid = 1
          └─ CORRELATED TRANSACTION: m1.WRITE[7], open at c81, not yet complete
             └─ why did it not complete: s_bvalid still 0
                └─ the slave is still counting wait states

Two guards keep this from becoming an explosion. A transaction is linked at most
once per tree, and only the interfaces' own signals are candidates — so the hop
happens where it means something and nowhere else.

The index is by **event stream**, not by path. A master port, the slave port it
drives and the testbench wire between them are three names for one net, and a
causal walk arrives at whichever the RTL happens to mention; keying on the
stream makes all three the same lookup.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from veritrace.protocol.model import Interface, Transaction

#: How many transaction hops one tree may contain. §8.16's worked example spans
#: three levels of abstraction; beyond a handful the answer stops being a
#: sentence and starts being a graph dump.
MAX_LINKS = 6


@dataclass(slots=True)
class Span:
    """A transaction's live interval, half-open at the end when it never closed."""

    start: int
    end: int | None
    txn: Transaction

    def covers(self, t: int) -> bool:
        return self.start <= t and (self.end is None or t <= self.end)


@dataclass(slots=True)
class TxnIndex:
    """(signal, time) -> the transaction in flight, if there is one."""

    #: Event stream id -> interface name.
    stream_iface: dict[int, str] = field(default_factory=dict)
    #: Interface name -> spans, ascending by start.
    spans: dict[str, list[Span]] = field(default_factory=dict)
    #: Interface name -> the interface itself, for the end-channel lookup.
    interfaces: dict[str, Interface] = field(default_factory=dict)
    _path_stream: dict[str, int | None] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.spans)

    @classmethod
    def build(cls, analysis: Any, store: Any) -> "TxnIndex":
        idx = cls()
        for ex in getattr(analysis, "extractions", []) or []:
            iface = ex.interface
            idx.interfaces[iface.name] = iface
            for path in iface.signals.values():
                h = store.find(path)
                if h is not None:
                    idx.stream_iface.setdefault(store.signal(h).stream_id, iface.name)
            spans = [Span(t.start_time, t.end_time, t) for t in ex.transactions]
            spans.sort(key=lambda s: s.start)
            idx.spans[iface.name] = spans
        return idx

    def _stream(self, store: Any, path: str) -> int | None:
        if path not in self._path_stream:
            h = store.find(path)
            self._path_stream[path] = None if h is None else store.signal(h).stream_id
        return self._path_stream[path]

    def iface_of(self, store: Any, path: str) -> str | None:
        s = self._stream(store, path)
        return None if s is None else self.stream_iface.get(s)

    def at(self, store: Any, path: str, t: int) -> Transaction | None:
        """The transaction in flight on `path`'s interface at `t`.

        The latest one that started at or before `t` and has not finished. When
        several overlap — outstanding transactions on the same interface — the
        most recent is the one the signal is currently carrying.
        """
        name = self.iface_of(store, path)
        if name is None:
            return None
        spans = self.spans.get(name) or []
        i = bisect_right([s.start for s in spans], t)
        for span in reversed(spans[:i]):
            if span.covers(t):
                return span.txn
        return None

    def end_signal(self, txn: Transaction) -> str | None:
        """Path of the `valid` whose rise would complete this transaction.

        This is what "why is it not finished" is a question about, and it is
        read out of the pack rather than assumed: the closing channel of the
        transaction type this transaction belongs to.
        """
        iface = self.interfaces.get(txn.iface)
        if iface is None:
            return None
        spec = next((t for t in iface.pack.transactions if t.name == txn.kind), None)
        phase = spec.end if spec is not None else None
        if phase is None:
            return None
        return iface.signals.get(iface.pack.channel(phase.channel).valid)

    def start_signal(self, iface_name: str, kind: str) -> str | None:
        """Path of the `valid` whose rise would *issue* a transaction of this
        kind — what `not_issued` is a question about."""
        iface = self.interfaces.get(iface_name)
        if iface is None:
            return None
        spec = next((t for t in iface.pack.transactions if t.name == kind), None)
        if spec is None:
            return None
        return iface.signals.get(iface.pack.channel(spec.start).valid)


# --- turning a transaction question into a signal question -------------------


@dataclass(slots=True)
class Question:
    """`why(txn.dma0.WRITE[128].not_issued)`, reduced to something answerable.

    §8.16 is explicit that this is the whole trick: *"the transaction was not
    issued" means `awvalid` never went to 1, so that is a graph question — but
    the result is presented at the right level.* This type is the seam: it
    carries the signal question the causal engine will answer, plus the sentence
    that says what was really asked.
    """

    signal: str
    time: int
    headline: str
    #: The transaction the question is about, when it exists.
    txn: Transaction | None = None
    #: The window the question was asked over, as raw timestamps.
    window: tuple[int, int] | None = None


def _blocked_at(store: Any, path: str, edges: list[int]) -> int | None:
    """A representative instant at which `path` was held at 0.

    The middle of the *longest* uninterrupted stretch of zero in the window.
    Neither end will do: the first low edge catches the design still coming out
    of reset, and the last one lands on the cycle the blockage is releasing —
    which explains why the signal is about to rise, the opposite of what was
    asked. The middle of the longest run is deep inside the stall, and it is
    deterministic, so two runs of the same query agree (P1).

    Observed (`value_at`) and not sampled (`value_before`), unlike the channel
    scan. The scan asks what the flops saw going into an edge; this asks what
    the waveform shows at the instant the answer is about, and handing the
    causal engine a timestamp where the signal has already risen would make it
    explain the wrong value.
    """
    h = store.find(path)
    if h is None or not edges:
        return None
    best = run = (0, -1)
    for i, t in enumerate(edges):
        v = store.value_at(h, t)
        if v is not None and v.to_int() == 0:
            run = (run[0] if run[1] == i - 1 else i, i)
            if run[1] - run[0] >= best[1] - best[0]:
                best = run
    if best[1] < 0:
        return None
    return edges[(best[0] + best[1]) // 2]


def question(
    analysis: Any, index: TxnIndex, store: Any, clock: Any, ref: Any
) -> Question:
    """Resolve a `txn.<iface>.<KIND>[n].<aspect>` query. Raises `ValueError`
    with a usable message when the reference names nothing."""
    ex = analysis.get(ref.iface)
    if ex is None:
        known = ", ".join(e.interface.name for e in analysis.extractions) or "none"
        raise ValueError(f"no interface named `{ref.iface}`; detected: {known}")
    iface = ex.interface
    kinds = {t.name for t in iface.pack.transactions}
    if ref.kind not in kinds:
        raise ValueError(
            f"{iface.pack.name} has no transaction type `{ref.kind}`; "
            f"try {', '.join(sorted(kinds))}"
        )

    same = [t for t in ex.transactions if t.kind == ref.kind]
    txn = next((t for t in same if t.index == ref.index), None)
    edges = list(getattr(clock, "edges", []) or [])
    fmt = (lambda t: f"c{clock.cycle_of(t)}") if clock is not None else str

    if ref.aspect == "not_completed":
        if txn is None:
            raise ValueError(
                f"{iface.name} has no {ref.kind}[{ref.index}]; "
                f"it produced {len(same)} ({ref.kind}[0]..[{len(same) - 1}])"
                if same
                else f"{iface.name} produced no {ref.kind} transactions at all"
            )
        path = index.end_signal(txn)
        if path is None:
            raise ValueError(f"{iface.pack.name}'s {ref.kind} has no closing channel")
        lo = txn.start_time
        hi = txn.end_time if txn.closed else (edges[-1] if edges else lo)
        t = _blocked_at(store, path, [e for e in edges if lo <= e <= hi]) or hi
        state = (
            f"completed at {fmt(txn.end_time)} after {fmt(txn.start_time)}"
            if txn.closed
            else "never completed"
        )
        return Question(
            signal=path,
            time=t,
            headline=f"{txn.label} on {iface.name} {state}; waiting on `{path}`",
            txn=txn,
            window=(lo, hi),
        )

    # not_issued
    path = index.start_signal(iface.name, ref.kind)
    if path is None:
        raise ValueError(f"{iface.pack.name}'s {ref.kind} has no starting channel")
    previous = next((t for t in reversed(same) if t.index < ref.index), None)
    lo = (previous.end_time or previous.start_time) if previous is not None else (edges[0] if edges else 0)
    hi = txn.start_time if txn is not None else (edges[-1] if edges else lo)
    window = [e for e in edges if lo <= e <= hi]
    t = _blocked_at(store, path, window)
    if t is None:
        # `valid` was high across the whole window: the transaction was not
        # blocked from being issued, it was blocked from being *accepted*.
        t = hi
        headline = (
            f"{ref.kind}#{ref.index} on {iface.name} was asserted throughout "
            f"{fmt(lo)}-{fmt(hi)} but not accepted; `{path}` was high"
        )
    elif txn is not None:
        headline = (
            f"{txn.label} on {iface.name} was issued at {fmt(txn.start_time)}; "
            f"this is why it was not issued earlier, in {fmt(lo)}-{fmt(hi)}"
        )
    else:
        headline = (
            f"{ref.kind}#{ref.index} was never issued on {iface.name} in "
            f"{fmt(lo)}-{fmt(hi)} ({len(same)} {ref.kind} transactions were)"
        )
    return Question(signal=path, time=t, headline=headline, txn=txn, window=(lo, hi))
