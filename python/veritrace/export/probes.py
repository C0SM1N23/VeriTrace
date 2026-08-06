"""ILA and SignalTap probe files from a causal cone — §8.11b.

The observation that makes this worth building before any on-board capture can
be read back: *the signals in the causal cone are exactly the ones worth
probing.* Translating `cone()` into an ILA or SignalTap configuration is a
direct mapping, and it needs only the graph — no trace, no capture.

That makes VeriTrace useful **before** the bug, when the capture is being set
up, which is the moment the choice of probes is actually made and the moment
getting it wrong costs a whole board iteration.

Both generators emit TCL, because both toolchains are driven by it. Neither is
a substitute for reading the vendor documentation on capture depth and clock
choice; the files name what to probe and leave the rest configured
conservatively, with the guesses marked in comments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

#: An ILA/SignalTap capture is a few thousand samples, not 10^8 (§8.11b). Past
#: this many probes the depth that fits on the device collapses, so the
#: generators say so rather than emitting a configuration that will not build.
CROWDED = 32


@dataclass(slots=True)
class ProbePlan:
    """What to capture, and on which clock."""

    #: Hierarchical paths, cone order — nearest to the signal of interest first.
    signals: list[str]
    #: The signal under investigation.
    target: str
    #: Sampling clock. Guessed from the design when not given, and marked as a
    #: guess in the output either way.
    clock: str | None = None
    depth: int = 1024
    #: Widths, for tools that want them declared.
    widths: dict[str, int] | None = None

    def width_of(self, path: str) -> int:
        return (self.widths or {}).get(path, 1)

    @property
    def crowded(self) -> bool:
        return len(self.signals) > CROWDED


def plan_from_cone(result: Any, graph: Any, clock: str | None = None, depth: int = 1024) -> ProbePlan:
    """Turn a `cone.Cone` into a probe plan.

    Untraced signals stay in: this is about what to *start* capturing, and a
    node the simulation never dumped is often exactly the one missing from the
    picture on the board.
    """
    signals = [n.path for n in result.nodes]
    widths = {}
    for p in signals:
        sig = graph.get(p)
        if sig is not None:
            widths[p] = sig.width
    return ProbePlan(
        signals=signals, target=result.root, clock=clock, depth=depth, widths=widths
    )


def _preamble(plan: ProbePlan, comment: str = "#") -> list[str]:
    out = [
        f"{comment} VeriTrace probe list — the causal cone of {plan.target}",
        f"{comment} {len(plan.signals)} signal(s), capture depth {plan.depth}",
    ]
    if plan.clock is None:
        out.append(f"{comment} WARNING: no sampling clock was given; set it below by hand.")
    if plan.crowded:
        out.append(
            f"{comment} WARNING: {len(plan.signals)} probes is a lot for one capture. "
            f"Narrow the cone with --depth, or split it across runs."
        )
    return out


def vivado(plan: ProbePlan) -> str:
    """A Vivado ILA insertion script (`ila_probes.tcl`).

    Uses `mark_debug` on the nets plus a debug core, which is the flow that
    works from a plain RTL project without hand-editing an IP instance.
    """
    clock = plan.clock or "<clk>"
    out = _preamble(plan)
    out += [
        "",
        "# Mark the cone for debug, then let the debug core wizard pick them up.",
    ]
    for path in plan.signals:
        out.append(f"set_property MARK_DEBUG true [get_nets -hier {{{_vivado_net(path)}}}]")
    out += [
        "",
        "create_debug_core u_ila_0 ila",
        f"set_property C_DATA_DEPTH {plan.depth} [get_debug_cores u_ila_0]",
        "set_property C_TRIGIN_EN false [get_debug_cores u_ila_0]",
        f"set_property port_width 1 [get_debug_ports u_ila_0/clk]",
        f"connect_debug_port u_ila_0/clk [get_nets {{{_vivado_net(clock)}}}]",
    ]
    for i, path in enumerate(plan.signals):
        width = plan.width_of(path)
        out += [
            "",
            f"# {path}",
            f"create_debug_port u_ila_0 probe" if i else "",
            f"set_property port_width {width} [get_debug_ports u_ila_0/probe{i}]",
            f"set_property PROBE_TYPE DATA_AND_TRIGGER [get_debug_ports u_ila_0/probe{i}]",
            f"connect_debug_port u_ila_0/probe{i} [get_nets [list {{{_vivado_net(path)}}}]]",
        ]
    out += [
        "",
        "# Trigger suggestion: the signal you asked about, on any change.",
        f"# The cone is ordered nearest-first, so probe0 is {plan.target}.",
    ]
    return "\n".join(x for x in out if x != "") + "\n"


def _vivado_net(path: str) -> str:
    """Vivado names nets with `/` between instances and keeps bus suffixes."""
    return path.replace(".", "/")


def quartus(plan: ProbePlan) -> str:
    """A Quartus SignalTap configuration script (`stp_probes.tcl`).

    Written against the `::quartus::stp` API, which builds an `.stp` file
    without opening the GUI.
    """
    clock = plan.clock or "<clk>"
    out = _preamble(plan)
    out += [
        "",
        "package require ::quartus::stp",
        "",
        "set stp [stp_create_instance]",
        f"stp_set_instance_property $stp sample_depth {plan.depth}",
        f"stp_set_instance_property $stp clock {{{_quartus_node(clock)}}}",
        "",
    ]
    for path in plan.signals:
        width = plan.width_of(path)
        node = _quartus_node(path)
        bus = f"[{width - 1}:0]" if width > 1 else ""
        out.append(f"stp_add_node $stp {{{node}{bus}}}")
    out += [
        "",
        f"# Trigger suggestion: {plan.target}, on any edge.",
        f"stp_set_node_property $stp {{{_quartus_node(plan.target)}}} trigger_condition either",
        "",
        "stp_write_file $stp stp_probes.stp",
        "stp_close_instance $stp",
    ]
    return "\n".join(out) + "\n"


def _quartus_node(path: str) -> str:
    """SignalTap names nodes with `|` between instances."""
    return path.replace(".", "|")


FORMATS = {"vivado": vivado, "quartus": quartus}


def render(fmt: str, plan: ProbePlan) -> str:
    try:
        return FORMATS[fmt](plan)
    except KeyError:
        raise ValueError(f"unknown probe format {fmt!r}; try vivado or quartus") from None


def guess_clock(graph: Any, signals: Iterable[str]) -> str | None:
    """The clock the probed flops are already synchronous to.

    Sampling a capture on the clock the logic runs on is the only choice that
    gives a readable waveform, and the graph already knows which one that is.
    """
    counts: dict[str, int] = {}
    for path in signals:
        sig = graph.get(path)
        if sig is None:
            continue
        for d in sig.drivers:
            if d.clock is not None:
                counts[d.clock.path()] = counts.get(d.clock.path(), 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
