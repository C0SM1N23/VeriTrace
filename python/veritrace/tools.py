"""Running an external tool that may only exist inside WSL.

Yosys, SymbiYosys and Verilator have no first-class Windows build. The project
already solves that for Verilator by going through WSL (`run_verilator.ps1` in
the user's own flow), and §8.27/§8.29 need the same for Yosys and `sby`. This is
that one seam, so no analysis module ever grows its own copy.

The rule is: **native if it is on PATH, WSL otherwise, and nothing else.** A tool
found on PATH is run directly with the paths it was given. A tool found only in
WSL is run through `wsl.exe`, and every argument that looks like a Windows path
is rewritten to `/mnt/<drive>/...` first — which is why the working directory
must also be a real filesystem path, not a UNC share.

`wsl.exe` is invoked with `argv` rather than a shell string on purpose: Git Bash
rewrites bare `/mnt/...` arguments into Windows paths before the process ever
sees them, and a shell string is exactly what gives it the chance.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

__all__ = ["ToolError", "Tool", "find", "require", "run_capture"]

#: `K:\dir\file` or `K:/dir/file`. Bare relative paths are left alone — they are
#: resolved by the tool against the working directory, which is translated too.
_WINPATH = re.compile(r"^[A-Za-z]:[\\/]")


class ToolError(RuntimeError):
    """A tool is missing, or a tool run failed."""


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` *and its descendants*.

    `Popen.kill()` signals the process it started and nothing below it. A
    simulator is a tree — `iverilog` drives `ivlpp` and `ivl`, and a package
    manager may install a launcher that runs the real binary as a child — so
    killing the top of it leaves the work running.
    """
    if os.name == "nt":
        # No process groups to kill on Windows; `taskkill /T` walks the
        # parent-PID tree, which is the same set.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, timeout=30, check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - the OS refused a kill
        pass


def run_capture(
    argv: list[str],
    cwd: Path | str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(..., capture_output=True, timeout=...)` that cannot outlive
    its timeout.

    Every external program this project runs is a simulator or a synthesiser, and
    the stock call has three ways of hanging on one:

    * **The timeout kills one process.** The rest of the tree keeps running, and
      a testbench with no `$finish` keeps writing its waveform at tens of MB per
      second. A three-second timeout in the test suite took a CI runner's disk
      three hours later.
    * **Draining the pipes waits for every writer.** After the kill,
      `subprocess.run` calls `communicate()` again to collect what was printed;
      that returns when the pipe has no writers left, and a surviving grandchild
      is one. Temporary files have no such rule, so the output is collected from
      files instead.
    * **The child inherits stdin.** `vvp` opens an interactive prompt on `$stop`
      and reads from it; inheriting a console that never sends EOF is a hang with
      no timeout attached at all.

    `TimeoutExpired` is raised with whatever the tool printed, so callers report
    the same thing they always did.
    """
    # POSIX: a session of its own, so `killpg` reaches every descendant.
    spawn = {} if os.name == "nt" else {"start_new_session": True}
    # UTF-8 rather than the locale encoding, and replacing rather than
    # raising: a simulator's output is diagnostic text, and a stray byte in it
    # must not become the failure the caller reports.
    opened = dict(mode="w+", encoding="utf-8", errors="replace")
    with tempfile.TemporaryFile(**opened) as out, tempfile.TemporaryFile(**opened) as err:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            **spawn,
        )

        def collected() -> tuple[str, str]:
            out.seek(0)
            err.seek(0)
            return out.read(), err.read()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            said, complained = collected()
            raise subprocess.TimeoutExpired(
                argv, timeout or 0.0, output=said, stderr=complained
            ) from None
        said, complained = collected()
        return subprocess.CompletedProcess(argv, proc.returncode, said, complained)


@dataclass(frozen=True, slots=True)
class Tool:
    """One external program, and where it was found."""

    name: str
    #: Empty when the tool is native; otherwise the WSL distribution.
    distro: str = ""

    @property
    def via_wsl(self) -> bool:
        return bool(self.distro)

    @property
    def where(self) -> str:
        return f"WSL ({self.distro})" if self.via_wsl else "PATH"

    def path(self, p: Path | str) -> str:
        """`p` as this tool will see it."""
        return _to_wsl(p) if self.via_wsl else str(p)

    def run(
        self,
        args: list[str],
        cwd: Path | str | None = None,
        timeout: float = 600.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run the tool. `args` are translated the same way `path()` translates."""
        argv = [self.name, *(_to_wsl(a) if self.via_wsl and _WINPATH.match(a) else a for a in args)]
        if self.via_wsl:
            # `--cd` rather than `subprocess(cwd=)`: the working directory has to
            # be expressed in the distribution's namespace, not Windows'.
            argv = ["wsl.exe", "-d", self.distro, *(["--cd", _to_wsl(cwd)] if cwd else []), "--", *argv]
            cwd = None
        try:
            out = run_capture(argv, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"{self.name} did not finish within {timeout:.0f}s") from e
        if check and out.returncode != 0:
            raise ToolError(f"{self.name} failed ({out.returncode}):\n{(out.stderr or out.stdout).strip()[-2000:]}")
        return out


def _to_wsl(p: Path | str) -> str:
    """`K:\\VeriTrace\\x` -> `/mnt/k/VeriTrace/x`; anything else, unchanged."""
    s = str(p)
    if not _WINPATH.match(s):
        return s.replace("\\", "/")
    w = PureWindowsPath(s)
    return "/mnt/" + w.drive[0].lower() + "/" + "/".join(w.parts[1:])


@functools.cache
def _distros() -> tuple[str, ...]:
    """Installed WSL distributions, or nothing at all off Windows.

    Cached, and never fatal: `find()` is called once per tool — twice at
    import time by the test suite alone — and each call costs a process on a
    machine that may have no WSL at all. The answer cannot change within one
    run, and a launcher that exists without the feature behind it is a
    machine with no distributions rather than an error.
    """
    if os.name != "nt" or shutil.which("wsl.exe") is None:
        return ()
    try:
        out = run_capture(["wsl.exe", "-l", "-q"], timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return ()
    # `wsl -l -q` answers in UTF-16, which decodes to text separated by NULs.
    lines = (l.strip().replace("\x00", "") for l in out.stdout.splitlines())
    return tuple(d for d in lines if d)


def find(name: str) -> Tool | None:
    """`name` as a runnable tool, natively or through WSL, or `None`."""
    if shutil.which(name):
        return Tool(name)
    for distro in _distros():
        try:
            out = run_capture(
                ["wsl.exe", "-d", distro, "--", "command", "-v", name], timeout=60
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return Tool(name, distro)
    return None


def require(name: str, install: str) -> Tool:
    """`find`, but a missing tool raises with the command that installs it.

    §13.2: a tool that is not there is a setup problem, and the message says how
    to fix it rather than what failed.
    """
    tool = find(name)
    if tool is None:
        hint = (
            f"  In WSL:   wsl -d Ubuntu -u root -- {install}\n" if os.name == "nt"
            else f"  {install}\n"
        )
        raise ToolError(f"{name} is not installed, or not on PATH.\n{hint}")
    return tool
