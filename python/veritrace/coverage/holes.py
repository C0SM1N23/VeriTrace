"""§8.12 — an uncovered point turned into the conditions that would close it.

*Instead of saying "write a test", compute from the graph exactly what has to
hold simultaneously.* §8.12's worked example:

    Uncovered: cpu_top.ex_stage.v:142, branch "else if (fwd_sel == 2'b10)"

    To reach this branch, all of these must hold at once:
        ex_valid == 1     (currently: 1 in 94% of cycles)     ok
        fwd_sel  == 2'b10 (currently: NEVER observed)          no
          fwd_sel <= 2'b10 when:  rs2_addr == mem_rd_addr
                                  && mem_reg_write == 1
                                  && rs2_addr != 0

Every line of that is read off something already built: the guard comes from
the driver at that source line (§5.2), the percentage from evaluating the
conjunct at every clock edge of the trace, and the last block from the drivers
of the signal that never took the value. *Derived, not invented* — which is
what makes it deterministic and worth acting on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from veritrace.analysis.whytrace import TraceView
from veritrace.coverage.model import CodeCoverage, Condition, Hole
from veritrace.graph.conditions import evaluate
from veritrace.graph.model import Binary, Const, Driver, Expr, Ref, refs, to_text

#: Uncovered points to derive conditions for. A run with thousands of them is a
#: run nobody is going to work through in one sitting, and evaluating every
#: conjunct of every one of them against the trace is the expensive part. The
#: rest are still listed as holes — only the derivation is capped, and the
#: report says so.
MAX_DERIVED = 200


def _conjuncts(e: Expr | None) -> Iterator[Expr]:
    """`a && b && c` -> a, b, c. Anything else is one condition."""
    match e:
        case Binary(op="&&", lhs=l, rhs=r):
            yield from _conjuncts(l)
            yield from _conjuncts(r)
        case None:
            return
        case _:
            yield e


def _drivers_at(graph: Any, file: str, line: int) -> list[Driver]:
    """Drivers whose assignment is on that source line.

    A relocated database may name a suffix of the elaborated path. Accept that
    only when it identifies a single file; a line number does not disambiguate
    two modules both called ``part.sv``.
    """
    requested = file.replace("\\", "/").removeprefix("./")
    files = {d.loc.file.replace("\\", "/") for sig in graph.signals.values()
             for d in sig.drivers if d.loc}
    matches = {f for f in files if f == requested or f.endswith("/" + requested)
               or requested.endswith("/" + f)}
    if not matches:
        matches = {f for f in files if Path(f).name == Path(requested).name}
    if len(matches) != 1:
        return []
    out: list[Driver] = []
    for sig in graph.signals.values():
        for d in sig.drivers:
            if d.loc and d.loc.line == line and d.loc.file.replace("\\", "/") in matches:
                out.append(d)
    return out


class _Sampler:
    """Evaluates a conjunct at every clock edge, once per distinct conjunct."""

    def __init__(self, view: TraceView, edges: list[int], before: bool = False) -> None:
        self.view = view
        self.edges = edges
        self.before = before
        self._cache: dict[str, tuple[int | None, int]] = {}

    def held(self, e: Expr) -> tuple[int | None, int]:
        key = to_text(e)
        got = self._cache.get(key)
        if got is not None:
            return got
        if not self.edges:
            out: tuple[int | None, int] = (None, 0)
        else:
            hits = 0
            decided = 0
            for t in self.edges:
                value = evaluate(e, lambda s: self.view.value(s, t, before=self.before)).truthy()
                if value.x:
                    continue
                decided += 1
                hits += 1 if value.v else 0
            out = (hits, decided) if decided else (None, 0)
        self._cache[key] = out
        return out


def _needed_value(e: Expr) -> tuple[Ref, Const] | None:
    """`fwd_sel == 2'b10` -> the signal and the value it has to take."""
    match e:
        case Binary(op="==", lhs=Ref() as r, rhs=Const() as c):
            return r, c
        case Binary(op="==", lhs=Const() as c, rhs=Ref() as r):
            return r, c
    return None


def fsm_holes(graph: Any, store: Any, clock: Any = None, elaboration: Any = None) -> list[Hole]:
    """Observed missing FSM transitions and their measured enabling conditions.

    This is shared by CLI coverage/uncovered, the API and the diagram. An
    untaken transition alone does not prove its residual guard was always
    false: the machine may simply never have entered the source state.
    """
    from veritrace import clocks
    from veritrace.analysis import fsm

    if graph is None or store is None:
        return []
    out = []
    for machine in fsm.extract(graph, elaboration, store):
        signal = graph.get(machine.signal)
        clock_path = next((driver.clock.path() for driver in signal.drivers if driver.clock), None)
        own_clock = (clocks.clock_at(store, clock_path) if clock_path else None) or clock
        fsm.overlay(machine, store, graph, own_clock)
        if not machine.visits:
            continue  # No observation is not evidence of zero executions.
        sampler = _Sampler(TraceView(graph, store), list(getattr(own_clock, "edges", []) or []), before=True)
        for transition in machine.transitions:
            if transition.is_reset or transition.src is None or transition.loc is None:
                continue
            if machine.taken.get((transition.src, transition.dst), 0):
                continue
            conditions = []
            for expression in _conjuncts(transition.full_guard):
                held, sampled = sampler.held(expression)
                conditions.append(Condition(
                    text=to_text(expression), held=held, sampled=sampled,
                    produced_by=_produced_by(graph, expression) if held == 0 else (),
                ))
            out.append(Hole(
                file=transition.loc.file, line=transition.loc.line,
                kind="fsm-transition",
                label=f"{machine.signal}: {machine.name_of(transition.src)} -> {machine.name_of(transition.dst)}",
                text=transition.guard, signal=machine.signal, conditions=conditions,
                note="This transition was taken 0 times in the loaded run. Conditions are sampled before its clock edge.",
            ))
    return out


def _produced_by(graph: Any, e: Expr) -> tuple[str, ...]:
    """How the signals in an unmet conjunct get the value it asks for.

    When the conjunct is `sig == K`, only the drivers that can actually assign
    `K` are listed — the rest are noise. When it is anything else, every driver
    of every signal it mentions is, because narrowing further would mean
    deciding satisfiability, and a wrong answer there is worse than a long list.
    """
    want = _needed_value(e)
    out: list[str] = []
    targets = [want[0].signal] if want else list(refs(e))
    for sid in targets:
        sig = graph.get(sid.path())
        if sig is None:
            continue
        for d in sig.drivers:
            if want is not None and isinstance(d.value, Const) and d.value.value != want[1].value:
                continue
            # An unconditional driver has the guard `1`; printing "when 1"
            # would read as a condition someone has to satisfy.
            guard = to_text(d.guard)
            assign = f"{sid.name} <= {to_text(d.value)}"
            out.append(f"{assign} when {guard}" if guard not in ("", "1") else assign)
        if len(out) > 8:
            break
    return tuple(out[:8])


def _source_line(root: Any, file: str, line: int) -> str:
    """The line itself, so the tab reads without a round trip to Source."""
    stem = Path(file).name
    candidates = [Path(file)]
    if root:
        candidates += [Path(root) / file, *Path(root).rglob(stem)]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace").splitlines()[line - 1].strip()
        except (OSError, IndexError):
            continue
    return ""


def derive(
    code: CodeCoverage,
    graph: Any,
    store: Any = None,
    clock: Any = None,
    project_root: Any = None,
) -> tuple[list[Hole], str]:
    """Conditions for every uncovered point, and a note when the cap was hit."""
    uncovered = code.uncovered()
    if not uncovered:
        return [], ""
    if graph is None:
        return (
            [Hole(file=p.file, line=p.line, kind=p.kind, label=p.label,
                  note="no RTL loaded, so no conditions could be derived")
             for p in uncovered[:MAX_DERIVED]],
            "",
        )

    edges = list(getattr(clock, "edges", []) or [])
    view = TraceView(graph, store) if store is not None else None
    sampler = _Sampler(view, edges) if view is not None else None

    out: list[Hole] = []
    for point in uncovered[:MAX_DERIVED]:
        hole = Hole(
            file=point.file,
            line=point.line,
            kind=point.kind,
            label=point.label,
            text=_source_line(project_root, point.file, point.line),
        )
        drivers = _drivers_at(graph, point.file, point.line)
        if not drivers:
            hole.note = "no assignment in the elaborated design is on this line"
            out.append(hole)
            continue
        driver = drivers[0]
        hole.signal = driver.target.path()
        for conj in _conjuncts(driver.guard):
            held, sampled = sampler.held(conj) if sampler is not None else (None, 0)
            cond = Condition(text=to_text(conj), held=held, sampled=sampled)
            if held == 0:
                cond.produced_by = _produced_by(graph, conj)
            hole.conditions.append(cond)
        if not hole.conditions:
            hole.note = "the assignment is unconditional; the line is unreachable only if its block is"
        out.append(hole)

    note = ""
    if len(uncovered) > MAX_DERIVED:
        note = (
            f"conditions derived for the first {MAX_DERIVED} of {len(uncovered)} "
            "uncovered points"
        )
    return out, note
