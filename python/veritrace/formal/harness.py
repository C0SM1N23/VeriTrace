"""Finding the interface in the RTL, and wrapping it for the solver — §8.27.

Two small pieces sit between "a pack" and "a `.sby` file".

**Detection with no trace.** §8.14's detector already knows how to spot an AXI
port on a set of signal names; it just happens to be pointed at a waveform. A
formal run has no waveform, and the design graph carries the same names, so the
graph is presented to the same detector through the adapter below rather than a
second matcher being written for it.

**A harness, not a `bind`.** Yosys' `bind` support is version-dependent, and more
importantly a formal run needs the design's inputs left *free* so the solver can
drive them — there is no testbench. So the design and the checker are
instantiated side by side in a wrapper whose ports are the design's inputs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from veritrace.graph.model import Kind
from veritrace.protocol import detect
from veritrace.protocol.model import Interface
from veritrace.protocol.pack import Pack
from veritrace.synth.model import Instance, Port


@dataclass(frozen=True, slots=True)
class _Sig:
    """A graph signal wearing the four attributes `detect` reads off a trace one."""

    scope: str
    name: str
    path: str
    width: int
    array_index: None = None


@dataclass(frozen=True, slots=True)
class _Stream:
    stream_id: int


class _RtlStore:
    """The design graph, presented as something `detect.detect` can index.

    `_identity` collapses the several names one bus wears — a master port and
    the slave port it drives — by asking which trace stream each signal is. The
    RTL's answer to the same question is the elaboration's port-connection
    equivalence classes (§7.2 step c), so they are unioned here and the class
    index stands in for the stream id. Without it one AXI port is reported once
    per module it passes through.
    """

    def __init__(self, graph: Any, aliases: list[tuple[str, str]]) -> None:
        self._graph = graph
        self._canon: dict[str, str] = {}
        for a, b in aliases:
            self._union(a, b)

    def _root(self, x: str) -> str:
        while self._canon.get(x, x) != x:
            x = self._canon[x]
        return x

    def _union(self, a: str, b: str) -> None:
        ra, rb = self._root(a), self._root(b)
        if ra != rb:
            self._canon[ra] = rb

    def signals(self) -> list[_Sig]:
        return [
            _Sig(".".join(s.id.hier), s.id.name, s.path, s.width)
            for s in self._graph
            if s.kind is not Kind.PARAM
        ]

    def find(self, path: str) -> str | None:
        return path if self._graph.get(path) is not None else None

    def signal(self, handle: str) -> _Stream:
        # Python salts ``hash(str)`` per process.  The value is only an
        # equivalence-class id, but it participates in interface de-duplication
        # and therefore belongs under P1's deterministic-input rule.  A stable
        # digest also removes the (admittedly tiny) chance that two unrelated
        # buses collapse because their process-local hashes collide.
        root = self._root(handle).encode("utf-8")
        return _Stream(int.from_bytes(hashlib.sha256(root).digest()[:8], "big"))


def interfaces(elaboration: Any, packs: list[Pack], config: Any = None) -> list[Interface]:
    """Every protocol interface in the RTL — §8.14 step 1, without a run."""
    graph = getattr(elaboration, "graph", None)
    if graph is None:
        return []
    aliases = list(getattr(elaboration, "aliases", []) or [])
    return detect.detect(_RtlStore(graph, aliases), packs, config)


def widths(elaboration: Any, iface: Interface) -> dict[str, int]:
    """Pack suffix -> bit width, so the checker declares real vectors.

    Without this every port is one bit wide and `awaddr == awaddr_q` compares
    the bottom bit of an address — a property that holds far more often than the
    one the pack wrote.
    """
    graph = getattr(elaboration, "graph", None)
    out: dict[str, int] = {}
    for suffix, path in iface.signals.items():
        sig = graph.get(path) if graph is not None else None
        if sig is not None:
            out[suffix] = sig.width
    for name in (iface.clock, iface.reset):
        sig = graph.get(name) if (graph is not None and name) else None
        if sig is not None:
            out[name.rsplit(".", 1)[-1]] = sig.width
    return out


def instance_of(elaboration: Any, path: str) -> Instance:
    """The module instantiated at `path`, with its ports and parameters."""
    instances: dict[str, str] = dict(getattr(elaboration, "instances", {}) or {})
    graph = getattr(elaboration, "graph", None)
    ports = sorted(
        (
            Port(s.id.name, "input" if s.kind is Kind.PORT_IN else "output", s.width)
            for s in (graph or ())
            if ".".join(s.id.hier) == path and s.kind in (Kind.PORT_IN, Kind.PORT_OUT)
        ),
        key=lambda p: p.name,
    )
    params = getattr(elaboration, "parameters", {}) or {}
    values = tuple(
        (p.name, p.value)
        for p in sorted(params.get(path, {}).values(), key=lambda p: p.name)
        if not p.local
    )
    return Instance(path=path, module=instances.get(path, ""), ports=ports, parameters=values)


TOP = "vt_formal_top"


def reset_constraint(reset: str | None, active_low: bool = True) -> list[str]:
    """§8.27 step 2's "constrangeri de reset", and it is not optional.

    A formal run starts every register at an arbitrary value — that is what makes
    the result a claim about all runs rather than about one. Left alone, the
    design therefore begins mid-transaction with nothing consistent, and every
    property fails at step 1 with a "counterexample" that is really just an
    impossible starting state. The result looks damning and means nothing, which
    is worse than no result at all.

    So the reset is asserted in the initial state and released afterwards.
    `$initstate` is Yosys' own "is this the first step", so this needs no counter
    of its own — and a counter would have an arbitrary initial value too.
    """
    if not reset:
        return [
            "  // No reset was identified for this interface, so the design starts in",
            "  // an arbitrary state and an early counterexample may be unreachable in",
            "  // practice. Name one with --reset to remove that doubt.",
        ]
    asserted = f"!{reset}" if active_low else reset
    released = reset if active_low else f"!{reset}"
    return [
        "  // §8.27 step 2 — reset constraints. Without these every register starts",
        "  // arbitrary and every property fails at step 1 for reasons that have",
        "  // nothing to do with the design.",
        "  always @(*) begin",
        f"    if ($initstate) assume ({asserted});",
        f"    else            assume ({released});",
        "  end",
    ]


def build(
    dut: Instance,
    iface: Interface,
    checker: str,
    checker_ports: list[Port],
    reset_active_low: bool = True,
) -> str:
    """A wrapper instantiating the design and the checker on the same nets.

    The design's inputs become the wrapper's inputs and are therefore *free*:
    `prep -top` leaves a top-level input unconstrained, which is precisely the
    "any legal stimulus" a bounded model check searches over. Its outputs become
    wires, so the checker can read them without the solver being able to choose
    them.
    """
    leaf = {suffix: path.rsplit(".", 1)[-1] for suffix, path in iface.signals.items()}
    inputs = [p for p in dut.ports if p.direction == "input"]
    outputs = [p for p in dut.ports if p.direction == "output"]
    known = {p.name for p in dut.ports}

    conns = []
    for p in checker_ports:
        # A port of the design with the same name first. The clock and reset
        # arrive that way — `render` names those ports after the interface's own
        # clock and reset, so they are already the design's spelling, and looking
        # for a literal `clk` instead ties the checker's clock to a constant and
        # produces a `$dff` no formal pass can do anything with.
        target = p.name if p.name in known else leaf.get(p.name)
        # A checker port with nothing behind it is tied off rather than left
        # dangling: an unconnected input in a formal harness is a free variable,
        # and the solver would satisfy the property by choosing it.
        conns.append(f"    .{p.name}({target if target in known else f'{p.width}\'b0'})")

    return "\n".join(
        [
            "// Generated by VeriTrace — §8.27. The design under free stimulus, with",
            f"// {checker} watching {iface.name}.",
            "//",
            "// Every input below is a port of this wrapper and therefore unconstrained:",
            "// the solver drives them, which is what makes the result a claim about",
            "// *all* runs to the depth searched rather than about one of them.",
            f"module {TOP} (",
            ",\n".join(f"  {p.decl}" for p in inputs),
            ");",
            *(f"  wire [{p.width - 1}:0] {p.name};" if p.width > 1 else f"  wire {p.name};" for p in outputs),
            "",
            # The parameters this instance actually elaborated to, not the
            # module's defaults. A formal result is about a *configuration*: the
            # AXI master in `designs/axi_lite` is correct at its default
            # `BAD_AT = -1` and violates AXI_AWSTABLE at the `BAD_AT = 9` the
            # testbench gives it, and verifying the wrong one of those proves
            # nothing about the design that exists.
            *(
                [
                    f"  {dut.module} #(",
                    ",\n".join(f"    .{n}({v})" for n, v in dut.parameters),
                    "  ) u_dut (",
                ]
                if dut.parameters
                else [f"  {dut.module} u_dut ("]
            ),
            ",\n".join(f"    .{p.name}({p.name})" for p in dut.ports),
            "  );",
            "",
            f"  {checker} u_chk (",
            ",\n".join(conns),
            "  );",
            "",
            *reset_constraint(
                iface.reset.rsplit(".", 1)[-1] if iface.reset else None, reset_active_low
            ),
            "endmodule",
            "",
        ]
    )
