"""Step 2 of §8.14: raw signals to channel events.

*For every channel, scan the trace in parallel and collect every cycle where
`valid && ready`, with the payload sampled at that cycle.*

The whole scan is two calls into `sample_before`, which is parallel across
signals in Rust and linear in the trace. Two details decide whether the numbers
that come out are right:

* **Sampling is `value_before` at the clock edge, never `value_at`.** At a rising
  edge the dump already holds the *results* of that edge; a handshake was decided
  by what the flops saw going into it (§5.5, problem 2). Reading the post-edge
  value shifts every transfer by a cycle and, worse, silently drops the last one.
* **Reset is not a transfer window.** Every protocol here forbids transfers while
  reset is asserted, and a bus that idles at X through reset would otherwise
  produce a burst of phantom beats at time zero. The masked cycles are counted
  and reported rather than quietly skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from veritrace.protocol.model import ChannelEvent, FieldValue, Interface
from veritrace.protocol.pack import Channel

#: A reset whose name looks like this is asserted low: `rst_n`, `aresetn`,
#: `s_axi_aresetn`, `nrst`. Only consulted when the trace itself cannot answer.
ACTIVE_LOW_RE = re.compile(r"(?:rst|reset)_?n\b|\bn_?(?:rst|reset)\b", re.IGNORECASE)


def field_value(v: Any) -> FieldValue:
    """A sampled `Value` as something a metric can compute with.

    An integer when the value is fully known and fits, otherwise the raw
    four-state digits — which keep `xxxx` distinguishable from `0000` in the
    exported table, because a field that was unknown and a field that was zero
    are different facts (P1).
    """
    if v is None:
        return None
    as_int = v.to_int()
    if as_int is not None:
        return as_int
    return v.bits


def _truthy(v: Any) -> bool | None:
    """One-bit sample as a three-valued condition."""
    if v is None:
        return None
    n = v.to_int()
    return None if n is None else bool(n)


@dataclass(slots=True)
class ChannelScan:
    channel: str
    events: list[ChannelEvent] = field(default_factory=list)
    #: Edges where valid was high but the transfer had not happened yet.
    stall_cycles: int = 0
    #: Set when the channel could not be scanned at all, e.g. `valid` not dumped.
    skipped: str = ""


class Sampler:
    """Every interface signal, sampled once at every clock edge.

    Built per interface and shared by the channel scan, the temporal rule
    engine and (from §8.17) stall attribution: they all want the same matrix,
    and sampling it three times would triple the only expensive part of the
    extraction.
    """

    def __init__(self, store: Any, edges: list[int]) -> None:
        self.store = store
        self.edges = edges
        self._cache: dict[str, list[Any]] = {}

    def column(self, path: str | None) -> list[Any] | None:
        """Samples of one signal at every edge, or `None` if it is not dumped."""
        if path is None:
            return None
        got = self._cache.get(path)
        if got is None:
            self.prefetch([path])
            got = self._cache.get(path)
        return got

    def prefetch(self, paths: list[str | None]) -> None:
        """Sample several signals in one parallel call.

        The reason `column` is not simply lazy: one call with twenty handles is
        one rayon batch, twenty calls are twenty.
        """
        wanted = [p for p in paths if p and p not in self._cache]
        if not wanted:
            return
        handles, kept = [], []
        for p in dict.fromkeys(wanted):
            h = self.store.find(p)
            if h is not None:
                handles.append(h)
                kept.append(p)
        if not handles:
            return
        for path, row in zip(kept, self.store.sample_before(handles, self.edges)):
            self._cache[path] = row

    def sample_at(self, paths: list[str | None], times: list[int]) -> dict[str, list[Any]]:
        """Sample signals at arbitrary ascending times, outside the edge grid."""
        handles, kept = [], []
        for p in dict.fromkeys(p for p in paths if p):
            h = self.store.find(p)
            if h is not None:
                handles.append(h)
                kept.append(p)
        if not handles or not times:
            return {}
        return dict(zip(kept, self.store.sample_before(handles, times)))


def reset_polarity(iface: Interface, levels: list[bool | None], config: Any = None) -> bool:
    """True when this interface's reset is asserted low.

    Read from the trace before the name, because the trace is evidence and a
    name is a convention: a run begins in reset and leaves it, so whichever
    level the signal *starts* at is the asserted one. Getting this backwards
    masks the entire run instead of the reset window and yields an interface
    that mysteriously carries no traffic, so it is worth not guessing.

    The name is the fallback for a reset that never toggles at all, and an
    explicit `reset.active` in `.veritrace.toml` (§4.3) overrides both.
    """
    configured = str(getattr(config, "reset_active", "") or "").lower()
    if configured in ("low", "high"):
        return configured == "low"
    known = [t for t in levels if t is not None]
    if len(set(known)) == 2:
        return known[0] is False
    leaf = iface.reset.rsplit(".", 1)[-1] if iface.reset else ""
    return bool(ACTIVE_LOW_RE.search(leaf))


def reset_mask(iface: Interface, sampler: Sampler, config: Any = None) -> list[bool]:
    """`True` at every edge where the interface's reset is asserted."""
    col = sampler.column(iface.reset)
    if col is None:
        return [False] * len(sampler.edges)
    levels = [_truthy(v) for v in col]
    active_low = reset_polarity(iface, levels, config)
    # An unknown reset early in a run means "not out of reset yet".
    return [True if t is None else (t is not active_low) for t in levels]


def scan(
    iface: Interface,
    sampler: Sampler,
    in_reset: list[bool],
) -> dict[str, ChannelScan]:
    """Channel events for every channel of `iface`."""
    pack = iface.pack
    sampler.prefetch(
        [iface.signals.get(s) for ch in pack.channels for s in (ch.valid, ch.ready) if s]
    )

    out: dict[str, ChannelScan] = {}
    for ch in pack.channels:
        out[ch.name] = _scan_one(iface, ch, sampler, in_reset)
    return out


def _scan_one(
    iface: Interface, ch: Channel, sampler: Sampler, in_reset: list[bool]
) -> ChannelScan:
    res = ChannelScan(channel=ch.name)
    valid = sampler.column(iface.signals.get(ch.valid))
    if valid is None:
        res.skipped = f"`{ch.valid}` is not in the trace"
        return res
    ready = sampler.column(iface.signals.get(ch.ready)) if ch.ready else None
    if ch.ready and ready is None:
        # A dumped valid without its ready is not a reason to give up: the
        # channel still transfers, we just cannot see the backpressure. Say so.
        res.skipped = f"`{ch.ready}` is not in the trace; treating every valid as accepted"

    edges = sampler.edges
    accepted: list[int] = []      # indices into edges
    assert_at: list[int] = []     # index where valid went high for that beat
    stalls: list[int] = []
    run_start: int | None = None
    waited = 0

    for i in range(len(edges)):
        if in_reset[i]:
            run_start, waited = None, 0
            continue
        v = _truthy(valid[i])
        if not v:
            # Unknown valid is not a transfer, and not a stall either.
            run_start, waited = None, 0
            continue
        if run_start is None:
            run_start, waited = i, 0
        r = True if ready is None else _truthy(ready[i])
        if r:
            accepted.append(i)
            assert_at.append(run_start)
            stalls.append(waited)
            run_start, waited = None, 0
        else:
            waited += 1
            res.stall_cycles += 1

    if not accepted:
        return res

    times = [edges[i] for i in accepted]
    payload_paths = [iface.signals.get(p) for p in ch.payload]
    columns = sampler.sample_at(payload_paths, times)

    for n, i in enumerate(accepted):
        fields: dict[str, FieldValue] = {}
        for name in ch.payload:
            path = iface.signals.get(name)
            col = columns.get(path) if path else None
            fields[name] = field_value(col[n]) if col is not None else None
        res.events.append(
            ChannelEvent(
                channel=ch.name,
                time=edges[i],
                assert_time=edges[assert_at[n]],
                stall=stalls[n],
                fields=fields,
            )
        )
    return res
