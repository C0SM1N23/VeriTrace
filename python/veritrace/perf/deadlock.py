"""Deadlock, livelock and starvation — §8.18.

*The most frequent problem on an interconnect, and the hardest to debug by
hand.* §8.18 gives the shape exactly:

    Node  = (agent, resource)      e.g. (dma0, bus_grant)
    Edge A->B = A waits for a resource held by B
    A cycle in the graph = deadlock

The whole question is what makes an agent, a resource and a holder *real* rather
than plausible, because §8.18's own warning is that the report must identify the
actual agents and resources and not merely say "something looks stuck". The
answers this module uses, and why:

* **Agent = an interface.** It is a named scope, it owns transactions, and it is
  what an engineer calls "the master".
* **Resource = the wire the agent is blocked on.** Not an abstraction: either
  the `ready` of a channel offering a beat nobody accepts, or the `valid` of the
  response channel an open transaction is waiting for. Both come straight out of
  the pack, and both are things you can put a cursor on.
* **Holder = an agent whose own signals drive that wire, and which is itself
  blocked over the same interval.** The first half is a static fact from the
  design graph; the second is a fact about the run.

The holder rule is the one that took the most getting right. "The agent with a
transaction in flight on the resource" is the obvious reading and it is wrong
twice over: ownership routinely outlives the transaction that acquired it (a
lock taken by a write that has already completed), and on a shared bus every
master's `ready` depends on every other master's `valid`, so *in flight* would
draw a cycle through any arbiter whose masters happen to be busy at once.

Requiring the holder to be blocked too is what makes the answer sharp, and it is
not an approximation — it is the definition. A deadlock is a set of agents each
of which is waiting on another; an agent that is making progress is not part of
one. So the blockage intervals below also require **zero transfers**: an agent
that moved a beat during the window was not deadlocked during it, whatever else
it was doing.

The graph is built from the transaction table rather than by running a `why()`
backwards from a suspicious instant, for three reasons: §8.18 says it is
computed while traversing the trace; without RTL there is no causal engine but
there are still transactions; and "[Why on each link]" means `why` is an action
*offered on* an edge, so the edge has to exist first.

Without RTL the holder cannot be identified, and the scan says so instead of
guessing. A blockage with no named holder is still reported — as a stall, which
is what it honestly is.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from veritrace.perf.model import Deadlock, Livelock, Liveness, Starvation, WaitEdge
from veritrace.protocol.model import Extraction, Transaction

#: How long a blockage must persist to be a finding rather than backpressure.
#: §8.18 leaves N to the caller; a hundred cycles is the same threshold §8.4
#: uses for a stuck signal, and using one number for "this is not just busy"
#: keeps the two detectors telling a consistent story.
MIN_CYCLES = 100

#: How far to look for a holder. The path from a master's `ready` to the master
#: that owns the bus runs through an arbiter, which is a handful of edges; past
#: this the answer stops being "who is holding it" and becomes "the design".
HOLDER_DEPTH = 8

#: §8.18's livelock pattern: *the FSM alternates between 2-3 states more than a
#: hundred times with zero transactions completed in the interval.*
LIVELOCK_TOGGLES = 100
LIVELOCK_STATES = 4

#: Guard on the livelock candidate scan. A state register is narrow; a wide
#: counter that visits four values is not an FSM, and scanning every signal in a
#: real design would cost more than the rest of the session.
LIVELOCK_MAX_WIDTH = 8


# --- blockages ---------------------------------------------------------------


def _runs(flags: Sequence[int], want: Any = None) -> Iterable[tuple[int, int, int]]:
    """Maximal runs of a repeated value, as `(value, first index, last index)`."""
    start = 0
    for i in range(1, len(flags) + 1):
        if i == len(flags) or flags[i] != flags[start]:
            if flags[start] and (want is None or flags[start] == want):
                yield flags[start], start, i - 1
            start = i


def blockages(ex: Extraction, min_cycles: int = MIN_CYCLES) -> list[WaitEdge]:
    """Every interval this interface spent unable to move, with the wire.

    Two shapes, and both are needed: an agent that cannot *issue* has no
    transaction to point at (that is §8.18's `dma0 waits for bus_grant`), while
    an agent that issued and got no answer has one and it is the most useful
    thing in the report.
    """
    out: list[WaitEdge] = []
    perf = ex.perf
    iface = ex.interface
    if perf is None or not len(perf):
        return out

    def add(path: str | None, lo: int, hi: int, txn: str | None, channel: str, blocked: str) -> None:
        """Record a blockage, unless the agent moved something during it.

        The zero-transfer test is what separates "blocked" from "busy". A master
        that accepted a beat inside the window was progressing, and progress is
        the one thing a deadlocked agent cannot do.
        """
        if path is None or hi - lo + 1 < min_cycles:
            return
        if any(perf.transferring[lo : hi + 1]):
            return
        out.append(
            WaitEdge(
                agent=iface.name,
                resource=path,
                holder=None,
                since=perf.edges[lo],
                until=perf.edges[hi],
                cycles=hi - lo + 1,
                txn=txn,
                channel=channel,
                blocked=blocked,
            )
        )

    # 1. A beat offered and refused, for longer than the threshold. This is
    #    §8.18's `dma0 waits for bus_grant`: the agent has no transaction yet,
    #    which is precisely the problem.
    for k, lo, hi in _runs(perf.blocked_on):
        name = perf.channels[k - 1]
        ready = iface.pack.channel(name).ready
        add(
            iface.signals.get(ready) if ready else None,
            lo,
            hi,
            _txn_at(ex, perf.edges[lo]),
            name,
            _would_be(ex, name, perf.edges[lo]),
        )

    # 2. A transaction that was issued and never completed. §8.18 builds its
    #    graph out of exactly these, which is why the assembler keeps them.
    for t in ex.transactions:
        if t.closed:
            continue
        spec = next((x for x in iface.pack.transactions if x.name == t.kind), None)
        lo, hi = perf.window(t.start_time, perf.edges[-1])
        add(
            _end_signal(iface, t),
            lo,
            max(lo, hi - 1),
            t.ref,
            spec.end.channel if spec is not None and spec.end is not None else "",
            f"{t.label} was issued and never completed",
        )

    out.sort(key=lambda e: (e.since, e.resource))
    return out


def _end_signal(iface: Any, t: Transaction) -> str | None:
    """The `valid` whose rise would complete `t` — read from the pack, not
    assumed."""
    spec = next((x for x in iface.pack.transactions if x.name == t.kind), None)
    phase = spec.end if spec is not None else None
    if phase is None:
        return None
    return iface.signals.get(iface.pack.channel(phase.channel).valid)


def _would_be(ex: Extraction, channel: str, t: int) -> str:
    """The transaction this refused handshake was trying to start.

    Named rather than left blank: `WRITE#5 could not be issued` is what the
    engineer sees on the waveform, and it is derivable — the pack says which
    transaction type starts on this channel, and the table says how many of them
    got through before the bus stopped.
    """
    spec = next((x for x in ex.interface.pack.transactions if x.start == channel), None)
    if spec is None:
        return f"a beat on {channel} was offered and refused"
    n = sum(1 for x in ex.transactions if x.kind == spec.name and x.start_time <= t)
    return f"{spec.name}#{n} could not be issued"


def _txn_at(ex: Extraction, t: int) -> str | None:
    """The transaction in flight on this interface at `t`, if any."""
    for txn in ex.transactions:
        if txn.start_time <= t and (txn.end_time is None or t <= txn.end_time):
            return txn.ref
    return None


# --- who holds it ------------------------------------------------------------


class Holders:
    """Which agents drive a given wire.

    One fan-in cone per wire, memoised: the topology does not change during a
    run, so the expensive half is paid once and the temporal half is a lookup.
    """

    def __init__(self, graph: Any, extractions: Sequence[Extraction]) -> None:
        self.graph = graph
        self.extractions = list(extractions)
        self._owner: dict[str, str] = {}
        for ex in self.extractions:
            for path in ex.interface.signals.values():
                self._owner.setdefault(path, ex.interface.name)
        self._cone: dict[str, list[tuple[int, str]]] = {}

    def candidates(self, resource: str) -> list[tuple[int, str]]:
        """Interfaces in the wire's fan-in cone, nearest first.

        Only the pack's own wires count as belonging to an interface — clock and
        reset are deliberately not in `signals`, or every agent would be found in
        every cone and the graph would be complete rather than informative.
        """
        if resource in self._cone:
            return self._cone[resource]
        found: list[tuple[int, str]] = []
        if self.graph is not None:
            from veritrace.analysis import cone as cone_mod

            try:
                c = cone_mod.cone(self.graph, resource, HOLDER_DEPTH, "fanin")
            except KeyError:
                c = None
            if c is not None:
                seen: dict[str, int] = {}
                for node in c.nodes:
                    owner = self._owner.get(node.path)
                    if owner is not None and node.depth < seen.get(owner, 1 << 30):
                        seen[owner] = node.depth
                found = sorted((d, name) for name, d in seen.items())
        self._cone[resource] = found
        return found


def _last_txn(ex: Extraction, t: int) -> str | None:
    """The most recent transaction on this interface at `t`.

    Reported alongside an edge as context, not used to decide it: a lock is
    routinely still held by a master whose acquiring transaction completed long
    ago, so "in flight" is the wrong test for ownership even though it is the
    tempting one.
    """
    best: Transaction | None = None
    for txn in ex.transactions:
        if txn.start_time <= t and (best is None or txn.start_time > best.start_time):
            best = txn
    return best.ref if best is not None else None


def wait_for_graph(
    extractions: Sequence[Extraction],
    graph: Any = None,
    clock: Any = None,
    min_cycles: int = MIN_CYCLES,
) -> tuple[list[WaitEdge], dict[str, str]]:
    """Every `A waits on R, held by B` arrow, with its live interval."""
    notes: dict[str, str] = {}
    blocks: dict[str, list[WaitEdge]] = {}
    for ex in extractions:
        found = blockages(ex, min_cycles)
        if found:
            blocks[ex.interface.name] = found
    if not blocks:
        return [], notes
    if graph is None:
        notes["deadlock"] = (
            f"{sum(len(v) for v in blocks.values())} persistent blockage(s) found, but "
            "naming who holds a wire needs the RTL; start with --rtl to complete "
            "the wait-for graph"
        )
        return [e for v in blocks.values() for e in v], notes

    by_name = {ex.interface.name: ex for ex in extractions}
    holders = Holders(graph, extractions)
    out: list[WaitEdge] = []
    best: dict[tuple[str, str, str], WaitEdge] = {}
    for agent, waits in blocks.items():
        for w in waits:
            named = False
            for _, other in holders.candidates(w.resource):
                if other == agent or other not in blocks:
                    continue
                for held in blocks[other]:
                    lo, hi = max(w.since, held.since), min(w.until, held.until)
                    if lo > hi:
                        continue  # the two were never stuck at the same time
                    named = True
                    key = (agent, w.resource, other)
                    edge = WaitEdge(
                        agent=agent,
                        resource=w.resource,
                        holder=other,
                        since=lo,
                        until=hi,
                        txn=w.txn,
                        channel=w.channel,
                        blocked=w.blocked,
                        holder_txn=_last_txn(by_name[other], lo),
                        # `lo`/`hi` are timestamps here, not indices — the
                        # overlap of two blockages that may sit on different
                        # clocks. Counting them as cycles without the clock is
                        # how an edge ends up claiming five million of them.
                        cycles=_cycles_between(clock, lo, hi),
                        evidence=f"`{w.resource}` is driven through {other}, "
                        f"which was itself blocked on `{held.resource}`",
                    )
                    if key not in best or edge.cycles > best[key].cycles:
                        best[key] = edge
            if not named:
                # Kept without a holder: a blockage nobody is holding is still a
                # stall worth reporting, and dropping it would hide the most
                # common case — a slow slave — behind the rarest one.
                w.evidence = (
                    f"nothing else was blocked while `{w.resource}` held up {agent}"
                )
                out.append(w)
    out.extend(best.values())
    out.sort(key=lambda e: (e.since, e.agent, e.resource))
    return out, notes


# --- cycles ------------------------------------------------------------------


def _cycles(edges: Sequence[WaitEdge]) -> list[list[WaitEdge]]:
    """Every simple cycle in the wait-for graph, each reported once.

    Only edges whose blocked intervals overlap can form one: agents that were
    stuck at different times were never stuck on each other.
    """
    by_agent: dict[str, list[WaitEdge]] = {}
    for e in edges:
        if e.holder:
            by_agent.setdefault(e.agent, []).append(e)

    found: list[list[WaitEdge]] = []
    seen: set[frozenset[str]] = set()

    def walk(path: list[WaitEdge], lo: int, hi: int) -> None:
        if len(path) > len(by_agent):
            return
        for e in by_agent.get(path[-1].holder or "", ()):
            t0, t1 = max(lo, e.since), min(hi, e.until)
            if t0 > t1:
                continue  # the two blockages never coexisted
            if e.holder == path[0].agent:
                key = frozenset(x.agent for x in (*path, e))
                if key not in seen:
                    seen.add(key)
                    found.append([*path, e])
                continue
            if any(x.agent == e.agent for x in path):
                continue
            walk([*path, e], t0, t1)

    for e in edges:
        if e.holder:
            walk([e], e.since, e.until)
    return found


def find_deadlocks(
    extractions: Sequence[Extraction],
    graph: Any = None,
    clock: Any = None,
    min_cycles: int = MIN_CYCLES,
) -> tuple[list[Deadlock], list[WaitEdge], dict[str, str]]:
    """The wait-for graph, and the cycles in it that persisted."""
    edges, notes = wait_for_graph(extractions, graph, clock, min_cycles)
    out: list[Deadlock] = []
    for ring in _cycles(edges):
        at = max(e.since for e in ring)
        until = min(e.until for e in ring)
        held = _cycles_between(clock, at, until)
        if held < min_cycles:
            continue
        first = min(ring, key=lambda e: e.since)
        out.append(
            Deadlock(
                agents=[e.agent for e in ring],
                edges=ring,
                at=at,
                cycles=held,
                first_txn=first.txn,
                first_txn_at=first.since,
                first_blocked=first.blocked,
            )
        )
    out.sort(key=lambda d: (d.at, d.agents))
    return out, edges, notes


def _cycles_between(clock: Any, t0: int, t1: int) -> int:
    if clock is None or getattr(clock, "period", None) in (None, 0):
        return max(0, t1 - t0)
    return clock.cycles_between(t0, t1)


# --- livelock and starvation --------------------------------------------------


def find_starvation(
    extractions: Sequence[Extraction], clock: Any = None, min_cycles: int = MIN_CYCLES
) -> list[Starvation]:
    """*An agent asks continuously and never receives, while others progress.*

    Both halves are required. Without the second clause a bus nobody is driving
    reads as starvation, which would put a finding on every idle design.
    """
    live = [ex for ex in extractions if ex.perf is not None and len(ex.perf)]
    if len(live) < 2:
        return []

    served_at: dict[int, set[str]] = {}
    for ex in live:
        perf = ex.perf
        for i, moved in enumerate(perf.transferring):
            if moved:
                served_at.setdefault(perf.edges[i], set()).add(ex.interface.name)

    out: list[Starvation] = []
    for ex in live:
        perf = ex.perf
        asking = bytes(
            int(bool(r) and not perf.transferring[i]) for i, r in enumerate(perf.requesting)
        )
        for _, lo, hi in _runs(asking):
            if hi - lo + 1 < min_cycles:
                continue
            others: set[str] = set()
            for i in range(lo, hi + 1):
                others |= served_at.get(perf.edges[i], set()) - {ex.interface.name}
            if not others:
                continue
            out.append(
                Starvation(
                    agent=ex.interface.name,
                    since=perf.edges[lo],
                    until=perf.edges[hi],
                    cycles=hi - lo + 1,
                    served=sorted(others),
                )
            )
    # §8.18 asks for the maximum duration per agent, so the longest run of each
    # is what gets reported rather than every run it had.
    worst: dict[str, Starvation] = {}
    for s in out:
        if s.agent not in worst or s.cycles > worst[s.agent].cycles:
            worst[s.agent] = s
    return sorted(worst.values(), key=lambda s: (-s.cycles, s.agent))


def find_livelocks(
    store: Any,
    extractions: Sequence[Extraction],
    clock: Any = None,
    graph: Any = None,
    min_toggles: int = LIVELOCK_TOGGLES,
) -> list[Livelock]:
    """*Progress without advance*: a state signal alternating while nothing
    completes.

    The window is the interval between completed transactions, so "zero
    transactions completed in the interval" is true by construction and the
    detector cannot fire on a design that is simply busy.
    """
    if clock is None or not getattr(clock, "edges", None):
        return []
    closes = sorted(
        t.end_time for ex in extractions for t in ex.transactions if t.end_time is not None
    )
    edges = clock.edges
    t0, t1 = edges[0], edges[-1]
    # The gaps in which nothing completed. A run with no transactions at all is
    # one gap, which is right: nothing completed for the whole run.
    gaps = [
        (a, b)
        for a, b in zip([t0, *closes], [*closes, t1])
        if _cycles_between(clock, a, b) >= min_toggles
    ]
    if not gaps:
        return []

    out: list[Livelock] = []
    streams: set[int] = set()
    for meta in store.signals():
        # A net appears once per scope that names it; reporting `state`,
        # `u_ctrl.state` and `tb.dut.state` as three spinning FSMs would be
        # three rows about one wire.
        if meta.stream_id in streams:
            continue
        streams.add(meta.stream_id)
        if meta.width > LIVELOCK_MAX_WIDTH or meta.width < 2:
            # Narrow-but-not-one-bit: a single-bit flag toggling is every
            # `valid` in the design, and reporting those would bury the FSM
            # that is actually spinning.
            continue
        if meta.n_events < min_toggles:
            continue
        for a, b in gaps:
            n = store.edge_count(meta.handle, a, b)
            if n < min_toggles:
                continue
            values = {v.bits for _, v in store.transitions(meta.handle, a, b)}
            if 2 <= len(values) <= LIVELOCK_STATES:
                out.append(
                    Livelock(
                        signal=meta.path,
                        since=a,
                        until=b,
                        toggles=n,
                        states=sorted(values),
                        completed=0,
                    )
                )
    out.sort(key=lambda x: (-x.toggles, x.signal))
    return out


def analyse(
    store: Any,
    extractions: Sequence[Extraction],
    graph: Any = None,
    clock: Any = None,
    min_cycles: int = MIN_CYCLES,
) -> tuple[Liveness, list[WaitEdge]]:
    """The whole §8.18 scan. Returns the findings and the wait-for graph."""
    out = Liveness()
    if not extractions:
        out.skipped["liveness"] = "no protocol interfaces, so there are no agents"
        return out, []

    out.deadlocks, edges, notes = find_deadlocks(extractions, graph, clock, min_cycles)
    out.skipped.update(notes)
    out.starvation = find_starvation(extractions, clock, min_cycles)
    try:
        out.livelocks = find_livelocks(store, extractions, clock, graph)
    except Exception as e:  # noqa: BLE001 - one scan must not lose the other two
        out.skipped["livelock"] = str(e)
    return out, edges
