"""A testbench that reproduces the bug — §8.3.

§8.3 asks for two different things and is emphatic that they must not be
confused, so this module produces both and labels them:

**`minimal`** — for a control module with a narrow interface (arbiter, FSM,
FIFO, AXI slave). The subtrace is projected onto the DUT's input ports, every
input that does not appear is tested against the don't-care hypothesis, and what
is left is replayed. Sixteen cycles instead of twelve hundred, and it goes in the
regression suite.

**`focused`** — for a CPU or anything with a memory inside it. §8.3 is blunt
that minimising here means minimising the *program*, which is a different and
harder problem, and that the honest answer is a **window cut with an entry
point**: run the recorded stimulus to just before the failure, then check and
dump. It is not minimisation and the UI must not call it that, so nothing here
does either.

Which one you get is decided by the DUT, not by a flag: a module with a memory
or a wide interface cannot be minimised cleanly and says so.

**The validation loop is the point.** §8.3: "VeriTrace can invoke the simulator
on the generated testbench and confirm that it fails. If it does not fail, the
minimisation was too aggressive — report it and back off." That loop is what
makes this feature trustworthy rather than interesting, so `build` runs it and
escalates through three fidelities until the failure reproduces:

    0  the minimal subtrace projection
    1  every transition of the inputs that projection kept
    2  every transition of every input port — a faithful replay, which
       reproduces by construction

The level it landed on travels with the result. A repro that needed level 2 is
still useful; pretending it was minimal would not be.

§8.3 names `verilator --binary`. Verilator is used when it is installed;
otherwise Icarus runs it, which is the simulator `simulate.py` already drives
for the same reason (it needs no flags and installs in a minute). Which one ran
is reported — a validation whose tool is unknown is not a validation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veritrace.analysis.whytrace import CausalNode, NodeKind, TraceView, effective_time
from veritrace.graph.conditions import BV, evaluate
from veritrace.graph.model import Kind, SignalId, refs
from veritrace.repro.subtrace import Subtrace

#: An interface wider than this is not a control module, whatever it is called,
#: and §8.3's minimisation stops being readable. The table there says 5–15
#: inputs; this is that, rounded up so a `[3:0]`-heavy AXI slave still qualifies.
NARROW_PORTS = 20

#: How many cycles of history a focused testbench checks over (§8.3: "run to
#: t_bug - 30"). Only the dump window depends on it; the stimulus always starts
#: at the beginning, because that is what makes the state at the entry point
#: right.
FOCUS_WINDOW = 30

#: Rounds of the don't-care fixpoint. Each round can only ever *promote* inputs
#: out of the don't-care set, so it terminates; this is a guard against a
#: pathological graph, not a real limit.
MAX_DONT_CARE_ROUNDS = 8

#: Printed by the generated testbench. Checking for this rather than for an exit
#: status is what keeps validation the same question under every simulator.
REPRO_MARKER = "VERITRACE_REPRO"
NO_REPRO_MARKER = "VERITRACE_NO_REPRO"


class ReproError(RuntimeError):
    """The design cannot be reduced to a testbench, and why."""


@dataclass(slots=True)
class Port:
    name: str
    path: str
    width: int
    direction: str  # "in" | "out"
    handle: int | None = None


@dataclass(slots=True)
class Validation:
    ran: bool = False
    #: True when the generated testbench reproduced the original failure.
    reproduced: bool = False
    tool: str = ""
    seconds: float = 0.0
    output: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "reproduced": self.reproduced,
            "tool": self.tool,
            "seconds": round(self.seconds, 2),
            "output": self.output[-4000:],
            "error": self.error,
        }


@dataclass(slots=True)
class Repro:
    #: `minimal` or `focused` — §8.3 insists these are named apart.
    mode: str
    #: Why that mode, in one sentence, for the UI to show beside the button.
    mode_reason: str
    module: str
    instance: str
    top: str
    code: str
    cycles: int
    #: Ports the testbench drives.
    driven: list[str] = field(default_factory=list)
    #: Ports held at 0, having survived the don't-care check (§8.3 step 4).
    tied: list[str] = field(default_factory=list)
    #: Stimulus assignments in the generated source.
    events: int = 0
    #: Fidelity level the generator was asked for — see the module docstring.
    level: int = 0
    symptom: dict[str, Any] = field(default_factory=dict)
    validation: Validation = field(default_factory=Validation)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "mode_reason": self.mode_reason,
            "module": self.module,
            "instance": self.instance,
            "top": self.top,
            "code": self.code,
            "cycles": self.cycles,
            "driven": self.driven,
            "tied": self.tied,
            "events": self.events,
            "level": self.level,
            "symptom": self.symptom,
            "validation": self.validation.to_dict(),
            "notes": self.notes,
        }


# --- picking the DUT --------------------------------------------------------


def choose_dut(elaboration: Any, symptom: str) -> tuple[str, str]:
    """The deepest instance that contains the symptom, and its module name.

    Deepest, because the smallest module containing the failure is the one whose
    interface is narrow enough to drive — and because a repro of `cpu_top` when
    the bug is in `lsu` is not a reduction of anything.
    """
    instances: dict[str, str] = getattr(elaboration, "instances", {}) or {}
    inside = [p for p in instances if symptom.startswith(p + ".")]
    if not inside:
        raise ReproError(
            f"{symptom} is not inside any module instance the elaboration knows about. "
            "Pass --rtl so the design graph has the hierarchy."
        )
    best = max(inside, key=lambda p: (p.count("."), len(p)))
    return best, instances[best]


def ports_of(graph: Any, instance: str) -> list[Port]:
    """The instance's own ports — one level down, not the whole subtree."""
    prefix = instance + "."
    out: list[Port] = []
    for sig in graph:
        if not sig.path.startswith(prefix) or "." in sig.path[len(prefix) :]:
            continue
        if sig.kind is Kind.PORT_IN:
            direction = "in"
        elif sig.kind is Kind.PORT_OUT:
            direction = "out"
        else:
            continue
        out.append(
            Port(
                name=sig.path[len(prefix) :],
                path=sig.path,
                width=max(1, sig.width),
                direction=direction,
                handle=sig.trace_handle,
            )
        )
    out.sort(key=lambda p: p.name)
    return out


def _loads_program(graph: Any, instance: str) -> str | None:
    """A memory inside the DUT whose contents arrive from outside the design.

    This is §8.3's CPU row, stated as something measurable. A FIFO's buffer is
    *not* it: the design writes it, so replaying the interface recreates it. What
    cannot be minimised is a memory the testbench loads — a program image —
    because then the stimulus *is* the image and reducing it is a different
    problem. `stimulus_only` says exactly that: written only by `initial`, which
    is what `$readmemh` is.
    """
    prefix = instance + "."
    for sig in graph:
        if not sig.path.startswith(prefix):
            continue
        if sig.kind is not Kind.MEM and not sig.elements:
            continue
        if sig.stimulus_only or sig.has_initializer or not sig.drivers:
            return sig.path
    return None


def classify(graph: Any, instance: str, ports: list[Port]) -> tuple[str, str]:
    """`("minimal" | "focused", why)`, from §8.3's table of what works.

    Note what is *not* consulted: the module's name. "It is called `arbiter` so
    it must be minimisable" is exactly the kind of guess that produces a
    testbench which compiles, runs, and reproduces nothing.

    This is only the *starting* answer. `build` runs the generated testbench and
    downgrades a `minimal` that turned out to need a faithful replay, so the
    label on the result describes what was achieved rather than what was hoped.
    """
    inputs = [p for p in ports if p.direction == "in"]
    loaded = _loads_program(graph, instance)
    if loaded is not None:
        return (
            "focused",
            f"{loaded} is loaded from outside the design, so minimising the stimulus would "
            "mean minimising that image — a different problem (§8.3). Cut to a window instead.",
        )
    if len(inputs) > NARROW_PORTS:
        return (
            "focused",
            f"{len(inputs)} input ports is past the {NARROW_PORTS} a minimised testbench "
            "stays readable at (§8.3). Cut to a window instead.",
        )
    return (
        "minimal",
        f"{len(inputs)} input ports — a narrow enough interface to minimise (§8.3).",
    )


# --- the don't-care hypothesis (§8.3 step 4) --------------------------------


def dont_care(
    graph: Any,
    store: Any,
    root: CausalNode,
    candidates: set[str],
) -> tuple[set[str], set[str]]:
    """Split `candidates` into inputs that may be tied to 0 and those that may not.

    §8.3 step 4: set each absent input to a constant 0 and re-evaluate the causal
    chain on the graph. If a guard that was true goes false, or one that was
    false comes true, that input was carrying the chain and is not a don't-care.

    Only the named inputs are substituted — everything else still reads its
    recorded value — so an input's influence is felt exactly where its own name
    appears in a driver. That is why the check can blame a broken node on the
    candidates that node mentions, and why the fixpoint converges: each round can
    only take names *out* of the don't-care set.
    """
    view = TraceView(graph, store)
    zeroed = set(candidates)
    for _round in range(MAX_DONT_CARE_ROUNDS):
        broken: set[str] = set()
        for node in root.walk():
            if node.kind not in (NodeKind.ASSIGNED, NodeKind.HOLD):
                continue
            sig = graph.get(node.signal.path())
            if sig is None or not sig.drivers:
                continue
            t_eff, before = effective_time(view, sig, node.time)

            def plain(s: SignalId, _t=t_eff, _b=before) -> BV | None:
                return view.value(s, _t, _b)

            def zeroed_read(s: SignalId, _t=t_eff, _b=before) -> BV | None:
                if s.path() in zeroed:
                    inner = graph.get(s.path())
                    return BV.of(0, max(1, inner.width) if inner else 1)
                return view.value(s, _t, _b)

            if _active_mask(sig, plain) == _active_mask(sig, zeroed_read):
                continue
            # This node behaves differently once those inputs are tied off.
            for d in sig.drivers:
                for ref in list(refs(d.guard)) + list(refs(d.value)):
                    if ref.path() in zeroed:
                        broken.add(ref.path())
        if not broken:
            break
        zeroed -= broken
    return zeroed, candidates - zeroed


def _active_mask(sig: Any, read) -> tuple[bool, ...]:
    """Which drivers are enabled, as a tuple — the thing that must not change."""
    out = []
    for d in sig.drivers:
        g = evaluate(d.guard, read).truthy()
        out.append((not g.x) and bool(g.v))
    return tuple(out)


# --- stimulus ---------------------------------------------------------------


@dataclass(slots=True)
class _Assign:
    #: How many `@(posedge clk)` the testbench has executed when this is applied.
    #: See `_stimulus` for why this is not the cycle number it came from.
    posedges: int
    port: str
    literal: str


def _literal(width: int, bits: str) -> str:
    """A SystemVerilog literal for what the trace actually recorded.

    Binary throughout, including `x` and `z` digits: the point of the repro is to
    put the DUT in the state it was in, and rounding an unknown to 0 would be a
    different experiment.
    """
    if not bits:
        return f"{width}'b" + "x" * width
    lead = bits[0]
    fill = lead if lead in "xzXZ" else "0"
    full = (fill * max(0, width - len(bits)) + bits)[-width:]
    return f"{width}'b{full.lower()}"


def _transitions(store: Any, port: Port, t0: int, t1: int) -> list[tuple[int, str]]:
    if port.handle is None:
        return []
    out: list[tuple[int, str]] = []
    start = store.value_at(port.handle, t0)
    if start is not None:
        out.append((t0, start.bits))
    for at, value in store.transitions(port.handle, t0, t1 + 1):
        if at <= t0:
            continue
        out.append((at, value.bits))
    return out


def _stimulus(
    store: Any,
    clock: Any,
    ports: list[Port],
    replayed: set[str],
    sub_events: list[tuple[str, int]],
    t0: int,
    t_bug: int,
    level: int,
) -> list[_Assign]:
    """Assignments, normalised to clock edges from the start (§8.3 step 3).

    The unit is **how many rising edges have already gone by**, not the cycle
    number the event carries, and the difference is a whole cycle of skew.

    A flop first sees a value at the earliest edge strictly after the value
    appeared. An event at time `t` therefore lands at edge `cycle_of(t) + 1`,
    because `cycle_of` is the index of the last edge at or *before* `t`. Placing
    it at `cycle_of(t)` instead drives it one cycle early — every stimulus
    arrives before the DUT is ready for it, and the failure quietly stops
    reproducing.

    The value in force at the first edge is the initial condition, applied before
    any edge at all.

    At level 0 only the subtrace's own events are replayed — that is the
    minimisation. At level 1 and above the full recorded transition list of each
    replayed port is used, which is what the escalation in `build` backs off to.
    """
    wanted = set(sub_events)
    out: list[_Assign] = []
    for port in ports:
        if port.direction != "in" or port.name not in replayed:
            continue
        for at, bits in _transitions(store, port, t0, t_bug):
            if at == t0:
                out.append(_Assign(0, port.name, _literal(port.width, bits)))
                continue
            if level == 0 and (port.path, at) not in wanted:
                continue
            out.append(
                _Assign(clock.cycle_of(at) + 1, port.name, _literal(port.width, bits))
            )
    # One assignment per (edge, port): a port that moved twice inside one cycle
    # is sampled once, at the value it settled on.
    latest: dict[tuple[int, str], _Assign] = {}
    for a in sorted(out, key=lambda a: (a.posedges, a.port)):
        latest[(a.posedges, a.port)] = a
    return [latest[k] for k in sorted(latest)]


# --- code generation --------------------------------------------------------


_IDENT = re.compile(r"[^A-Za-z0-9_]")


def _module_name(symptom: str, mode: str) -> str:
    leaf = _IDENT.sub("_", symptom.rsplit(".", 1)[-1])
    return f"tb_{'repro' if mode == 'minimal' else 'focus'}_{leaf}"


def _params(elaboration: Any, instance: str) -> list[tuple[str, str]]:
    """Parameters this instance overrode — the repro has to override them too."""
    table = (getattr(elaboration, "parameters", {}) or {}).get(instance, {})
    return [
        (p.name, p.value)
        for p in table.values()
        if getattr(p, "overridden", False) and not getattr(p, "local", False)
    ]


def generate(
    sub: Subtrace,
    graph: Any,
    store: Any,
    clock: Any,
    elaboration: Any,
    root: CausalNode | None = None,
    level: int = 0,
    mode: str | None = None,
) -> Repro:
    """Write the testbench. No simulator involved — `build` is what validates it."""
    symptom = sub.symptom
    if symptom is None:
        raise ReproError("the causal chain has no symptom to reproduce")
    if clock is None or clock.period is None:
        raise ReproError(
            "a repro needs a clock with a measurable period; none was resolved for this trace. "
            "Set clocks.primary in .veritrace.toml."
        )

    instance, module = choose_dut(elaboration, symptom.signal)
    ports = ports_of(graph, instance)
    if not ports:
        raise ReproError(f"{instance} ({module}) has no ports in the design graph")
    chosen_mode, why = classify(graph, instance, ports)
    if mode is not None:
        chosen_mode = mode
        why = f"asked for explicitly (auto-detected: {chosen_mode})"

    clk_port = _clock_port(ports, clock, instance)
    if clk_port is None:
        raise ReproError(
            f"{module} has no clock input this repro could drive "
            f"(looked for {clock.path} among its ports)"
        )

    t0 = clock.edges[0]
    t_bug = symptom.time
    inputs = [p for p in ports if p.direction == "in" and p.name != clk_port.name]

    notes: list[str] = []
    if chosen_mode == "focused" or level >= 2:
        replayed = {p.name for p in inputs}
        tied: set[str] = set()
    else:
        by_path = {p.path: p.name for p in inputs}
        on_ports = {e.signal for e in sub.events if e.signal in by_path}
        keep = {by_path[p] for p in on_ports}
        # §8.3 step 4, on everything the subtrace did not name.
        candidates = set(by_path) - on_ports
        safe, promoted = dont_care(
            graph, store, root if root is not None else _root_of(sub, graph), candidates
        )
        keep |= {by_path[p] for p in promoted}
        replayed = keep
        tied = {by_path[p] for p in safe}
        if promoted:
            notes.append(
                f"{len(promoted)} input(s) could not be tied off: the chain changes without them."
            )

    sub_events = [(e.signal, e.time) for e in sub.events]
    assigns = _stimulus(
        store, clock, ports, replayed, sub_events, t0, t_bug, 0 if level == 0 else 1
    )
    # The symptom was observed at `t_bug`, i.e. after the edge whose index
    # `cycle_of` returns. Same +1 as the stimulus, for the same reason.
    last_cycle = clock.cycle_of(t_bug) + 1

    code = _render(
        top=_module_name(symptom.signal, chosen_mode),
        mode=chosen_mode,
        module=module,
        instance=instance,
        ports=ports,
        clk=clk_port,
        replayed=sorted(replayed),
        tied=sorted(tied),
        assigns=assigns,
        last_cycle=last_cycle,
        symptom=symptom,
        store=store,
        graph=graph,
        clock=clock,
        params=_params(elaboration, instance),
        level=level,
        notes=notes,
        original_cycles=clock.n_cycles,
    )
    return Repro(
        mode=chosen_mode,
        mode_reason=why,
        module=module,
        instance=instance,
        top=_module_name(symptom.signal, chosen_mode),
        code=code,
        cycles=last_cycle,
        driven=sorted(replayed),
        tied=sorted(tied),
        events=len(assigns),
        level=level,
        symptom={
            "signal": symptom.signal,
            "value": symptom.value,
            "time": symptom.time,
            "cycle": symptom.cycle,
        },
        notes=notes,
    )


def _root_of(sub: Subtrace, graph: Any) -> CausalNode:
    """A stand-in tree when only the subtrace survived.

    `dont_care` walks the causal tree, and callers hand it the real one; this
    keeps the signature honest when a `Subtrace` arrives without its tree —
    the check then sees only the events, which is a weaker but not a wrong test.
    """
    from veritrace.analysis.whytrace import Reason

    nodes = [
        CausalNode(
            signal=SignalId.parse(e.signal),
            time=e.time,
            value=e.value,
            kind=NodeKind(e.node_kind),
            reason=Reason(e.reason),
        )
        for e in sub.events
    ]
    head = nodes[0]
    head.children = nodes[1:]
    return head


def _clock_port(ports: list[Port], clock: Any, instance: str) -> Port | None:
    from veritrace.clocks import CLOCK_NAME_RE

    want = clock.path
    for p in ports:
        if p.path == want or p.name == want.rsplit(".", 1)[-1]:
            return p
    for p in ports:
        if p.direction == "in" and p.width == 1 and CLOCK_NAME_RE.search(p.name):
            return p
    return None


_HEADERS = {
    "minimal": (
        "Generated by VeriTrace — minimal repro (§8.3).\n"
        "// Stimulus reduced to the events the causal chain depends on; every other\n"
        "// input was checked against the don't-care hypothesis and tied off."
    ),
    "focused": (
        "Generated by VeriTrace — FOCUSED testbench (§8.3), not a minimal one.\n"
        "// The recorded stimulus is replayed up to the failure and the window around\n"
        "// it is dumped. Nothing here has been minimised: for a design with a memory\n"
        "// inside it, that would mean minimising the program, which is a different\n"
        "// and harder problem than this tool solves."
    ),
}


def _render(**kw: Any) -> str:
    store, graph, clock = kw["store"], kw["graph"], kw["clock"]
    symptom, ports, clk = kw["symptom"], kw["ports"], kw["clk"]
    half = max(1, (clock.period or 2) // 2)
    timescale = _timescale(store)
    sig = graph.get(symptom.signal)
    width = max(1, sig.width if sig is not None else 1)
    handle = sig.trace_handle if sig is not None else None
    observed = store.value_at(handle, symptom.time) if handle is not None else None
    literal = _literal(width, observed.bits if observed is not None else "")
    rel = symptom.signal[len(kw["instance"]) + 1 :] or symptom.signal.rsplit(".", 1)[-1]

    lines: list[str] = []
    add = lines.append
    add(f"`timescale {timescale}/{timescale}")
    add(f"// {_HEADERS[kw['mode']]}")
    add(f"// Symptom: {symptom.signal} = {symptom.value} at c{symptom.cycle}")
    add(
        f"// Original: {kw['original_cycles']} cycles"
        f"  |  this testbench: {kw['last_cycle']} cycles,"
        f" {len(kw['replayed'])} driven input(s), {len(kw['tied'])} tied off"
    )
    for note in kw["notes"]:
        add(f"// Note: {note}")
    add("")
    add(f"module {kw['top']};")

    # Declarations. Outputs are `wire`, inputs `logic` so the stimulus can drive
    # them; a repro that cannot be edited by hand afterwards is half a repro.
    add(f"  logic {clk.name} = 1'b0;")
    for p in ports:
        if p.name == clk.name:
            continue
        decl = "logic" if p.direction == "in" else "wire"
        vec = "" if p.width == 1 else f" [{p.width - 1}:0]"
        add(f"  {decl}{vec} {p.name};")
    add("")
    add(f"  always #{half} {clk.name} = ~{clk.name};")
    add("")

    params = kw["params"]
    inst_head = f"  {kw['module']}"
    if params:
        inst_head += " #(" + ", ".join(f".{n}({v})" for n, v in params) + ")"
    add(inst_head + " u_dut (")
    add(",\n".join(f"    .{p.name}({p.name})" for p in ports))
    add("  );")
    add("")

    # §12 wants a waveform; a repro that produces one is also the thing you open
    # next. Windowed, so a long focused run does not write a gigabyte — but only
    # when there is a run long enough for a window to mean anything, otherwise
    # the dump would be switched off and never switched back on.
    window_start = max(0, kw["last_cycle"] - FOCUS_WINDOW)
    add("  initial begin")
    add(f'    $dumpfile("{kw["top"]}.vcd");')
    add(f"    $dumpvars(0, {kw['top']});")
    if window_start > 0:
        add("    $dumpoff;")
    add("  end")
    add("")

    add("  initial begin")
    tied_ports = [p for p in ports if p.direction == "in" and p.name in set(kw["tied"])]
    if tied_ports:
        add("    // Checked against the don't-care hypothesis (§8.3 step 4): the")
        add("    // causal chain re-evaluates the same way with these at zero.")
        for p in tied_ports:
            add(f"    {p.name} = {p.width}'b0;")
    add("")

    cycle = 0
    by_cycle: dict[int, list[_Assign]] = {}
    for a in kw["assigns"]:
        by_cycle.setdefault(a.posedges, []).append(a)
    for at in sorted(by_cycle):
        gap = at - cycle
        if gap == 1:
            add(f"    @(posedge {clk.name});")
        elif gap > 1:
            add(f"    repeat ({gap}) @(posedge {clk.name});")
        for a in by_cycle[at]:
            add(f"    {a.port} = {a.literal};")
        if window_start > 0 and at >= window_start > cycle:
            add("    $dumpon;   // the window §8.3 asks for")
        cycle = at
    if window_start > cycle:
        add(f"    repeat ({window_start - cycle}) @(posedge {clk.name});")
        add("    $dumpon;   // the window §8.3 asks for")
        cycle = window_start
    if kw["last_cycle"] > cycle:
        add(f"    repeat ({kw['last_cycle'] - cycle}) @(posedge {clk.name});")
    add("")
    add(f"    // c{symptom.cycle}: {symptom.signal} was {symptom.value} here.")
    add("    // The expected value is not in the trace — only the wrong one is — so")
    add("    // the check is that the symptom value must not be present.")
    add(f"    assert (u_dut.{rel} !== {literal})")
    add(
        f'      else $fatal(1, "{REPRO_MARKER}: {rel} = {symptom.value} '
        f'at cycle %0d (VeriTrace)", {kw["last_cycle"]});'
    )
    add(f'    $display("{NO_REPRO_MARKER}: {rel} is not {symptom.value} here");')
    add("    $finish;")
    add("  end")
    add("endmodule")
    return "\n".join(lines) + "\n"


def _timescale(store: Any) -> str:
    """The trace's own unit, so a cycle in the repro is a cycle in the dump."""
    text = str(getattr(store, "timescale", "") or "").strip()
    return text if re.fullmatch(r"\d*\s*[munpf]?s", text) else "1ns"


# --- validation (§8.3) ------------------------------------------------------


def _verilator() -> str | None:
    return shutil.which("verilator")


def validate(
    repro: Repro,
    sources: list[Path],
    work: Path,
    timeout: float = 120.0,
    incdirs: list[str] | None = None,
    defines: list[str] | None = None,
) -> Validation:
    """Compile and run the generated testbench; did it reproduce the failure?

    Not "did it compile". §8.3's loop is only worth anything if the answer is
    about the *failure*, so the verdict comes from the marker the testbench
    prints, which every simulator reports the same way.
    """
    from veritrace import simulate

    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tb = work / f"{repro.top}.sv"
    tb.write_text(repro.code, encoding="utf-8")

    started = _time.perf_counter()
    verilator = _verilator()
    try:
        if verilator:
            out, tool = _run_verilator(verilator, tb, sources, work, timeout, incdirs, defines)
        else:
            icarus = simulate.find_iverilog()
            if icarus is None:
                return Validation(
                    ran=False,
                    error=(
                        "no simulator to validate with. §8.3 uses `verilator --binary`; "
                        "Icarus works too. Install either, or run the testbench yourself."
                    ),
                )
            out, tool = _run_icarus(icarus, tb, sources, work, repro.top, timeout, incdirs, defines)
    except subprocess.TimeoutExpired:
        return Validation(
            ran=False,
            seconds=_time.perf_counter() - started,
            error=f"the generated testbench did not finish within {timeout:g}s",
        )
    except OSError as e:
        return Validation(ran=False, error=f"could not run the simulator: {e}")

    seconds = _time.perf_counter() - started
    if REPRO_MARKER in out:
        return Validation(ran=True, reproduced=True, tool=tool, seconds=seconds, output=out)
    if NO_REPRO_MARKER in out:
        return Validation(
            ran=True,
            reproduced=False,
            tool=tool,
            seconds=seconds,
            output=out,
            error="the testbench ran but the symptom was not there — the reduction was too aggressive",
        )
    return Validation(
        ran=False,
        tool=tool,
        seconds=seconds,
        output=out,
        error="the testbench did not compile or did not run to the check",
    )


def _run_icarus(
    tools: tuple[str, str],
    tb: Path,
    sources: list[Path],
    work: Path,
    top: str,
    timeout: float,
    incdirs: list[str] | None,
    defines: list[str] | None,
) -> tuple[str, str]:
    from veritrace import simulate

    iverilog, vvp = tools
    exe = work / f"{tb.stem}.vvp"
    cmd = [iverilog, "-g2012", "-o", str(exe), "-s", top]
    for d in simulate.include_path(work, [tb, *sources], list(incdirs or [])):
        cmd += ["-I", str(d)]
    for d in defines or []:
        cmd += [f"-D{d}"]
    cmd += [str(tb), *(str(s) for s in sources)]
    built = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if built.returncode != 0:
        return (built.stdout or "") + (built.stderr or ""), "iverilog"
    ran = subprocess.run(
        [vvp, str(exe)], cwd=str(work), capture_output=True, text=True, timeout=timeout, check=False
    )
    return (ran.stdout or "") + (ran.stderr or ""), "iverilog"


def _run_verilator(
    verilator: str,
    tb: Path,
    sources: list[Path],
    work: Path,
    timeout: float,
    incdirs: list[str] | None,
    defines: list[str] | None,
) -> tuple[str, str]:
    """§8.3's `verilator --binary`, which compiles and runs in one step."""
    from veritrace import simulate

    cmd = [
        verilator,
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--Mdir",
        str(work / "obj_dir"),
        "--top-module",
        tb.stem,
    ]
    for d in simulate.include_path(work, [tb, *sources], list(incdirs or [])):
        cmd.append(f"-I{d}")
    for d in defines or []:
        cmd.append(f"+define+{d}")
    cmd += [str(tb), *(str(s) for s in sources)]
    built = subprocess.run(
        cmd, cwd=str(work), capture_output=True, text=True, timeout=timeout, check=False
    )
    text = (built.stdout or "") + (built.stderr or "")
    exe = work / "obj_dir" / f"V{tb.stem}"
    exe = exe if exe.is_file() else exe.with_suffix(".exe")
    if built.returncode != 0 or not exe.is_file():
        return text, "verilator"
    ran = subprocess.run(
        [str(exe)], cwd=str(work), capture_output=True, text=True, timeout=timeout, check=False
    )
    return text + (ran.stdout or "") + (ran.stderr or ""), "verilator"


# --- the loop §8.3 asks for -------------------------------------------------


def build(
    sub: Subtrace,
    graph: Any,
    store: Any,
    clock: Any,
    elaboration: Any,
    root: CausalNode | None = None,
    sources: list[Path] | None = None,
    work: Path | None = None,
    validate_it: bool = True,
    timeout: float = 120.0,
    incdirs: list[str] | None = None,
    defines: list[str] | None = None,
    mode: str | None = None,
) -> Repro:
    """Generate, validate, and back off until the failure actually reproduces.

    Without sources to compile against, the level-0 testbench is returned
    unvalidated and says so — a generated file nobody ran is a draft, and §8.3 is
    clear that the loop is what makes it more than that.
    """
    first = generate(sub, graph, store, clock, elaboration, root=root, level=0, mode=mode)
    if not validate_it or not sources:
        if validate_it:
            first.notes.append("not validated: no RTL sources were given to compile against.")
        return first

    work = Path(work) if work is not None else Path.cwd() / ".veritrace" / "repro"
    levels = [0] if first.mode == "focused" else [0, 1, 2]
    attempt = first
    for level in levels:
        attempt = (
            first
            if level == 0
            else generate(sub, graph, store, clock, elaboration, root=root, level=level, mode=mode)
        )
        attempt.validation = validate(
            attempt, sources, work, timeout=timeout, incdirs=incdirs, defines=defines
        )
        if attempt.validation.reproduced:
            if level > 0:
                attempt.notes.append(
                    f"level {level}: the reduced stimulus did not reproduce the failure, "
                    "so more of the recorded input was put back (§8.3)."
                )
            if level >= 2 and attempt.mode == "minimal":
                # §8.3 is explicit that these must not share a name. Every input
                # is being replayed, so nothing was minimised, and the label has
                # to say what happened rather than what was attempted.
                attempt.mode = "focused"
                attempt.mode_reason = (
                    "the minimised stimulus did not reproduce the failure, so this is a "
                    "faithful replay of every input — a window cut, not a minimisation (§8.3)."
                )
            return attempt
        if not attempt.validation.ran:
            # A compile error will not be fixed by replaying more stimulus.
            return attempt
    attempt.notes.append(
        "even a faithful replay of every input did not reproduce the symptom — "
        "the failure depends on state this module does not receive through its ports."
    )
    return attempt
