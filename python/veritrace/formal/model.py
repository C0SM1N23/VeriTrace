"""What a formal run can honestly say — §8.27.

§8.27 is explicit: *"Marcheaza rezultatul ca `PROVED (bounded, depth=20)`,
niciodata ca `PROVED` gol."* That is enforced here by the type rather than by
remembering. There is no bare `PROVED` member to reach for, `Verdict.HELD`
carries the depth it held to, and `__str__` cannot render one without the other.

The distinction matters because bounded model checking answers a different
question from the one people hear. "No counterexample within 20 cycles" is a
real result and a useful one; "proved" is a claim about every run of every
length, and BMC does not make it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Verdict(Enum):
    #: No counterexample exists within the depth searched. Not "proved".
    HELD = "held"
    #: A counterexample exists, and is on disk.
    FAILED = "failed"
    #: The solver gave up, or the property was never emitted.
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Property:
    """One rule, and what the solver made of it."""

    id: str
    text: str
    verdict: Verdict = Verdict.UNKNOWN
    #: How deep the search went. Every verdict carries it; a `HELD` without one
    #: would be exactly the unqualified claim §8.27 forbids.
    depth: int = 0
    #: For FAILED: the step the assertion first fails at, and the waveform.
    step: int | None = None
    trace: Path | None = None
    #: For UNKNOWN: why. For skipped rules this is `_usable`'s own wording, so
    #: the reason a property was not checked reads the same everywhere.
    reason: str = ""
    #: The design signal this rule is *about* — the subject of its consequent.
    #: Recorded here because it is the natural thing to ask why about when the
    #: counterexample opens, and only the pack knows which signal that is.
    subject: str = ""

    def __str__(self) -> str:
        if self.verdict is Verdict.HELD:
            return f"HELD (bounded, depth={self.depth})  {self.id}"
        if self.verdict is Verdict.FAILED:
            at = "" if self.step is None else f" at step {self.step}"
            return f"FAILED{at}  {self.id}"
        return f"UNKNOWN  {self.id} — {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "verdict": self.verdict.value,
            "depth": self.depth,
            "step": self.step,
            "trace": str(self.trace) if self.trace else None,
            "reason": self.reason,
            "label": str(self),
        }


@dataclass(slots=True)
class FormalReport:
    """Everything one `veritrace formal` produced."""

    iface: str = ""
    pack: str = ""
    mode: str = "bmc"
    depth: int = 20
    engine: str = ""
    properties: list[Property] = field(default_factory=list)
    #: The generated checker and .sby, kept so the run can be repeated by hand.
    work: Path | None = None
    log: str = ""
    elapsed_s: float = 0.0

    @property
    def held(self) -> list[Property]:
        return [p for p in self.properties if p.verdict is Verdict.HELD]

    @property
    def failed(self) -> list[Property]:
        return [p for p in self.properties if p.verdict is Verdict.FAILED]

    @property
    def counterexample(self) -> Path | None:
        """The first failing trace, which is the one §8.27 step 4 opens."""
        for p in self.failed:
            if p.trace is not None:
                return p.trace
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "pack": self.pack,
            "mode": self.mode,
            "depth": self.depth,
            "engine": self.engine,
            "properties": [p.to_dict() for p in self.properties],
            "held": len(self.held),
            "failed": len(self.failed),
            "work": str(self.work) if self.work else None,
            "elapsed_s": round(self.elapsed_s, 2),
        }


@dataclass(slots=True)
class Reachability:
    """§8.35's verdict on one coverage hole."""

    file: str
    line: int
    label: str
    condition: str
    #: `reachable`, `unreachable` or `unknown`.
    status: str = "unknown"
    depth: int = 0
    step: int | None = None
    trace: Path | None = None
    reason: str = ""

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}"
        if self.status == "reachable":
            return f"{where}  REACHABLE (formal), counterexample at depth {self.step}"
        if self.status == "unreachable":
            return f"{where}  UNREACHABLE — proved to depth {self.depth}, so: dead code"
        return f"{where}  UNKNOWN — {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "label": self.label,
            "condition": self.condition,
            "status": self.status,
            "depth": self.depth,
            "step": self.step,
            "trace": str(self.trace) if self.trace else None,
            "reason": self.reason,
            "text": str(self),
        }
