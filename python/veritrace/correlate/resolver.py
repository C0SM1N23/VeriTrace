"""Matching RTL signals to trace signals — the algorithm of §7.2.

Four attempts, in order, stopping at the first that succeeds:

  a. exact path
  b. path after normalisation (escaped ids, bracket mangling, `[]` vs `__`)
  c. port-connection equivalence class — `.clk(sys_clk)` is two names, one wire
  d. longest unique suffix — only when unambiguous

Anything left is marked NOT_TRACED rather than guessed at. The match rate is a
first-class metric (§7.2): below 90% something is wrong and the user has to
know.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from veritrace.correlate.heuristics import build_index, normalize, suffixes
from veritrace.graph.model import DesignGraph, Signal


class Method(Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    EQUIVALENCE = "equivalence"
    SUFFIX = "suffix"
    ELEMENTS = "elements"
    NOT_TRACED = "not_traced"


@dataclass(slots=True)
class CorrelationReport:
    total: int = 0
    matched: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    #: Paths with no trace signal, split by whether the graph can rebuild them.
    unmatched: list[str] = field(default_factory=list)
    reconstructible: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return (self.matched / self.total) if self.total else 0.0

    @property
    def percent(self) -> float:
        return round(100.0 * self.rate, 1)

    def summary(self) -> str:
        return f"{self.matched}/{self.total} signals correlated ({self.percent}%)"


class _Union:
    """Union-find over signal paths, for port-connection equivalence (§7.1)."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def members(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            groups[self.find(x)].append(x)
        return groups


def _element_of(path: str) -> tuple[str | None, int]:
    """`top.arr[2]` -> `("top.arr", 2)`; anything else -> `(None, -1)`.

    Only a trailing numeric subscript counts. An instance name such as
    `g_lane[2]` sits mid-path and is part of the hierarchy, not a selection.
    """
    if not path.endswith("]"):
        return None, -1
    open_at = path.rfind("[")
    if open_at <= 0:
        return None, -1
    inner = path[open_at + 1 : -1]
    if not inner.lstrip("-").isdigit():
        return None, -1
    return path[:open_at], int(inner)


def correlate(
    graph: DesignGraph,
    trace_paths: dict[str, int],
    aliases: list[tuple[str, str]] | None = None,
) -> CorrelationReport:
    """Attach `trace_handle` to every signal in `graph` that the trace contains.

    `trace_paths` maps a hierarchical path to its handle; `aliases` are the
    port-connection pairs discovered during elaboration.
    """
    exact, by_suffix, by_element = build_index(trace_paths)

    # Equivalence classes, so a port and the net connected to it can stand in
    # for one another.
    uf = _Union()
    element_aliases: dict[str, dict[int, str]] = defaultdict(dict)
    for a, b in aliases or []:
        # `arr[2] == inst.port` is not an equivalence between two whole signals:
        # unioning them would tie the entire array to one lane. It is an
        # equivalence between one *element* and that port, kept aside.
        base, idx = _element_of(b)
        if base is not None:
            element_aliases[normalize(base)][idx] = normalize(a)
            continue
        uf.union(normalize(a), normalize(b))
    classes = uf.members()

    report = CorrelationReport(total=len(graph))
    counts: dict[str, int] = defaultdict(int)

    for sig in graph:
        method, handle = _match(sig, trace_paths, exact, by_suffix, uf, classes)
        sig.trace_handle = handle
        if handle is None:
            # A memory has no signal of its own; correlate its words.
            sig.elements = dict(by_element.get(normalize(sig.path), {}))
            # An array wired to instance ports — `.dout(lane_dout[i])` — has one
            # element per instance and no signal of its own in the dump either.
            # The elaborator records each connection as `lane_dout[2] ==
            # g_lane[2].u_fifo.dout`, so the words are found through the
            # equivalence classes rather than by name (§7.1).
            for idx, other in element_aliases.get(normalize(sig.path), {}).items():
                if idx not in sig.elements and other in exact:
                    sig.elements[idx] = exact[other]
            if sig.elements:
                method = Method.ELEMENTS
        counts[method.value] += 1
        if sig.is_traced:
            report.matched += 1
        else:
            (report.reconstructible if sig.drivers else report.unmatched).append(sig.path)

    # A signal that is absent but derivable is not a correlation failure — it is
    # reconstructible from the graph on demand (§7.3).
    report.unmatched.sort()
    report.reconstructible.sort()
    report.by_method = dict(counts)
    return report


def _match(
    sig: Signal,
    raw: dict[str, int],
    exact: dict[str, int],
    by_suffix: dict[str, list[int]],
    uf: _Union,
    classes: dict[str, list[str]],
) -> tuple[Method, int | None]:
    path = sig.path
    n = normalize(path)

    # (a) the path as written.
    if path in raw:
        return Method.EXACT, raw[path]
    # (b) the path after normalisation.
    if n in exact:
        return Method.NORMALIZED, exact[n]

    # (c) equivalence class: try every other name this signal is tied to.
    root = uf.find(n) if n in uf.parent else None
    if root is not None:
        for other in classes.get(root, ()):
            if other != n and other in exact:
                return Method.EQUIVALENCE, exact[other]

    # (d) longest unique suffix. Ambiguity means no answer, not a guess.
    for s in suffixes(n):
        hits = by_suffix.get(s)
        if hits and len(set(hits)) == 1:
            return Method.SUFFIX, hits[0]

    return Method.NOT_TRACED, None


def format_report(report: CorrelationReport, limit: int = 20) -> str:
    """Console report (§7.2 step 4)."""
    lines = [report.summary()]
    if report.by_method:
        order = [m.value for m in Method]
        parts = [f"{k}={report.by_method[k]}" for k in order if report.by_method.get(k)]
        lines.append("  by method: " + ", ".join(parts))
    if report.percent < 90:
        lines.append(
            "  WARNING: below 90%. Check the simulator dump flags — see §4.0 "
            "(Verilator needs --no-inline and --trace-structs)."
        )
    if report.reconstructible:
        lines.append(
            f"  {len(report.reconstructible)} not dumped but reconstructible from RTL:"
        )
        lines += [f"    ~ {p}" for p in report.reconstructible[:limit]]
    if report.unmatched:
        lines.append(f"  {len(report.unmatched)} not correlated:")
        lines += [f"    - {p}" for p in report.unmatched[:limit]]
        if len(report.unmatched) > limit:
            lines.append(f"    ... and {len(report.unmatched) - limit} more")
    return "\n".join(lines)
