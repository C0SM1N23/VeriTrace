"""Shareable sessions — §13.8.

`veritrace share` writes a `.vtsession`: the layout, the bookmarks, the
annotations, the query you were asking, and a reference to the dump *by hash*.
You send the file; your colleague applies it and sees your screen.

Two rules shape the format, both from §13.8:

- **No server.** The bundle is a file, and applying it writes the layout sidecar
  and the notes files. Nothing registers, nothing syncs.
- **The dump is referenced, not carried.** A trace is gigabytes and the receiver
  already has it — what they lack is the certainty that it is the *same* one, so
  the hash travels and is checked on the way in.

It is also plain sorted JSON with no timestamp in it, so two shares of the same
screen are byte-identical (P1) and the file is worth committing next to the bug
it explains.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veritrace import notes as notes_mod

__all__ = ["VERSION", "SUFFIX", "build", "write", "read", "apply", "Applied"]

#: Bumped when the bundle layout changes incompatibly.
VERSION = 1

SUFFIX = ".vtsession"


def build(
    trace: Path,
    layout: dict[str, Any],
    root: Path,
    query: str = "",
    version: str = "",
) -> dict[str, Any]:
    """The bundle for one screen.

    `layout` is the sidecar as it stands — it already holds the signals, groups,
    radix, cursors, bookmarks and suppressions, so sharing is a copy rather than
    a second serialisation format that could disagree with the first.
    """
    from veritrace._native import TraceStore

    store = TraceStore(str(trace))
    provenance = layout.get("provenance") or {}
    bundle: dict[str, Any] = {
        "vtsession": VERSION,
        "veritrace": version,
        "trace": {
            # The name is a hint for finding the file; the hash is the identity.
            "name": trace.name,
            "sha256": store.source_sha256,
            "n_signals": store.n_signals,
            "timescale": store.timescale,
        },
        "rtl_sha256": provenance.get("rtl_sha256"),
        "layout": {k: v for k, v in layout.items() if k != "provenance"},
        # §13.8 lists the current query as part of the screen: a shared session
        # is usually a shared *question*, and the tree below it is derivable.
        "query": query or str(layout.get("query") or ""),
        "notes": [
            {"path": p.relative_to(root).as_posix(), "text": p.read_text(encoding="utf-8")}
            for p in notes_mod.discover(root)
        ],
    }
    return bundle


def write(path: Path, bundle: dict[str, Any]) -> Path:
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "layout" not in data:
        raise ValueError(f"{path} is not a VeriTrace session bundle")
    got = data.get("vtsession")
    if got != VERSION:
        raise ValueError(f"{path}: session format {got}, this build reads {VERSION}")
    return data


@dataclass(slots=True)
class Applied:
    """What landed, and what the receiver should know about it."""

    layout_path: Path
    notes: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    query: str = ""


def apply(bundle: dict[str, Any], trace: Path, root: Path, force: bool = False) -> Applied:
    """Install a colleague's screen over the local copy of the same dump.

    The hash mismatch is a warning and not a refusal on purpose: the layout is
    still the fastest way to see what they were looking at, and P7's rule is
    that a mismatch degrades the answer rather than withholding it. What it must
    not do is stay quiet — bookmarks at times from another run point at nothing.
    """
    from veritrace._native import TraceStore
    from veritrace.api.sessions import LayoutFile

    out = Applied(layout_path=Path())
    # A .vtsession is an exchanged, therefore untrusted, JSON document.  Note
    # paths are relative project paths; accepting ``../`` or an absolute path
    # would let restoring a screen overwrite arbitrary files.  Validate every
    # destination before writing the layout too, so a rejected bundle has no
    # partial side effects.
    project = root.resolve()
    note_entries: list[tuple[Path, str]] = []
    for entry in bundle.get("notes") or []:
        if not isinstance(entry, dict) or "path" not in entry or "text" not in entry:
            raise ValueError("session note entries need path and text")
        relative = Path(str(entry["path"]))
        if relative.is_absolute():
            raise ValueError(f"session note path must be relative: {relative}")
        dest = (project / relative).resolve()
        if dest != project and project not in dest.parents:
            raise ValueError(f"session note path escapes project root: {relative}")
        note_entries.append((dest, str(entry["text"])))

    want = (bundle.get("trace") or {}).get("sha256")
    got = TraceStore(str(trace)).source_sha256
    if want and got and want != got:
        out.warnings.append(
            f"this dump is not the one that was shared ({got[:12]} vs {want[:12]}) — "
            "signal names are likely to match, times are not"
        )

    layout_file = LayoutFile.for_trace(trace)
    # `save` keeps the local provenance record, which is right: it describes this
    # machine's trace and RTL, not the sender's.
    layout_file.save(dict(bundle["layout"]))
    out.layout_path = layout_file.path
    out.query = str(bundle.get("query") or "")

    for dest, text in note_entries:
        if dest.exists() and dest.read_text(encoding="utf-8") != text and not force:
            # Someone else's annotations must never overwrite yours silently;
            # git is the merge tool here, as it is for everything else in §13.8.
            out.warnings.append(f"{dest}: already exists and differs — kept, use --force to replace")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        out.notes.append(dest)
    return out
