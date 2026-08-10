"""Step 6 of §8.14: the extracted transactions, written to Parquet.

*Persistence: the result written to `txn/<iface>.parquet`.*

§6.3 is explicit that this file is not merely a cache. It is the interoperability
promise: *Andrei wants a scatter plot of DMA latency? `pl.read_parquet(
"dump.vtx/txn/axi_m0.parquet")`. No API, no documentation, without asking you.*
So the columns are named the way a pack names them — `addr`, `len`, `latency` —
rather than being an internal encoding, and re-running the extraction rewrites
the same file rather than an adjacent private one.

It doubles as the cache. Extraction is linear in the trace but a big trace is
big, and re-opening a session should not redo it. The stamp beside the table
records everything that would change the answer: the dump, the pack, and the
engine. Any difference and the table is rebuilt rather than trusted, because a
stale transaction table is exactly the kind of quiet lie P1 forbids.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritrace._native import read_txn_table, write_txn_table
from veritrace.clocks import Clock
from veritrace.perf.model import CyclePerf
from veritrace.protocol.model import Beat, Extraction, Interface, Transaction, Violation

#: Bumped when the columns or the assembly semantics change, so an old table is
#: rebuilt instead of being read with today's assumptions.
SCHEMA_VERSION = 4

#: Columns every table has, whatever the protocol. Kept first and in this order
#: so a `SELECT *` is readable without knowing the pack.
FIXED_COLUMNS = (
    "index", "kind", "status", "start_time", "end_time", "start_cycle",
    "end_cycle", "duration", "id", "outstanding", "n_beats", "violations",
)


def txn_dir(trace_path: Path) -> Path:
    """`dump.vtx/txn/`, the location §6.3 specifies.

    A raw `.vcd` has no store directory to put it in, so the tables sit beside
    it under the same name — the CLI converts before analysing anyway, and this
    only matters for a caller that skipped that.
    """
    p = Path(trace_path)
    return (p if p.is_dir() else p.with_name(p.name + ".txn")) / "txn"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def table_path(trace_path: Path, iface: str) -> Path:
    return txn_dir(trace_path) / f"{_safe(iface)}.parquet"


def perf_path(trace_path: Path, iface: str) -> Path:
    """§8.17's per-cycle profile, beside the transaction table.

    Its own file rather than more columns on the transaction table: it has one
    row per clock edge, not one per transaction, and §6.3's promise is that a
    colleague can `read_parquet` the table and get transactions. A table where
    half the rows are cycles would break that for the sake of one fewer file.
    """
    return txn_dir(trace_path) / f"{_safe(iface)}.perf.parquet"


def beats_path(trace_path: Path, iface: str) -> Path:
    """§8.19's data beats, beside the transaction table.

    Its own file for the same reason the profile has one: a row per beat is not
    a row per transaction, and §6.3 promises that reading the table gives
    transactions. It is also the export a data-integrity question wants on its
    own — `read_parquet(".../mem.beats.parquet")` is every byte the bus moved.
    """
    return txn_dir(trace_path) / f"{_safe(iface)}.beats.parquet"


#: Columns of the per-cycle profile, in `CyclePerf` field order.
PERF_COLUMNS = ("edge", "label", "requesting", "transferring", "outstanding", "blocked_on")

#: Columns of the beat table, in `Beat` field order minus `iface` — which is the
#: file's own name and would be the same string on every row.
BEAT_COLUMNS = ("txn", "dir", "time", "beat", "addr", "data", "strobe", "stride")


def _column_name(raw: str, taken: set[str], prefix: str) -> str:
    """Keep a pack's own field names, prefixing only on a real collision."""
    name = _safe(raw)
    if name in taken:
        name = f"{prefix}{name}"
    while name in taken:
        name += "_"
    return name


def to_columns(
    extraction: Extraction, clock: Clock | None
) -> tuple[list[tuple[str, list[Any]]], dict[str, list[str]]]:
    """The transaction table as ordered `(name, values)` columns.

    Also returns which columns came from fields and which from metrics. The
    table itself keeps the pack's own names — `addr`, `latency` — because §6.3's
    argument is that a colleague can read it without documentation, and
    `metric_latency` would be documentation. The split is recorded beside it so
    a restored table is identical to a freshly extracted one rather than
    approximately it.
    """
    txns = extraction.transactions
    cycle = (lambda t: None if t is None or clock is None else clock.cycle_of(t))

    columns: list[tuple[str, list[Any]]] = [
        ("index", [t.index for t in txns]),
        ("kind", [t.kind for t in txns]),
        ("status", [t.status for t in txns]),
        ("start_time", [t.start_time for t in txns]),
        ("end_time", [t.end_time for t in txns]),
        ("start_cycle", [cycle(t.start_time) for t in txns]),
        ("end_cycle", [cycle(t.end_time) for t in txns]),
        ("duration", [t.duration() for t in txns]),
        ("id", [t.id for t in txns]),
        ("outstanding", [t.outstanding for t in txns]),
        ("n_beats", [len(t.events) for t in txns]),
        ("violations", [",".join(v.rule for v in t.violations) or None for t in txns]),
    ]
    taken = set(FIXED_COLUMNS)

    # Union of field and metric names across every transaction type, so a table
    # with reads and writes carries both `bresp` and `rresp` with nulls where
    # the type does not have one.
    origin: dict[str, list[str]] = {"fields": [], "metrics": []}
    for source, prefix in (("fields", "field_"), ("metrics", "metric_")):
        names: list[str] = []
        for t in txns:
            for k in getattr(t, source):
                if k not in names:
                    names.append(k)
        for raw in names:
            col = _column_name(raw, taken, prefix)
            taken.add(col)
            columns.append((col, [getattr(t, source).get(raw) for t in txns]))
            origin[source].append(col)
    return columns, origin


def _stamp(store: Any, iface: Interface) -> dict[str, Any]:
    """Everything whose change would invalidate the table."""
    pack = iface.pack
    body = ""
    if pack.path is not None:
        try:
            body = pack.path.read_text(encoding="utf-8")
        except OSError:
            body = ""
    return {
        "schema": SCHEMA_VERSION,
        "trace": getattr(store, "source_sha256", None),
        "n_events": getattr(store, "n_events", None),
        "pack": pack.slug,
        "pack_sha256": hashlib.sha256(body.encode()).hexdigest()[:16],
        "signals": sorted(iface.signals.values()),
    }


#: Interfaces are extracted in parallel and all of them stamp the same index, so
#: the read-modify-write below has to be one operation. In-process only, which is
#: the scope that matters: two servers on one dump would each rebuild rather than
#: corrupt, since every entry carries the stamp it was written with.
_INDEX_LOCK = threading.Lock()


@dataclass(slots=True)
class Cache:
    """The `txn/index.json` beside the tables."""

    path: Path

    @classmethod
    def for_trace(cls, trace_path: Path) -> "Cache":
        return cls(txn_dir(trace_path) / "index.json")

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def update(self, key: str, entry: dict[str, Any]) -> None:
        """Record one interface's stamp, without losing another thread's."""
        with _INDEX_LOCK:
            data = self.load()
            data.setdefault("interfaces", {})[key] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)


def is_fresh(trace_path: Path, store: Any, iface: Interface) -> bool:
    """True when the table on disk was built from exactly this input."""
    entry = (Cache.for_trace(trace_path).load().get("interfaces") or {}).get(iface.name)
    if not entry:
        return False
    return entry.get("stamp") == _stamp(store, iface) and table_path(trace_path, iface.name).is_file()


def write(
    trace_path: Path, store: Any, extraction: Extraction, clock: Clock | None
) -> Path:
    """Write one interface's table and stamp it. Returns the file written."""
    out = table_path(trace_path, extraction.interface.name)
    columns, origin = to_columns(extraction, clock)
    write_txn_table(str(out), columns)

    perf = extraction.perf
    if perf is not None and len(perf):
        write_txn_table(
            str(perf_path(trace_path, extraction.interface.name)),
            [
                ("edge", list(perf.edges)),
                ("label", list(perf.labels)),
                ("requesting", list(perf.requesting)),
                ("transferring", list(perf.transferring)),
                ("outstanding", list(perf.outstanding)),
                ("blocked_on", list(perf.blocked_on)),
            ],
        )

    if extraction.beats:
        write_txn_table(
            str(beats_path(trace_path, extraction.interface.name)),
            [(name, [getattr(b, name) for b in extraction.beats]) for name in BEAT_COLUMNS],
        )

    Cache.for_trace(trace_path).update(
        extraction.interface.name,
        {
            "stamp": _stamp(store, extraction.interface),
            "n_beats": len(extraction.beats),
            "file": out.name,
            "columns": origin,
            # The profile's own names, which the columnar file cannot carry:
            # without them the labels are integers with no meaning.
            "perf": (
                None
                if perf is None or not len(perf)
                else {
                    "buckets": list(perf.buckets),
                    "channels": list(perf.channels),
                    "notes": dict(perf.notes),
                }
            ),
            # Everything the table itself does not carry, so a reused table
            # still yields the same report rather than a silently emptier one.
            "n_events": extraction.n_events,
            "n_matched": extraction.n_matched,
            "sampled_cycles": extraction.sampled_cycles,
            "skipped": dict(extraction.skipped),
            "violations": [v.to_dict() for v in extraction.violations],
        },
    )
    return out


def restore(trace_path: Path, store: Any, iface: Interface) -> Extraction | None:
    """Rebuild an extraction from a table that is still valid, or `None`.

    The per-beat events are not restored — nothing outside the assembler reads
    them — so a caller that needs them asks for a re-extraction explicitly.
    """
    if not is_fresh(trace_path, store, iface):
        return None
    entry = (Cache.for_trace(trace_path).load().get("interfaces") or {})[iface.name]
    path = table_path(trace_path, iface.name)
    try:
        txns = load_transactions(path, iface.name, entry.get("columns"))
    except Exception:  # noqa: BLE001 - a damaged cache must not close the session
        return None
    ex = Extraction(
        interface=iface,
        transactions=txns,
        violations=[Violation(**v) for v in entry.get("violations") or []],
        sampled_cycles=int(entry.get("sampled_cycles") or 0),
        n_events=int(entry.get("n_events") or 0),
        n_matched=int(entry.get("n_matched") or 0),
        skipped=dict(entry.get("skipped") or {}),
        parquet=str(path),
        perf=_restore_perf(trace_path, iface, entry.get("perf")),
        beats=_restore_beats(trace_path, iface, int(entry.get("n_beats") or 0)),
    )
    by_ref = ex.by_ref()
    for v in ex.violations:
        txn = by_ref.get(v.txn or "")
        if txn is not None:
            txn.violations.append(v)
    return ex


def _restore_beats(trace_path: Path, iface: Interface, expected: int) -> list[Beat]:
    """Read back §8.19's beats, or nothing.

    A short or missing file costs the scoreboard, not the session — but it must
    not cost it *silently*, so the count recorded when the table was written is
    checked against what came back. A truncated beat table would otherwise look
    exactly like a bus that moved less data.
    """
    if not expected:
        return []
    path = beats_path(trace_path, iface.name)
    if not path.is_file():
        return []
    try:
        cols = read(path)
    except Exception:  # noqa: BLE001 - a damaged cache must not close the session
        return []
    n = len(cols.get("time") or [])
    if n != expected:
        return []
    return [
        Beat(iface=iface.name, **{name: cols[name][i] for name in BEAT_COLUMNS})
        for i in range(n)
    ]


def _restore_perf(trace_path: Path, iface: Interface, meta: Any) -> CyclePerf | None:
    """Read back §8.17's profile, or `None` if it was never written.

    A missing or damaged profile costs the Performance tab, not the session:
    the transaction table is still valid, and `measure()` says why the profile
    is absent rather than quietly reporting an interface that never stalled.
    """
    if not isinstance(meta, dict):
        return None
    path = perf_path(trace_path, iface.name)
    if not path.is_file():
        return None
    try:
        cols = read(path)
        return CyclePerf(
            buckets=list(meta.get("buckets") or []),
            channels=list(meta.get("channels") or []),
            edges=[int(v) for v in cols["edge"]],
            labels=bytes(int(v) for v in cols["label"]),
            requesting=bytes(int(v) for v in cols["requesting"]),
            transferring=bytes(int(v) for v in cols["transferring"]),
            outstanding=bytes(int(v) for v in cols["outstanding"]),
            blocked_on=bytes(int(v) for v in cols["blocked_on"]),
            notes=dict(meta.get("notes") or {}),
        )
    except Exception:  # noqa: BLE001 - a damaged profile must not close the session
        return None


def read(path: Path | str) -> dict[str, list[Any]]:
    """Read a table back as `{column: values}`.

    Exposed for `veritrace txn --from-cache` and for tests; the engine's own
    reuse path goes through `is_fresh` and skips reading entirely when it can.
    """
    return dict(read_txn_table(str(path)))


def rows(path: Path | str) -> list[dict[str, Any]]:
    """The same table, one dict per transaction."""
    cols = read(path)
    n = len(next(iter(cols.values()), []))
    return [{k: v[i] for k, v in cols.items()} for i in range(n)]


def load_transactions(
    path: Path | str, iface: str, columns: dict[str, list[str]] | None = None
) -> list[Transaction]:
    """Rebuild `Transaction` objects from a cached table.

    Beats are not stored per row, so the reconstructed transactions carry their
    metrics and fields but not their individual events. Callers that need the
    events re-extract; callers that need the table (the UI, the CLI, `txn()`)
    do not.

    `columns` says which of the non-fixed columns were metrics. Without it the
    two would be told apart by name, which is exactly the guess this argument
    exists to avoid — `latency` is a metric on one pack and could be a payload
    field on another.
    """
    metrics = set((columns or {}).get("metrics") or ())
    fixed = set(FIXED_COLUMNS)
    out: list[Transaction] = []
    for r in rows(path):
        txn = Transaction(
            iface=iface,
            kind=str(r.get("kind") or ""),
            index=int(r.get("index") or 0),
            start_time=int(r.get("start_time") or 0),
            end_time=r.get("end_time"),
            id=r.get("id"),
            outstanding=int(r.get("outstanding") or 0),
        )
        for k, v in r.items():
            # A table is columnar, so it carries the union of every transaction
            # type's fields with nulls in the gaps. Dropping them on the way back
            # makes a restored transaction identical to a freshly extracted one
            # rather than merely equivalent to it — and an absent field and a
            # null one already mean the same thing to the expression layer.
            if k in fixed or v is None:
                continue
            target = txn.metrics if k in metrics or k.startswith("metric_") else txn.fields
            target[k.removeprefix("metric_").removeprefix("field_")] = v
        out.append(txn)
    return out
