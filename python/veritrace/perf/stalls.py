"""Stall attribution — §8.17, the most valuable thing in the section.

*For every cycle the bus did not transfer, evaluate a cascade of causes in
priority order, defined in the protocol pack. Each cycle gets exactly one label,
the first that matches. The sum is 100%, verifiable. Without that, attribution
is guesswork.*

The whole point is the last sentence. A chart saying "roughly a third was
arbitration" is an impression; "3.200 of 10.000 cycles were lost: 1.800 SDRAM
refresh, 900 arbitration, 500 sink backpressure" is a number someone can act on.
Three decisions keep it in the second category:

* **The denominator is every sampled cycle**, reset included, and reset is a
  bucket of its own. Excluding cycles from the total is how attribution quietly
  stops adding up.
* **`other` exists and is shown.** A cycle that no rung claimed is not dropped,
  and a large `other` is the honest signal that the pack's cascade is incomplete
  (P1) rather than a design that is mysteriously idle.
* **A rung that cannot be evaluated is removed and reported.** It does not
  silently never fire, which would be indistinguishable from a cause that never
  occurred.

This runs inside extraction step 2, where the sampled matrix already exists.
A second pass over the trace to answer "why was it slow" would double the only
expensive part of the session for something the first pass had in hand.
"""

from __future__ import annotations

from typing import Any, Sequence

from veritrace.perf.model import OTHER, OUTSTANDING_CAP, RESET, TRANSFER, CyclePerf
from veritrace.protocol import expr
from veritrace.protocol.assemble import CycleEnv
from veritrace.protocol.channels import ChannelScan, Sampler
from veritrace.protocol.model import Interface, Transaction

#: Above this the memo key stops paying for itself and the cascade is evaluated
#: directly. Reached only by a pack whose rungs read a wide bus by value.
MAX_MEMO_SIGNALS = 24


def _truthy(v: Any) -> bool:
    if v is None or isinstance(v, str):
        return False
    n = v if isinstance(v, int) else v.to_int()
    return bool(n)


def _aggregate(
    iface: Interface,
    sampler: Sampler,
    transfer: Sequence[str],
    every: Sequence[str],
    n: int,
) -> tuple[bytes, bytes, bytes]:
    """`valid`, `ready` and the refused channel, per cycle.

    §8.17's `valid`/`ready` are about *useful work*, so they are taken over the
    transfer channels only. A protocol with one channel makes them the wires
    themselves; AXI has five, and §8.17 still writes `valid && !ready`. So:

    * `valid` is high when **any** transfer channel is offering a beat;
    * `ready` is high when nothing being offered is refused — and, when nothing
      is being offered at all, when some sink would have accepted one.

    That makes §8.17's two literal examples mean what they say on either shape:
    `valid && !ready` is "a beat is being refused", `!valid && ready` is "the
    sink was willing and the source had nothing".

    The refused-channel index spans **every** channel, not just those two.
    §8.18's canonical deadlock is an agent stuck in its address phase, and a bus
    where the address is refused while no data is even offered would otherwise
    look perfectly idle.
    """
    pack = iface.pack
    valid = bytearray(n)
    ready = bytearray(n)
    blocked = bytearray(n)
    useful = {name for name in transfer}
    cols: list[tuple[bool, list[Any] | None, list[Any] | None]] = []
    for name in every:
        ch = pack.channel(name)
        cols.append(
            (
                name in useful,
                sampler.column(iface.signals.get(ch.valid)),
                sampler.column(iface.signals.get(ch.ready)) if ch.ready else None,
            )
        )
    for i in range(n):
        offered = False
        refused = 0
        any_ready = False
        for k, (counts, vcol, rcol) in enumerate(cols):
            v = _truthy(vcol[i]) if vcol is not None else False
            # A channel with no `ready` in the dump always accepts: the scan
            # already treats it that way, and the two must not disagree.
            r = True if rcol is None else _truthy(rcol[i])
            if v and not r and not refused:
                refused = k + 1
            if not counts:
                continue
            if r:
                any_ready = True
            if v:
                offered = True
        valid[i] = int(offered)
        ready[i] = int((not _refused_useful(cols, i)) if offered else any_ready)
        blocked[i] = refused
    return bytes(valid), bytes(ready), bytes(blocked)


def _refused_useful(cols: Sequence[tuple[bool, Any, Any]], i: int) -> bool:
    for counts, vcol, rcol in cols:
        if not counts:
            continue
        if (_truthy(vcol[i]) if vcol is not None else False) and not (
            True if rcol is None else _truthy(rcol[i])
        ):
            return True
    return False


def _outstanding(transactions: Sequence[Transaction], edges: Sequence[int], cap: int) -> bytes:
    """Open transactions at each edge, by sweeping starts and ends.

    A sweep rather than a scan per cycle: the counter only changes where a
    transaction begins or ends, so this is linear in transactions, not in
    transactions times cycles.
    """
    delta: dict[int, int] = {}
    for t in transactions:
        delta[t.start_time] = delta.get(t.start_time, 0) + 1
        if t.end_time is not None:
            # Open *through* the closing edge: a transaction completing at c50
            # was still in flight at c50, which is what the response means.
            delta[t.end_time + 1] = delta.get(t.end_time + 1, 0) - 1
    keys = sorted(delta)
    out = bytearray(len(edges))
    live = 0
    k = 0
    for i, t in enumerate(edges):
        while k < len(keys) and keys[k] <= t:
            live += delta[keys[k]]
            k += 1
        out[i] = min(cap, live)
    return bytes(out)


def attribute(
    iface: Interface,
    sampler: Sampler,
    in_reset: Sequence[bool],
    scans: dict[str, ChannelScan],
    transactions: Sequence[Transaction],
) -> CyclePerf:
    """Label every clock edge with exactly one cause."""
    pack = iface.pack
    edges = sampler.edges
    n = len(edges)
    channels = pack.perf.channels(pack)
    buckets = [RESET, TRANSFER, *[r.name for r in pack.stall_reasons], OTHER]
    perf = CyclePerf(buckets=buckets, edges=list(edges))
    if not n:
        return perf

    sampler.prefetch(
        [iface.signals.get(s) for c in pack.channels for s in (c.valid, c.ready) if s]
    )
    every = [c.name for c in pack.channels]
    valid, ready, blocked = _aggregate(iface, sampler, channels, every, n)
    perf.requesting = valid
    perf.blocked_on = blocked
    perf.channels = every
    perf.outstanding = _outstanding(transactions, edges, OUTSTANDING_CAP)

    transferring = bytearray(n)
    at = {t: i for i, t in enumerate(edges)}
    for name in channels:
        scan = scans.get(name)
        for ev in scan.events if scan else ():
            i = at.get(ev.time)
            if i is not None:
                transferring[i] = 1
    perf.transferring = bytes(transferring)

    extra: dict[str, Sequence[Any]] = {
        "valid": valid,
        "ready": ready,
        "transfer": transferring,
        "outstanding": perf.outstanding,
        "in_reset": bytes(int(bool(x)) for x in in_reset),
    }
    # Bucket index comes from the rung's position in the *pack*, not from its
    # position in the surviving list, so dropping an unevaluable rung never
    # renumbers the ones below it.
    rungs = [(k + 2, r) for k, r in enumerate(pack.stall_reasons)]

    # Only what the cascade actually reads gets sampled — the same rule the
    # temporal rules follow, and the reason a pack with a dozen rungs costs no
    # more than the union of the wires they mention.
    wanted = {n_ for _, r in rungs for n_ in expr.identifiers(r.node)}
    paths = sorted({iface.signals[n_] for n_ in wanted if n_ in iface.signals})
    sampler.prefetch(list(paths))
    columns = {p: c for p in paths if (c := sampler.column(p)) is not None}

    # §8.17's arbitration rung is about a wire outside the interface, so a name
    # the interface does not own is looked up as a path in the trace before it
    # is called an error. Memoised, misses included: a name that is not a signal
    # either would otherwise be searched once per cycle.
    resolved: dict[str, Sequence[Any] | None] = {}

    def resolve(name: str) -> Sequence[Any] | None:
        if name not in resolved:
            col = sampler.column(name)
            if col is None and iface.scope:
                col = sampler.column(f"{iface.scope}.{name}")
            resolved[name] = col
        return resolved[name]

    labels = bytearray(n)
    memo: dict[tuple, int] = {}
    keyed = _memo_key(iface, rungs, columns, extra, resolve)
    other = len(buckets) - 1

    i = 0
    while i < n:
        if in_reset[i]:
            labels[i] = 0
            i += 1
            continue
        if transferring[i]:
            labels[i] = 1
            i += 1
            continue
        key = keyed(i) if keyed is not None else None
        if key is not None and (hit := memo.get(key)) is not None:
            labels[i] = hit
            i += 1
            continue

        label, failed = other, None
        for offset, rung in rungs:
            try:
                if expr.evaluate(rung.node, CycleEnv(iface, columns, i, {}, extra, resolve)):
                    label = offset
                    break
            except expr.ExprError as e:
                failed = (rung, str(e))
                break
        if failed is not None:
            # Drop the rung for the rest of the run — it cannot become evaluable
            # later — and redo this cycle, so the rungs *below* it still get
            # their chance at it. Losing a rung must not silently reassign
            # cycles that a lower one would have claimed.
            rung, why = failed
            perf.notes[rung.name] = why
            rungs = [r for r in rungs if r[1] is not rung]
            memo.clear()
            keyed = _memo_key(iface, rungs, columns, extra, resolve)
            continue

        labels[i] = label
        if key is not None:
            memo[key] = label
        i += 1

    perf.labels = bytes(labels)
    return perf


def _memo_key(
    iface: Interface,
    rungs: list[tuple[int, Any]],
    columns: dict[str, Any],
    extra: dict[str, Sequence[Any]],
    resolve: Any,
) -> Any:
    """A function from cycle index to a hashable key the cascade depends on.

    `None` when memoising would be unsound or pointless: an unbounded `$past(x,
    n)` cannot be keyed, and a cascade reading half the design would key on more
    values than it saves.
    """
    names: set[str] = set()
    depth = 0
    for _, rung in rungs:
        names |= expr.identifiers(rung.node)
        d = expr.lookback(rung.node)
        if d < 0:
            return None
        depth = max(depth, d)
    cols: list[Sequence[Any]] = []
    for name in sorted(names):
        path = iface.signals.get(name)
        col = columns.get(path) if path else extra.get(name)
        if col is None:
            col = resolve(name)
        if col is not None:
            cols.append(col)
    if not cols or len(cols) > MAX_MEMO_SIGNALS:
        return None

    def key(i: int) -> tuple:
        return tuple(
            _cell(c, j) for c in cols for j in range(i - depth, i + 1)
        )

    return key


def _cell(col: Sequence[Any], i: int) -> Any:
    if not 0 <= i < len(col):
        return None
    v = col[i]
    if v is None or isinstance(v, (int, str)):
        return v
    # `Value` is hashable and compares by content, so it can key the memo
    # directly — no need to render it to a string first.
    return v
