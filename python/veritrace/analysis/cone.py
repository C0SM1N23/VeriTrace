"""Cone and fan-out — §8.6.

*"From 4000 signals you are left with 8."* Backward BFS answers "what could
have produced this"; forward BFS answers "if I change this, what breaks".

The third mode is the one that earns its keep: **cone intersected with
activity**. A fan-in cone still contains every reset, every clock and every
strap that has not moved since time zero. Dropping the signals that did not
toggle in the visible window removes them, and §8.6 puts that at another 60%
of the noise.

Depth is measured in graph edges, not in logic levels: one BFS step is one
signal, so `--depth 3` means "three names away", which is what someone reading
the result expects.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from veritrace.graph.model import DesignGraph, Role

Direction = Literal["fanin", "fanout", "both"]

#: §8.6 wants a filter, not another way to select the whole design. Past this
#: the result stops being a cone and the caller should say what it wants.
DEFAULT_DEPTH = 4


@dataclass(slots=True)
class ConeNode:
    path: str
    depth: int
    #: "fanin" or "fanout" — which direction reached it.
    direction: str
    #: Why the edge exists, for the nearest hop that reached this node (§5.4).
    role: str = ""
    #: True when the signal transitioned inside the activity window.
    active: bool = True


@dataclass(slots=True)
class Cone:
    root: str
    depth: int
    direction: Direction
    nodes: list[ConeNode] = field(default_factory=list)
    #: Window the activity filter was applied over, or None when unfiltered.
    window: tuple[int, int] | None = None
    #: Nodes dropped by the activity filter. Reported, never silently hidden.
    n_inactive: int = 0

    def paths(self) -> list[str]:
        return [n.path for n in self.nodes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "depth": self.depth,
            "direction": self.direction,
            "window": list(self.window) if self.window else None,
            "n_inactive": self.n_inactive,
            "nodes": [
                {
                    "path": n.path,
                    "depth": n.depth,
                    "direction": n.direction,
                    "role": n.role,
                    "active": n.active,
                }
                for n in self.nodes
            ],
        }


def _neighbours(graph: DesignGraph, path: str, forward: bool) -> Iterable[tuple[str, str]]:
    """(neighbour, role) pairs one edge away."""
    if forward:
        # Roles on the forward side would need a reverse role index for no gain:
        # the question "what breaks" does not turn on guard-vs-value.
        for dst in sorted(graph.fanout(path)):
            yield dst, ""
    else:
        seen: dict[str, str] = {}
        for e in graph.edges_into(path):
            # Keep the most specific role when a signal feeds a node twice: a
            # guard edge is what §5.4 says the interesting path runs through.
            src = str(e.src)
            if src not in seen or e.role is Role.GUARD:
                seen[src] = e.role.value
        yield from sorted(seen.items())


def _bfs(graph: DesignGraph, root: str, depth: int, forward: bool) -> dict[str, ConeNode]:
    out: dict[str, ConeNode] = {}
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    seen = {root}
    direction = "fanout" if forward else "fanin"
    while queue:
        path, d = queue.popleft()
        if d >= depth:
            continue
        for nxt, role in _neighbours(graph, path, forward):
            if nxt in seen:
                continue
            seen.add(nxt)
            out[nxt] = ConeNode(path=nxt, depth=d + 1, direction=direction, role=role)
            queue.append((nxt, d + 1))
    return out


def cone(
    graph: DesignGraph,
    signal: str,
    depth: int = DEFAULT_DEPTH,
    direction: Direction = "fanin",
    store: Any = None,
    window: tuple[int, int] | None = None,
    include_inactive: bool = False,
) -> Cone:
    """Signals within `depth` edges of `signal`.

    Passing `store` and `window` applies the activity intersection of §8.6.
    Inactive signals are dropped by default and counted, so the report can say
    how much of the cone it removed rather than quietly shrinking.
    """
    if graph.get(signal) is None:
        raise KeyError(signal)

    nodes: dict[str, ConeNode] = {}
    if direction in ("fanin", "both"):
        nodes.update(_bfs(graph, signal, depth, forward=False))
    if direction in ("fanout", "both"):
        for path, node in _bfs(graph, signal, depth, forward=True).items():
            # A signal reachable both ways keeps the shorter hop.
            if path not in nodes or node.depth < nodes[path].depth:
                nodes[path] = node

    nodes.pop(signal, None)
    result = Cone(root=signal, depth=depth, direction=direction, window=window)
    result.nodes = [
        ConeNode(path=signal, depth=0, direction="root"),
        *sorted(nodes.values(), key=lambda n: (n.depth, n.path)),
    ]

    if store is not None and window is not None:
        _mark_activity(result, graph, store, window)
        if not include_inactive:
            result.nodes = [n for n in result.nodes if n.active or n.depth == 0]
    return result


def _mark_activity(result: Cone, graph: DesignGraph, store: Any, window: tuple[int, int]) -> None:
    """Flag which cone members actually moved in the window.

    One `constant_signals` pass in Rust covers the whole trace in parallel,
    which beats a per-signal round trip once a cone gets wide.
    """
    t0, t1 = window
    frozen = set(store.constant_signals(t0, t1))
    for node in result.nodes:
        sig = graph.get(node.path)
        handle = sig.trace_handle if sig is not None else None
        if handle is None:
            # Not in the dump: §7.3 says it may still be reconstructible, so it
            # stays in the cone rather than being filtered out on no evidence.
            node.active = True
            continue
        node.active = handle not in frozen
    result.n_inactive = sum(1 for n in result.nodes if not n.active)
