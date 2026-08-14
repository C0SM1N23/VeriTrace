"""A `.vtx` store for whatever path the caller has — §6.3.

§13's examples pass raw dumps (`veritrace serve dump.fst`), so the conversion has
to be transparent. It lives here rather than in the CLI because the CLI is no
longer the only caller: §8.29 simulates twice and then diffs, and both of its
waveforms arrive as fresh VCDs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

__all__ = ["ensure"]

RAW = (".vcd", ".fst")


def ensure(path: Path, announce: Callable[[str], None] | None = None) -> Path:
    """`path` as a store, converting a raw dump if that is what it is.

    An existing store is reused unless the dump is newer — the case that matters
    is re-running the simulation, which must not silently serve yesterday's data.
    """
    path = Path(path)
    if path.is_dir() or path.suffix.lower() not in RAW:
        return path

    from veritrace import _native

    out = path.with_name(path.name + ".vtx")
    fresh = (
        out.is_dir()
        and (out / "index.bin").exists()
        and out.stat().st_mtime >= path.stat().st_mtime
    )
    if not fresh:
        if announce:
            announce(f"converting {path} -> {out}")
        _native.convert(str(path), str(out))
    return out
