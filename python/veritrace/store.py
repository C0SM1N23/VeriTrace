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


def _source_of(store: Path) -> Path | None:
    """The dump a `.vtx` was converted from, if it is still beside it.

    `dump.vcd.vtx` names its source outright; `dump.vtx` is the older shape and
    the source is whichever raw dump shares its stem.
    """
    name = store.name
    if name.endswith(".vtx"):
        stem = name[:-4]
        candidates = [store.with_name(stem)] if Path(stem).suffix.lower() in RAW else []
        candidates += [store.with_name(stem + ext) for ext in RAW]
        for c in candidates:
            if c.is_file():
                return c
    return None


def _readable(store: Path) -> bool:
    """Whether this build can open the store that is already there.

    The index carries a format version. A store written by an older build is
    stale in the same way a store older than its dump is: the dump is still on
    disk, so the answer is to convert it again rather than to hand the user a
    version number and stop.
    """
    from veritrace import _native

    try:
        _native.TraceStore(str(store))
    except Exception:  # noqa: BLE001 - any unreadable store is reconverted
        return False
    return True


def _matches_source(store: Path, source: Path) -> bool:
    """Whether `store` was built from the bytes currently in `source`.

    An mtime comparison alone is not enough, and the hole is not theoretical: a
    dump restored by `git checkout`, copied with `cp -p`, or extracted from an
    archive keeps its old timestamp, so a store built later reads as fresh and
    every answer that follows is about the previous run — with total confidence,
    which is the failure §5.7 exists to prevent.

    The size is the O(1) half of the answer and comes from the directory entry.
    The hash is the whole answer but costs a full read of the dump, which §4.2's
    < 1 s reopen cannot pay at a gigabyte — so it is recorded at convert time
    and compared only when the caller asks for certainty.
    """
    from veritrace import _native

    try:
        recorded = _native.TraceStore(str(store)).source_bytes
    except Exception:  # noqa: BLE001 - an unreadable store is reconverted anyway
        return False
    # `None` is a store written before the size was recorded, or one converted
    # from something with no file behind it. Nothing to compare, so nothing to
    # contradict: fall back to the timestamp on its own.
    return recorded is None or recorded == source.stat().st_size


def ensure(path: Path, announce: Callable[[str], None] | None = None) -> Path:
    """`path` as a store, converting a raw dump if that is what it is.

    An existing store is reused unless the dump has changed under it — the case
    that matters is re-running the simulation, which must not silently serve
    yesterday's data.
    """
    path = Path(path)
    if path.is_dir() or path.suffix.lower() not in RAW:
        # A store handed over directly. If this build cannot read it — an index
        # written by an older version — rebuild it from the dump beside it. The
        # alternative is a stack trace about a version number, on a machine that
        # has everything it needs to fix the problem itself.
        if path.is_dir() and not _readable(path):
            src = _source_of(path)
            if src is None:
                raise ValueError(
                    f"{path} was written by a different version of VeriTrace and the "
                    "dump it came from is not beside it. Convert the dump again: "
                    "`veritrace convert <dump.vcd>`."
                )
            if announce:
                announce(f"{path} is an older store format; rebuilding from {src.name}")
            from veritrace import _native

            _native.convert(str(src), str(path))
        return path

    from veritrace import _native

    out = path.with_name(path.name + ".vtx")
    fresh = (
        out.is_dir()
        and (out / "index.bin").exists()
        and out.stat().st_mtime >= path.stat().st_mtime
        and _readable(out)
        and _matches_source(out, path)
    )
    if not fresh:
        if announce:
            announce(f"converting {path} -> {out}")
        _native.convert(str(path), str(out))
    return out
