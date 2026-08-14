"""What a timing correlation produces — §8.30."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Hop:
    """One step of a critical path, as Vivado reported it."""

    delay_ns: float
    cumulative_ns: float
    resource: str
    #: Filled in by the correlation: where in the RTL this net comes from.
    loc: str = ""


@dataclass(slots=True)
class Path:
    slack: float = 0.0
    met: bool = True
    source: str = ""
    destination: str = ""
    group: str = ""
    kind: str = ""
    delay_ns: float = 0.0
    hops: list[Hop] = field(default_factory=list)

    #: Correlation results (§8.30's third bullet), filled in by `correlate`.
    #: `None` means no trace was given; 0 means the endpoint never changed.
    toggles: int | None = None
    source_signal: str = ""
    dest_signal: str = ""
    #: Where the endpoints are declared. §8.30's second bullet: negative slack on
    #: a name like `state_reg[1]` means nothing until it is a line of RTL.
    source_loc: str = ""
    dest_loc: str = ""
    note: str = ""

    @property
    def violated(self) -> bool:
        return not self.met or self.slack < 0

    @property
    def dead(self) -> bool:
        """A critical path the simulation never exercised — §8.30's false priority."""
        return self.toggles == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slack": self.slack,
            "met": self.met,
            "violated": self.violated,
            "source": self.source,
            "destination": self.destination,
            "group": self.group,
            "kind": self.kind,
            "delay_ns": self.delay_ns,
            "toggles": self.toggles,
            "dead": self.dead,
            "source_signal": self.source_signal,
            "dest_signal": self.dest_signal,
            "source_loc": self.source_loc,
            "dest_loc": self.dest_loc,
            "note": self.note,
            "hops": [
                {"delay_ns": h.delay_ns, "cumulative_ns": h.cumulative_ns,
                 "resource": h.resource, "loc": h.loc}
                for h in self.hops
            ],
        }


@dataclass(slots=True)
class TimingReport:
    wns: float | None = None
    tns: float | None = None
    paths: list[Path] = field(default_factory=list)
    #: Set when a trace was correlated in, so a reader knows whether `dead` means
    #: "never toggled" or "not checked".
    correlated: bool = False

    @property
    def violated(self) -> list[Path]:
        return [p for p in self.paths if p.violated]

    @property
    def false_priorities(self) -> list[Path]:
        """Violated paths the simulation never exercised.

        §8.30's whole argument: this is information no timing tool has, because
        Vivado has never seen the simulation and the simulator has never seen the
        timing report.
        """
        return [p for p in self.violated if p.dead]

    def to_dict(self) -> dict[str, Any]:
        return {
            "wns": self.wns,
            "tns": self.tns,
            "correlated": self.correlated,
            "n_paths": len(self.paths),
            "n_violated": len(self.violated),
            "paths": [p.to_dict() for p in self.paths],
        }
