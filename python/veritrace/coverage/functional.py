"""§8.21 — functional coverage from transactions, with no covergroup written.

*Which values of `awlen`, `awsize`, `awburst` appeared? Automatic bins over the
domain the pack declares. Crosses. Consecutive-pair matrices. Corners.*

Everything here is a pass over the transaction table that §8.13 already
produced, so the cost of this feature on a session is milliseconds and its
requirement on the user is nothing — which is the difference between coverage
that exists and coverage that was going to be written next sprint.

The bins come from the pack when it declares them and from the *signal's own
width* when it does not: a 2-bit field has four possible values, all four are
cells, and the ones that never occurred are the holes. A design that only ever
issued INCR bursts should see FIXED and WRAP as empty boxes, not as values that
were never mentioned.
"""

from __future__ import annotations

from typing import Any

from veritrace.coverage.model import Cell, CoverPoint, FunctionalCoverage, Key
from veritrace.protocol import expr
from veritrace.protocol.assemble import TxnEnv
from veritrace.protocol.model import Extraction, Transaction
from veritrace.protocol.pack import Cover, Pack

#: A field wider than this gets bucketed rather than enumerated: 2^32 empty
#: cells is not a coverage report, it is a denial of service on the browser.
MAX_ENUMERATED_BITS = 5

#: Automatic buckets for a wide field. Powers of two, because that is how burst
#: lengths, sizes and addresses are actually distributed.
_WIDE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("0", 0, 0),
    ("1", 1, 1),
    ("2-3", 2, 3),
    ("4-15", 4, 15),
    ("16-255", 16, 255),
    ("256+", 256, 1 << 62),
)


class _CovEnv:
    """A transaction's scope, plus the two names a `[[cover]]` needs.

    `kind` is the transaction type — the axis §8.21's sequence matrix is about,
    and not a payload field, so it has to be provided. `stride` is the bus width
    in bytes, which is what lets a corner say `wstrb != (1 << stride) - 1`
    instead of hard-coding a number that is wrong on a wider bus.
    """

    def __init__(self, txn: Transaction, pack: Pack, clock: Any, stride: int) -> None:
        self.parent = TxnEnv(txn, pack, clock)
        self.values = {"kind": txn.kind, "stride": stride}

    def lookup(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        return self.parent.lookup(name)

    def call(self, fn: str, args: Any, ev: Any) -> Any:
        return self.parent.call(fn, args, ev)


def _stride(store: Any, iface: Any, pack: Pack) -> int:
    """Bus width in bytes, from whichever field the pack calls its data."""
    name = pack.perf.data or (pack.integrity.write.data if pack.integrity and pack.integrity.write else None)
    path = iface.signals.get(name) if name else None
    if not path or store is None:
        return 4
    h = store.find(path)
    if h is None:
        return 4
    return max(1, (store.signal(h).width + 7) // 8)


def _width_of(store: Any, iface: Any, field: str) -> int | None:
    path = iface.signals.get(field)
    if not path or store is None:
        return None
    h = store.find(path)
    return None if h is None else store.signal(h).width


def _auto_bins(
    store: Any, iface: Any, field: str, observed: set[Any]
) -> tuple[tuple[str, ...], dict[Any, str]]:
    """Bins for a field the pack said nothing about.

    Three cases, in order of how much is known: a string-valued axis (`kind`)
    enumerates what the pack declares; a narrow signal enumerates its whole
    domain, so unvisited values are visible as holes; a wide one is bucketed.
    """
    if any(isinstance(v, str) for v in observed):
        labels = tuple(sorted(str(v) for v in observed))
        return labels, {v: str(v) for v in observed}

    width = _width_of(store, iface, field)
    if width is not None and width <= MAX_ENUMERATED_BITS:
        labels = tuple(str(v) for v in range(1 << width))
        return labels, {v: str(v) for v in range(1 << width)}

    labels = tuple(b[0] for b in _WIDE_BUCKETS)
    mapping: dict[Any, str] = {}
    for v in observed:
        if isinstance(v, int):
            mapping[v] = next((n for n, lo, hi in _WIDE_BUCKETS if lo <= v <= hi), "256+")
    return labels, mapping


def _declared_bins(bins: dict[str, tuple[int, int]]) -> tuple[tuple[str, ...], Any]:
    labels = tuple(bins)

    def classify(v: Any) -> str | None:
        if not isinstance(v, int):
            return None
        return next((n for n, (lo, hi) in bins.items() if lo <= v <= hi), None)

    return labels, classify


def _axis(
    store: Any, iface: Any, field: str, values: list[Any], declared: dict[str, dict[str, tuple[int, int]]]
) -> tuple[tuple[str, ...], Any]:
    """Labels for one axis, and the function that puts a value in one of them."""
    if field in declared:
        return _declared_bins(declared[field])
    labels, mapping = _auto_bins(store, iface, field, {v for v in values if v is not None})
    return labels, lambda v: mapping.get(v)


def _values(
    envs: list[_CovEnv], field: str, point: CoverPoint
) -> list[Any] | None:
    """One field, read off every transaction. `None` when the pack named
    something no transaction has — a typo, and reported as one."""
    out: list[Any] = []
    for env in envs:
        try:
            out.append(env.lookup(field))
        except expr.ExprError as e:
            point.skipped = str(e)
            return None
    return out


def _cells(labels: tuple[tuple[str, ...], ...], hits: dict[Key, int]) -> list[Cell]:
    """Every cell of the (possibly multi-axis) grid, in order.

    Built from the label lists and not from what was observed, because a cell
    that is missing from the report is not a hole anyone can see.
    """
    grid: list[Key] = [()]
    for axis in labels:
        grid = [k + (lab,) for k in grid for lab in axis]
    return [Cell(key=k, hits=hits.get(k, 0)) for k in grid]


def _field_point(
    spec: Cover, store: Any, iface: Any, envs: list[_CovEnv], declared: Any
) -> CoverPoint:
    point = CoverPoint(name=spec.name, shape="field", axes=(spec.field or "",), msg=spec.msg)
    values = _values(envs, spec.field or "", point)
    if values is None:
        return point
    labels, classify = _axis(store, iface, spec.field or "", values, declared)
    hits: dict[Key, int] = {}
    for v in values:
        label = classify(v)
        if label is not None:
            hits[(label,)] = hits.get((label,), 0) + 1
    point.labels = (labels,)
    point.cells = _cells(point.labels, hits)
    return point


def _cross_point(
    spec: Cover, store: Any, iface: Any, envs: list[_CovEnv], declared: Any
) -> CoverPoint:
    point = CoverPoint(name=spec.name, shape="cross", axes=spec.cross, msg=spec.msg)
    columns: list[list[Any]] = []
    classifiers = []
    labels: list[tuple[str, ...]] = []
    for field in spec.cross:
        values = _values(envs, field, point)
        if values is None:
            return point
        axis, classify = _axis(store, iface, field, values, declared)
        columns.append(values)
        classifiers.append(classify)
        labels.append(axis)
    hits: dict[Key, int] = {}
    for row in zip(*columns):
        key = tuple(c(v) for c, v in zip(classifiers, row))
        if all(k is not None for k in key):
            hits[key] = hits.get(key, 0) + 1  # type: ignore[arg-type]
    point.labels = tuple(labels)
    point.cells = _cells(point.labels, hits)
    return point


def _sequence_point(
    spec: Cover, store: Any, iface: Any, envs: list[_CovEnv], declared: Any
) -> CoverPoint:
    """§8.21's consecutive-pair matrix. The axes are the same domain twice —
    `READ -> WRITE` is a cell, and an empty one means the test never turned the
    bus around in that direction."""
    field = spec.sequence or ""
    point = CoverPoint(name=spec.name, shape="sequence", axes=(f"{field} (from)", f"{field} (to)"), msg=spec.msg)
    values = _values(envs, field, point)
    if values is None:
        return point
    labels, classify = _axis(store, iface, field, values, declared)
    hits: dict[Key, int] = {}
    for a, b in zip(values, values[1:]):
        key = (classify(a), classify(b))
        if all(k is not None for k in key):
            hits[key] = hits.get(key, 0) + 1  # type: ignore[arg-type]
    point.labels = (labels, labels)
    point.cells = _cells(point.labels, hits)
    return point


def _corner_point(spec: Cover, envs: list[_CovEnv]) -> CoverPoint:
    """A corner is one cell: reached, or a hole."""
    point = CoverPoint(
        name=spec.name, shape="corner", axes=(spec.name,), labels=(("hit",),), msg=spec.msg
    )
    node = spec.node
    hits = 0
    for env in envs:
        try:
            value = expr.evaluate(node, env)
        except expr.ExprError as e:
            # A corner naming a field this protocol does not carry is a fact
            # about the pack, not a hole in the test.
            point.skipped = str(e)
            return point
        if value:
            hits += 1
    point.cells = [Cell(key=("hit",), hits=hits)]
    return point


def _automatic(store: Any, iface: Any, envs: list[_CovEnv], txns: list[Transaction]) -> list[Cover]:
    """What to cover when the pack declares nothing.

    Every field the transactions actually carry, plus the sequence matrix over
    the transaction kind. Weaker than a declared set — it can only bin what the
    design happened to have — but it means a pack written before §8.21 existed
    still produces a matrix, which is the difference between a feature that
    applies to three packs and one that applies to all of them.
    """
    fields: list[str] = []
    for txn in txns:
        for name in list(txn.fields) + [f for e in txn.events for f in e.fields]:
            if name not in fields:
                fields.append(name)
    return [Cover(field=f) for f in sorted(fields)] + [Cover(sequence="kind")]


def measure(store: Any, extraction: Extraction, clock: Any = None) -> FunctionalCoverage:
    """§8.21 for one interface."""
    iface = extraction.interface
    pack = iface.pack
    out = FunctionalCoverage(iface=iface.name, pack=pack.name, n_transactions=len(extraction.transactions))
    if not extraction.transactions:
        return out

    stride = _stride(store, iface, pack)
    envs = [_CovEnv(t, pack, clock, stride) for t in extraction.transactions]
    specs = list(pack.cover)
    if not specs:
        specs = _automatic(store, iface, envs, extraction.transactions)
        out.automatic = True

    # A `[[cover]] field` entry with declared bins is also the axis definition
    # for every cross and sequence that mentions it, so a pack states its domain
    # once. §8.21 calls these "the domain declared in the pack"; this is where
    # that declaration is read.
    declared = {c.field: c.bins for c in specs if c.field and c.bins}

    for spec in specs:
        match spec.shape:
            case "field":
                out.points.append(_field_point(spec, store, iface, envs, declared))
            case "cross":
                out.points.append(_cross_point(spec, store, iface, envs, declared))
            case "sequence":
                out.points.append(_sequence_point(spec, store, iface, envs, declared))
            case "corner":
                out.points.append(_corner_point(spec, envs))
    return out
