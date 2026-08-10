"""What §8.19's analysis produces.

One type carries the weight, and it is not defined here: `protocol.model.Beat`,
produced during extraction. Everything in this package is a pass over a list of
them, which is why the scoreboard, the path comparison and the ordering check
share no code and can contradict each other nowhere — they are three readings of
the same table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from veritrace.protocol.model import Beat, FieldValue

#: A sampled payload: an integer, the raw four-state digits when the beat
#: carried X/Z, or `None` when the signal had no value yet.
Payload = FieldValue

__all__ = [
    "Beat",
    "IfaceStats",
    "IntegrityReport",
    "Mismatch",
    "OrderResult",
    "Payload",
    "TrackResult",
    "TrackStep",
    "format_lanes",
]


def format_lanes(lanes: Iterable[int]) -> str:
    """`0-1` / `0-1, 3` — byte lanes as a reader would write them."""
    ordered = sorted(set(lanes))
    if not ordered:
        return "none"
    runs: list[tuple[int, int]] = []
    for i in ordered:
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


@dataclass(slots=True)
class Mismatch:
    """One place the data was not what it should have been.

    `lanes` is the point of the whole feature. "The read did not match" sends
    someone back to the waveform; "bytes 0-1 were dropped between `s` and `m`"
    sends them to a line of RTL.
    """

    #: `scoreboard` (a read disagreed with what was written), `path` (the same
    #: write looked different on two interfaces) or `order` (a stream).
    kind: str
    #: Byte lane indices, relative to the beat's own base address.
    lanes: tuple[int, ...]
    addr: int | None
    expected: int | None
    observed: int | None
    #: Trace time of the *second* observation — the one that revealed it.
    time: int
    #: Where in the path: an interface, or `a -> b` for a path mismatch.
    where: str
    #: Transaction refs of the two observations, in that order.
    source: str = ""
    victim: str = ""
    detail: str = ""
    #: Full path of the wire this finding is about — the one that carried the
    #: wrong bytes. Resolved from the pack's own field names, so it is a real
    #: signal a `why()` can start from.
    signal: str | None = None
    #: VTQ that opens that signal in Causal at the instant it went wrong. §11.4
    #: requires a working `[why]` on every row, and a data mismatch has the most
    #: obvious question of any finding: where did this value come from.
    why: str | None = None

    @property
    def lane_text(self) -> str:
        return format_lanes(self.lanes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lanes": list(self.lanes),
            "lane_text": self.lane_text,
            "addr": self.addr,
            "expected": self.expected,
            "observed": self.observed,
            "time": self.time,
            "where": self.where,
            "source": self.source,
            "victim": self.victim,
            "detail": self.detail,
            "signal": self.signal,
            "why": self.why,
        }


@dataclass(slots=True)
class TrackStep:
    """One line of §8.19's chain — a beat, plus what it meant."""

    time: int
    iface: str
    txn: str
    dir: str
    addr: int | None
    data: Payload
    strobe: int
    stride: int
    note: str = ""
    #: True when this step is where the data stopped agreeing with the source.
    mismatch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "iface": self.iface,
            "txn": self.txn,
            "dir": self.dir,
            "addr": self.addr,
            "data": self.data,
            "strobe": self.strobe,
            "stride": self.stride,
            "note": self.note,
            "mismatch": self.mismatch,
        }


@dataclass(slots=True)
class TrackResult:
    """`track(addr=0x4000)` / `track(data=0xDEADBEEF)`."""

    query: str
    steps: list[TrackStep] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "steps": [s.to_dict() for s in self.steps],
            "mismatches": [m.to_dict() for m in self.mismatches],
            "note": self.note,
        }


@dataclass(slots=True)
class OrderResult:
    """§8.19's third check, on a stream with no address."""

    source: str
    sink: str
    n_in: int = 0
    n_out: int = 0
    #: Words that went in and never came out, as (index, value).
    lost: list[tuple[int, Payload]] = field(default_factory=list)
    #: Words that came out more than once.
    duplicated: list[tuple[int, Payload]] = field(default_factory=list)
    #: Pairs `(index in, index out)` that came out somewhere other than where
    #: the order says they should have.
    reordered: list[tuple[int, int]] = field(default_factory=list)
    #: Words that came out having never gone in. §8.19 names three bugs; this is
    #: the fourth thing the alignment can find, and dropping it into one of the
    #: other three would misname it (P1).
    spurious: list[tuple[int, Payload]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.lost or self.duplicated or self.reordered or self.spurious)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sink": self.sink,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "lost": [{"index": i, "value": v} for i, v in self.lost],
            "duplicated": [{"index": i, "value": v} for i, v in self.duplicated],
            "reordered": [{"a": a, "b": b} for a, b in self.reordered],
            "spurious": [{"index": i, "value": v} for i, v in self.spurious],
            "clean": self.clean,
        }


@dataclass(slots=True)
class IfaceStats:
    """What one interface contributed, so "found nothing" and "looked at
    nothing" are different lines on the report (P1, P7)."""

    iface: str
    pack: str
    writes: int = 0
    reads: int = 0
    #: Read bytes that had a written value to be compared against. A read of an
    #: address nobody wrote is not a mismatch and is not counted here either.
    compared: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "pack": self.pack,
            "writes": self.writes,
            "reads": self.reads,
            "compared": self.compared,
        }


@dataclass(slots=True)
class IntegrityReport:
    """Everything §8.19 found, for the session and for `--fail-on integrity`."""

    interfaces: list[IfaceStats] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    #: Address sequences that matched across two interfaces, so a reader can see
    #: which stretches of the path were actually compared rather than assume it.
    compared_paths: list[tuple[str, str, int]] = field(default_factory=list)
    beats: list[Beat] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def clean(self) -> bool:
        return not self.mismatches

    def to_dict(self, with_beats: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "interfaces": [i.to_dict() for i in self.interfaces],
            "mismatches": [m.to_dict() for m in self.mismatches],
            "compared_paths": [
                {"a": a, "b": b, "writes": n} for a, b, n in self.compared_paths
            ],
            "n_beats": len(self.beats),
            "skipped": dict(self.skipped),
            "ms": round(self.elapsed_ms, 2),
        }
        if with_beats:
            out["beats"] = [b.to_dict() for b in self.beats]
        return out
