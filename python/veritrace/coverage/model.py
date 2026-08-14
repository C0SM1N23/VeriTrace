"""What TAB 7 shows — §8.21 (functional) and §8.12 (code), in one shape.

The tab is *one* tab with two sections for a reason §11.4 states plainly: both
answer the same question, "what did I not test". So both reduce to the same
type here — a set of points, each either hit or not — and the frontend renders
one of them as a matrix and the other as a heatmap over source, rather than the
two systems that consolidation replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A bin key. Tuples because a cross has one component per axis and a plain
#: field has one — same type, no special case downstream.
Key = tuple[str, ...]


@dataclass(slots=True)
class Cell:
    key: Key
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"key": list(self.key), "hits": self.hits}


@dataclass(slots=True)
class CoverPoint:
    """One §8.21 coverage point: field bins, a cross, a sequence matrix, or a
    corner (a single cell that was either reached or not).

    `labels` is per axis and *ordered*, so an empty cell is a cell that exists
    and was never reached — which is the entire point. A dictionary of the
    values that did occur cannot express a hole.
    """

    name: str
    shape: str
    axes: tuple[str, ...] = ()
    labels: tuple[tuple[str, ...], ...] = ()
    cells: list[Cell] = field(default_factory=list)
    msg: str = ""
    #: Why this point could not be measured. A point that could not be evaluated
    #: is not a point at 0% (P1) — one is a gap in the test, the other a gap in
    #: the pack.
    skipped: str = ""

    @property
    def total(self) -> int:
        return len(self.cells)

    @property
    def covered(self) -> int:
        return sum(1 for c in self.cells if c.hits)

    @property
    def score(self) -> float | None:
        return None if not self.cells else self.covered / self.total

    def holes(self) -> list[Key]:
        return [c.key for c in self.cells if not c.hits]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": self.shape,
            "axes": list(self.axes),
            "labels": [list(a) for a in self.labels],
            "cells": [c.to_dict() for c in self.cells],
            "total": self.total,
            "covered": self.covered,
            "score": None if self.score is None else round(self.score, 4),
            "msg": self.msg,
            "skipped": self.skipped,
        }


@dataclass(slots=True)
class FunctionalCoverage:
    """§8.21 for one interface."""

    iface: str
    pack: str
    n_transactions: int = 0
    points: list[CoverPoint] = field(default_factory=list)
    #: True when the pack declared no `[[cover]]` and the bins below were
    #: derived from the fields the transactions happened to carry. Stated, not
    #: hidden: automatic bins are a useful default and a weaker claim.
    automatic: bool = False

    @property
    def score(self) -> float | None:
        live = [p for p in self.points if p.cells and not p.skipped]
        if not live:
            return None
        return sum(p.covered for p in live) / sum(p.total for p in live)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "pack": self.pack,
            "n_transactions": self.n_transactions,
            "automatic": self.automatic,
            "score": None if self.score is None else round(self.score, 4),
            "points": [p.to_dict() for p in self.points],
        }


@dataclass(slots=True)
class LineCoverage:
    """One imported code-coverage point (§8.12).

    `kind` keeps line, branch and toggle apart because closing them takes
    different work, and a single blended percentage is the number that lets a
    project claim 90% while never having taken an else branch.
    """

    file: str
    line: int
    count: int
    kind: str = "line"
    #: The tool's own label for the point, when it has one (`if`, `else`,
    #: `fwd_sel`) — this is what makes the derived condition point at the right
    #: branch rather than at the whole line.
    label: str = ""

    @property
    def covered(self) -> bool:
        return self.count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "count": self.count,
            "kind": self.kind,
            "label": self.label,
            "covered": self.covered,
        }


@dataclass(slots=True)
class FileCoverage:
    file: str
    points: list[LineCoverage] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return sum(1 for p in self.points if p.covered)

    @property
    def score(self) -> float | None:
        return None if not self.points else self.covered / len(self.points)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "total": len(self.points),
            "covered": self.covered,
            "score": None if self.score is None else round(self.score, 4),
            "points": [p.to_dict() for p in self.points],
        }


@dataclass(slots=True)
class CodeCoverage:
    """Everything imported from one coverage database."""

    #: `verilator` or `vivado` — recorded because the two count branches
    #: differently and comparing their percentages is meaningless.
    source: str = ""
    path: str = ""
    files: list[FileCoverage] = field(default_factory=list)
    error: str = ""

    @property
    def total(self) -> int:
        return sum(len(f.points) for f in self.files)

    @property
    def covered(self) -> int:
        return sum(f.covered for f in self.files)

    @property
    def score(self) -> float | None:
        return None if not self.total else self.covered / self.total

    def uncovered(self) -> list[LineCoverage]:
        return [p for f in self.files for p in f.points if not p.covered]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "total": self.total,
            "covered": self.covered,
            "score": None if self.score is None else round(self.score, 4),
            "files": [f.to_dict() for f in self.files],
            "error": self.error,
        }


@dataclass(slots=True)
class Condition:
    """One conjunct of §8.12's derivation: what has to hold, and whether it
    ever did."""

    text: str
    #: Cycles the conjunct held, out of those sampled. `None` when it could not
    #: be evaluated against this trace.
    held: int | None = None
    sampled: int = 0
    #: How the signals in it get the value asked for — §8.12's second level,
    #: read off the graph rather than suggested.
    produced_by: tuple[str, ...] = ()

    @property
    def ever(self) -> bool | None:
        return None if self.held is None else self.held > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "held": self.held,
            "sampled": self.sampled,
            "ever": self.ever,
            "produced_by": list(self.produced_by),
        }


@dataclass(slots=True)
class Hole:
    """An uncovered point, with what §8.12 says it takes to close it."""

    file: str
    line: int
    kind: str = "line"
    label: str = ""
    #: The source line itself, so the tab reads without a round trip.
    text: str = ""
    signal: str = ""
    conditions: list[Condition] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "label": self.label,
            "text": self.text,
            "signal": self.signal,
            "conditions": [c.to_dict() for c in self.conditions],
            "note": self.note,
        }


@dataclass(slots=True)
class CoverageReport:
    """The whole of TAB 7."""

    functional: list[FunctionalCoverage] = field(default_factory=list)
    code: CodeCoverage | None = None
    holes: list[Hole] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    #: Set only when a database was found and could not be read. `skipped["code"]`
    #: covers both that and plain absence, which is what the CLI wants to print;
    #: a reader needs to tell them apart, because "none configured" is a normal
    #: state and "the one you configured is unusable" is not.
    code_error: str | None = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "functional": [f.to_dict() for f in self.functional],
            "code": self.code.to_dict() if self.code else None,
            "holes": [h.to_dict() for h in self.holes],
            "skipped": dict(self.skipped),
            "code_error": self.code_error,
            "ms": round(self.elapsed_ms, 2),
        }
