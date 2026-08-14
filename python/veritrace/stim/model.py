"""What stimgen produces — §8.32.

An `Item` is one legal transaction with concrete field values. A `Plan` is the
sequence of them plus, for each coverage hole that was asked for, whether it was
targeted and how — because "I generated 1000 transactions" is not the claim
§8.32 makes. The claim is that the holes close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Item:
    """One transaction to drive: a kind, and a value per drivable field."""

    kind: str
    values: dict[str, int]
    #: The coverage point this item exists to hit, when it was generated for one.
    #: Random filler has none, and saying which is which is what makes a plan
    #: reviewable rather than a wall of numbers.
    targets: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "values": dict(self.values), "targets": self.targets}


@dataclass(frozen=True, slots=True)
class Target:
    """A coverage hole stimgen was asked to close, and what became of it."""

    point: str
    bin: str
    #: `targeted`, or a reason it could not be.
    status: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"point": self.point, "bin": self.bin, "status": self.status, "reason": self.reason}


@dataclass(slots=True)
class Plan:
    pack: str = ""
    iface: str = ""
    seed: int = 0
    items: list[Item] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    #: Field -> width in bits, as the generator understood them.
    widths: dict[str, int] = field(default_factory=dict)

    @property
    def targeted(self) -> list[Target]:
        return [t for t in self.targets if t.status == "targeted"]

    @property
    def unreachable(self) -> list[Target]:
        return [t for t in self.targets if t.status != "targeted"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack": self.pack,
            "iface": self.iface,
            "seed": self.seed,
            "n": len(self.items),
            "widths": dict(self.widths),
            "items": [i.to_dict() for i in self.items],
            "targets": [t.to_dict() for t in self.targets],
        }
