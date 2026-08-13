"""Minimal causal subtrace — §8.2.

The causal tree of §8.1 is complete and therefore large: a real chain is 30–200
nodes, most of them intermediate steps that a reader does not need. §8.2 asks a
narrower question — *which events actually explain the bug* — and answers it
with five steps over the tree:

1. collect every `(signal, time)` on the tree,
2. drop anything that is not a transition of that signal,
3. coalesce a signal repeated at the same value,
4. sort chronologically,
5. delta-debug: drop each event that the explanation survives without.

Two of those need saying out loud, because they are where an implementation can
quietly stop being honest.

**Step 2 snaps rather than deletes.** A node asks about `lock_r` at c1247, but
`lock_r` last moved at c13; c1247 is a sample, not an event. Deleting the node
would lose the fact, so the time is moved back to the transition that set the
value — which is also what makes the narrative read as a story instead of a
list of readings at one instant. A signal that never moved at all keeps the
start of the trace and is marked `held`, since "it has been 0 since before the
run" is a fact about the bug, not noise.

**Step 5 is a graph check, not a re-simulation.** §8.2 phrases it as "run why()
on E\\{e} and see whether it reaches the same terminal". There is no way to run
`why` against a *subset of events* — it reads the trace, not an event list — so
the same question is asked of the causal DAG the trace already produced: with
`e` removed, is there still a path from the symptom to the same root cause? If
yes, `e` was a detour. §8.3 step 4 uses exactly this device ("re-evaluare
simbolica pe graf") for the don't-care hypothesis, so it is the spec's own
notion of a symbolic re-check rather than a substitute invented here.

The result is the 5–12 line narrative of §8.2. When it comes out longer, it is
reported longer: a chain that genuinely needs 20 events is a fact about the
design, and truncating it to hit a number in the spec would be the one failure
mode P1 rules out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from veritrace.analysis.whytrace import CausalNode, NodeKind, root_cause

#: Delta-debugging is O(n²) in the number of events, and n is 20–50 after steps
#: 1–4 (§8.2). A truncated `why` on a pathological design can hand over more;
#: past this the pass is skipped and says so rather than stalling the UI.
MAX_DELTA_EVENTS = 250


@dataclass(slots=True)
class Event:
    """One line of the §8.2 narrative."""

    signal: str
    #: When the value was *established*, not when it was asked about — step 2.
    time: int
    cycle: int | None
    value: str
    #: What it was before, when the trace shows a transition there.
    prev: str | None
    #: `transition`, `held` (the value was never re-driven) or `terminal`.
    kind: str
    #: The `Reason` that put this node in the tree, so a consumer can pick a
    #: template without re-deriving it (§11.5).
    reason: str
    node_kind: str
    loc: dict[str, Any] | None
    detail: str
    #: True for the deepest terminal on the primary path — the root cause.
    is_root_cause: bool = False
    #: True for the event the question was asked about.
    is_symptom: bool = False
    #: Filled in by `narrate.describe`; empty until then.
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "time": self.time,
            "cycle": self.cycle,
            "value": self.value,
            "prev": self.prev,
            "kind": self.kind,
            "reason": self.reason,
            "node_kind": self.node_kind,
            "loc": self.loc,
            "detail": self.detail,
            "is_root_cause": self.is_root_cause,
            "is_symptom": self.is_symptom,
            "text": self.text,
        }


@dataclass(slots=True)
class Subtrace:
    events: list[Event] = field(default_factory=list)
    #: How many `(signal, time)` pairs steps 1–4 produced, before delta-debugging.
    considered: int = 0
    #: How many step 5 removed as redundant.
    dropped: int = 0
    #: False when the tree never reached a terminal that explains anything, so
    #: the narrative ends on the deepest step rather than on a cause (P1).
    reached_root_cause: bool = True
    #: Set when the event count made step 5 too expensive to run.
    delta_skipped: bool = False

    @property
    def root_cause(self) -> Event | None:
        return next((e for e in self.events if e.is_root_cause), None)

    @property
    def symptom(self) -> Event | None:
        return next((e for e in self.events if e.is_symptom), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [e.to_dict() for e in self.events],
            "considered": self.considered,
            "dropped": self.dropped,
            "reached_root_cause": self.reached_root_cause,
            "delta_skipped": self.delta_skipped,
        }


def _key(signal: str, t: int) -> tuple[str, int]:
    return (signal, t)


class _Snapper:
    """Step 2: a node's time, moved to the transition that set the value.

    Cached because a wide tree asks about the same signal at the same instant
    dozens of times, and each miss is a binary search in the store.
    """

    def __init__(self, store: Any, t0: int) -> None:
        self.store = store
        self.t0 = t0
        self._handles: dict[str, int | None] = {}
        self._cache: dict[tuple[str, int], tuple[int, bool]] = {}

    def handle(self, path: str) -> int | None:
        if path not in self._handles:
            self._handles[path] = self.store.find(path)
        return self._handles[path]

    def snap(self, path: str, t: int) -> tuple[int, bool]:
        """`(time, is_transition)` — when this value was established."""
        key = (path, t)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        h = self.handle(path)
        out: tuple[int, bool]
        if h is None:
            # Not in the dump: reconstructed from the graph (§7.3). The node's
            # own time is the only honest answer.
            out = (t, False)
        else:
            at = self.store.last_change_before(h, t + 1)
            if at is None:
                # Never moved in the whole run — the value predates the trace.
                out = (self.t0, False)
            else:
                out = (at, at == t)
        self._cache[key] = out
        return out

    def value_before(self, path: str, t: int) -> str | None:
        h = self.handle(path)
        if h is None:
            return None
        v = self.store.value_before(h, t)
        return str(v) if v is not None else None


def _collect(root: CausalNode) -> list[CausalNode]:
    """Step 1, breadth-first.

    Breadth-first on purpose: the same `(signal, time)` is reachable by several
    paths through the DAG, and the shallowest node is the one whose `reason` and
    source location describe the step the reader is following.
    """
    out: list[CausalNode] = []
    seen: set[int] = set()
    queue: list[CausalNode] = [root]
    seen.add(id(root))
    while queue:
        node = queue.pop(0)
        out.append(node)
        for child in node.children:
            if id(child) not in seen:
                seen.add(id(child))
                queue.append(child)
    return out


def _target(root: CausalNode) -> tuple[CausalNode, bool]:
    """The node the explanation has to reach, and whether it is a real cause.

    `root_cause` follows the primary path to the deepest terminal that answers
    anything (§8.1). When there is none — a chain that ran into a depth limit or
    a cycle — the deepest primary-path node stands in, and the caller says so
    rather than presenting a dead end as a conclusion.
    """
    found = root_cause(root)
    if found is not None:
        return found, True
    deepest = root
    seen: set[int] = {id(root)}
    while True:
        nxt = next((c for c in deepest.children if c.is_primary_path and id(c) not in seen), None)
        if nxt is None:
            return deepest, False
        seen.add(id(nxt))
        deepest = nxt


def _reaches(root: CausalNode, target: CausalNode, allowed: set[tuple[str, int]], key_of) -> bool:
    """Is there still a path from `root` to `target` using only `allowed`?

    Step 5's test. Iterative rather than recursive: a deep chain on a pipelined
    design outruns the interpreter's stack, and this runs once per candidate
    event.
    """
    if key_of(root) not in allowed:
        return False
    stack = [root]
    seen: set[int] = {id(root)}
    while stack:
        node = stack.pop()
        if node is target:
            return True
        for child in node.children:
            if id(child) in seen or key_of(child) not in allowed:
                continue
            seen.add(id(child))
            stack.append(child)
    return False


def minimise(
    root: CausalNode,
    store: Any,
    clock: Any = None,
    delta: bool = True,
) -> Subtrace:
    """The §8.2 subtrace of a causal tree.

    `delta=False` skips step 5, which the UI uses while a chain is still being
    explored: steps 1–4 are linear and steps 1–5 are quadratic, and the answer
    only differs by events that were already redundant.
    """
    t0, _t1 = store.time_range
    snapper = _Snapper(store, t0)
    nodes = _collect(root)
    target, reached = _target(root)

    # Steps 1 and 2: every node, at the time its value was established.
    snapped: dict[int, tuple[str, int]] = {}
    for node in nodes:
        at, _is_transition = snapper.snap(node.signal.path(), node.time)
        snapped[id(node)] = _key(node.signal.path(), at)

    def key_of(node: CausalNode) -> tuple[str, int]:
        got = snapped.get(id(node))
        if got is None:
            # Reachable only through a repeated stub the collection never
            # expanded; snap it now rather than treat it as absent.
            at, _ = snapper.snap(node.signal.path(), node.time)
            got = _key(node.signal.path(), at)
            snapped[id(node)] = got
        return got

    # One event per distinct key, described by the shallowest node that reached
    # it (`_collect` is breadth-first, so first wins).
    first: dict[tuple[str, int], CausalNode] = {}
    for node in nodes:
        first.setdefault(key_of(node), node)

    # Step 3: a signal that appears twice at the same value is one fact. Keep
    # the earliest occurrence — that is when it became true.
    by_signal_value: dict[tuple[str, str], int] = {}
    keep: dict[tuple[str, int], CausalNode] = {}
    for key in sorted(first, key=lambda k: (k[1], k[0])):
        node = first[key]
        sig_val = (key[0], node.value)
        earlier = by_signal_value.get(sig_val)
        if earlier is not None and earlier <= key[1]:
            continue
        by_signal_value[sig_val] = key[1]
        keep[key] = node

    # The symptom and the root cause are never dropped: without the first there
    # is no question, and without the second there is no answer.
    pinned = {key_of(root), key_of(target)}
    for key in pinned:
        keep.setdefault(key, first.get(key, root))

    considered = len(keep)
    allowed = set(keep)
    dropped = 0
    skipped = False

    # Step 5: delta-debugging. Chronological order so the removals a reader
    # would question — the early ones, which set the scene — are tried first.
    if delta and considered <= MAX_DELTA_EVENTS:
        for key in sorted(allowed - pinned, key=lambda k: (k[1], k[0])):
            trial = allowed - {key}
            if _reaches(root, target, trial, key_of):
                allowed = trial
                dropped += 1
    elif delta:
        skipped = True

    events: list[Event] = []
    for key in sorted(allowed, key=lambda k: (k[1], k[0])):
        node = keep[key]
        path, at = key
        is_transition = at == node.time and node.kind is not NodeKind.HOLD
        held = not is_transition
        events.append(
            Event(
                signal=path,
                time=at,
                cycle=clock.cycle_of(at) if clock is not None else None,
                value=node.value,
                prev=snapper.value_before(path, at) if is_transition else None,
                kind=(
                    "terminal"
                    if node.kind is NodeKind.TERMINAL
                    else ("held" if held else "transition")
                ),
                reason=node.reason.value,
                node_kind=node.kind.value,
                loc=(
                    {"file": node.loc.file, "line": node.loc.line, "col": node.loc.col}
                    if node.loc
                    else None
                ),
                detail=node.detail,
                is_root_cause=key == key_of(target) and reached,
                is_symptom=key == key_of(root),
            )
        )

    return Subtrace(
        events=events,
        considered=considered,
        dropped=dropped,
        reached_root_cause=reached,
        delta_skipped=skipped,
    )


