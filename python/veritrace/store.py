"""A `.vtx` store for whatever path the caller has — §6.3.

§13's examples pass raw dumps (`veritrace serve dump.fst`), so the conversion has
to be transparent. It lives here rather than in the CLI because the CLI is no
longer the only caller: §8.29 simulates twice and then diffs, and both of its
waveforms arrive as fresh VCDs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time

__all__ = ["ensure", "identity"]

RAW = (".vcd", ".fst")


def identity(path: Path) -> Path:
    """Stable user-state owner for a raw dump, its store or an immutable generation."""
    if path.suffix.lower() in RAW:
        return path.with_name(path.name + ".vtx")
    if path.name.endswith(".vtx"):
        base, dot, nonce = path.name[:-4].rpartition(".")
        if dot and nonce.isdigit() and Path(base).suffix.lower() in RAW:
            return path.with_name(base + ".vtx")
    return path


def _source_of(store: Path) -> Path | None:
    """The dump a `.vtx` was converted from, if it is still beside it.

    `dump.vcd.vtx` names its source outright; `dump.vtx` is the older shape and
    the source is whichever raw dump shares its stem.
    """
    name = store.name
    if name.endswith(".vtx"):
        stem = name[:-4]
        # Immutable rerun generation: ``dump.vcd.<nonce>.vtx``.
        base, dot, nonce = stem.rpartition(".")
        if dot and nonce.isdigit() and Path(base).suffix.lower() in RAW:
            candidate = store.with_name(base)
            if candidate.is_file():
                return candidate
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

    The native store records size, nanosecond mtime and a platform change token
    (inode/ctime on Unix, file-id/NTFS ChangeTime on Windows). The ordinary
    reopen is therefore O(1). If that identity changed, the native verifier
    hashes once: changed bytes rebuild, while a metadata-only touch refreshes
    the identity so later reopens are O(1) again.
    """
    from veritrace import _native

    try:
        opened = _native.TraceStore(str(store))
        return bool(opened.verify_source(str(source)))
    except Exception:  # noqa: BLE001 - an unreadable store is reconverted anyway
        return False


def _is_current(store: Path, source: Path) -> bool:
    """Whether `store` is still the answer for what is in `source` today.

    Store-directory timestamps are deliberately not consulted. They can change
    when transaction tables are added and can be newer than a restored dump.
    The recorded source identity plus conditional hash is the actual proof.
    """
    return _matches_source(store, source)


def ensure(path: Path, announce: Callable[[str], None] | None = None) -> Path:
    """`path` as a store, converting a raw dump if that is what it is.

    An existing store is reused unless the dump has changed under it — the case
    that matters is re-running the simulation, which must not silently serve
    yesterday's data.
    """
    path = Path(path)
    if path.is_dir() or path.suffix.lower() not in RAW:
        # A store handed over directly — by the caller, or by `trace.default`
        # in `.veritrace.toml` resolving to a `.vtx` that is already there.
        # Either way the dump it came from may still be beside it, and may have
        # moved on since: re-running the simulation is the whole reason this
        # check exists, and pointing at the store rather than at the dump must
        # not be a way around it.
        if path.is_dir():
            src = _source_of(path)
            why = None
            if not _readable(path):
                # An index written by an older version. The alternative to
                # rebuilding is a stack trace about a version number, on a
                # machine that has everything it needs to fix the problem.
                why = "is an older store format"
            elif src is not None and not _is_current(path, src):
                why = f"is older than {src.name}"
            if why is not None:
                if src is None:
                    raise ValueError(
                        f"{path} was written by a different version of VeriTrace and the "
                        "dump it came from is not beside it. Convert the dump again: "
                        "`veritrace convert <dump.vcd>`."
                    )
                if announce:
                    announce(f"{path} {why}; rebuilding from {src.name}")
                from veritrace import _native

                # The canonical cache deliberately remains immutable while a
                # live process may still have its index mmap'd.  A later CLI
                # invocation that is configured with that canonical path must
                # therefore find the newest generation, or it would create a
                # duplicate generation on every open after the first rerun.
                canonical = src.with_name(src.name + ".vtx")
                newest = _latest_generation(canonical)
                for candidate in (newest, canonical):
                    if (
                        candidate is not None
                        and candidate != path
                        and _readable(candidate)
                        and _is_current(candidate, src)
                    ):
                        return candidate

                # A live UI memory-maps the old store.  On Windows that makes
                # replacing its files fail with AccessDenied, so a simulator
                # rerun could not be opened until every viewer was closed.
                # Stores are immutable generations: readers keep the old one,
                # while this caller receives a newly converted sibling.
                path = _generation(canonical) if canonical.exists() else canonical
                _native.convert(str(src), str(path))
        return path

    from veritrace import _native

    canonical = path.with_name(path.name + ".vtx")
    # A previous rerun may already have produced an immutable generation.
    # Reuse the newest one that describes the current source.
    newest = _latest_generation(canonical)
    # Only the latest readable generation is probed.  Probing every historical
    # generation hashes a changed 1 GB dump once *per rerun*, defeating the
    # reopen budget.  If the latest is not current, the canonical store may
    # still be (for example after checking out an older dump), otherwise a new
    # immutable generation is the unambiguous answer.
    out = newest if newest is not None and _is_current(newest, path) else canonical
    fresh = (
        out.is_dir()
        and (out / "index.bin").exists()
        and _readable(out)
        and _is_current(out, path)
    )
    if not fresh:
        if canonical.is_dir():
            out = _generation(canonical)
        if announce:
            announce(f"converting {path} -> {out}")
        _native.convert(str(path), str(out))
    return out


def _generation(canonical: Path) -> Path:
    """A collision-free sibling for an immutable replacement store."""
    while True:
        base = canonical.name[:-4] if canonical.name.endswith(".vtx") else canonical.name
        out = canonical.with_name(f"{base}.{time.time_ns()}.vtx")
        if not out.exists():
            return out


def _latest_generation(canonical: Path) -> Path | None:
    """Newest readable immutable generation of ``canonical``, if any."""
    base = canonical.name[:-4] if canonical.name.endswith(".vtx") else canonical.name
    generated = sorted(
        (p for p in canonical.parent.glob(base + ".*.vtx") if p.is_dir()),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    return next((p for p in generated if _readable(p)), None)
