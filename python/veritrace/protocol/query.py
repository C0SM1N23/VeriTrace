"""Running the transaction queries of §10.1 against an extraction.

    txn(dma0)
    txn(dma0, type=WRITE, addr=0x4000:0x5000)
    txn(dma0) | slowest(10)
    txn(dma0) | open()

Filters are generic on purpose: `type` and `status` are the only names this
module knows, and everything else is matched against the transaction's own
fields and metrics. So a pack that adds `burst` gets `txn(iface, burst=1)` with
no change here, which is the same rule §8.14 applies to the extraction engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from veritrace.analysis.vtq import Call, Pipeline, QueryError
from veritrace.protocol.model import Transaction

#: Stages that refine a transaction list, as §10.1 writes them.
STAGES = ("open", "closed", "slowest", "fastest", "first", "limit", "violations")


@dataclass(slots=True)
class TxnResult:
    transactions: list[Transaction] = field(default_factory=list)
    #: Interfaces the query looked at, for an honest "0 of 40" rather than "0".
    scanned: int = 0
    ifaces: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.transactions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transactions": [t.to_dict() for t in self.transactions],
            "n": self.n,
            "scanned": self.scanned,
            "interfaces": list(self.ifaces),
        }


def _matches(txn: Transaction, key: str, want: Any) -> bool:
    if key in ("type", "kind"):
        return txn.kind.lower() == str(want).lower()
    if key == "status":
        return txn.status == str(want).lower()
    if key in ("id", "master"):
        got: Any = txn.id if key == "id" else txn.iface
    elif key in txn.fields:
        got = txn.fields[key]
    elif key in txn.metrics:
        got = txn.metrics[key]
    else:
        raise QueryError(f"no field or metric named `{key}` on these transactions")
    if got is None:
        return False
    if isinstance(want, tuple):  # an inclusive lo:hi range
        return isinstance(got, int) and want[0] <= got <= want[1]
    if isinstance(want, str) and isinstance(got, str):
        return got.lower() == want.lower()
    return got == want


def _duration(t: Transaction) -> int:
    """Sort key for slowest/fastest.

    An unfinished transaction sorts as the slowest there is, because that is
    what it is: it has been running since it started and has not stopped.
    """
    d = t.duration()
    return d if d is not None else 1 << 62


def _stage(txns: list[Transaction], call: Call) -> list[Transaction]:
    n = int(call.args[0]) if call.args else 10
    match call.name:
        case "open":
            return [t for t in txns if not t.closed]
        case "closed":
            return [t for t in txns if t.closed]
        case "violations":
            return [t for t in txns if t.violations]
        case "slowest":
            return sorted(txns, key=_duration, reverse=True)[:n]
        case "fastest":
            return sorted([t for t in txns if t.closed], key=_duration)[:n]
        case "first" | "limit":
            return txns[:n]
    raise QueryError(f"unknown stage `{call.name}()`; try {', '.join(STAGES)}")


def run(analysis: Any, pipeline: Pipeline) -> TxnResult:
    """Execute a parsed `txn(...)` pipeline."""
    src = pipeline.source
    if src.name != "txn":
        raise QueryError(f"`{src.name}()` is not a transaction query")

    name = str(src.args[0]) if src.args else None
    if name is None:
        chosen = list(analysis.extractions)
    else:
        ex = analysis.get(name)
        if ex is None:
            known = ", ".join(e.interface.name for e in analysis.extractions) or "none"
            raise QueryError(f"no interface named `{name}`; detected: {known}")
        chosen = [ex]

    txns = [t for e in chosen for t in e.transactions]
    out = TxnResult(scanned=len(txns), ifaces=[e.interface.name for e in chosen])

    for key, want in src.kwargs.items():
        txns = [t for t in txns if _matches(t, key, want)]
    for call in pipeline.stages:
        txns = _stage(txns, call)

    out.transactions = txns
    return out
