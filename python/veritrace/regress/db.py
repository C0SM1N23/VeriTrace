"""The regression database — §13.6.

One `.duckdb` next to the project, no server. Each CI run writes a row and the
project gets a memory: *"since when is the DMA slower"* stops being an oral
history and becomes a `SELECT`.

**Three columns are not optional**, and §13.6 says why: without the random seed,
the simulator and its version, and a hash of the RTL, a nightly failure is not
reproducible and the row is an anecdote. They are `NOT NULL` here so a run that
cannot say them fails to record rather than recording something useless.

The shape is four flat tables rather than one wide one. A metric that arrives
later — a new pack, a new check — adds rows, never a migration, and the example
query in §13.6 (`SELECT commit, p99_latency FROM txn_metrics WHERE iface='dma0'`)
is the one people actually write.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DB = "regressions.duckdb"

# The columns the Performance history UI may request.  Keeping this whitelist
# beside the schema makes the endpoint data-driven without interpolating an
# arbitrary browser string into SQL.
HISTORY_METRICS: dict[str, tuple[str, str, bool]] = {
    "p50_latency": ("p50 latency", "cycles", True),
    "p95_latency": ("p95 latency", "cycles", True),
    "p99_latency": ("p99 latency", "cycles", True),
    "max_latency": ("maximum latency", "cycles", True),
    "throughput": ("throughput", "transactions/cycle", False),
    "max_outstanding": ("maximum outstanding", "transactions", True),
    "violations": ("violations", "count", True),
}

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS run_ids START 1;

CREATE TABLE IF NOT EXISTS runs (
    run_id            BIGINT PRIMARY KEY,
    ts                TIMESTAMP NOT NULL,
    tag               VARCHAR,
    commit_sha        VARCHAR,
    -- §13.6's three mandatory columns. A row without them is not a record of a
    -- run, it is a rumour about one.
    seed              BIGINT     NOT NULL,
    simulator         VARCHAR    NOT NULL,
    simulator_version VARCHAR    NOT NULL,
    rtl_sha256        VARCHAR    NOT NULL,
    -- Everything needed to run it again, verbatim.
    command           VARCHAR,
    work_dir          VARCHAR,
    trace_path        VARCHAR,
    top               VARCHAR,
    sim_seconds       DOUBLE,
    host              VARCHAR,
    veritrace_version VARCHAR
);

CREATE TABLE IF NOT EXISTS txn_metrics (
    run_id          BIGINT NOT NULL,
    iface           VARCHAR NOT NULL,
    pack            VARCHAR,
    n_transactions  BIGINT,
    p50_latency     DOUBLE,
    p95_latency     DOUBLE,
    p99_latency     DOUBLE,
    max_latency     DOUBLE,
    throughput      DOUBLE,
    max_outstanding BIGINT,
    violations      BIGINT
);

CREATE TABLE IF NOT EXISTS findings (
    run_id   BIGINT NOT NULL,
    grp      VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    n        BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage (
    run_id  BIGINT NOT NULL,
    kind    VARCHAR NOT NULL,   -- line | functional | mutation | formal
    scope   VARCHAR,            -- an interface, a file, or NULL for the whole design
    covered BIGINT,
    total   BIGINT,
    score   DOUBLE
);
"""


@dataclass(slots=True)
class Run:
    """One recorded simulation, with what it takes to run it again."""

    seed: int
    simulator: str
    simulator_version: str
    rtl_sha256: str
    tag: str = ""
    commit_sha: str = ""
    command: str = ""
    work_dir: str = ""
    trace_path: str = ""
    top: str = ""
    sim_seconds: float = 0.0
    run_id: int | None = None
    txn: list[dict[str, Any]] = field(default_factory=list)
    findings: list[tuple[str, str, int]] = field(default_factory=list)
    coverage: list[tuple[str, str | None, int, int, float | None]] = field(default_factory=list)


def connect(path: Path | str = DEFAULT_DB):
    """Open (and create) the database. DuckDB is a file, so this is the install."""
    import duckdb

    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def sha256_of(paths: list[Path]) -> str:
    """One hash over the RTL, order-independent.

    Sorted by path and hashed with the name included, so moving a file changes
    the hash — because it changes the design.
    """
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


def git_commit(root: Path | str = ".") -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def record(con, run: Run) -> int:
    """Insert `run` and its metrics, and return the id §13.6's `reproduce` takes."""
    missing = [
        n for n, v in (
            ("seed", run.seed), ("simulator", run.simulator),
            ("simulator_version", run.simulator_version), ("rtl_sha256", run.rtl_sha256),
        ) if v in (None, "")
    ]
    if missing:
        raise ValueError(
            f"cannot record a run without {', '.join(missing)} — §13.6 makes these "
            "mandatory because a run missing them is not reproducible"
        )

    from veritrace import __version__

    run_id = con.execute("SELECT nextval('run_ids')").fetchone()[0]
    con.execute(
        """INSERT INTO runs VALUES (?, now(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            run_id, run.tag, run.commit_sha, run.seed, run.simulator,
            run.simulator_version, run.rtl_sha256, run.command, run.work_dir,
            run.trace_path, run.top, run.sim_seconds, platform.node(), __version__,
        ],
    )
    for m in run.txn:
        con.execute(
            "INSERT INTO txn_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, m.get("iface"), m.get("pack"), m.get("n"), m.get("p50"),
             m.get("p95"), m.get("p99"), m.get("max"), m.get("throughput"),
             m.get("outstanding"), m.get("violations")],
        )
    for grp, severity, n in run.findings:
        con.execute("INSERT INTO findings VALUES (?, ?, ?, ?)", [run_id, grp, severity, n])
    for kind, scope, covered, total, score in run.coverage:
        con.execute(
            "INSERT INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, kind, scope, covered, total, score],
        )
    run.run_id = run_id
    return run_id


def get(con, run_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM runs WHERE run_id = ?", [run_id]).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in con.description]
    return dict(zip(cols, row))


def latest(con) -> dict[str, Any] | None:
    row = con.execute("SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    return get(con, row[0]) if row else None


def query(con, sql: str) -> tuple[list[str], list[tuple]]:
    """Whatever the user asked, with its column names.

    Deliberately unrestricted: §13.6's whole promise is *"aici e un `SELECT`"*,
    and a query language with a safe subset would be a different, smaller
    promise. The file is local and the user owns it.
    """
    cur = con.execute(sql)
    return [d[0] for d in cur.description], cur.fetchall()


def performance_history(
    path: Path | str,
    iface: str,
    metric: str = "p99_latency",
    limit: int = 40,
) -> dict[str, Any]:
    """A UI-ready trend for one performance metric (§11.4, §13.6).

    This is deliberately a reader of the same ``runs`` and ``txn_metrics``
    rows written by :func:`record`; there is no parallel JSON history that can
    drift from ``veritrace history``.  It opens the database read-only and does
    not create an empty one merely because somebody clicked the toggle.

    ``regression`` marks the single recorded step with the largest relative
    worsening from its predecessor.  Latency/violations/outstanding worsen
    upward, throughput worsens downward.  With fewer than two values there is
    no claimed regression.
    """
    db_path = Path(path)
    label, unit, lower_is_better = HISTORY_METRICS.get(metric, ("", "", True))
    if not label:
        raise ValueError(
            f"unknown performance metric {metric!r}; choose one of "
            + ", ".join(HISTORY_METRICS)
        )
    if not iface.strip():
        raise ValueError("an interface is required for performance history")
    if not db_path.is_file():
        return {
            "available": False,
            "reason": f"{db_path.name} does not exist yet — run `veritrace record` after a simulation.",
            "iface": iface,
            "metric": metric,
            "label": label,
            "unit": unit,
            "lower_is_better": lower_is_better,
            "points": [],
            "regression_run_id": None,
            "delta": None,
        }

    import duckdb

    con = None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        rows = con.execute(
            f"""SELECT r.run_id, r.ts, r.tag, r.commit_sha, m.{metric}
                FROM txn_metrics m JOIN runs r USING (run_id)
                WHERE m.iface = ? AND m.{metric} IS NOT NULL
                ORDER BY r.run_id DESC LIMIT ?""",
            [iface, max(2, min(int(limit), 500))],
        ).fetchall()
    except Exception as exc:
        return {
            "available": False,
            "reason": f"cannot read {db_path.name}: {exc}",
            "iface": iface,
            "metric": metric,
            "label": label,
            "unit": unit,
            "lower_is_better": lower_is_better,
            "points": [],
            "regression_run_id": None,
            "delta": None,
        }
    finally:
        if con is not None:
            con.close()

    # The graph reads left-to-right, oldest-to-newest.
    points = [
        {
            "run_id": int(run_id),
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "tag": tag or "",
            "commit": commit or "",
            "value": float(value),
        }
        for run_id, ts, tag, commit, value in reversed(rows)
    ]

    regression_run_id: int | None = None
    worst = 0.0
    for previous, current in zip(points, points[1:]):
        raw = current["value"] - previous["value"]
        bad = raw if lower_is_better else -raw
        # Relative change lets latency and throughput use the same ranking.
        score = bad / abs(previous["value"]) if previous["value"] else bad
        if score > worst:
            worst = score
            regression_run_id = current["run_id"]

    delta = None
    if len(points) >= 2:
        delta = points[-1]["value"] - points[-2]["value"]
    for point in points:
        point["regression"] = point["run_id"] == regression_run_id

    return {
        "available": True,
        "reason": "" if points else f"no recorded {metric} values for {iface}",
        "iface": iface,
        "metric": metric,
        "label": label,
        "unit": unit,
        "lower_is_better": lower_is_better,
        "points": points,
        "regression_run_id": regression_run_id,
        "delta": delta,
    }


def scorecard_snapshot(path: Path | str) -> dict[str, Any]:
    """Inputs for §8.36 from the latest recorded run, plus history identity.

    The scorecard's ``--history`` option used to exist only in the
    specification.  This reader deliberately uses the same normalised tables
    written by :func:`record`; it does not invent a parallel summary format.
    The caller turns these plain rows into the report model, while ``run_count``
    and ``run_id`` make it observable which point in project history was used.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(
            f"{db_path} does not exist yet — `veritrace record` writes it"
        )

    import duckdb

    con = None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        latest_row = con.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if latest_row is None:
            raise ValueError(f"{db_path} contains no recorded runs")
        run_columns = [description[0] for description in con.description]
        run = dict(zip(run_columns, latest_row))
        run_id = int(run["run_id"])
        run_count = int(con.execute("SELECT count(*) FROM runs").fetchone()[0])

        txn_columns = [
            "iface", "pack", "n_transactions", "p50_latency", "p95_latency",
            "p99_latency", "max_latency", "throughput", "max_outstanding",
            "violations",
        ]
        txn_rows = con.execute(
            """SELECT iface, pack, n_transactions, p50_latency, p95_latency,
                      p99_latency, max_latency, throughput, max_outstanding,
                      violations
               FROM txn_metrics WHERE run_id = ? ORDER BY iface""",
            [run_id],
        ).fetchall()
        findings = con.execute(
            """SELECT grp, severity, n FROM findings
               WHERE run_id = ? ORDER BY grp, severity""",
            [run_id],
        ).fetchall()
        coverage = con.execute(
            """SELECT kind, scope, covered, total, score FROM coverage
               WHERE run_id = ? ORDER BY kind, scope""",
            [run_id],
        ).fetchall()
    except duckdb.Error as exc:
        raise ValueError(f"cannot read recorded runs from {db_path}: {exc}") from exc
    finally:
        if con is not None:
            con.close()

    return {
        "path": str(db_path),
        "run": run,
        "run_id": run_id,
        "run_count": run_count,
        "transactions": [dict(zip(txn_columns, row)) for row in txn_rows],
        "findings": [
            {"group": group, "severity": severity, "n": int(n)}
            for group, severity, n in findings
        ],
        "coverage": [
            {
                "kind": kind,
                "scope": scope,
                "covered": int(covered or 0),
                "total": int(total or 0),
                "score": float(score) if score is not None else None,
            }
            for kind, scope, covered, total, score in coverage
        ],
    }
