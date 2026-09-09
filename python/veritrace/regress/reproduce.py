"""Running a recorded run again — §13.6.

The command and the seed come straight out of the row; the only judgement here
is what to say when the RTL has moved on. §13.6 is explicit that the tool must
not pretend:

    "RTL-ul s-a schimbat de la rularea 4821 (commit abc123 -> def456).
     Reproducerea nu e garantata identica."

Same principle as §5.7's provenance banner. A reproduction that quietly runs
against different sources is worse than no reproduction, because the result
looks like evidence.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritrace.regress import db


@dataclass(slots=True)
class Reproduction:
    run_id: int
    command: str
    seed: int
    simulator: str
    simulator_version: str
    work_dir: str
    #: The hash recorded then, and the hash of the RTL as it is now.
    rtl_then: str = ""
    rtl_now: str = ""
    commit_then: str = ""
    commit_now: str = ""
    #: Empty when the sources are identical; otherwise what changed and what
    #: that means for the result.
    caveat: str = ""

    @property
    def identical(self) -> bool:
        return bool(self.rtl_now) and self.rtl_now == self.rtl_then

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "seed": self.seed,
            "simulator": f"{self.simulator} {self.simulator_version}".strip(),
            "work_dir": self.work_dir,
            "rtl_then": self.rtl_then,
            "rtl_now": self.rtl_now,
            "identical": self.identical,
            "caveat": self.caveat,
        }


class ReproductionError(RuntimeError):
    """A recorded run lacks executable provenance, or could not be rerun."""


@dataclass(slots=True)
class Replay:
    """The observable result of executing a recorded command."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def plan(con, run_id: int, rtl: list[Path] | None = None, root: Path | str = ".") -> Reproduction:
    """The recorded command, and an honest statement about the sources."""
    row = db.get(con, run_id)
    if row is None:
        raise KeyError(f"no run {run_id} in this database")

    out = Reproduction(
        run_id=run_id,
        command=row.get("command") or "",
        seed=int(row.get("seed") or 0),
        simulator=row.get("simulator") or "",
        simulator_version=row.get("simulator_version") or "",
        work_dir=row.get("work_dir") or "",
        rtl_then=row.get("rtl_sha256") or "",
        commit_then=row.get("commit_sha") or "",
        commit_now=db.git_commit(root),
    )
    if rtl:
        out.rtl_now = db.sha256_of(list(rtl))

    if not out.rtl_now:
        out.caveat = (
            "the current RTL was not hashed (pass --rtl), so nothing can be said "
            "about whether this reproduces the original run"
        )
    elif not out.identical:
        moved = ""
        if out.commit_then and out.commit_now and out.commit_then != out.commit_now:
            moved = f" ({out.commit_then} -> {out.commit_now})"
        out.caveat = (
            f"the RTL has changed since run {run_id}{moved}. "
            "The reproduction is not guaranteed to be identical."
        )
    return out


def execute(reproduction: Reproduction, timeout: float = 3600.0) -> Replay:
    """Execute the command in the directory and with the seed it recorded.

    The database is local and the command was supplied by its owner, so this is
    intentionally a real shell command rather than an argv guessed from a
    string.  Guessing would break Make recipes, pipelines and simulator plusargs.
    """
    if not reproduction.command.strip():
        raise ReproductionError(
            "run has no recorded command; record it again with --command (or from "
            "a waveform produced by `veritrace run`)"
        )
    if not reproduction.work_dir:
        raise ReproductionError(
            "run has no recorded working directory; it cannot be reproduced exactly"
        )
    cwd = Path(reproduction.work_dir)
    if not cwd.is_dir():
        raise ReproductionError(
            f"the recorded working directory no longer exists: {cwd}"
        )

    command = reproduction.command
    if os.name == "nt" and "\n" in command:
        # Older VeriTrace rows stored compile and run on separate lines.  cmd's
        # `/c` executes only the first line, silently.  Preserve their intended
        # fail-fast sequence when replaying those rows.
        command = " && ".join(line.strip() for line in command.splitlines() if line.strip())
    env = os.environ.copy()
    # A Make/cocotb flow commonly reads SEED, while VERITRACE_SEED is
    # unambiguous for custom wrappers.  Explicit command-line plusargs remain
    # untouched and therefore still win according to the simulator's rules.
    env["SEED"] = str(reproduction.seed)
    env["VERITRACE_SEED"] = str(reproduction.seed)
    try:
        out = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReproductionError(
            f"recorded command did not finish within {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise ReproductionError(f"could not execute the recorded command: {exc}") from exc
    return Replay(out.returncode, out.stdout or "", out.stderr or "")
