"""Channel events to data beats — the table §8.19 reasons over.

A beat is a channel event that carried the pack's data field, resolved to the
*byte address* it landed at and the *byte width* of the bus. Both come out of
what is already there — the pack's `[integrity].beat_addr` expression and the
data signal's own width — so nothing here samples the trace a second time.

It lives in the protocol layer, next to `[perf]`'s per-cycle profile and for the
same reason: it is derived from the pack's own declaration while the channel
events are in hand, and those events are not kept afterwards. A pass that ran
later would have to re-extract the whole trace to see them again.
"""

from __future__ import annotations

from typing import Any

from veritrace.protocol import expr
from veritrace.protocol.assemble import TxnEnv
from veritrace.protocol.model import Beat, ChannelEvent, Extraction, Transaction
from veritrace.protocol.pack import Integrity, Pack, Port


def _stride(store: Any, iface: Any, field_name: str) -> int | None:
    """Bus width in bytes, from the data signal itself.

    Deliberately not a pack declaration: a pack describes a protocol and the
    same protocol runs at 32 and at 512 bits. The trace knows which.
    """
    path = iface.signals.get(field_name)
    if not path or store is None:
        return None
    h = store.find(path)
    if h is None:
        return None
    width = store.signal(h).width
    return max(1, (width + 7) // 8)


class _BeatEnv:
    """`beat_addr`'s scope: the transaction, plus this beat's own index.

    Layered over `TxnEnv` rather than replacing it so an expression like AXI4's
    `awburst == 0 ? addr : addr + beat * stride` can read a payload field and a
    beat index in the same breath.
    """

    def __init__(self, parent: TxnEnv, beat: int, stride: int) -> None:
        self.parent = parent
        self.values = {"beat": beat, "stride": stride}

    def lookup(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        return self.parent.lookup(name)

    def call(self, fn: str, args: Any, ev: Any) -> Any:
        return self.parent.call(fn, args, ev)


def _data_events(txn: Transaction, port: Port) -> list[ChannelEvent]:
    """The beats that carried data, in trace order.

    Selected by *which events have the field*, not by channel name: a pack that
    puts `rdata` on `R` and one that puts it on a single combined channel both
    work, and neither needs a second declaration saying where the data lives.
    """
    return [e for e in txn.events if port.data in e.fields]


def _mask(width_bytes: int) -> int:
    return (1 << width_bytes) - 1


def of_transaction(
    txn: Transaction,
    pack: Pack,
    integrity: Integrity,
    port: Port,
    direction: str,
    stride: int,
    clock: Any,
    notes: dict[str, str],
) -> list[Beat]:
    out: list[Beat] = []
    events = _data_events(txn, port)
    if not events:
        return out
    env = TxnEnv(txn, pack, clock)
    node = expr.parse(integrity.beat_addr) if integrity.addressed else None
    for i, ev in enumerate(events):
        addr: int | None = None
        if node is not None:
            try:
                value = expr.evaluate(node, _BeatEnv(env, i, stride))
            except expr.ExprError as e:
                notes.setdefault("beats address", str(e))
                value = None
            addr = value if isinstance(value, int) else None
            if addr is None:
                notes.setdefault(
                    "beats address",
                    "some beats carried an address that could not be decoded",
                )
        strobe = ev.fields.get(port.strobe) if port.strobe else None
        # No strobe field, or a design that does not carry one: every lane was
        # written. That is what a protocol without byte enables *means*, and
        # defaulting to zero instead would make every write invisible.
        if not isinstance(strobe, int):
            if port.strobe and strobe is not None:
                notes.setdefault(
                    "beats strobe",
                    f"`{port.strobe}` carried X/Z on some beats; assumed every lane",
                )
            strobe = _mask(stride)
        out.append(
            Beat(
                iface=txn.iface,
                txn=txn.ref,
                dir=direction,
                time=ev.time,
                beat=i,
                addr=addr,
                data=ev.fields.get(port.data),
                strobe=strobe & _mask(stride),
                stride=stride,
            )
        )
    return out


def collect(
    store: Any, extraction: Extraction, clock: Any, notes: dict[str, str]
) -> list[Beat]:
    """Every data beat on one interface, in trace order.

    Empty when the pack declares no `[integrity]`: §8.19 cannot guess which of a
    pack's transactions is a write, and a confident wrong guess is worse than
    nothing. `notes` collects the reasons, which travel with the extraction so a
    session restored from cache still says why it tracked nothing (P1).
    """
    iface = extraction.interface
    integrity = iface.pack.integrity
    if integrity is None:
        return []

    out: list[Beat] = []
    for port, direction in ((integrity.write, "write"), (integrity.read, "read")):
        if port is None:
            continue
        stride = _stride(store, iface, port.data)
        if stride is None:
            notes.setdefault(
                "beats", f"`{port.data}` is not in this trace, so {direction}s were not tracked"
            )
            continue
        for txn in extraction.transactions:
            if txn.kind == port.kind:
                out += of_transaction(
                    txn, iface.pack, integrity, port, direction, stride, clock, notes
                )
    out.sort(key=lambda b: b.cycle_key)
    return out
