"""FSM extraction — §8.8 steps 1–3, and the overlay of step 5.

§8.8 is explicit about what justifies this feature, and it is not the diagram:
the coverage overlay is "frumos dar sezonier", while the **static checks are
things no simulation can find** — a dead state nothing reached is invisible to a
test that never went there. So the extraction here is built to answer those
checks first and to draw a picture second, and everything it produces is
available with **no trace at all**.

The transitions come from the design graph rather than from a second walk over
the AST, and that is the one decision worth explaining. `conditions.assignments`
already turns a `case`/`if` chain into one driver per branch, carrying the
accumulated path condition as its guard:

    guard = aresetn && (mstate == 2'd1) && (aw_ok && w_ok)   value = 2'd2

Which is exactly `(from_state, guard, to_state, loc)` — §8.8 step 3 — with the
`mstate == K` conjunct naming the source state and the value naming the target.
Re-parsing the AST would be a second opinion about what the RTL says, and the
two would drift.

Three things the extraction has to get right, because each is a way to report a
bug that is not there:

* **A `Ternary` value is two transitions.** `state <= done ? DONE : NEXT` leaves
  a state by two different edges, and treating it as one target invents a dead
  state.
* **Symbolic names are not optional.** pyslang leaves a `localparam` reference as
  a name, not a folded constant, so `sstate == S_IDLE` has to be resolved
  against the instance's parameter table or the two spellings of state 0 look
  like two different states.
* **A guard with no `state == K` conjunct applies from every state.** That is
  what a reset branch is, and also what a global override is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from veritrace.graph.model import (
    Binary,
    Const,
    Driver,
    Expr,
    Kind,
    Ref,
    Signal,
    SignalId,
    SourceLoc,
    Ternary,
    Unary,
    to_text,
)

#: §8.8 step 1: "domeniu de valori mic si discret (< 32 valori distincte)". A
#: register with more targets than this is a counter or a datapath, whatever it
#: is called.
MAX_STATES = 32

#: How many state changes the overlay carries. A machine that switches every
#: cycle for a million cycles would otherwise put a megabyte on the wire to draw
#: a bar a thousand pixels wide; past this the timeline says it is truncated
#: rather than quietly showing the first part as if it were the whole run.
MAX_SEQUENCE = 20000

#: §8.8 step 1's last bullet, and its own warning about it: a *weak* signal,
#: never decisive. It only breaks ties between candidates that already passed
#: every structural test.
NAME_HINT = re.compile(r"(^|_)(state|st|fsm|phase|mode)(_|$)|state", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class State:
    """One value of the state register."""

    value: int
    #: `S_IDLE` when a parameter or enum names it, else `2'd1`.
    name: str
    #: True when this is where the reset branch lands.
    is_reset: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "name": self.name, "is_reset": self.is_reset}


@dataclass(slots=True)
class Transition:
    """`(from_state, guard, to_state, loc)` — §8.8 step 3."""

    #: `None` means "from any state": a reset branch, or an unconditional override.
    src: int | None
    dst: int
    #: The guard with the `state == K` conjunct removed — the condition that
    #: actually decides, rather than the one that merely says where we are.
    guard: str
    loc: SourceLoc | None
    #: True when this edge is the reset branch.
    is_reset: bool = False
    #: The full driver guard, kept for the checks that reason about it.
    full_guard: Expr | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "guard": self.guard,
            "loc": ({"file": self.loc.file, "line": self.loc.line} if self.loc else None),
            "is_reset": self.is_reset,
        }


@dataclass(slots=True)
class Machine:
    """One extracted state machine."""

    signal: str
    width: int
    decl_loc: SourceLoc | None
    states: list[State] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    #: Value the reset branch assigns, or `None` when nothing resets it — which
    #: is itself one of §8.8's findings.
    reset_state: int | None = None
    #: The reset signal itself. Every guard in the `else` branch carries it, so
    #: a check asking "does this state's exits cover every case" has to take it
    #: out first or `S_DONE -> S_IDLE [rst_n]` looks conditional when it is the
    #: unconditional exit of a perfectly good machine.
    reset_signal: str | None = None
    #: Registers written in the same `always_ff` blocks as the state. §8.8's
    #: "reset incomplet" is about these, not only about the state itself.
    companions: list[str] = field(default_factory=list)
    #: Filled by `overlay` when a trace is available — §8.8 step 5.
    visits: dict[int, int] = field(default_factory=dict)
    cycles_in: dict[int, int] = field(default_factory=dict)
    taken: dict[tuple[int | None, int], int] = field(default_factory=dict)
    #: `(cycle, state)` for every change, in order — §8.8 step 6's "secventa
    #: completa". Totals alone cannot draw it: a bar built from per-state sums
    #: would show the states in declaration order, which is a picture of a run
    #: that did not happen. Capped, and `sequence_truncated` says when.
    sequence: list[tuple[int, int]] = field(default_factory=list)
    #: Exact half-open [start, end, state] stays, including the final stay.
    #: Never reconstruct timestamps from a median clock period (gated clocks).
    intervals: list[tuple[int, int, int]] = field(default_factory=list)
    clock_path: str | None = None
    sequence_truncated: bool = False
    #: How the candidate was chosen, so a wrong one can be traced to a decision.
    why_candidate: str = ""

    @property
    def scope(self) -> str:
        return self.signal.rsplit(".", 1)[0]

    def name_of(self, value: int) -> str:
        for s in self.states:
            if s.value == value:
                return s.name
        return f"{self.width}'d{value}"

    def state(self, value: int) -> State | None:
        return next((s for s in self.states if s.value == value), None)

    def out_of(self, value: int) -> list[Transition]:
        """Edges leaving `value`, including the ones that leave every state."""
        return [t for t in self.transitions if t.src == value or t.src is None]

    def written_at(self, value: int) -> SourceLoc | None:
        """Where the RTL says what happens *in* this state.

        Not `out_of`: that includes the edges leaving every state, and the reset
        branch is one of them — so a finding about `S_TRAP` would point at the
        `if (!rst_n)` line instead of at the arm that traps. §8.8's acceptance
        criterion asks for the exact location, and the exact location is the arm.
        """
        for t in self.transitions:
            if t.src == value and not t.is_reset and t.loc is not None:
                return t.loc
        for t in self.transitions:
            if t.dst == value and not t.is_reset and t.loc is not None:
                return t.loc
        return self.decl_loc

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "width": self.width,
            "loc": (
                {"file": self.decl_loc.file, "line": self.decl_loc.line}
                if self.decl_loc
                else None
            ),
            "states": [s.to_dict() for s in self.states],
            "transitions": [t.to_dict() for t in self.transitions],
            "reset_state": self.reset_state,
            "companions": self.companions,
            "visits": {str(k): v for k, v in self.visits.items()},
            "cycles_in": {str(k): v for k, v in self.cycles_in.items()},
            "taken": {f"{k[0]}->{k[1]}": v for k, v in self.taken.items()},
            "sequence": [list(p) for p in self.sequence],
            "intervals": [list(p) for p in self.intervals],
            "clock_path": self.clock_path,
            "sequence_truncated": self.sequence_truncated,
            "why_candidate": self.why_candidate,
        }


# --- step 2: symbolic names -------------------------------------------------


class _Names:
    """Parameter reference -> value, per scope — §8.8 step 2.

    pyslang keeps a `localparam` reference as a name rather than folding it, so
    `sstate == S_IDLE` arrives as a name and has to be resolved or the two
    spellings of state 0 look like two different states.

    **Resolution only goes this way.** The reverse — value to name — is
    deliberately absent, and the first version of this module had it and was
    wrong: it labelled `mstate` state 0 `START_DELAY`, because that unrelated
    parameter happens to be 0 in the same scope. A state's name is the name
    *the RTL used at the comparison*, not any constant that shares its value.
    Where the RTL wrote `2'd0`, the state has no symbolic name and saying so is
    the honest answer.
    """

    def __init__(self, elaboration: Any) -> None:
        self._value: dict[tuple[str, str], int] = {}
        for scope, table in (getattr(elaboration, "parameters", {}) or {}).items():
            for name, param in table.items():
                value = _as_int(getattr(param, "value", None))
                if value is not None:
                    self._value[(scope, name)] = value

    def value(self, scope: str, name: str) -> int | None:
        """Resolve a parameter reference, searching outwards through the scopes."""
        parts = scope.split(".")
        for i in range(len(parts), 0, -1):
            got = self._value.get((".".join(parts[:i]), name))
            if got is not None:
                return got
        return self._value.get(("", name))


def _as_int(value: Any) -> int | None:
    """`2'b01`, `32'd7`, `7`, `1'b1` -> int. `None` when it is not a constant."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    m = re.fullmatch(r"(?:(\d+)\s*)?'\s*[sS]?([bBoOdDhH])([0-9a-fA-F_]+)", text)
    if m:
        try:
            return int(m.group(3).replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(2).lower()])
        except ValueError:
            return None
    try:
        return int(text, 0)
    except ValueError:
        return None


# --- guard analysis ---------------------------------------------------------


#: How far to follow a combinational wire when looking for `state == K` in a
#: guard. Three is enough for `start = req && (state == IDLE)` and for one more
#: level of naming on top of it; past that the substituted expression is too
#: large to be a useful edge label anyway.
INLINE_DEPTH = 3

#: Expressions past this many nodes are left alone. A guard that inlines into a
#: 200-term expression tells a reader nothing and costs the walk real time.
INLINE_MAX_NODES = 60


def _too_big(e: Expr | None, budget: int = INLINE_MAX_NODES) -> bool:
    """Does this expression have more than `budget` nodes?

    Counts nodes with an explicit stack and stops at the budget. The recursive
    version this replaces decremented the budget per *level* rather than per
    node, so on a wide tree it bounded the depth and nothing else — a 60-deep
    expression meant 2**60 visits. It did not hang, it just made the whole test
    suite take two hours instead of ninety seconds, which is the shape of
    slowness that survives review.
    """
    stack = [e]
    seen = 0
    while stack:
        node = stack.pop()
        if node is None:
            continue
        seen += 1
        if seen > budget:
            return True
        match node:
            case Binary(lhs=a, rhs=b):
                stack += [a, b]
            case Unary(arg=a):
                stack.append(a)
            case Ternary(cond=c, then=t, other=o):
                stack += [c, t, o]
    return False


def _inline(e: Expr, graph: Any, depth: int = INLINE_DEPTH) -> Expr:
    """Substitute combinational wires into a guard, so the state test surfaces.

    Real RTL rarely writes `state == IDLE` inside the `always_ff`. It writes

        wire start = req && (state_q == S_IDLE);
        ...
        else if (start) state_q <= we ? S_WR : S_RD;

    and a detector that only looks at the literal guard finds no state machine
    in a CPU's load/store unit — which is what the first version of this module
    did on a real design: zero machines, on a file with `S_IDLE` in it.

    Only unconditional single-driver combinational signals are substituted.
    Anything guarded would need its condition folded in as well, and a wrong
    substitution is worse than none: it would put a state comparison where the
    RTL has not got one.
    """
    if depth <= 0 or graph is None:
        return e
    match e:
        case Ref(signal=sid):
            sig = graph.get(sid.path())
            if sig is None or sig.kind is Kind.REG or len(sig.drivers) != 1:
                return e
            d = sig.drivers[0]
            if d.is_sequential or not isinstance(d.guard, Const) or not d.guard.value:
                return e
            if _too_big(d.value):
                return e
            return _inline(d.value, graph, depth - 1)
        case Binary(op=op, lhs=a, rhs=b):
            return Binary(op, _inline(a, graph, depth), _inline(b, graph, depth))
        case Unary(op=op, arg=a):
            return Unary(op, _inline(a, graph, depth))
        case _:
            return e


def _conjuncts(e: Expr | None) -> list[Expr]:
    """Flatten `a && b && c` into its terms. Anything else is one term."""
    if e is None:
        return []
    if isinstance(e, Binary) and e.op in ("&&", "&"):
        return _conjuncts(e.lhs) + _conjuncts(e.rhs)
    return [e]


def _equality(
    term: Expr, target: str, names: _Names, scope: str
) -> tuple[int, str | None] | None:
    """`state == K` -> `(K, name)` for the state register `target`.

    `name` is the identifier the RTL used, or `None` where it wrote a literal.
    That is what §8.8 step 2 resolves, and carrying it from the comparison is
    the only way to label a state without inventing a name for it.
    """
    if not isinstance(term, Binary) or term.op != "==":
        return None
    for a, b in ((term.lhs, term.rhs), (term.rhs, term.lhs)):
        if isinstance(a, Ref) and a.signal.path() == target:
            return _const_of(b, names, scope)
    return None


def _const_of(e: Expr, names: _Names, scope: str) -> tuple[int, str | None] | None:
    """A constant, or a parameter reference resolved to one (§8.8 step 2)."""
    if isinstance(e, Const):
        return None if e.x else (e.value, None)
    if isinstance(e, Ref):
        got = names.value(scope, e.signal.name)
        return None if got is None else (got, e.signal.name)
    return None


@dataclass(slots=True)
class _Guard:
    """A driver guard, split into what it says about the state and what decides."""

    #: States the guard asserts we are in.
    equals: list[tuple[int, str | None]] = field(default_factory=list)
    #: States it asserts we are *not* in — a `default` branch is all of these.
    nots: list[tuple[int, str | None]] = field(default_factory=list)
    #: Everything else: the condition that actually decides the transition,
    #: which is what belongs on an edge label rather than "we are in IDLE"
    #: repeated on every arrow out of IDLE.
    residual: Expr | None = None


def _disjuncts(e: Expr) -> list[Expr]:
    if isinstance(e, Binary) and e.op in ("||", "|"):
        return _disjuncts(e.lhs) + _disjuncts(e.rhs)
    return [e]


def _harvest(e: Expr, target: str, names: _Names, scope: str) -> list[tuple[int, str | None]]:
    """State tests hidden inside a guard — `req && (state == IDLE)`.

    Two shapes, and both are common once combinational wires are substituted:

    * a conjunction — `req && state == IDLE` says we are in IDLE;
    * a disjunction where **every** branch pins a state —
      `(state == RD && rvalid) || (state == WR && bvalid)`, which is what a
      `done` signal expands to. Then the sources are those states.

    Never under a negation, and never a disjunction with a branch that pins
    nothing: `a || state == K` does not say we are in K. Reading either as a
    source would draw an edge the RTL has not got.
    """
    branches = _disjuncts(e)
    if len(branches) > 1:
        found: list[tuple[int, str | None]] = []
        for branch in branches:
            here = _harvest(branch, target, names, scope)
            if not here:
                return []  # one branch is unconstrained, so the whole thing is
            found.extend(here)
        return found
    return [
        hit
        for term in _conjuncts(e)
        if (hit := _equality(term, target, names, scope)) is not None
    ]


def _split_guard(
    guard: Expr, target: str, names: _Names, scope: str, graph: Any = None
) -> _Guard:
    """Split a driver guard into what it says about the state and what decides.

    The state test is looked for in the guard *with combinational wires
    substituted*, but the label keeps the **original** wording. A guard that
    reads `start` in the source should read `start` on the edge too: expanding
    it to `req && (state_q == S_IDLE)` is how the diagram becomes unreadable,
    and the reader already knows which state the arrow leaves.
    """
    out = _Guard()
    rest: list[Expr] = []
    for term in _conjuncts(guard):
        expanded = _inline(term, graph) if graph is not None else term

        hit = _equality(expanded, target, names, scope)
        if hit is not None:
            out.equals.append(hit)
            continue
        if isinstance(expanded, Unary) and expanded.op in ("!", "~"):
            hit = _equality(expanded.arg, target, names, scope)
            if hit is not None:
                out.nots.append(hit)
                continue

        # Not purely a state test, but it may carry one: keep the term as the
        # label and take the state it implies.
        if expanded is not term:
            out.equals.extend(_harvest(expanded, target, names, scope))
        rest.append(term)

    for term in rest:
        out.residual = _and(out.residual, term)
    return out


def _truth(e: Expr | None) -> bool | None:
    """`True`/`False` for a constant condition, `None` when it is not one."""
    if isinstance(e, Const) and not e.x:
        return bool(e.value)
    if isinstance(e, Unary) and e.op in ("!", "~"):
        inner = _truth(e.arg)
        return None if inner is None else not inner
    return None


def _and(a: Expr | None, b: Expr | None) -> Expr | None:
    """Conjunction, folding the constants away.

    `bvalid && 1` on an edge label is noise; `bvalid && 0` is a phantom edge
    that the impossible-transition check would then report as a bug. Both come
    out of a `?:` whose condition is a literal, which real RTL does have.
    """
    if a is None:
        return b
    if b is None:
        return a
    for x, y in ((a, b), (b, a)):
        t = _truth(x)
        if t is True:
            return y
        if t is False:
            return x
    return Binary("&&", a, b)


def _targets(
    value: Expr, names: _Names, scope: str
) -> list[tuple[int, str | None, Expr | None]]:
    """Where a driver's value can land: `(state, name, extra condition)`.

    `state <= done ? DONE : NEXT` is **two** transitions. Collapsing it to one
    is how an extraction invents a dead state on perfectly good RTL. A branch
    whose condition folds to false contributes nothing, which is how a phantom
    edge is avoided rather than reported.
    """
    if isinstance(value, Ternary):
        out: list[tuple[int, str | None, Expr | None]] = []
        for branch, cond in ((value.then, value.cond), (value.other, Unary("!", value.cond))):
            if _truth(cond) is False:
                continue
            for dst, name, extra in _targets(branch, names, scope):
                out.append((dst, name, _and(cond, extra)))
        return out
    hit = _const_of(value, names, scope)
    return [(hit[0], hit[1], None)] if hit is not None else []


# --- step 1: candidates -----------------------------------------------------


def _sequential(sig: Signal) -> list[Driver]:
    return [d for d in sig.drivers if d.is_sequential]


def candidates(graph: Any, elaboration: Any = None, store: Any = None) -> list[Signal]:
    """Signals that look like state registers — §8.8 step 1, in its own order.

    Every test but the last is structural. The name is consulted only to rank
    what survived, because §8.8 calls it a weak signal and a tool that trusts it
    finds an "FSM" in every register called `st_count`.
    """
    names = _Names(elaboration)
    found: list[tuple[int, str, Signal]] = []
    for sig in graph:
        if sig.kind not in (Kind.REG, Kind.WIRE) or sig.width > 16:
            continue
        seq = _sequential(sig)
        if not seq:
            continue
        scope = sig.path.rsplit(".", 1)[0]
        # Cheapest test first, and the order is not cosmetic. Substituting
        # combinational wires into a guard walks the graph, and doing it for
        # every sequential register in a design before checking whether the
        # register even assigns constants made the whole test suite time out.
        # A counter has no constant targets, so it never reaches the expensive
        # half.
        values = {dst for d in seq for dst, _n, _extra in _targets(d.value, names, scope)}
        if not values or len(values) > MAX_STATES:
            continue
        # It must be compared against constants in its own guards — that is what
        # "apare in conditia unui case sau intr-un lant de if" means, checked
        # rather than assumed from a name.
        if not any(
            (g.equals or g.nots)
            for g in (_split_guard(d.guard, sig.path, names, scope, graph) for d in seq)
        ):
            continue
        if store is not None:
            distinct = _distinct_in_trace(store, sig)
            if distinct is not None and distinct > MAX_STATES:
                continue
        found.append((0 if NAME_HINT.search(sig.id.name) else 1, sig.path, sig))
    found.sort()
    return [sig for _hint, _path, sig in found]


def _distinct_in_trace(store: Any, sig: Signal) -> int | None:
    if sig.trace_handle is None:
        return None
    lo, hi = store.time_range
    seen = {v.bits for _t, v in store.transitions(sig.trace_handle, lo, hi + 1)}
    return len(seen)


# --- extraction -------------------------------------------------------------


def extract_one(sig: Signal, graph: Any, elaboration: Any = None) -> Machine:
    """Everything §8.8 steps 2 and 3 produce for one state register.

    Two passes, and the second one needs the first. A `default` branch's guard
    is "none of the labels matched", so what it means depends on which states
    exist — and that is only known once every driver has been read. Treating it
    as "from any state" instead, which is what a single pass forces, gives every
    state an outgoing edge and quietly disables the dead-state check.
    """
    names = _Names(elaboration)
    scope = sig.path.rsplit(".", 1)[0]
    machine = Machine(
        signal=sig.path,
        width=max(1, sig.width),
        decl_loc=sig.decl_loc,
        why_candidate=(
            "assigned in always_ff, compared against constants in its own guards"
            + (", and named like a state" if NAME_HINT.search(sig.id.name) else "")
        ),
    )

    # Pass 1: every state the RTL mentions, and the name it used for each.
    parsed: list[tuple[Driver, _Guard, list[tuple[int, str | None, Expr | None]]]] = []
    labels: dict[int, str] = {}
    for driver in _sequential(sig):
        guard = _split_guard(driver.guard, sig.path, names, scope, graph)
        targets = _targets(driver.value, names, scope)
        parsed.append((driver, guard, targets))
        for value, name in (*guard.equals, *guard.nots, *((v, n) for v, n, _ in targets)):
            if name is not None:
                labels.setdefault(value, name)
            else:
                labels.setdefault(value, "")
    values = sorted(labels)

    # Pass 1b: which driver is the reset branch, and what resets it. Needed
    # before the labels are built, because the reset term comes off every one.
    for driver, guard, targets in parsed:
        if _is_reset_guard(guard) and targets:
            machine.reset_state = targets[0][0]
            machine.reset_signal = _reset_name(_conjuncts(guard.residual)[0])
            break

    # Pass 2: the transitions.
    for driver, guard, targets in parsed:
        is_reset = _is_reset_guard(guard)
        srcs = _sources(guard, values)
        for dst, _name, extra in targets:
            cond = _and(guard.residual, extra)
            if _truth(cond) is False:
                continue  # a branch the RTL cannot take is not a transition
            label = cond if is_reset else _strip(cond, machine.reset_signal)
            for src in srcs:
                machine.transitions.append(
                    Transition(
                        src=src,
                        dst=dst,
                        guard=to_text(label) if label is not None else "always",
                        loc=driver.loc,
                        is_reset=is_reset,
                        full_guard=_and(driver.guard, extra),
                    )
                )

    machine.states = [
        State(
            value=v,
            name=labels.get(v) or f"{machine.width}'d{v}",
            is_reset=v == machine.reset_state,
        )
        for v in values
    ]
    machine.companions = _companions(sig, graph)
    return machine


def _sources(guard: _Guard, values: list[int]) -> list[int | None]:
    """Which states a driver's guard can fire from.

    * `state == A` — from A.
    * `state != A && state != B` — from everything else. This is a `default`
      branch, and expanding it to the complement rather than to "any state" is
      what keeps a state that only self-loops looking dead, which it is.
    * neither — from any state: a reset branch or an unconditional override.
    """
    if guard.equals:
        return [v for v, _name in guard.equals]
    if guard.nots:
        excluded = {v for v, _name in guard.nots}
        rest = [v for v in values if v not in excluded]
        # Every known state is excluded: the case is complete and this branch is
        # unreachable. It contributes no edges — and `checks` reports it, since
        # "unreachable" and "reaches nothing" are different things to say.
        return list(rest)
    return [None]


def _is_reset_guard(guard: _Guard) -> bool:
    """Is this driver the reset branch?

    The branch whose condition is **exactly** the reset, with no state test: for
    `if (!rst_n) state <= IDLE; else case (state) ...`, that is the first arm and
    nothing else.

    Deliberately *not* "the guard mentions the reset signal", which is what the
    first version of this module asked. In `always_ff @(posedge clk or negedge
    rst_n)` the elaborator records `rst_n` on **every** driver of the block and
    the `else` arm's path condition carries it too — so that test marked every
    transition in the design as a reset edge, and since reset edges do not count
    as escapes, every state in every machine was reported dead. Six machines,
    seventeen dead states, on RTL where three of them were correct.

    Works for both polarities and for a synchronous reset, because all three
    have the same shape: one term, about the reset, and no state.
    """
    if guard.equals or guard.nots or guard.residual is None:
        return False
    terms = _conjuncts(guard.residual)
    return len(terms) == 1 and _reset_name(terms[0]) is not None


def _strip(guard: Expr | None, signal: str | None) -> Expr | None:
    """Drop the reset term from an edge label.

    Every arm of the `else` branch carries `rst_n`, so without this every edge
    in every diagram reads `rst_n && …`. The reader knows the machine is out of
    reset; what they want on the arrow is what decides.
    """
    if guard is None or signal is None:
        return guard
    kept = [t for t in _conjuncts(guard) if _reset_name(t) != signal]
    out: Expr | None = None
    for term in kept:
        out = _and(out, term)
    return out


_RESET_NAME = re.compile(r"(^|[._])(a?rst|a?reset)(_?n)?([._\d]|$)", re.IGNORECASE)


def _looks_like_reset(e: Expr) -> bool:
    return _reset_name(e) is not None


def _reset_name(e: Expr | None) -> str | None:
    """The reset signal a condition is about, if it is about one.

    Negations are unwrapped **repeatedly**, not once. `if (~rst_n) ... else ...`
    gives the else-arm the path condition `!~rst_n` — two of them — and stopping
    at the first leaves `!~rst_n && start` on every edge of a design written that
    way, which is most Verilog-2001.
    """
    while isinstance(e, Unary) and e.op in ("!", "~"):
        e = e.arg
    if isinstance(e, Ref) and _RESET_NAME.search(e.signal.name):
        return e.signal.path()
    return None


def _companions(sig: Signal, graph: Any) -> list[str]:
    """Other registers written in the same `always_ff` block as the state.

    §8.8's "reset incomplet" is about the machine, not only its state register:
    a counter updated beside the state and left unreset starts at X on the
    board, which is exactly the non-deterministic start the check is for.

    Scoped to the same instance, and that is not a detail. `block_lines` are
    positions in a *file*, so two instances of one module share them exactly —
    matching on lines alone put `node1`'s registers in `node0`'s machine and
    listed every companion twice.
    """
    blocks = {(d.loc.file, d.block_lines) for d in _sequential(sig) if d.block_lines and d.loc}
    if not blocks:
        return []
    scope = sig.path.rsplit(".", 1)[0] + "."
    out: set[str] = set()
    for other in graph:
        if other.path == sig.path or not other.path.startswith(scope):
            continue
        if "." in other.path[len(scope) :]:
            continue  # inside a child instance, not this block
        if any(
            d.is_sequential and d.loc is not None and (d.loc.file, d.block_lines) in blocks
            for d in other.drivers
        ):
            out.add(other.path)
    return sorted(out)


def extract(graph: Any, elaboration: Any = None, store: Any = None) -> list[Machine]:
    """Every state machine in the design (§8.8 steps 1–3)."""
    return [extract_one(sig, graph, elaboration) for sig in candidates(graph, elaboration, store)]


# --- step 5: the trace overlay ----------------------------------------------


def overlay(machine: Machine, store: Any, graph: Any, clock: Any = None) -> Machine:
    """Visited states, edges taken and time spent — §8.8 step 5.

    A view, not a justification (§8.8 is explicit about that), so it is a
    separate pass: everything the checks need is already there without it, and a
    design with no dump still gets every finding.
    """
    from itertools import groupby
    from veritrace import clocks

    machine.visits.clear()
    machine.cycles_in.clear()
    machine.taken.clear()
    machine.sequence.clear()
    machine.intervals.clear()
    machine.sequence_truncated = False
    machine.clock_path = None
    sig = graph.get(machine.signal)
    if sig is None or sig.trace_handle is None:
        return machine
    paths = {d.clock.path() for d in sig.drivers if d.clock}
    if len(paths) == 1:
        clock = clocks.clock_at(store, next(iter(paths)))
    elif paths:
        clock = None  # More than one actual clock has no single cycle axis.
    machine.clock_path = clock.path if clock is not None else None
    lo, hi = store.time_range
    events = store.transitions(sig.trace_handle, lo, hi + 1)
    if not events:
        return machine

    previous: int | None = None
    previous_t: int | None = None
    def stay(start: int, end: int, value: int) -> None:
        spent = clock.cycles_between(start, end) if clock is not None else end - start
        machine.cycles_in[value] = machine.cycles_in.get(value, 0) + spent
        if end > start:
            if len(machine.intervals) < MAX_SEQUENCE:
                machine.intervals.append((start, end, value))
            else:
                machine.sequence_truncated = True

    # Only the final delta-cycle value describes the settled state at time t.
    for t, same_time in groupby(events, key=lambda event: event[0]):
        value = list(same_time)[-1][1]
        current = value.to_int()
        if current == previous:
            continue
        if previous is not None and previous_t is not None:
            stay(previous_t, t, previous)
        if current is None:
            previous, previous_t = None, None
            continue
        machine.visits[current] = machine.visits.get(current, 0) + 1
        if len(machine.sequence) < MAX_SEQUENCE:
            machine.sequence.append(
                (clock.cycle_of(t) if clock is not None else t, current)
            )
        else:
            machine.sequence_truncated = True
        if previous is not None and previous_t is not None:
            machine.taken[(previous, current)] = machine.taken.get((previous, current), 0) + 1
        previous, previous_t = current, t
    if previous is not None and previous_t is not None:
        stay(previous_t, hi, previous)
    return machine


def named(machine: Machine) -> Iterator[tuple[str, str, str]]:
    """`(from, to, guard)` with states spelled by name — for a text report."""
    for t in machine.transitions:
        yield (
            "any" if t.src is None else machine.name_of(t.src),
            machine.name_of(t.dst),
            t.guard,
        )


def find(machines: list[Machine], name: str) -> Machine | None:
    """Look a machine up by full path or by leaf name."""
    for m in machines:
        if m.signal == name or m.signal.rsplit(".", 1)[-1] == name:
            return m
    return None


def signal_id(path: str) -> SignalId:
    return SignalId.parse(path)
