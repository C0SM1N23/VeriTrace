"""Annotations as text files — §13.8.

    notes/dma_deadlock.vtnotes

*"versionabile, review-abile, cautabile cu grep. Nu baza de date, nu cloud."*
That sentence is the whole design, and it rules out the obvious alternatives:
one note per line so `grep` finds it, an anchor written the way the rest of the
tool writes one, and no index anywhere.

    # why the DMA wedges after the third descriptor
    top.u_dma.state @ 12500 :: never leaves ARB — grant drops a cycle early
    rtl/dma.sv:42 :: this `if` should test `busy` too

P2 wants every claim localised, so a note carries either a `(signal, time)` or
a `(file, line)`. Neither is mandatory — a note about the run as a whole is
still worth keeping, and refusing it would only push it into a commit message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Note", "SUFFIX", "parse", "render", "read", "append", "discover"]

SUFFIX = ".vtnotes"

#: `<anchor> :: <text>`. Spaced, so a `::` inside SystemVerilog scope syntax
#: (`pkg::TYPE`) in the note text cannot be read as the separator.
SEP = " :: "

#: `top.u_dma.state @ 12500`, times in trace units as everything else prints them.
_AT = re.compile(r"^(?P<signal>\S+)\s*@\s*(?P<time>\d+)$")
#: `rtl/dma.sv:42`. Anchored on the last colon so a Windows `K:\...` survives.
_LOC = re.compile(r"^(?P<file>.+):(?P<line>\d+)$")


@dataclass(frozen=True, slots=True)
class Note:
    text: str
    signal: str = ""
    time: int | None = None
    file: str = ""
    line: int | None = None

    @property
    def anchor(self) -> str:
        if self.signal and self.time is not None:
            return f"{self.signal} @ {self.time}"
        if self.file and self.line is not None:
            return f"{self.file}:{self.line}"
        return ""

    def __str__(self) -> str:
        return f"{self.anchor}{SEP}{self.text}" if self.anchor else self.text


def parse(text: str) -> list[Note]:
    """Notes from a `.vtnotes` file. Anything unparseable is kept as free text.

    A file people edit by hand cannot have a syntax error that loses a note —
    the worst outcome of a typo is an annotation with no anchor.
    """
    out: list[Note] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        anchor, sep, body = line.partition(SEP)
        if not sep:
            out.append(Note(text=line))
            continue
        at = _AT.match(anchor)
        if at:
            out.append(Note(body, signal=at["signal"], time=int(at["time"])))
            continue
        loc = _LOC.match(anchor)
        if loc:
            out.append(Note(body, file=loc["file"], line=int(loc["line"])))
            continue
        out.append(Note(text=line))
    return out


def render(notes: list[Note], title: str = "") -> str:
    head = [f"# {title}"] if title else []
    return "\n".join(head + [str(n) for n in notes]) + "\n"


def read(path: Path) -> list[Note]:
    if not path.exists():
        return []
    return parse(path.read_text(encoding="utf-8", errors="replace"))


def append(path: Path, note: Note) -> None:
    """Add one note, creating the file and its directory if needed.

    Append rather than rewrite: two people annotating the same file get a merge
    conflict git can resolve, instead of one silently overwriting the other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + str(note) + "\n", encoding="utf-8")


def discover(root: Path) -> list[Path]:
    """Every `.vtnotes` under `root/notes/`, and any beside the config itself."""
    found = sorted((root / "notes").glob(f"*{SUFFIX}")) + sorted(root.glob(f"*{SUFFIX}"))
    return list(dict.fromkeys(found))
