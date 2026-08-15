"""The project's memory — §13.6.

One DuckDB file next to the project. Every CI run writes a row; "since when is
the DMA slower" becomes a `SELECT` instead of twenty manual re-runs.
"""

from veritrace.regress.db import DEFAULT_DB, Run, connect, get, git_commit, latest, query, record, sha256_of
from veritrace.regress.reproduce import Reproduction, plan

__all__ = [
    "DEFAULT_DB", "Run", "connect", "get", "git_commit", "latest", "query",
    "record", "sha256_of", "Reproduction", "plan",
]
