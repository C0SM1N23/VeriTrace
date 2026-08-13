"""The five static FSM checks — §8.8's table.

§8.8 says plainly what makes this feature worth building, and it is not the
diagram: *"Astea se calculeaza **fara trace**, doar din graf, si apar direct in
tab-ul Checks. Un FSM cu o stare moarta e un bug garantat pe care nicio simulare
nu-l gaseste daca stimulii n-au ajuns acolo."* So nothing here reads a waveform.
Given RTL and nothing else, every finding below is available.

| Check | Fires when |
|---|---|
| `fsm_dead_state` | no satisfiable edge leaves the state, other than reset |
| `fsm_unreachable_state` | no path to it from the reset state |
| `fsm_impossible_transition` | the guard contradicts itself |
| `fsm_incomplete_guards` | no self-transition and the exits do not cover every input |
| `fsm_incomplete_reset` | a register of the machine has no reset branch |

**Satisfiability is decided, never guessed.** The procedure below recognises
three contradictions — a literal false, a term beside its own negation, and two
different values demanded of one signal — and answers *unknown* for everything
else. Unknown is treated as satisfiable, so a guard this module cannot reason
about never becomes a reported bug. Inventing a bug is the one failure a static
checker does not recover from: it gets switched off, and then the real findings
go with it (P1).

Two narrowings worth stating out loud, because each is a place where the literal
reading of §8.8 would fire on correct RTL:

* **A `default` on a complete case is not an impossible transition.** Its guard
  *is* contradictory — every state value is excluded — but that is defensive
  coding, not the "bug de tastare" §8.8 is after. The extractor gives such a
  branch no edges at all, so there is nothing here to report and nothing to
  suppress.
* **Reset edges do not rescue a dead state.** A state you can only leave by
  resetting the chip is exactly §8.8's "intri si nu mai iesi", so those edges
  are excluded when deciding.
"""

from __future__ import annotations

from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity
from veritrace.analysis.fsm import Machine, Transition, _conjuncts
from veritrace.graph.model import Binary, Const, Expr, Ref, Unary, to_text

#: Every check name this module can emit. `--fail-on` and `checks.disable` use
#: them, so the list is API and lives in one place.
CHECKS = (
    "fsm_dead_state",
    "fsm_unreachable_state",
    "fsm_impossible_transition",
    "fsm_incomplete_guards",
    "fsm_incomplete_reset",
)


# --- satisfiability ---------------------------------------------------------


def _key(e: Expr) -> str:
    """Structural identity, so `a && b` and `b && a` are told apart from `!a`."""
    return to_text(e)


def _negation(e: Expr) -> tuple[str, bool]:
    """`(term, is_negated)` — `!a` and `a` share a term."""
    if isinstance(e, Unary) and e.op in ("!", "~"):
        return _key(e.arg), True
    return _key(e), False


def satisfiable(guard: Expr | None) -> bool | None:
    """Can this guard ever hold? `None` when this module cannot tell.

    Three contradictions are recognised, and they are the ones a typo actually
    produces:

    * a literal `0`;
    * `a && !a`, in any arrangement;
    * `s == A && s == B` with `A != B`, which is what a mistyped state label in
      a nested condition looks like.

    Anything else is `None`. §8.8's example is `a && !a` "dupa simplificare",
    and a full simplifier is a SAT solver — which would make every answer here
    depend on a solver's timeout rather than on the design.
    """
    if guard is None:
        return True
    terms = _conjuncts(guard)
    seen: dict[str, bool] = {}
    equals: dict[str, int] = {}
    decided = False

    for term in terms:
        if isinstance(term, Const) and not term.x:
            if not term.value:
                return False
            continue

        key, negated = _negation(term)
        if key in seen and seen[key] != negated:
            return False
        seen[key] = negated

        # `signal == constant`, on either side.
        if isinstance(term, Binary) and term.op == "==":
            for a, b in ((term.lhs, term.rhs), (term.rhs, term.lhs)):
                if isinstance(a, Ref) and isinstance(b, Const) and not b.x:
                    path = a.signal.path()
                    if path in equals and equals[path] != b.value:
                        return False
                    equals[path] = b.value
                    decided = True

    return True if decided or len(terms) > 1 else None


def _live(t: Transition) -> bool:
    """An edge that can actually be taken, ignoring what reset can do."""
    return not t.is_reset and satisfiable(t.full_guard) is not False


# --- the checks -------------------------------------------------------------


def _reachable(machine: Machine) -> set[int]:
    """States reachable from reset by edges that are not the reset itself."""
    if machine.reset_state is None:
        return set()
    edges: dict[int, set[int]] = {}
    everywhere: set[int] = set()
    for t in machine.transitions:
        if not _live(t):
            continue
        if t.src is None:
            everywhere.add(t.dst)
        else:
            edges.setdefault(t.src, set()).add(t.dst)

    seen = {machine.reset_state}
    stack = [machine.reset_state]
    while stack:
        here = stack.pop()
        for dst in edges.get(here, set()) | everywhere:
            if dst not in seen:
                seen.add(dst)
                stack.append(dst)
    return seen


def dead_states(machine: Machine) -> Iterator[Finding]:
    """§8.8: a state with no satisfiable way out. Half the FSM deadlocks."""
    for state in machine.states:
        exits = [
            t
            for t in machine.out_of(state.value)
            if t.dst != state.value and _live(t)
        ]
        if exits:
            continue
        loc = machine.written_at(state.value)
        yield Finding(
            group=Group.FSM,
            severity=Severity.ERROR,
            check="fsm_dead_state",
            title=f"{state.name} is a dead state: nothing leaves it but reset",
            signal=machine.signal,
            loc=loc,
            detail=(
                f"{machine.signal} enters {state.name} and has no satisfiable transition "
                "to any other state. Reaching it stops the machine until the chip is reset."
            ),
            why=f"why({machine.signal})",
            notes=(
                "Found without a trace: a simulation only sees this if the stimulus "
                "reaches the state.",
            ),
        )


def unreachable_states(machine: Machine) -> Iterator[Finding]:
    """§8.8: dead code, or a transition somebody forgot to write."""
    if machine.reset_state is None:
        return
    reachable = _reachable(machine)
    for state in machine.states:
        if state.value in reachable:
            continue
        loc = machine.written_at(state.value)
        yield Finding(
            group=Group.FSM,
            severity=Severity.WARN,
            check="fsm_unreachable_state",
            title=f"{state.name} cannot be reached from {machine.name_of(machine.reset_state)}",
            signal=machine.signal,
            loc=loc,
            detail=(
                f"No sequence of transitions leads from the reset state to {state.name}. "
                "Either it is dead code, or a transition into it was never written."
            ),
            why=f"why({machine.signal})",
        )


def impossible_transitions(machine: Machine) -> Iterator[Finding]:
    """§8.8: a guard that contradicts itself — a typo in a condition."""
    for t in machine.transitions:
        if satisfiable(t.full_guard) is not False:
            continue
        src = "any state" if t.src is None else machine.name_of(t.src)
        yield Finding(
            group=Group.FSM,
            severity=Severity.WARN,
            check="fsm_impossible_transition",
            title=f"{src} -> {machine.name_of(t.dst)} can never be taken",
            signal=machine.signal,
            loc=t.loc or machine.decl_loc,
            detail=(
                f"The guard `{to_text(t.full_guard)}` contradicts itself, so this "
                "branch is dead. That is usually a mistyped condition."
            ),
            why=f"why({machine.signal})",
        )


def _deciding(t: Transition, machine: Machine) -> set[tuple[str, bool]]:
    """The terms of a guard that actually choose this edge.

    Two are dropped, and both would otherwise make a correct machine look
    incomplete:

    * the **state test** — every edge out of S carries `state == S`, which says
      where we are rather than what decides;
    * the **reset signal** — every edge in the `else` branch carries `rst_n`,
      so `S_DONE -> S_IDLE [rst_n]` is the *unconditional* exit of a clean
      machine and reading it as a condition reports a bug on correct RTL.
    """
    out: set[tuple[str, bool]] = set()
    for term in _conjuncts(t.full_guard):
        key, negated = _negation(term)
        if machine.reset_signal is not None and key.rsplit(".", 1)[-1] in (
            machine.reset_signal,
            machine.reset_signal.rsplit(".", 1)[-1],
        ):
            continue
        if isinstance(term, Binary) and term.op == "==":
            refs = {term.lhs, term.rhs}
            if any(isinstance(r, Ref) and r.signal.path() == machine.signal for r in refs):
                continue
        out.add((key, negated))
    return out


def _complementary(sets: list[set[tuple[str, bool]]]) -> bool:
    """Do two of these guards differ by exactly one negated term?

    `g && c` beside `g && !c` covers every value of `c`, which is what a `?:`
    on the next state produces and what a two-armed `if/else` produces. It is
    the common way an FSM state *does* say what to do in every case.
    """
    for i, a in enumerate(sets):
        for b in sets[i + 1 :]:
            same = a & b
            left, right = a - same, b - same
            if len(left) == 1 and len(right) == 1:
                (ka, na), (kb, nb) = next(iter(left)), next(iter(right))
                if ka == kb and na != nb:
                    return True
    return False


def incomplete_guards(machine: Machine) -> Iterator[Finding]:
    """§8.8: no self-transition, and the exits do not cover every input.

    Reported at `info`, and that is deliberate. An idle state waiting for a
    request has exactly this shape and is correct; so does a state that hangs
    because somebody only wrote the happy path, and the two are not
    distinguishable from the RTL alone. §11.4's suppression, with a reason, is
    how a project says which of its own it has looked at — the same mechanism
    every other judgement call in the tool uses.
    """
    for state in machine.states:
        out = [t for t in machine.out_of(state.value) if _live(t)]
        if any(t.dst == state.value for t in out):
            continue  # an explicit self-transition says what holding means
        if not out:
            continue  # that is a dead state, and `dead_states` said so already
        deciding = [_deciding(t, machine) for t in out]
        if any(not d for d in deciding):
            continue  # an unconditional exit covers everything
        if _complementary(deciding):
            continue
        yield Finding(
            group=Group.FSM,
            severity=Severity.INFO,
            check="fsm_incomplete_guards",
            title=f"{state.name} holds when none of its {len(out)} exit condition(s) is true",
            signal=machine.signal,
            loc=machine.written_at(state.value),
            detail=(
                f"{state.name} has no self-transition and its exits do not cover every "
                "case, so an unexpected input leaves the machine where it is. Intended "
                "for a state that waits; a hang for one that should not."
            ),
            why=f"why({machine.signal})",
        )


def incomplete_reset(machine: Machine, graph: Any) -> Iterator[Finding]:
    """§8.8: a register of the machine with no reset branch.

    About the machine, not only its state register. A counter updated beside the
    state and left unreset starts at X on the board — the non-deterministic
    start §8.8 is warning about, and one that simulation hides whenever the
    testbench happens to initialise it.
    """
    unreset: list[str] = []
    if not _has_reset(graph, machine.signal):
        unreset.append(machine.signal)
    unreset += [c for c in machine.companions if not _has_reset(graph, c)]
    if not unreset:
        return
    leaves = ", ".join(p.rsplit(".", 1)[-1] for p in unreset)
    # The declaration of the register that is actually wrong, not of the machine:
    # a finding that points at `state` when the problem is `count` sends the
    # reader to the one line where nothing is missing.
    culprit = graph.get(unreset[0]) if graph is not None else None
    yield Finding(
        group=Group.FSM,
        severity=Severity.WARN,
        check="fsm_incomplete_reset",
        title=f"{len(unreset)} register(s) of this machine are never reset: {leaves}",
        signal=machine.signal,
        loc=(culprit.decl_loc if culprit is not None else machine.decl_loc),
        detail=(
            "Every register written in the machine's clocked block should have a reset "
            "branch, or the machine starts in a state the board decides and the "
            "simulator does not."
        ),
        why=f"why({unreset[0]})",
        related=tuple(unreset),
    )


def _has_reset(graph: Any, path: str) -> bool:
    """Does this register have a branch the reset takes?

    Asked of the *guards*, not of `Driver.reset`. That field records the reset
    in the block's event list, so in `always_ff @(posedge clk or negedge rst_n)`
    every register of the block carries it — including the counter that the
    block never actually resets, which is precisely the bug this check exists
    to find. Reading it would answer "yes" for every register in every design
    with an asynchronous reset, and the check would never fire at all.
    """
    from veritrace.analysis.fsm import _Names, _is_reset_guard, _split_guard

    sig = graph.get(path) if graph is not None else None
    if sig is None:
        return True  # cannot tell; do not invent a finding
    seq = [d for d in sig.drivers if d.is_sequential]
    if not seq:
        return True
    names = _Names(None)
    scope = path.rsplit(".", 1)[0]
    return any(_is_reset_guard(_split_guard(d.guard, path, names, scope)) for d in seq)


# --- the entry point --------------------------------------------------------


def scan(
    graph: Any,
    elaboration: Any = None,
    store: Any = None,
    config: Any = None,
) -> Iterator[Finding]:
    """Every §8.8 finding in the design. Needs the graph; the trace is optional.

    `store` is used only to reject a candidate whose value range is too wide to
    be a state (§8.8 step 1). Every check below is computed from the graph, so
    a session opened without a dump reports exactly the same list.
    """
    from veritrace.analysis import fsm as fsm_mod

    if graph is None:
        return
    ignored = getattr(config, "is_ignored", None)
    for machine in fsm_mod.extract(graph, elaboration, store):
        if ignored is not None and ignored(machine.signal):
            continue
        yield from dead_states(machine)
        yield from unreachable_states(machine)
        yield from impossible_transitions(machine)
        yield from incomplete_guards(machine)
        yield from incomplete_reset(machine, graph)
