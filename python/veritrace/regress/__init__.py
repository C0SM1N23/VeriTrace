"""The project's memory — §13.6.

One DuckDB file next to the project. Every CI run writes a row; "since when is
the DMA slower" becomes a `SELECT` instead of twenty manual re-runs.
"""

from veritrace.regress.db import (
    DEFAULT_DB,
    HISTORY_METRICS,
    Run,
    connect,
    get,
    git_commit,
    latest,
    performance_history,
    query,
    record,
    scorecard_snapshot,
    sha256_of,
)
from veritrace.regress.reproduce import Replay, Reproduction, ReproductionError, execute, plan

__all__ = [
    "DEFAULT_DB", "Run", "connect", "get", "git_commit", "latest", "query",
    "record", "sha256_of", "HISTORY_METRICS", "performance_history",
    "scorecard_snapshot",
    "Reproduction", "ReproductionError", "Replay", "plan", "execute",
]
