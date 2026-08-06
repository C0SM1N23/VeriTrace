"""One call that produces everything TAB 9 shows — §8.17 and §8.18 together.

The seam the CLI, the API and the tests all go through, so "what the Performance
tab shows" has exactly one definition and a headless run and a browser cannot
disagree about it.
"""

from __future__ import annotations

import time as _time
from typing import Any, Sequence

from veritrace.perf import deadlock, metrics
from veritrace.perf.model import PerfReport, WaitEdge


def build(
    store: Any,
    extractions: Sequence[Any],
    graph: Any = None,
    clock: Any = None,
    config: Any = None,
) -> tuple[PerfReport, list[WaitEdge]]:
    """Metrics per interface, fairness across them, and the liveness scan."""
    started = _time.perf_counter()
    out = PerfReport()
    min_cycles = int(getattr(config, "deadlock_cycles", 0) or deadlock.MIN_CYCLES)

    for ex in extractions:
        out.interfaces.append(metrics.measure(ex, store, clock))
    out.fairness = metrics.fairness(extractions)
    out.liveness, edges = deadlock.analyse(store, extractions, graph, clock, min_cycles)

    out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return out, edges
