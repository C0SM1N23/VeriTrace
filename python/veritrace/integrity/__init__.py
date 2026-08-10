"""§8.19 — data integrity: the automatic scoreboard and `track()`.

*I wrote 0xDEADBEEF to 0x4000 through the DMA. What arrived in memory?*

Three checks, all of them derived from transactions that were already extracted
for §8.13, so none of them costs a second pass over the trace:

* **scoreboard** — a byte-granular `addr -> value` model, updated from every
  observed write on an interface and compared against every read on it. The
  free testbench of §8.19, with no line of SystemVerilog written.
* **path** — the same write, seen on two interfaces. §8.19 is explicit that the
  thing to follow is the *(address, sequence)* pair rather than the value, and
  that is what makes "which byte, and where in the path" answerable.
* **order** — for streams with no address, the third of §8.19: the n-th word in
  must be the n-th word out, so reordering, duplication and loss are visible.
"""

from veritrace.integrity.model import (
    Beat,
    IntegrityReport,
    Mismatch,
    OrderResult,
    TrackResult,
    TrackStep,
)
from veritrace.integrity.report import build

__all__ = [
    "Beat",
    "IntegrityReport",
    "Mismatch",
    "OrderResult",
    "TrackResult",
    "TrackStep",
    "build",
]
