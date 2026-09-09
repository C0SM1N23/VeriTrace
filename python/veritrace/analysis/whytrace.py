"""Backward causal slicing — §8.1.

Answers "why is S == V at time T" by walking the design graph backwards against
the trace. Two things make the answers trustworthy rather than plausible:

* **The time model of §5.5.** A sequential signal is explained at the clock edge
  that produced it, and its inputs are read with `value_before` — the values as
  they were *going into* the edge. Reading them at the edge gives the absurd
  "state is WAIT because next_state is WAIT" when next_state has already moved
  on.
* **Pruning by relevance.** For `a & b == 0` only the zero operands are to
  blame; for a mux only the selected branch is. Without it the cone explodes.

Ordering uses the oldest-last-transition heuristic (§8.1): a signal that has not
moved in a long time is the suspect, one that toggles every cycle is a
consequence. That is presentation only — every branch is still in the tree, so
the result stays complete and deterministic (P1).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from veritrace.graph.conditions import BV, evaluate
from veritrace.graph.model import (
    Binary,
    Concat,
    Const,
    DesignGraph,
    Driver,
    Expr,
    Kind,
    Ref,
    Reduce,
    Signal,
    SignalId,
    Slice,
    SourceLoc,
    Ternary,
    Unary,
    refs,
    to_text,
)

MAX_DEPTH = 40
#: A real design converges well inside this (§8.1); the cap is a safety net.
MAX_NODES = 2000


class NodeKind(Enum):
    ASSIGNED = "assigned"
    HOLD = "hold"
    CONFLICT = "conflict"
    TERMINAL = "terminal"
    #: §8.16 — the chain crossed from a signal into the transaction that signal
    #: belongs to. The one node in the tree that is not about a signal.
    TXN_LINK = "txn_link"
    #: §11.4's why-not interaction: this value is wanted, but the conditions
    #: that would produce it are not currently true.
    COUNTERFACTUAL = "counterfactual"


class Reason(Enum):
    PRIMARY_INPUT = "primary_input"
    CONSTANT = "constant"
    UNKNOWN_X = "unknown_x"
    UNDRIVEN = "undriven"
    CONFLICT = "conflict"
    CYCLE = "cycle"
    DEPTH_LIMIT = "depth_limit"
    BLACKBOX_IP = "blackbox_ip"
    NOT_TRACED = "not_traced"
    #: §8.11b — the chain reached the first sample of an on-board capture. The
    #: cause is before the window, which is a fact about the capture and not
    #: about the design, so it is its own terminal.
    CAPTURE_BOUNDARY = "capture_boundary"
    ASSIGNED = "assigned"
    HOLD = "hold"
    COUNTERFACTUAL = "counterfactual"

    # §8.5 — where an X actually came from. `UNKNOWN_X` stays as the answer of
    # last resort: it says "this is X", these say why, which is the difference
    # between a report of 200 X's and a report of two causes.
    UNINITIALIZED_REG = "uninitialized_reg"
    UNCONNECTED_PORT = "unconnected_port"
    OUT_OF_RANGE = "out_of_range"
    X_FROM_ARITH = "x_from_arith"
    TRISTATE_Z = "tristate_z"

    # §8.16 — a correlated transaction, on this interface or another.
    TXN_OPEN = "txn_open"
    TXN_IN_FLIGHT = "txn_in_flight"


#: The §8.5 terminals. Membership is what makes a node a *root cause* of an X
#: rather than a step on the way to one.
X_TERMINALS = frozenset(
    {
        Reason.UNINITIALIZED_REG,
        Reason.UNCONNECTED_PORT,
        Reason.OUT_OF_RANGE,
        Reason.X_FROM_ARITH,
        Reason.TRISTATE_Z,
        Reason.UNKNOWN_X,
        # Two drivers disagreeing resolve to X, and the conflict *is* the
        # origin — the walk stops there with a complete explanation. Leaving it
        # out made every X caused by a multi-driver invisible to §8.5, which is
        # the one X whose cause the tool can state with certainty.
        Reason.CONFLICT,
    }
)

X_TERMINAL_DETAIL: dict[Reason, str] = {
    Reason.UNINITIALIZED_REG: "register with no reset, never written",
    Reason.UNCONNECTED_PORT: "instance port left unconnected",
    Reason.OUT_OF_RANGE: "array index outside the declared bounds",
    Reason.X_FROM_ARITH: "arithmetic on an unknown operand",
    Reason.TRISTATE_Z: "tri-state net with no enable active",
    Reason.UNKNOWN_X: "unknown, and the cause is not in the trace",
    Reason.CONFLICT: "more than one driver active at the same time",
}


@dataclass(slots=True)
class CausalNode:
    signal: SignalId
    time: int
    value: str
    kind: NodeKind
    reason: Reason
    loc: SourceLoc | None = None
    last_change: int | None = None
    children: list["CausalNode"] = field(default_factory=list)
    #: True on the branch the heuristic ranks first. Presentation only.
    is_primary_path: bool = True
    #: Guard or value expression that produced this step, as text.
    detail: str = ""
    #: `m1.WRITE[7]` on a TXN_LINK node — §8.16.
    txn: str | None = None
    #: True when the value was *computed* from the drivers instead of read from
    #: the trace (§7.3). P2 applies here too: inference presented as measurement
    #: is the one thing a causal chain must never do.
    derived: bool = False
    #: Width at this point in the elaborated graph.  The browser cannot infer
    #: it from ``value`` (an unknown 32-bit bus is rendered simply as ``x``),
    #: and reconstructed waveform rows need the real width to format values.
    width: int = 1

    def walk(self, seen: set[int] | None = None) -> Iterable["CausalNode"]:
        """Every distinct node, once.

        The result of `why` is a DAG, not a tree: identical (signal, time)
        questions are memoised and the same node is reachable by many paths. A
        wide mux or a shared bus makes that sharing dense, so expanding it
        naively is exponential — a real chain measured 31 KB of repeated
        subtrees before this guard existed.
        """
        seen = set() if seen is None else seen
        if id(self) in seen:
            return
        seen.add(id(self))
        yield self
        for c in self.children:
            yield from c.walk(seen)

    def to_dict(self, seen: set[int] | None = None) -> dict:
        """Serialise once per distinct node.

        A node reached a second time is emitted as a stub with `repeated: true`
        and no children: the client already has the full node, and re-sending
        the subtree is how a 40-node answer becomes a megabyte of JSON.
        """
        seen = set() if seen is None else seen
        repeated = id(self) in seen
        seen.add(id(self))
        if repeated:
            return {
                "signal": self.signal.path(),
                "time": self.time,
                "value": self.value,
                "kind": self.kind.value,
                "reason": self.reason.value,
                "loc": None,
                "last_change": self.last_change,
                "is_primary_path": self.is_primary_path,
                "detail": self.detail,
                "txn": self.txn,
                "derived": self.derived,
                "width": self.width,
                "repeated": True,
                "children": [],
            }
        return {
            "signal": self.signal.path(),
            "time": self.time,
            "value": self.value,
            "kind": self.kind.value,
            "reason": self.reason.value,
            "loc": (
                {"file": self.loc.file, "line": self.loc.line, "col": self.loc.col}
                if self.loc
                else None
            ),
            "last_change": self.last_change,
            "is_primary_path": self.is_primary_path,
            "detail": self.detail,
            "txn": self.txn,
            "derived": self.derived,
            "width": self.width,
            "repeated": False,
            "children": [c.to_dict(seen) for c in self.children],
        }


#: Terminals that answer "why". Anything else is a step on the way there, so a
#: chain ending elsewhere has not reached a root cause and must say so.
ROOT_REASONS = frozenset(
    {
        Reason.CONSTANT,
        Reason.PRIMARY_INPUT,
        Reason.UNDRIVEN,
        Reason.CONFLICT,
        Reason.BLACKBOX_IP,
        # §8.11b: "the cause is before the capture window" is an answer — the
        # honest one — not a walk that gave up halfway.
        Reason.CAPTURE_BOUNDARY,
        Reason.UNINITIALIZED_REG,
        Reason.UNCONNECTED_PORT,
        Reason.OUT_OF_RANGE,
        Reason.X_FROM_ARITH,
        Reason.TRISTATE_Z,
    }
)


def _spine(node: "CausalNode") -> list["CausalNode"]:
    """The primary path, symptom first — the same one `root_cause` ends on.

    A DAG has no single "the chain", so this is the branch the §8.1 heuristic
    ranked first at every step: what the Causal tab opens on, and what a reader
    is being pointed at. Every other branch is still under `root`.
    """
    out: list["CausalNode"] = []
    cur: "CausalNode | None" = node
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = next((c for c in cur.children if c.is_primary_path), None)
    return out


def root_cause(node: "CausalNode") -> "CausalNode | None":
    """The deepest terminal on the primary path — what the chain concluded.

    Follows `is_primary_path`, so this is the same node the Causal tab opens
    on: a report and the UI disagreeing about the root cause would be worse
    than either being wrong alone. `None` means the walk never reached a
    terminal that explains anything, which callers report rather than hide.
    """
    best: "CausalNode | None" = None
    cur: "CausalNode | None" = node
    while cur is not None:
        if cur.reason in ROOT_REASONS:
            best = cur
        if cur.reason is Reason.CONFLICT:
            # §8.1: more than one active driver is "bug in sine" — the answer,
            # not a step towards one. Its children are the two drivers' inputs,
            # shown for context; concluding "a is a primary input" from them
            # would name something that is not the problem.
            return cur
        cur = next((c for c in cur.children if c.is_primary_path), None)
    return best


def bv_from_bits(bits: str, width: int) -> BV:
    """Canonical VCD digits -> four-state value."""
    if not bits:
        return BV.unknown(width)
    lead = bits[0]
    fill = lead if lead in "xzXZ" else "0"
    full = fill * max(0, width - len(bits)) + bits
    full = full[-width:] if width else full
    v = x = 0
    for i, ch in enumerate(reversed(full)):
        if ch in "1":
            v |= 1 << i
        elif ch in "xXzZ":
            x |= 1 << i
    return BV(v, x, max(width, 1))


class TraceView:
    """The trace as why-trace needs it: values, edges and change times.

    Wraps the native store so the analysis never learns about handles, and
    caches reads — the same (signal, time) is asked for many times while a tree
    is built.
    """

    def __init__(self, graph: DesignGraph, store) -> None:
        self.graph = graph
        self.store = store
        self._cache: dict[tuple[str, int, bool], BV | None] = {}
        self._last_change_cache: dict[tuple[str, int], int | None] = {}

    def signal(self, sid: SignalId) -> Signal | None:
        return self.graph.get(sid.path())

    def value(self, sid: SignalId, t: int, before: bool = False) -> BV | None:
        key = (sid.path(), t, before)
        if key in self._cache:
            return self._cache[key]
        out = self._read(sid, t, before)
        self._cache[key] = out
        return out

    def bits(self, sid: SignalId, t: int) -> str | None:
        """Raw four-state digits, X and Z still distinguishable.

        `BV` folds Z into the unknown plane because that is what evaluation
        needs, but §8.5 has to tell a floating tri-state bus from an
        uninitialised register, and only the digits carry that.
        """
        sig = self.signal(sid)
        if sig is None or sig.trace_handle is None:
            return None
        val = self.store.value_at(sig.trace_handle, t)
        return val.bits if val is not None else None

    def _read(self, sid: SignalId, t: int, before: bool) -> BV | None:
        sig = self.signal(sid)
        if sig is None:
            return None
        if sig.trace_handle is not None:
            fn = self.store.value_before if before else self.store.value_at
            val = fn(sig.trace_handle, t)
            return bv_from_bits(val.bits, sig.width) if val is not None else None
        # Not dumped, but combinationally derivable from the graph (§7.3).
        # Sequential state and memories cannot be recovered from their current
        # inputs; doing so invents history that the trace never recorded.
        if sig.is_reconstructible:
            return self._reconstruct(sig, t, before)
        return None

    def _reconstruct(self, sig: Signal, t: int, before: bool) -> BV | None:
        """Evaluate the active driver's expression instead of observing."""
        for d in sig.drivers:
            g = evaluate(d.guard, lambda s: self.value(s, t, before)).truthy()
            if not g.x and g.v:
                return evaluate(d.value, lambda s: self.value(s, t, before))
        return None

    def last_change(self, sid: SignalId, t: int) -> int | None:
        key = (sid.path(), t)
        if key in self._last_change_cache:
            return self._last_change_cache[key]
        sig = self.signal(sid)
        if sig is None or sig.trace_handle is None:
            result = None
        else:
            result = self.store.last_change_before(sig.trace_handle, t)
        self._last_change_cache[key] = result
        return result

    def last_changes(self, signals: list[SignalId], t: int) -> dict[str, int | None]:
        """Last transitions for a cone, crossing Python/Rust only once when wide.

        A normal causal step has two or three inputs and stays on the targeted
        query.  Generated logic, arbiters and decode trees can have hundreds;
        making one PyO3/Parquet call per input dominated the entire 200-node
        budget.  At that width the store's parallel all-signal scan is cheaper
        and also fills this view's ordinary cache, so later recursion is free.
        """
        unique = {signal.path(): signal for signal in signals}
        missing = [
            signal
            for path, signal in unique.items()
            if (path, t) not in self._last_change_cache
        ]
        if len(missing) >= 64:
            bulk = self.store.last_change_all(t)
            for signal in missing:
                sig = self.signal(signal)
                handle = sig.trace_handle if sig is not None else None
                value = (
                    bulk[handle]
                    if handle is not None and 0 <= handle < len(bulk)
                    else None
                )
                self._last_change_cache[(signal.path(), t)] = value
        else:
            for signal in missing:
                self.last_change(signal, t)
        return {
            path: self._last_change_cache.get((path, t))
            for path in unique
        }

    def first_x(self, sid: SignalId) -> int | None:
        sig = self.signal(sid)
        if sig is None or sig.trace_handle is None:
            return None
        return self.store.first_x(sig.trace_handle)

    def is_constant(self, sid: SignalId, t0: int, t1: int) -> bool:
        sig = self.signal(sid)
        if sig is None or sig.trace_handle is None:
            return False
        return self.store.is_constant(sig.trace_handle, t0, t1)

    def last_posedge(self, clock: SignalId, t: int) -> int | None:
        """Most recent rising edge of `clock` at or before `t`.

        Two steps at most: the last change is either the rise itself or the
        fall that followed it.
        """
        sig = self.signal(clock)
        if sig is None or sig.trace_handle is None:
            return None
        h = sig.trace_handle
        at = self.store.last_change_before(h, t + 1)
        for _ in range(2):
            if at is None:
                return None
            v = self.store.value_at(h, at)
            if v is not None and v.to_int() == 1:
                return at
            at = self.store.last_change_before(h, at)
        return None


# --- relevance pruning (§8.1) ----------------------------------------------


def relevant_refs(expr: Expr, read: Callable[[SignalId], BV | None]) -> list[SignalId]:
    """Signals that actually determined `expr`'s value.

    For a mux only the selected branch matters; for `a & b == 0` only the zero
    operands do. Everything else is noise in the causal chain.
    """
    out: list[SignalId] = []
    _relevant(expr, read, out)
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [s for s in out if not (s.path() in seen or seen.add(s.path()))]


def _relevant(e: Expr, read, out: list[SignalId], known: BV | None = None) -> None:
    match e:
        case Ref(signal=s):
            out.append(s)
        case Ternary(cond=c, then=t, other=o):
            _relevant(c, read, out)
            cv = evaluate(c, read).truthy()
            if cv.x:
                _relevant(t, read, out)
                _relevant(o, read, out)
            else:
                _relevant(t if cv.v else o, read, out)
        case Binary(op=op, lhs=l, rhs=r) if op in ("&", "&&", "|", "||"):
            res = (known if known is not None else evaluate(e, read)).truthy()
            controlling = 0 if op in ("&", "&&") else 1
            if not res.x and res.v == controlling:
                # Only the operands forcing the result are to blame.
                l_raw, r_raw = evaluate(l, read), evaluate(r, read)
                for sub, raw in ((l, l_raw), (r, r_raw)):
                    val = raw.truthy()
                    if val.x or val.v == controlling:
                        _relevant(sub, read, out, raw)
                return
            if not res.x:
                # A known non-controlling result proves the truth value of
                # both operands: 0 from OR means both are zero; 1 from AND
                # means both are non-zero. Propagating that fact avoids
                # re-evaluating every left-associated prefix of a wide guard.
                # The old walk was O(n^2), making the specified 200-node
                # causal query miss its latency budget despite doing no extra
                # useful work.
                _relevant(l, read, out, res)
                _relevant(r, read, out, res)
                return
            _relevant(l, read, out)
            _relevant(r, read, out)
        case Binary(lhs=l, rhs=r):
            _relevant(l, read, out)
            _relevant(r, read, out)
        case Unary(arg=a) | Reduce(arg=a):
            _relevant(a, read, out)
        case Concat(parts=ps):
            for p in ps:
                _relevant(p, read, out)
        case Slice(arg=a, msb=m, lsb=l):
            _relevant(a, read, out)
            # The index is a cause too. `mem[rd_ptr]` reading the wrong word is
            # a `rd_ptr` bug as often as a memory bug, and before this the
            # index never appeared in the chain at all. Constant bounds
            # contribute nothing, so an ordinary `x[7:0]` is unaffected.
            _relevant(m, read, out)
            if not _same(m, l):
                _relevant(l, read, out)
        case _:
            out.extend(refs(e))


def _same(a: Expr, b: Expr) -> bool:
    """Whether two sub-expressions are the identical node — an element select.

    `mem[i]` elaborates to `Slice(mem, msb=i, lsb=i)`, so msb and lsb being the
    same expression is what distinguishes selecting one element from taking a
    range of bits.
    """
    return a is b or a == b


def mem_selects(e: Expr, view: "TraceView") -> list[tuple[Signal, Expr]]:
    """Every `mem[index]` in an expression, as (memory signal, index) — §5.6.

    A dynamically indexed memory read is not an ordinary reference: the answer
    is in one element, and that element was written at some earlier time. Both
    facts have to be recovered before the walk can continue, so they are found
    here and handled by `_mem_read` rather than falling into `relevant_refs`,
    which would yield the array as a whole and explain nothing.
    """
    out: list[tuple[Signal, Expr]] = []

    def walk(n: Expr) -> None:
        match n:
            case Slice(arg=Ref(signal=s), msb=m, lsb=l) if _same(m, l):
                sig = view.signal(s)
                if sig is not None and (sig.kind is Kind.MEM or sig.elements):
                    out.append((sig, m))
                    walk(m)  # the index has its own causes
                    return
                walk(m)
            case Slice(arg=a, msb=m, lsb=l):
                walk(a)
                walk(m)
                walk(l)
            case Binary(lhs=l, rhs=r):
                walk(l)
                walk(r)
            case Unary(arg=a) | Reduce(arg=a):
                walk(a)
            case Ternary(cond=c, then=t, other=o):
                walk(c)
                walk(t)
                walk(o)
            case Concat(parts=ps):
                for p in ps:
                    walk(p)

    walk(e)
    return out


# --- X terminals (§8.5) ----------------------------------------------------


def _out_of_range(e: Expr, read: Callable[[SignalId], BV | None], view: "TraceView") -> bool:
    """A select whose index is outside the declared bounds of what it indexes.

    Evaluated against the trace, not guessed from the declaration: an index
    only goes out of range for particular values, which is exactly why the bug
    survives review and shows up as an X mid-run.
    """
    match e:
        case Slice(arg=Ref(signal=s), msb=m, lsb=l):
            sig = view.signal(s)
            if sig is None:
                return False
            # A memory indexes over its *elements*, anything else over its bits.
            # For a memory the bound is the declared depth and nothing else:
            # `width` is one element (so `mem[8]` on a 64-entry array of bytes
            # would read as out of range) and `elements` is only what the
            # simulator chose to dump (so a truncated `--trace-max-array` would
            # do the same). Without a declared depth no claim is made — calling
            # a legal index illegal is worse than saying nothing.
            if sig.kind is Kind.MEM or sig.elements:
                if not sig.depth:
                    return any(_out_of_range(x, read, view) for x in (m, l))
                limit = sig.depth - 1
            else:
                limit = sig.width - 1
            for idx in (evaluate(m, read).to_int(), evaluate(l, read).to_int()):
                if idx is not None and not 0 <= idx <= limit:
                    return True
            return any(_out_of_range(x, read, view) for x in (m, l))
        case Slice(arg=a, msb=m, lsb=l):
            return any(_out_of_range(x, read, view) for x in (a, m, l))
        case Binary(lhs=lhs, rhs=rhs):
            return _out_of_range(lhs, read, view) or _out_of_range(rhs, read, view)
        case Unary(arg=a) | Reduce(arg=a):
            return _out_of_range(a, read, view)
        case Ternary(cond=c, then=t, other=o):
            return any(_out_of_range(x, read, view) for x in (c, t, o))
        case Concat(parts=ps):
            return any(_out_of_range(p, read, view) for p in ps)
        case _:
            return False


#: Operators where an unknown operand poisons the whole result — §8.5's
#: `X_FROM_ARITH`. Bitwise and logical operators are excluded: they have
#: controlling values, so `0 & x` is 0 and no X was created.
ARITH_OPS = frozenset({"+", "-", "*", "/", "%", "<<", ">>", "<<<", ">>>", "**"})


def _x_from_arith(e: Expr, read: Callable[[SignalId], BV | None]) -> bool:
    match e:
        case Binary(op=op, lhs=lhs, rhs=rhs):
            if op in ARITH_OPS and not evaluate(e, read).known:
                if not evaluate(lhs, read).known or not evaluate(rhs, read).known:
                    return True
            return _x_from_arith(lhs, read) or _x_from_arith(rhs, read)
        case Unary(arg=a) | Reduce(arg=a) | Slice(arg=a):
            return _x_from_arith(a, read)
        case Ternary(cond=c, then=t, other=o):
            return any(_x_from_arith(x, read) for x in (c, t, o))
        case Concat(parts=ps):
            return any(_x_from_arith(p, read) for p in ps)
        case _:
            return False


def classify_x(
    sig: Signal,
    sid: SignalId,
    t: int,
    view: "TraceView",
    read: Callable[[SignalId], BV | None],
) -> Reason:
    """Why this signal is X, per the terminal table of §8.5.

    Tested most specific first. `UNKNOWN_X` is the honest fallback: the value
    is unknown and the tool cannot say more, which beats picking the
    nearest-looking cause (P1).
    """
    bits = view.bits(sid, t)
    if bits is not None and "z" in bits.lower() and "x" not in bits.lower():
        # Pure Z is a floating net, not a corrupted computation.
        return Reason.TRISTATE_Z
    if sig.unconnected:
        return Reason.UNCONNECTED_PORT
    if not sig.drivers:
        # A register with no driver at all is undriven, not uninitialised.
        return Reason.UNKNOWN_X

    active = [d for d in sig.drivers if _guard_true(d, read)]
    for d in active:
        if _out_of_range(d.value, read, view):
            return Reason.OUT_OF_RANGE
    for d in active:
        if _x_from_arith(d.value, read):
            return Reason.X_FROM_ARITH

    # A flop with no reset that has been X since the first sample was never
    # written: its value is whatever the simulator started it at.
    if sig.kind is Kind.REG and any(d.is_sequential for d in sig.drivers):
        if not any(d.reset is not None for d in sig.drivers):
            first = view.first_x(sid)
            if first is not None and view.last_change(sid, t) in (None, first):
                return Reason.UNINITIALIZED_REG
    return Reason.UNKNOWN_X


def _guard_true(d: Driver, read) -> bool:
    g = evaluate(d.guard, read).truthy()
    return (not g.x) and bool(g.v)


def effective_time(view: "TraceView", sig: Signal, t: int) -> tuple[int, bool]:
    """When to read a signal's inputs, and whether to read them *before* — §5.5.

    A combinational signal is explained at `t`. A sequential one is explained at
    the clock edge that produced it, with its inputs as they were going *into*
    that edge — otherwise the answer is the absurd "state is WAIT because
    next_state is WAIT" after next_state has already moved on.

    Public because §8.3's don't-care check has to re-evaluate the same drivers at
    the same instants: evaluating them one edge out would silently declare live
    inputs to be don't-cares.
    """
    if not any(d.is_sequential for d in sig.drivers):
        return t, False
    clock = next((d.clock for d in sig.drivers if d.clock), None)
    edge = view.last_posedge(clock, t) if clock else None
    return (edge, True) if edge is not None else (t, False)


def falsifying_terms(expr: Expr, read: Callable[[SignalId], BV | None]) -> list[SignalId]:
    """Signals responsible for `expr` being false.

    AND: the false operands. OR: all of them, since all are false. NOT: the
    operand, which must be true.
    """
    out: list[SignalId] = []
    _falsify(expr, read, out)
    seen: set[str] = set()
    return [s for s in out if not (s.path() in seen or seen.add(s.path()))]


def _falsify(e: Expr, read, out: list[SignalId]) -> None:
    match e:
        case Binary(op=op, lhs=l, rhs=r) if op in ("&", "&&"):
            for sub in (l, r):
                v = evaluate(sub, read).truthy()
                if v.x or not v.v:
                    _falsify(sub, read, out)
        case Binary(op=op, lhs=l, rhs=r) if op in ("|", "||"):
            _falsify(l, read, out)
            _falsify(r, read, out)
        case Unary(op="!", arg=a) | Unary(op="~", arg=a):
            # `!a` is false because `a` is true.
            _relevant(a, read, out)
        case Ternary(cond=c, then=t, other=o):
            cv = evaluate(c, read).truthy()
            _relevant(c, read, out)
            if cv.x:
                _falsify(t, read, out)
                _falsify(o, read, out)
            else:
                _falsify(t if cv.v else o, read, out)
        case _:
            _relevant(e, read, out)


# --- the algorithm ---------------------------------------------------------


@dataclass(slots=True)
class WhyResult:
    root: CausalNode
    nodes: int
    elapsed_ms: float
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "root": self.root.to_dict(),
            # §13's own example is `--json | jq '.chain[0].loc'`: the spine from
            # the symptom down to the root cause, flat, for a shell that should
            # not have to walk a tree. The tree stays under `root` — this is the
            # same nodes in the order someone reads them, not a second answer.
            "chain": [
                {
                    "signal": n.signal.path(),
                    "time": n.time,
                    "value": n.value,
                    "reason": n.reason.value,
                    "loc": (
                        {"file": n.loc.file, "line": n.loc.line, "col": n.loc.col}
                        if n.loc
                        else None
                    ),
                    "detail": n.detail,
                    "derived": n.derived,
                }
                for n in _spine(self.root)
            ],
            "stats": {
                "nodes": self.nodes,
                "ms": round(self.elapsed_ms, 2),
                "truncated": self.truncated,
            },
        }


class WhyTracer:
    def __init__(
        self,
        graph: DesignGraph,
        store,
        max_depth: int = MAX_DEPTH,
        txn_index: Any = None,
        capture_start: int | None = None,
        on_node: Callable[[CausalNode], None] | None = None,
    ) -> None:
        self.graph = graph
        self.store = store
        #: §8.11b. Set to the first sample time when the trace is an on-board
        #: capture rather than a simulation: the window has a front edge, and a
        #: chain that reaches it has run out of evidence, not out of design.
        self.capture_start = capture_start
        self.view = TraceView(graph, store)
        self.max_depth = max_depth
        #: §8.16. `None` means no protocol pack matched this design, and the
        #: walk stays purely at signal level — which is the correct answer, not
        #: a degraded one.
        self.txn_index = txn_index
        # Optional progress observer used by the WebSocket API (§10.2).  It is
        # called only after a node and all of its children are complete, so a
        # client never receives a half-populated object.  The analysis itself
        # does not depend on the observer; REST/CLI callers leave it unset.
        self.on_node = on_node
        self._memo: dict[tuple[str, int], CausalNode] = {}
        self._stack: set[str] = set()
        self._linked: set[str] = set()
        self._count = 0

    # -- entry point -----------------------------------------------------

    def why(self, signal: str | SignalId, t: int) -> WhyResult:
        sid = SignalId.parse(signal) if isinstance(signal, str) else signal
        self._memo.clear()
        self._stack.clear()
        self._linked.clear()
        self._count = 0
        started = _time.perf_counter()
        root = self._why(sid, t, 0)
        return WhyResult(
            root=root,
            nodes=self._count,
            elapsed_ms=(_time.perf_counter() - started) * 1000.0,
            truncated=self._count >= MAX_NODES,
        )

    def why_not(self, signal: str | SignalId, t: int, desired: BV) -> WhyResult:
        """Explain which real conditions prevented ``signal`` becoming desired.

        This is §11.4's counterfactual operation, not an expectation note on a
        normal ``why``.  Drivers whose value evaluates to the requested value
        are candidates; their false/unknown guard terms are the causes.  If no
        driver can produce the value under the observed inputs, the active
        value expression is traced instead and the result says so explicitly.
        No input is changed and no alternate simulation is invented (P1).
        """
        sid = SignalId.parse(signal) if isinstance(signal, str) else signal
        self._memo.clear()
        self._stack.clear()
        self._linked.clear()
        self._count = 0
        started = _time.perf_counter()

        sig = self.view.signal(sid)
        if sig is None:
            root = self._node(sid, t, NodeKind.TERMINAL, Reason.NOT_TRACED)
            root.detail = f"cannot ask for {desired}: the signal is not in the RTL graph"
        else:
            actual = self.view.value(sid, t)
            if actual == desired:
                # The counterfactual is already factual. Return the ordinary
                # causal answer, but keep a precise note at its root.
                ordinary = self._why(sid, t, 0)
                ordinary.detail = (
                    f"the requested value {desired} is already observed; " + ordinary.detail
                ).rstrip()
                root = ordinary
            else:
                t_eff, child_before = effective_time(self.view, sig, t)
                read = lambda s: self.view.value(s, t_eff, child_before)  # noqa: E731
                candidates: list[tuple[Driver, BV]] = []
                active: list[Driver] = []
                for driver in sig.drivers:
                    guard = evaluate(driver.guard, read).truthy()
                    if guard.known and guard.v:
                        active.append(driver)
                    value = evaluate(driver.value, read)
                    if value == desired and (guard.x or not guard.v):
                        candidates.append((driver, guard))

                root = self._node(
                    sid,
                    t,
                    NodeKind.COUNTERFACTUAL,
                    Reason.COUNTERFACTUAL,
                    loc=(candidates[0][0].loc if candidates else sig.decl_loc),
                )
                root.detail = f"wanted {desired}; observed {actual if actual is not None else '?'}"

                causes: list[SignalId] = []
                if candidates:
                    root.detail += "; these guard conditions prevented the assignment"
                    for driver, guard in candidates:
                        causes.extend(
                            relevant_refs(driver.guard, read)
                            if guard.x
                            else falsifying_terms(driver.guard, read)
                        )
                elif active:
                    root.detail += "; no active driver evaluated to the requested value"
                    for driver in active:
                        causes.extend(relevant_refs(driver.value, read))
                else:
                    root.detail += "; no driver can currently produce the requested value"
                    for driver in sig.drivers:
                        causes.extend(relevant_refs(driver.guard, read))

                seen: set[str] = set()
                causes = [
                    cause
                    for cause in causes
                    if not (cause.path() in seen or seen.add(cause.path()))
                ]
                root.children = self._children(causes, t_eff, 0, child_before)
                if self.on_node is not None:
                    self.on_node(root)

        return WhyResult(
            root=root,
            nodes=self._count,
            elapsed_ms=(_time.perf_counter() - started) * 1000.0,
            truncated=self._count >= MAX_NODES,
        )

    # -- internals -------------------------------------------------------

    def _node(self, sid, t, kind, reason, before: bool = False, **kw) -> CausalNode:
        self._count += 1
        # §5.5: when a sequential parent explained this signal at its clock
        # edge, the value that mattered is the one going *into* the edge. Showing
        # the post-edge value here is the display half of "state is WAIT because
        # next_state is WAIT" — the node would contradict the reasoning above it.
        val = self.view.value(sid, t, before)
        sig = self.view.signal(sid)
        return CausalNode(
            signal=sid,
            time=t,
            value=str(val) if val is not None else "?",
            kind=kind,
            reason=reason,
            last_change=self.view.last_change(sid, t),
            # §7.3: this value was evaluated from the graph, not observed. The
            # user has to be able to tell the two apart.
            derived=sig is not None and sig.is_reconstructible,
            width=sig.width if sig is not None else 1,
            **kw,
        )

    def _why(self, sid: SignalId, t: int, depth: int, before: bool = False) -> CausalNode:
        key = (sid.path(), t, before)
        if key in self._memo:
            return self._memo[key]
        if depth > self.max_depth or self._count >= MAX_NODES:
            return self._node(sid, t, NodeKind.TERMINAL, Reason.DEPTH_LIMIT, before=before)
        if sid.path() in self._stack:
            # Feedback is normal in RTL (§5.4); stop without memoising, since
            # the answer depends on where we entered the loop.
            return self._node(sid, t, NodeKind.TERMINAL, Reason.CYCLE, before=before)

        self._stack.add(sid.path())
        try:
            node = self._classify(sid, t, depth, before)
            self._link_transaction(node, sid, t, depth)
        finally:
            self._stack.discard(sid.path())

        if node.reason is not Reason.CYCLE:
            self._memo[key] = node
        if self.on_node is not None:
            self.on_node(node)
        return node

    # -- §8.16 -----------------------------------------------------------

    def _link_transaction(self, node: CausalNode, sid: SignalId, t: int, depth: int) -> None:
        """Cross from a signal into the transaction it belongs to.

        Appended rather than substituted: the signal-level explanation stays
        exactly as it was, and the transaction is an additional, higher-level
        answer beside it. A reader who only wants signals can ignore the node;
        a reader asking "who is holding this bus" reads only that node.
        """
        from veritrace.protocol.link import MAX_LINKS

        idx = self.txn_index
        if idx is None or len(self._linked) >= MAX_LINKS or depth > self.max_depth:
            return
        txn = idx.at(self.store, sid.path(), t)
        if txn is None or txn.ref in self._linked:
            return
        self._linked.add(txn.ref)

        self._count += 1
        open_ended = not txn.closed
        link = CausalNode(
            signal=sid,
            time=txn.start_time,
            value=txn.label,
            kind=NodeKind.TXN_LINK,
            reason=Reason.TXN_OPEN if open_ended else Reason.TXN_IN_FLIGHT,
            is_primary_path=not node.children,
            txn=txn.ref,
        )
        fields = ", ".join(
            f"{k}={v:#x}" if isinstance(v, int) and k in ("addr", "araddr", "awaddr") else f"{k}={v}"
            for k, v in list(txn.fields.items())[:3]
            if v is not None
        )
        state = "never completed" if open_ended else "still in flight here"
        link.detail = f"{txn.iface}: {txn.label} {state}" + (f" ({fields})" if fields else "")

        # Continue at that level: what a transaction is waiting for is its
        # closing handshake, and the pack already says which signal that is.
        end_path = idx.end_signal(txn)
        if end_path is not None and end_path != sid.path():
            link.children = [self._why(SignalId.parse(end_path), t, depth + 1)]
            for i, c in enumerate(link.children):
                c.is_primary_path = i == 0
        node.children.append(link)

    def _classify(self, sid: SignalId, t: int, depth: int, before: bool = False) -> CausalNode:
        sig = self.view.signal(sid)
        if sig is None:
            return self._node(sid, t, NodeKind.TERMINAL, Reason.NOT_TRACED, before=before)

        if sig.trace_handle is None and sig.drivers and not sig.is_reconstructible:
            node = self._node(
                sid,
                t,
                NodeKind.TERMINAL,
                Reason.NOT_TRACED,
                before=before,
                loc=sig.decl_loc,
            )
            if sig.kind is Kind.MEM:
                node.detail = (
                    f"{sig.id.name} is not available as a complete value in the trace. "
                    "Dump the required elements explicitly; Verilator needs "
                    "--trace-max-array N and Icarus needs $dumpvars on each element."
                )
            else:
                node.detail = (
                    "this is sequential state that was not dumped; its history cannot "
                    "be reconstructed from the inputs at the current time"
                )
            return node

        path = sid.path()
        for prefix, module in self.graph.blackboxes.items():
            # Inside the black box, or a wire coming out of it. The second case
            # is the one that matters in practice: the IP's internals are rarely
            # in the dump, but the net it drives always is, and without this it
            # reads as an undriven wire rather than as a boundary (§7.4b).
            if path.startswith(prefix + ".") or (
                path in self.graph.blackbox_driven and prefix.rsplit(".", 1)[0] == sid.hier[0]
            ):
                n = self._node(sid, t, NodeKind.TERMINAL, Reason.BLACKBOX_IP, before=before, loc=sig.decl_loc)
                n.detail = module
                return n

        # 1. terminals, in the order of §8.1.
        observed = self.view.value(sid, t, before)
        unknown = observed is not None and not observed.known
        # Driven from outside the design: an unconnected input port, or a
        # signal only the testbench writes. Neither is a floating net, which is
        # what `UNDRIVEN` means.
        if not sig.drivers and (sig.kind is Kind.PORT_IN or sig.stimulus_only):
            return self._node(sid, t, NodeKind.TERMINAL, Reason.PRIMARY_INPUT, before=before, loc=sig.decl_loc)
        if sig.kind is Kind.PARAM:
            return self._node(sid, t, NodeKind.TERMINAL, Reason.CONSTANT, before=before, loc=sig.decl_loc)
        # "Constant for the whole run" is §8.1's terminal, but only where the
        # RTL says the signal cannot change. A guarded register that happened
        # never to fire is the *symptom*, and stopping there answers nothing —
        # it also makes the root cause depend on the simulator, since whether a
        # register shows an initial x->0 transition differs between dumps of the
        # same design. Walking on keeps the answer the same either way.
        #
        # A signal that is X throughout is likewise constant in the arithmetic
        # sense and useless as an answer; §8.5 wants to know why it is X.
        if not unknown and sig.is_tie_off and sig.is_traced:
            if self.view.is_constant(sid, 0, t + 1):
                return self._node(sid, t, NodeKind.TERMINAL, Reason.CONSTANT, before=before, loc=sig.decl_loc)
        # §8.11b: on an on-board capture the window is all there is. A value
        # already settled at the first sample was decided before the trigger,
        # and saying "undriven" or "constant" about it would be a claim the
        # capture cannot support.
        if self.capture_start is not None and sig.is_traced:
            if self.view.is_constant(sid, self.capture_start, t + 1):
                node = self._node(
                    sid, t, NodeKind.TERMINAL, Reason.CAPTURE_BOUNDARY, before=before,
                    loc=sig.decl_loc,
                )
                node.detail = (
                    f"already settled at the first sample (c0); the cause is before the "
                    f"window. Trigger on a change of {sid.name} next time, or probe its "
                    "drivers: " + ", ".join(sorted({r.name for d in sig.drivers for r in refs(d.guard)})[:4])
                    if sig.drivers
                    else "already settled at the first sample (c0); the cause is before the window"
                )
                return node

        if not sig.drivers:
            reason = Reason.UNKNOWN_X if unknown else Reason.UNDRIVEN
            if unknown and sig.unconnected:
                reason = Reason.UNCONNECTED_PORT
            return self._node(sid, t, NodeKind.TERMINAL, reason, before=before, loc=sig.decl_loc)

        # 2. which driver was active, and when (§5.5, problem 2).
        # `before` describes how *this* node's own value was asked for; the
        # pair below describes how its causes must be read. Conflating the two
        # makes a node display the value going into its edge instead of the one
        # it settled at — the same §5.5 confusion, one level up.
        t_eff, child_before = effective_time(self.view, sig, t)

        read = lambda s: self.view.value(s, t_eff, child_before)  # noqa: E731

        # 2b. §8.5. An X that originates here is the answer; an X inherited from
        # upstream is not, so only a specific terminal stops the walk. This is
        # what turns "200 X's" into "2 root causes".
        if unknown:
            reason = classify_x(sig, sid, t, self.view, read)
            if reason is not Reason.UNKNOWN_X:
                node = self._node(sid, t, NodeKind.TERMINAL, reason, before=before, loc=sig.decl_loc)
                node.detail = X_TERMINAL_DETAIL[reason]
                return node

        active = [d for d in sig.drivers if self._is_active(d, read)]

        if not active:
            return self._hold(sig, sid, t, t_eff, read, depth, child_before)
        if len(active) > 1:
            node = self._node(
                sid, t, NodeKind.CONFLICT, Reason.CONFLICT, loc=active[0].loc
            )
            node.detail = f"{len(active)} drivers active at once"
            node.children = [
                self._why(r, t_eff, depth + 1, child_before)
                for d in active
                for r in relevant_refs(d.value, read)
            ][: 8]
            return node

        # 3. the value came from this driver's expression — and from whatever
        # made *this* driver the active one. §8.1 recurses on the value; §5.4 is
        # explicit that the guard path is the one that matters when asking why a
        # signal holds the value it does, so the enabling guard terms are causes
        # too. `relevant_refs` on a true guard yields exactly those (for an OR
        # only the true operand, for an AND all of them).
        d = active[0]
        node = self._node(sid, t, NodeKind.ASSIGNED, Reason.ASSIGNED, before=before, loc=d.loc)
        node.detail = to_text(d.value)
        if not isinstance(d.guard, Const):
            node.detail += f"   [guard: {to_text(d.guard)}]"
        causes = relevant_refs(d.value, read) + relevant_refs(d.guard, read)
        # §5.6: a `mem[i]` read is answered by the element and by the write that
        # put the value there, not by the array as a whole. Those come first —
        # the data is the answer, the index is the supporting cast.
        reads = [
            self._mem_read(m, idx, read, t_eff, depth, child_before)
            for m, idx in mem_selects(d.value, self.view)
        ]
        indexed = {m.path for m, _ in mem_selects(d.value, self.view)}
        kids = self._children([c for c in causes if c.path() not in indexed], t_eff, depth, child_before)
        if reads:
            for k in kids:
                k.is_primary_path = False
            for i, r in enumerate(reads):
                r.is_primary_path = i == 0
            node.children = reads + kids
        else:
            node.children = kids
        return node

    def _mem_read(self, mem: Signal, index: Expr, read, t: int, depth: int, before: bool = False) -> CausalNode:
        """One `mem[i]` read, resolved to its element and to the write — §5.6.

        Three steps the spec is explicit about: evaluate the index, make the
        element the subject instead of the array, and **jump back to the last
        write of that element**. The jump is what turns "the memory holds 0x40"
        into "0x40 was written at c200 by the store at dma.sv:88", which is the
        whole reason the case is called out separately.
        """
        idx = evaluate(index, read).to_int()
        name = mem.id.name if idx is None else f"{mem.id.name}[{idx}]"
        sid = SignalId(mem.id.hier, name)

        if idx is None:
            node = self._node(sid, t, NodeKind.TERMINAL, Reason.UNKNOWN_X, loc=mem.decl_loc)
            node.detail = "the index is unknown here, so no element can be named"
            return node

        handle = mem.elements.get(idx)
        if handle is None:
            # §5.6 is explicit that this is said plainly rather than answered
            # around: a chain built on an array nobody dumped is a guess.
            node = self._node(sid, t, NodeKind.TERMINAL, Reason.NOT_TRACED, loc=mem.decl_loc)
            node.detail = (
                f"{mem.id.name} is not in the trace"
                if not mem.elements
                else f"{mem.id.name}[{idx}] is not in the trace ({len(mem.elements)} of "
                f"{mem.depth or '?'} elements were dumped)"
            )
            node.detail += ". Verilator needs --trace-max-array N, Icarus an "
            node.detail += "explicit $dumpvars on the elements."
            return node

        written = self.store.last_change_before(handle, t + 1)
        val = self.store.value_at(handle, t)
        node = CausalNode(
            signal=sid,
            time=written if written is not None else t,
            value=val.bits if val is not None else "?",
            kind=NodeKind.ASSIGNED,
            reason=Reason.ASSIGNED,
            loc=mem.decl_loc,
            last_change=written,
        )
        self._count += 1
        if written is None:
            node.kind = NodeKind.TERMINAL
            node.reason = Reason.UNDRIVEN
            node.detail = f"{name} was never written in this run"
            return node

        # The walk continues from the *write*, not from the read. Everything
        # below this node is about a different instant, which is exactly the
        # answer someone is after.
        node.detail = f"written at {name}"
        writer = self._writer(mem, written, depth)
        if writer is not None:
            d, t_eff, read_w = writer
            node.loc = d.loc
            node.detail = to_text(d.value)
            if not isinstance(d.guard, Const):
                node.detail += f"   [guard: {to_text(d.guard)}]"
            causes = relevant_refs(d.value, read_w) + relevant_refs(d.guard, read_w)
            node.children = self._children(causes, t_eff, depth + 1)
        return node

    def _writer(self, mem: Signal, t: int, depth: int):
        """The driver of `mem` that was enabled at `t`, with its read context."""
        if depth >= self.max_depth or self._count >= MAX_NODES:
            return None
        t_eff, before = effective_time(self.view, mem, t)
        read_w = lambda s: self.view.value(s, t_eff, before)  # noqa: E731
        active = [d for d in mem.drivers if self._is_active(d, read_w)]
        return (active[0], t_eff, read_w) if len(active) == 1 else None

    def _is_active(self, d: Driver, read) -> bool:
        g = evaluate(d.guard, read).truthy()
        return (not g.x) and bool(g.v)

    def _hold(self, sig: Signal, sid, t, t_eff, read, depth, before: bool = False) -> CausalNode:
        """No driver fired: the signal kept its value.

        The question becomes "why was every guard false", which is the case that
        matters most in practice (§8.1).
        """
        node = self._node(sid, t, NodeKind.HOLD, Reason.HOLD, before=before, loc=sig.decl_loc)
        node.detail = "held: no driver was enabled"
        culprits: list[SignalId] = []
        for d in sig.drivers:
            culprits.extend(falsifying_terms(d.guard, read))
        seen: set[str] = set()
        culprits = [c for c in culprits if not (c.path() in seen or seen.add(c.path()))]
        node.children = self._children(culprits, t_eff, depth, before)
        return node

    def _children(self, causes: list[SignalId], t_eff: int, depth: int, before: bool = False) -> list[CausalNode]:
        """Recurse into causes, oldest last transition first (§8.1).

        The ordering is the heuristic; the set is complete either way.
        """
        changes = self.view.last_changes(causes, t_eff)
        ranked = sorted(
            causes,
            key=lambda s: (
                changes[s.path()] if changes[s.path()] is not None else -1,
                s.path(),
            ),
        )
        out = [self._why(c, t_eff, depth + 1, before) for c in ranked]
        for i, n in enumerate(out):
            n.is_primary_path = i == 0
        return out
