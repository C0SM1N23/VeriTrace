"""Signal search and hierarchy navigation.

Matching is a deterministic subsequence score, not a similarity heuristic (P1):
the same query always returns the same ordering, and a signal either matches or
it does not.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Cap on returned rows, so a one-character query on a 50k-signal design does
#: not serialise the whole design table.
DEFAULT_LIMIT = 200


def score(query: str, path: str) -> int | None:
    """Rank `path` against `query`, or `None` when it does not match.

    Lower is better. Characters of the query must appear in order; runs of
    adjacent matches and matches right after a `.` or `_` boundary score better,
    which is what makes `cdt` find `cpu.dec.tag` ahead of incidental hits.
    """
    if not query:
        return 0
    q = query.lower()
    p = path.lower()

    # A plain substring is always the best kind of match.
    idx = p.find(q)
    if idx >= 0:
        # Prefer matches late in the path (the leaf name) and short paths.
        tail_bonus = 0 if idx >= p.rfind(".") else 40
        return tail_bonus + len(path) - len(query) + idx // 8

    penalty = 0
    pos = 0
    prev_end = -2
    for ch in q:
        found = p.find(ch, pos)
        if found < 0:
            return None
        if found != prev_end + 1:
            # Gap between matched characters; cheaper if it lands on a boundary.
            on_boundary = found > 0 and p[found - 1] in "._[/"
            penalty += 2 if on_boundary else 10
        prev_end = found
        pos = found + 1
    return 200 + penalty + len(path)


def search_signals(
    signals: Iterable[Any], query: str, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """Signals matching `query`, best first."""
    scored: list[tuple[int, str, Any]] = []
    for s in signals:
        sc = score(query, s.path)
        if sc is not None:
            scored.append((sc, s.path, s))
    # Path is the tiebreaker, so equal scores still order deterministically.
    scored.sort(key=lambda row: (row[0], row[1]))
    return [signal_json(s) for _, _, s in scored[:limit]]


def signal_json(s: Any) -> dict[str, Any]:
    return {
        "handle": s.handle,
        "path": s.path,
        "name": s.name,
        "scope": s.scope,
        "width": s.width,
        "kind": s.kind,
        "stream_id": s.stream_id,
        "msb": s.msb,
        "lsb": s.lsb,
        "array_index": s.array_index,
        "n_events": s.n_events,
    }


def hierarchy_level(store: Any, path: str | None) -> dict[str, Any]:
    """Children of one scope: sub-scopes and the signals declared in it.

    Lazy per level as §10.1 asks — a 50k-signal design must not serialise its
    whole tree to show the top of it.

    `n_signals` counts the whole subtree, not just this level: it is what the
    tree shows beside a collapsed scope, and "add everything under here" needs
    to say how much that is before it does it.
    """
    prefix = path or ""
    scopes = store.scopes()
    by_id = {s.scope_id: s for s in scopes}

    def parent_path(s: Any) -> str:
        return by_id[s.parent].path if s.parent is not None and s.parent in by_id else ""

    # Counted once into a dict rather than rescanned per child. The old form was
    # quadratic in the number of scopes, which is invisible on a testbench and
    # is not on the 50k-signal design this endpoint exists for.
    n_children: dict[str, int] = {}
    for s in scopes:
        n_children[parent_path(s)] = n_children.get(parent_path(s), 0) + 1

    all_signals = store.signals()
    own: list[Any] = []
    subtree: dict[str, int] = {}
    for sig in all_signals:
        if sig.scope == prefix:
            own.append(sig)
        # Every ancestor scope of this signal gets one, so a collapsed node can
        # say what it is hiding.
        scope = sig.scope
        while scope:
            subtree[scope] = subtree.get(scope, 0) + 1
            scope = scope.rpartition(".")[0]

    children = [
        {
            "name": s.name,
            "kind": s.kind,
            "path": s.path,
            "n_children": n_children.get(s.path, 0),
            "n_signals": subtree.get(s.path, 0),
        }
        for s in scopes
        if parent_path(s) == prefix
    ]
    children.sort(key=lambda c: c["name"])

    signals = sorted((signal_json(s) for s in own), key=lambda s: s["name"])
    return {"path": prefix, "scopes": children, "signals": signals}


def subtree_signals(store: Any, path: str | None, limit: int = 5000) -> list[dict[str, Any]]:
    """Every signal at or below `path`, in hierarchy order.

    This is `add wave -r /scope/*`: the single action that separates a usable
    viewer from one where a design is assembled a signal at a time. Doing it
    client-side would mean either holding all 50k signals in the browser or
    walking the tree one HTTP request per scope, and §10.1 made the tree lazy
    precisely to avoid the first.

    Hierarchy order, not alphabetical: the wave should read like the design.
    """
    prefix = (path or "").strip()
    rows = [
        s
        for s in store.signals()
        if not prefix or s.scope == prefix or s.scope.startswith(prefix + ".")
    ]
    rows.sort(key=lambda s: (s.scope, s.name))
    return [signal_json(s) for s in rows[:limit]]
