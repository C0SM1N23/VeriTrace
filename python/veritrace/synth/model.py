"""What a synth-diff run produces — §8.29."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SynthDiff:
    """The two runs, and where they part.

    `divergences` is the §8.7 report proper; everything else is what it takes to
    read it — which netlist, from which synthesiser, on which ports.
    """

    synthesiser: str = ""
    netlist: Path | None = None
    rtl_trace: Path | None = None
    gate_trace: Path | None = None
    #: Top-level ports of the DUT. §8.29 step 4 compares these and nothing else:
    #: synthesis is free to rename, merge and delete everything inside, so an
    #: internal signal that differs is not a mismatch, it is a synthesiser doing
    #: its job.
    ports: tuple[str, ...] = ()
    report: Any = None
    #: Set when the flow could not run to the end — a missing tool, a design
    #: Yosys refuses. Stated rather than raised, so a scorecard can carry it.
    skipped: str = ""
    elapsed_s: float = 0.0

    @property
    def matched(self) -> bool:
        return bool(self.ports) and not self.skipped and self.report is not None and (
            self.report.compared == len(self.ports) and not self.report.divergences
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "synthesiser": self.synthesiser,
            "netlist": str(self.netlist) if self.netlist else None,
            "rtl_trace": str(self.rtl_trace) if self.rtl_trace else None,
            "gate_trace": str(self.gate_trace) if self.gate_trace else None,
            "ports": list(self.ports),
            "matched": self.matched,
            "skipped": self.skipped,
            "divergences": (
                [d.to_dict() for d in self.report.divergences] if self.report else []
            ),
            "elapsed_s": round(self.elapsed_s, 2),
        }


@dataclass(frozen=True, slots=True)
class Port:
    name: str
    direction: str
    width: int = 1

    @property
    def decl(self) -> str:
        vec = "" if self.width <= 1 else f"[{self.width - 1}:0] "
        return f"{self.direction} {vec}{self.name}"


@dataclass(frozen=True, slots=True)
class Instance:
    """The DUT as the testbench instantiates it.

    Synthesis elaborates parameters away, so the netlist has none — but §8.29
    insists the netlist is simulated with *the same testbench*, and that
    testbench overrides them. Both halves are therefore needed: the values, to
    synthesise at, and the declarations, to put back on the shim.
    """

    path: str
    module: str
    ports: tuple[Port, ...] = ()
    parameters: tuple[tuple[str, str], ...] = ()
    files: tuple[Path, ...] = field(default_factory=tuple)
