"""Transactions that already exist, in the shape everything else reads — §8.34.

The whole value of this feature is that it produces **the same `Extraction`** the
signal-level engine produces. A monitor's transactions then get latency, stalls,
deadlock, integrity, functional coverage and transaction-level why-trace for
free, because none of those know or care where the transactions came from.

So there is no new model here. What is needed is a `Pack` and an `Interface` for
data that has neither — an ingested stream has no channels to detect and no rules
to check — and that is what `synthetic()` builds: a pack that declares nothing,
so every analysis that asks "does this pack say X" gets a truthful no rather than
a guess.
"""

from __future__ import annotations

from typing import Any

from veritrace.protocol.model import Extraction, Interface, Transaction
from veritrace.protocol.pack import Detect, Pack

#: What an ingested pack is called. Visible in the UI, so it says where the
#: transactions came from rather than pretending to be a protocol.
SOURCES = {"uvm": "UVM transaction database", "cocotb": "cocotb monitor log"}


def synthetic(source: str, name: str) -> Pack:
    """A pack for transactions nobody derived from signals.

    Deliberately empty. A pack's channels, rules and metrics are all statements
    about *wires*, and an ingested transaction has none — inventing a channel so
    the object looks complete would make `n_events` and the correlation rate lie
    about work that never happened (P1).
    """
    return Pack(
        name=f"{SOURCES.get(source, source)} ({name})",
        version="ingested",
        detect=Detect(),
        channels=(),
        transactions=(),
    )


def interface(source: str, name: str, scope: str = "") -> Interface:
    return Interface(
        name=name,
        scope=scope or name,
        prefix="",
        pack=synthetic(source, name),
        signals={},
    )


def extraction(source: str, name: str, txns: list[Transaction], scope: str = "") -> Extraction:
    """One ingested stream, as the rest of VeriTrace expects to receive it.

    `n_events`/`n_matched` are set equal on purpose: every transaction the
    monitor recorded *is* matched, because the monitor is the authority here.
    The correlation rate then reads 100%, which is the truth — the number
    measures how much of a *signal* scan landed in a transaction, and no signal
    scan ran.
    """
    out = Extraction(interface=interface(source, name, scope), transactions=txns)
    out.n_events = out.n_matched = len(txns)
    return out


def order(txns: list[Transaction]) -> list[Transaction]:
    """Sorted by start, and renumbered.

    `Transaction.index` is the ordinal within (interface, kind) in start order —
    that is what `txn.dma0.WRITE[7]` means, and a monitor that logged out of
    order would otherwise produce references that do not match the timeline.
    """
    txns.sort(key=lambda t: (t.start_time, t.kind))
    seen: dict[tuple[str, str], int] = {}
    for t in txns:
        key = (t.iface, t.kind)
        t.index = seen.get(key, 0)
        seen[key] = t.index + 1
    return txns


def to_dict(extractions: list[Extraction]) -> dict[str, Any]:
    return {
        "interfaces": [
            {
                "name": e.interface.name,
                "pack": e.interface.pack.name,
                "n": len(e.transactions),
                "open": len(e.open_transactions),
            }
            for e in extractions
        ],
        "n_transactions": sum(len(e.transactions) for e in extractions),
    }
