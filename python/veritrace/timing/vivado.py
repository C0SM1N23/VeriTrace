"""Reading Vivado's `report_timing_summary` — §8.30.

Not a static timing analysis. §8.30 is explicit: *"Nu faci STA. Faci navigare si
atribuire peste STA-ul altcuiva."* Vivado already computed the paths; what it
cannot do is say which of them the design actually exercises, because it has
never seen a simulation.

The parser is deliberately tolerant. A timing report's exact columns move between
Vivado versions and between `-max_paths` settings, so what is matched is the
handful of labelled fields that have been stable for a decade — `Slack`,
`Source`, `Destination`, `Data Path Delay` — and a path with a field missing is
kept with that field empty rather than dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

from veritrace.timing.model import Hop, Path as TPath, TimingReport

#: `Slack (VIOLATED) :        -0.417ns` and `Slack (MET) : 1.234ns`.
_SLACK = re.compile(r"^\s*Slack\s*\((MET|VIOLATED)\)\s*:\s*(-?[\d.]+)\s*ns", re.M)
_FIELD = re.compile(r"^\s{2,}(Source|Destination|Path Group|Path Type|Data Path Delay)\s*:\s*(.+?)\s*$", re.M)
#: One row of the per-hop table:
#:
#:     SLICE_X12Y30   FDRE (Prop_fdre_C_Q)   0.456   0.456 r  regs/rdata_reg[7]/Q
#:                    net (fo=3, routed)     1.204   1.660    regs/rdata[7]
#:
#: The two numbers are the increment and the cumulative delay; the resource is
#: the last field, and the optional `r`/`f` between them is the edge. Anchoring
#: on the numbers rather than on columns is what survives Vivado renaming a
#: header or changing a width between versions.
_HOP = re.compile(
    r"^\s+.*?(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*(?:[rf]\s+)?(\S+)\s*$", re.M
)
#: The summary block Vivado prints before the paths.
_WNS = re.compile(r"WNS\(ns\)\s*.*?\n\s*-+.*?\n\s*(-?[\d.]+)\s+(-?[\d.]+)", re.S)


def parse(text: str) -> TimingReport:
    """Every critical path in a `report_timing_summary`, worst slack first."""
    report = TimingReport()
    wns = _WNS.search(text)
    if wns:
        report.wns, report.tns = float(wns.group(1)), float(wns.group(2))

    # Each path begins at its own `Slack (...)` line and runs to the next one.
    marks = list(_SLACK.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.start() : end]
        path = TPath(slack=float(m.group(2)), met=m.group(1) == "MET")
        for name, value in _FIELD.findall(block):
            match name:
                case "Source":
                    path.source = pin(value)
                case "Destination":
                    path.destination = pin(value)
                case "Path Group":
                    path.group = value
                case "Path Type":
                    path.kind = value.split()[0]
                case "Data Path Delay":
                    path.delay_ns = _ns(value)
        path.hops = [
            Hop(delay_ns=float(d), cumulative_ns=float(c), resource=r.strip())
            for d, c, r in _HOP.findall(block)
            # Only rows that name a netlist resource. The clock-edge row and the
            # separator carry numbers too, and a hop with nothing to attribute
            # the delay to is not one a reader can act on.
            if "/" in r
        ]
        report.paths.append(path)

    report.paths.sort(key=lambda p: p.slack)
    return report


def load(path: Path | str) -> TimingReport:
    return parse(Path(path).read_text(encoding="utf-8", errors="replace"))


def pin(text: str) -> str:
    """`top/u_ctrl/state_reg[3]/C` -> `top/u_ctrl/state_reg[3]`.

    The pin is what Vivado reports; the *register* is what correlates with a
    signal in the trace, and the pin name after the last slash is a port of the
    flop (`C`, `D`, `Q`), never part of the hierarchy.
    """
    name = text.strip().split()[0]
    head, _, tail = name.rpartition("/")
    return head if head and len(tail) <= 3 and tail.isalpha() else name


def _ns(text: str) -> float:
    m = re.search(r"(-?[\d.]+)", text)
    return float(m.group(1)) if m else 0.0
