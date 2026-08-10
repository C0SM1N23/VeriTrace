"""TAB 7 — coverage, code and functional, unified (§11.4, §8.12, §8.21).

Two sections, one loop: *find the hole -> learn what closes it -> write the
test -> run -> back to Wave.* The code half is imported from Verilator or
Vivado; the functional half is computed here from the transactions the protocol
packs already extracted, so it needs no simulator feature and no covergroup.
"""

from veritrace.coverage.model import (
    CodeCoverage,
    Condition,
    CoverageReport,
    CoverPoint,
    FunctionalCoverage,
    Hole,
    LineCoverage,
)
from veritrace.coverage.report import build

__all__ = [
    "CodeCoverage",
    "Condition",
    "CoverPoint",
    "CoverageReport",
    "FunctionalCoverage",
    "Hole",
    "LineCoverage",
    "build",
]
