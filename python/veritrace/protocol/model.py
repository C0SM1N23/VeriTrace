"""What the extraction engine produces — §8.13.

The shape §8.13 asks for, in one place:

    #128  AXI_WRITE  m=dma0  addr=0x2000_4000  len=16  size=4  burst=INCR
          issued c1200 - addr_acc c1203 - first_data c1205 - last c1220
          latency: 22 cicluri (addr->resp)  -  stall: 3 cicluri backpressure

Two fields carry more weight than they look:

* **`index`** is the `#128`. Numbered per (interface, type) in start order, so
  `txn.dma0.WRITE[128]` names the same transaction across a CLI run, the UI and
  a `why()` query — and across re-runs of the same dump, because the ordering is
  the trace's, not a dictionary's.
* **`status`** distinguishes *closed* from *never closed*. A transaction that
  never completed is not missing data, it is the finding: `txn(iface) | open()`
  is how §11.4b's deadlock candidates are found, and §8.18 builds its wait-for
  graph out of exactly these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from veritrace.perf.model import CyclePerf
from veritrace.protocol.pack import Pack

#: A field value after sampling: an integer, the raw four-state digits when it
#: carried X/Z or exceeded 64 bits, or `None` when the signal had no value yet.
FieldValue = int | str | None


@dataclass(slots=True)
class Interface:
    """One protocol interface found in the design (§8.14, step 1)."""

    name: str
    #: Hierarchical scope the signals live in.
    scope: str
    #: The prefix that was stripped to match the pack, e.g. `m_axi_`.
    prefix: str
    pack: Pack
    #: Pack-relative suffix -> full hierarchical path in the trace.
    signals: dict[str, str]
    clock: str | None = None
    reset: str | None = None
    #: Other names the same wires appear under — the master port and the slave
    #: port it drives. One interface, several spellings.
    aliases: list[str] = field(default_factory=list)

    def path_of(self, suffix: str) -> str | None:
        return self.signals.get(suffix)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "prefix": self.prefix,
            "pack": self.pack.name,
            "pack_slug": self.pack.slug,
            "clock": self.clock,
            "reset": self.reset,
            "aliases": list(self.aliases),
            "signals": dict(self.signals),
        }


@dataclass(slots=True)
class ChannelEvent:
    """One accepted beat: the clock edge where valid and ready were both high.

    `assert_time` and `stall` come from the same scan and are what §8.17 later
    attributes cycles with, so they are recorded even when no metric reads them
    — recomputing them would mean a second pass over the trace.
    """

    channel: str
    #: The edge the transfer happened on.
    time: int
    #: The edge `valid` first went high in the run that ended in this transfer.
    assert_time: int
    #: Edges where `valid` was high and `ready` was not.
    stall: int
    fields: dict[str, FieldValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "time": self.time,
            "assert_time": self.assert_time,
            "stall": self.stall,
            "fields": dict(self.fields),
        }


@dataclass(slots=True)
class Beat:
    """One data beat, resolved to a byte address — §8.19's unit of reasoning.

    A write beat says *these bytes of memory now hold these values*; a read beat
    says *this is what came back*. Both are the same shape, so the scoreboard is
    one ordered walk rather than two.

    Produced during extraction (`protocol.beats`) because it is derived from the
    channel events, which are not kept: the transaction table records how many
    beats there were, not what each carried.
    """

    iface: str
    #: `dma.WRITE[3]` — how the transaction is named everywhere else.
    txn: str
    #: `"write"` or `"read"`.
    dir: str
    time: int
    #: 0-based index of this beat within its transaction.
    beat: int
    #: Byte address of lane 0, from the pack's `beat_addr`. `None` when the pack
    #: declares no address (a stream) or the address could not be decoded.
    addr: int | None
    data: FieldValue
    #: Byte-enable mask, already defaulted to "every lane" when the protocol has
    #: no strobes.
    strobe: int
    #: Bus width in bytes.
    stride: int

    @property
    def cycle_key(self) -> tuple[int, str, int]:
        """Total order over beats from different interfaces at the same instant.

        Ties broken by name and beat index rather than left to sort stability,
        so two runs over the same trace produce the same report (P1).
        """
        return (self.time, self.iface, self.beat)

    def lanes(self) -> dict[int, int]:
        """Byte address -> byte value, for the lanes this beat actually drove.

        Empty when the payload was not a plain integer: a beat carrying X is a
        beat whose bytes are unknown, and inventing zeros for it would put a
        fabricated value into the reference memory (P1).
        """
        if self.addr is None or not isinstance(self.data, int):
            return {}
        return {
            self.addr + i: (self.data >> (8 * i)) & 0xFF
            for i in range(self.stride)
            if self.strobe >> i & 1
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "txn": self.txn,
            "dir": self.dir,
            "time": self.time,
            "beat": self.beat,
            "addr": self.addr,
            "data": self.data,
            "strobe": self.strobe,
            "stride": self.stride,
        }


@dataclass(slots=True)
class Violation:
    """A protocol rule that did not hold (§8.14, step 5)."""

    rule: str
    severity: str
    msg: str
    time: int
    #: Signal the rule was about, when it names one.
    signal: str | None = None
    #: `iface.TYPE[n]`, when the violation belongs to a transaction.
    txn: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "msg": self.msg,
            "time": self.time,
            "signal": self.signal,
            "txn": self.txn,
        }


@dataclass(slots=True)
class Transaction:
    iface: str
    kind: str
    #: `#128`: ordinal within (interface, kind), in start order.
    index: int
    start_time: int
    end_time: int | None = None
    #: Protocol id (`awid`), when the pack declares one. Enables outstanding
    #: and out-of-order matching.
    id: FieldValue = None
    #: Named values lifted by `key =`, plus every payload field of the start
    #: event. This is what `txn(iface, addr=0x4000)` filters on.
    fields: dict[str, FieldValue] = field(default_factory=dict)
    #: Evaluated `[metrics]`, at close.
    metrics: dict[str, FieldValue] = field(default_factory=dict)
    events: list[ChannelEvent] = field(default_factory=list)
    #: Transactions already open when this one started (§8.17's outstanding).
    outstanding: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.end_time is not None

    @property
    def status(self) -> str:
        return "complete" if self.closed else "open"

    @property
    def ref(self) -> str:
        """`dma0.WRITE[128]` — how a transaction is named everywhere."""
        return f"{self.iface}.{self.kind}[{self.index}]"

    @property
    def label(self) -> str:
        """`WRITE#128` — the §8.13 display form."""
        return f"{self.kind}#{self.index}"

    def duration(self) -> int | None:
        return None if self.end_time is None else self.end_time - self.start_time

    def beats(self, channel: str) -> list[ChannelEvent]:
        return [e for e in self.events if e.channel == channel]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "iface": self.iface,
            "kind": self.kind,
            "index": self.index,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "id": self.id,
            "fields": dict(self.fields),
            "metrics": dict(self.metrics),
            "outstanding": self.outstanding,
            "n_events": len(self.events),
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass(slots=True)
class Extraction:
    """Everything one interface produced.

    `sampled_cycles` and `correlation` are not decoration: §8.14's acceptance
    criterion is *the correlation rate between signals and transactions is
    reported explicitly*, and a run that quietly found 3 of 400 transactions has
    to be visibly different from one that found all 3 (P1, P7).
    """

    interface: Interface
    transactions: list[Transaction] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    #: Clock edges the scan sampled.
    sampled_cycles: int = 0
    #: Channel events seen, and how many landed in a transaction.
    n_events: int = 0
    n_matched: int = 0
    #: Why a channel or rule could not be evaluated. Recorded, never swallowed.
    skipped: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    #: Where the table was written, when it was.
    parquet: str | None = None
    #: §8.17's per-cycle attribution, produced by the same pass that found the
    #: channel events. Carried here rather than recomputed because a second scan
    #: of the trace to answer "why was it slow" would double the only expensive
    #: part of opening a session.
    perf: CyclePerf | None = None
    #: §8.19's data beats, for the same reason and from the same pass. The
    #: transaction table records how many beats a transaction had; only this
    #: records what each one carried, and the scoreboard needs the payload.
    beats: list[Beat] = field(default_factory=list)

    @property
    def correlation(self) -> float | None:
        """Share of channel events that ended up inside a transaction.

        `None` when there were no events at all — an interface that never moved
        has no rate, and reporting 0% or 100% would both be inventions.
        """
        return None if not self.n_events else 100.0 * self.n_matched / self.n_events

    @property
    def open_transactions(self) -> list[Transaction]:
        return [t for t in self.transactions if not t.closed]

    def by_ref(self) -> dict[str, Transaction]:
        return {t.ref: t for t in self.transactions}

    def to_dict(self, with_transactions: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "interface": self.interface.to_dict(),
            "n_transactions": len(self.transactions),
            "n_open": len(self.open_transactions),
            "n_events": self.n_events,
            "n_matched": self.n_matched,
            "correlation": (
                None if self.correlation is None else round(self.correlation, 1)
            ),
            "sampled_cycles": self.sampled_cycles,
            "kinds": sorted({t.kind for t in self.transactions}),
            "violations": [v.to_dict() for v in self.violations],
            "skipped": dict(self.skipped),
            "ms": round(self.elapsed_ms, 2),
            "parquet": self.parquet,
        }
        if with_transactions:
            out["transactions"] = [t.to_dict() for t in self.transactions]
        return out
