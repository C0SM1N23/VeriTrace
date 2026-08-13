"""Comparing two runs — §8.7.

`align` puts the two traces on one axis (timescale normalised first, then anchor
matching); `report` finds where they part company and asks both why.
"""

from veritrace.diff.align import (
    AlignError,
    Alignment,
    Side,
    align,
    fs_per_tick,
    side,
)
from veritrace.diff.report import (
    DiffReport,
    Divergence,
    compare,
    compare_transactions,
    explain,
)

__all__ = [
    "AlignError",
    "Alignment",
    "Side",
    "align",
    # Exported so nothing has to reach for `veritrace.diff.align` the *module*,
    # which the `align` function above shadows on this package.
    "fs_per_tick",
    "side",
    "DiffReport",
    "Divergence",
    "compare",
    "compare_transactions",
    "explain",
]
