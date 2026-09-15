"""RTL memory inventory and native trace samples, separate from DRAM timing.

Declarations establish address bounds. Only dumped words establish values;
neither a reset convention nor an observed read justifies filling unseen RAM.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import re

from veritrace.graph.model import Kind


def memories(graph):
    if graph is None:
        return []
    return sorted((s for s in graph.signals.values() if s.kind is Kind.MEM or s.depth),
                  key=lambda s: s.path)


def inventory(graph) -> list[dict]:
    return [{"path": s.path, "width": s.width, "depth": s.depth,
             "left": s.array_left, "right": s.array_right,
             "captured": len(s.elements), "source": str(s.decl_loc),
             "supported": s.array_left is not None and s.array_right is not None}
            for s in memories(graph)]


def capture_elements(graph, patterns) -> list[str]:
    found = memories(graph)
    selected = {}
    for pattern in patterns:
        matched = [s for s in found if s.path == pattern or fnmatchcase(s.path, pattern)]
        if not matched:
            raise ValueError(f"No RTL memory matches {pattern!r}; use a full hierarchical path or '*'.")
        selected.update((s.path, s) for s in matched)
    elements = []
    for path, s in sorted(selected.items()):
        if s.array_left is None or s.array_right is None:
            raise ValueError(f"{path}: capture currently requires a one-dimensional fixed array.")
        if not re.fullmatch(r"[a-zA-Z_$][\w$]*(?:\[-?\d+\])?(?:\.[a-zA-Z_$][\w$]*(?:\[-?\d+\])?)*", path):
            raise ValueError(f"{path}: automatic memory capture does not support escaped identifiers.")
        elements.extend(f"{path}[{i}]" for i in range(min(s.array_left, s.array_right),
                                                   max(s.array_left, s.array_right) + 1))
    return elements


def sample(graph, store, path: str, time: int, offset: int, count: int) -> dict:
    s = next((s for s in memories(graph) if s.path == path), None)
    if s is None:
        raise KeyError(path)
    if s.array_left is None or s.array_right is None:
        raise ValueError("Only one-dimensional fixed arrays can be inspected by word address.")
    if not store.time_range[0] <= time <= store.time_range[1]:
        raise ValueError("Memory sample time is outside the captured trace.")
    if offset >= s.depth:
        raise ValueError("Memory page offset is outside the declared array.")
    first = min(s.array_left, s.array_right) + offset
    words = []
    for index in range(first, min(first + count, max(s.array_left, s.array_right) + 1)):
        handle = s.elements.get(index)
        value = store.value_at(handle, time) if handle is not None else None
        words.append({"index": index, "path": f"{path}[{index}]",
                      "captured": handle is not None,
                      "bits": value.bits if value is not None else None})
    return {"path": path, "time": time, "offset": offset, "words": words}
