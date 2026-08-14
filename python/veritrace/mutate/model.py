"""What a mutation run produces — §8.28.

The report is deliberately about *survivors*, not about the score. The score is
one number for a README; a survivor is a line of the design with a mutation that
nothing observed, which is a piece of work someone can actually do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Mutation:
    """One edit: replace `[start, end)` of `file` with `now`.

    A byte span rather than a rewritten AST. The site comes from the syntax tree
    — which is what makes it syntactically valid, per §8.28 — but the edit is a
    replacement, so every other byte of the file is untouched and a mutant diffs
    against the original in exactly one place.

    `start` and `end` are **byte** offsets, because slang's are: a single accented
    character in a comment is one Python index and two bytes, and mixing the two
    silently moves every span after it in the file.
    """

    operator: str
    file: Path
    line: int
    start: int
    end: int
    was: str
    now: str
    #: The source line the span sits on, stripped — what a reader recognises.
    context: str = ""

    @property
    def id(self) -> str:
        return f"{self.file.name}:{self.line}:{self.start}:{self.operator}"

    def apply(self, data: bytes) -> bytes:
        return data[: self.start] + self.now.encode("utf-8") + data[self.end :]

    def __str__(self) -> str:
        return f"{self.file.name}:{self.line}  {self.was}  ->  {self.now}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "file": str(self.file),
            "line": self.line,
            "was": self.was,
            "now": self.now,
            "context": self.context,
        }


@dataclass(frozen=True, slots=True)
class Survivor:
    """A mutation the suite did not notice, and why that is interesting."""

    mutation: Mutation
    #: Empty when the run was clean; otherwise how the suite ended.
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.mutation.to_dict(), "note": self.note}


@dataclass(slots=True)
class MutationReport:
    killed: int = 0
    survivors: list[Survivor] = field(default_factory=list)
    #: Mutants whose build failed. Not scored either way: a mutant that does not
    #: compile tests the *compiler*, and counting it as killed would inflate the
    #: score with mutations no test could have been expected to catch (P1).
    invalid: list[Mutation] = field(default_factory=list)
    seed: int = 0
    sampled: int = 0
    total_sites: int = 0
    elapsed_s: float = 0.0
    #: The command that decides kill-or-survive, quoted back so the number can
    #: be read together with what produced it.
    command: str = ""

    @property
    def scored(self) -> int:
        return self.killed + len(self.survivors)

    @property
    def score(self) -> float | None:
        """Killed over scored. `None` when nothing ran, which is not 0%."""
        return None if not self.scored else self.killed / self.scored

    def by_file(self) -> dict[Path, list[Survivor]]:
        out: dict[Path, list[Survivor]] = {}
        for s in sorted(self.survivors, key=lambda s: (str(s.mutation.file), s.mutation.line)):
            out.setdefault(s.mutation.file, []).append(s)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": None if self.score is None else round(self.score, 4),
            "killed": self.killed,
            "scored": self.scored,
            "survivors": [s.to_dict() for s in self.survivors],
            "invalid": [m.to_dict() for m in self.invalid],
            "seed": self.seed,
            "sampled": self.sampled,
            "total_sites": self.total_sites,
            "command": self.command,
            "elapsed_s": round(self.elapsed_s, 2),
        }
