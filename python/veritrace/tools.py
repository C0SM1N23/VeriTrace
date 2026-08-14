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

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

__all__ = ["ToolError", "Tool", "find", "require"]

#: `K:\dir\file` or `K:/dir/file`. Bare relative paths are left alone — they are
#: resolved by the tool against the working directory, which is translated too.
_WINPATH = re.compile(r"^[A-Za-z]:[\\/]")


class ToolError(RuntimeError):
    """A tool is missing, or a tool run failed."""


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
            out = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                errors="replace",
            )
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


def _distros() -> list[str]:
    """Installed WSL distributions, or nothing at all off Windows."""
    if os.name != "nt" or shutil.which("wsl.exe") is None:
        return []
    out = subprocess.run(
        ["wsl.exe", "-l", "-q"], capture_output=True, text=True, timeout=30, errors="replace"
    )
    # `wsl -l -q` answers in UTF-16, which `text=True` decodes as NULs.
    return [d for d in (l.strip().replace("\x00", "") for l in out.stdout.splitlines()) if d]


def find(name: str) -> Tool | None:
    """`name` as a runnable tool, natively or through WSL, or `None`."""
    if shutil.which(name):
        return Tool(name)
    for distro in _distros():
        out = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "command", "-v", name],
            capture_output=True, text=True, timeout=60, errors="replace",
        )
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
