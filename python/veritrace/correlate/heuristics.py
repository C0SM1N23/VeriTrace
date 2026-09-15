"""Name normalisation and indexing for correlation — the cases in §7.1.

Kept separate from the resolver so the tricky string rules can be tested on
their own; they are where the surprises live.
"""

from __future__ import annotations

import re
from collections import defaultdict

#: `g_lane[2]` and `g_lane__BRA__2__KET__` are the same scope. Verilator emits
#: the second form when it flattens; VCS and Icarus keep the first.
_VERILATOR_BRACKETS = ((r"__BRA__", "["), (r"__KET__", "]"))

_INDEX = re.compile(r"\[(\d+)\]")


def normalize(path: str) -> str:
    """Canonical form for comparing two hierarchical names.

    Handles the §7.1 cases: escaped identifiers (leading backslash and the
    trailing space they require), Verilator's bracket mangling, and `[]` versus
    `__` for generate indices.
    """
    p = path.strip()
    for pat, rep in _VERILATOR_BRACKETS:
        p = p.replace(pat, rep)
    parts = []
    for seg in p.split("."):
        seg = seg.strip()
        if seg.startswith("\\"):
            # Escaped identifier: the backslash and terminal space are syntax,
            # not part of the name.
            seg = seg[1:].strip()
        # `g_lane__2` -> `g_lane[2]`, so both spellings meet in the middle.
        seg = re.sub(r"__(\d+)$", r"[\1]", seg)
        parts.append(seg)
    return ".".join(parts).lower()


def suffixes(path: str) -> list[str]:
    """Every trailing sub-path, longest first: `a.b.c` -> a.b.c, b.c, c."""
    parts = path.split(".")
    return [".".join(parts[i:]) for i in range(len(parts))]


_ELEMENT = re.compile(r"^(.*)\[(-?\d+)\]$")


def build_index(
    paths: dict[str, int],
) -> tuple[dict[str, int], dict[str, list[int]], dict[str, dict[int, int]]]:
    """Index trace paths for exact/normalised, suffix and element lookup.

    Returns `(normalised -> handle, normalised suffix -> handles, array base ->
    {index: handle})`. A suffix with more than one handle is ambiguous and must
    not be used (§7.2 step d). The element map exists because a memory is dumped
    one word at a time — `mem[0]`, `mem[1]` — with no signal for the array
    itself (§5.6).
    """
    exact: dict[str, int] = {}
    by_suffix: dict[str, list[int]] = defaultdict(list)
    by_element: dict[str, dict[int, int]] = defaultdict(dict)
    for path, handle in paths.items():
        n = normalize(path)
        exact.setdefault(n, handle)
        for s in suffixes(n):
            by_suffix[s].append(handle)
        m = _ELEMENT.match(n)
        if m:
            by_element[m.group(1)][int(m.group(2))] = handle
    return exact, by_suffix, by_element


def strip_indices(path: str) -> str:
    """`g_lane[2].fifo.wptr` -> `g_lane.fifo.wptr`, for grouping siblings."""
    return _INDEX.sub("", path)
