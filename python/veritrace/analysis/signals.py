"""Choosing which name to report a finding against.

A port connection ties several hierarchical names to one wire: `tb.rst_n`,
`tb.dut.rst_n` and `tb.dut.u_dut.rst_n` are the same signal, share one event
stream in the dump, and are frozen or X together. Printing all three for one
fact is what makes an automatic report unreadable, and it is also what makes
causal analysis stop early — the outer names have no drivers in the graph, so a
backward walk that starts there terminates immediately.

So every whole-trace scan starts from one canonical signal per stream, picked
by a rule that is total and therefore reproducible (P1).
"""

from __future__ import annotations

from typing import Any


def rank(meta: Any, graph: Any) -> tuple:
    """Sort key that puts the most useful name for a stream first.

    A signal with a *real* driver wins: that is where the RTL defines it and
    where a fix goes. Port connections are drivers too — they have to be, or
    the causal walk stops at every module boundary — but they only pass a value
    along, so a name that merely receives one through a port is no better than
    the plain alias it used to be. Failing that, the shortest path, which is
    the name a reader already knows.
    """
    sig = graph.get(meta.path) if graph is not None else None
    real = bool(sig is not None and any(not _is_port(d) for d in sig.drivers))
    return (not real, meta.path.count("."), meta.path)


def _is_port(driver: Any) -> bool:
    return getattr(driver.kind, "value", "") == "inst_port"


def canonical(store: Any, graph: Any = None, config: Any = None) -> list[Any]:
    """One `SignalMeta` per event stream, in a stable order."""
    streams: dict[int, list[Any]] = {}
    for meta in store.signals():
        if config is not None and config.is_ignored(meta.path):
            continue
        streams.setdefault(meta.stream_id, []).append(meta)
    return sorted(
        (min(group, key=lambda m: rank(m, graph)) for group in streams.values()),
        key=lambda m: m.path,
    )
