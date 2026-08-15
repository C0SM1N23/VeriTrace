"""Handshake structure — the last row of §8.9's table.

Everything else §8.9 asks for (transfers, throughput, backpressure, starvation,
latency percentiles, burst histogram) is computed from transactions by §8.17 and
the protocol packs, because a pack already knows which signals form the
handshake. One item cannot come from a trace at all:

> Violari de protocol: ... `ready` combinational dependent de `valid`
> (deadlock risk)

That is a question about the *design*, not about a run. A dump where the two
never deadlocked proves nothing — the combinational path is still there, and the
day the other side of the bus also waits, both wait forever. AXI names it
outright: a slave must not assert AWREADY on the basis of AWVALID within the
same cycle.

So this is a structural check over the graph, and it needs no trace. It reports
only what it can prove: a purely combinational path from `valid` to `ready`,
with the drivers it went through.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity
from veritrace.graph.model import DesignGraph

CHECK = "handshake_comb_loop"

#: A path longer than this is not evidence of anything a reader can act on, and
#: the interesting cases are one or two levels of logic.
MAX_HOPS = 6


def scan(analysis: Any, graph: DesignGraph | None = None, config: Any = None) -> Iterator[Finding]:
    """One finding per interface whose `ready` depends combinationally on `valid`."""
    if analysis is None or graph is None:
        return
    for ex in analysis.extractions:
        iface = ex.interface
        for channel in iface.pack.channels:
            if not channel.ready:
                continue
            ready = iface.signals.get(_leaf(channel.ready))
            valid = iface.signals.get(_leaf(channel.valid))
            if not ready or not valid or ready == valid:
                continue
            if config is not None and config.is_ignored(ready):
                continue
            path = _comb_path(graph, ready, valid)
            if path is None:
                continue
            sig = graph.get(ready)
            yield Finding(
                group=Group.PROTOCOL,
                severity=Severity.WARN,
                check=CHECK,
                title=(
                    f"{iface.name}.{channel.name}: {_short(ready)} depends "
                    f"combinationally on {_short(valid)}"
                ),
                signal=ready,
                loc=sig.decl_loc if sig is not None else None,
                detail=(
                    "A ready that is a function of valid in the same cycle is a "
                    "deadlock waiting for the other side to do the same. It is "
                    "legal to compute ready from anything else; AXI forbids "
                    "exactly this dependency."
                ),
                notes=(
                    "path: " + " <- ".join(_short(p) for p in path),
                    "Structural: found in the RTL, not observed in this run. A "
                    "trace where it never deadlocked does not clear it.",
                ),
                related=(iface.name, channel.name),
            )


def _leaf(name: str) -> str:
    """A pack's signal name as `iface.signals` keys it.

    `valid` in a pack may be an expression (`psel && penable`); only a bare
    name can be looked up, and anything else is skipped rather than guessed at.
    """
    return name.strip()


def _short(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _comb_path(graph: DesignGraph, ready: str, valid: str) -> list[str] | None:
    """Combinational path from `valid` to `ready`, or None.

    Only continuous and `always_comb` drivers are followed. A register on the
    way is what makes the dependency legal — the whole point of the check is
    that the two signals settle in the *same* cycle.
    """
    start = graph.get(ready)
    if start is None:
        return None
    queue: deque[tuple[str, list[str]]] = deque([(ready, [ready])])
    seen = {ready}
    while queue:
        node, trail = queue.popleft()
        if len(trail) > MAX_HOPS:
            continue
        sig = graph.get(node)
        if sig is None:
            continue
        for d in sig.drivers:
            if d.is_sequential:
                continue  # a flop breaks the loop, which is the correct design
            for e in graph.edges_into(node):
                src = str(e.src)
                if src in seen:
                    continue
                if src == valid:
                    return trail + [src]
                seen.add(src)
                queue.append((src, trail + [src]))
    return None
