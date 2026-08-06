"""Performance and liveness — §8.17, §8.18, TAB 9.

*Why is it slow* and *why did it stop* answered with attributed numbers rather
than impressions. Both are folds over the transaction table of §8.14, so this
package adds no pass over the trace: extraction produces one per-cycle record
per interface, and everything here is arithmetic on it.
"""

from veritrace.perf.model import (
    Deadlock,
    Fairness,
    Histogram,
    InterfacePerf,
    Livelock,
    Liveness,
    PerfReport,
    Series,
    StallProfile,
    Starvation,
    WaitEdge,
)

__all__ = [
    "Deadlock",
    "Fairness",
    "Histogram",
    "InterfacePerf",
    "Livelock",
    "Liveness",
    "PerfReport",
    "Series",
    "StallProfile",
    "Starvation",
    "WaitEdge",
]
