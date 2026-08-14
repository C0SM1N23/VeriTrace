"""Switching activity, for power estimation — §8.31.

Nearly free once the store exists: the toggle count and the time spent at each
level are what a SAIF file is, and counting transitions is what the store does.

**Honestly scoped.** xsim writes SAIF natively (`-saif` at elaboration), so a
Vivado flow already has this. The feature is for **Icarus, Verilator and
ModelSim**, which write none — and there the alternative is Vivado's 12.5%
default toggle rate, which is a guess applied uniformly to a design where the
clock toggles every cycle and the reset toggles twice.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any

#: SAIF durations are integers in the file's own timescale, so the unit is the
#: trace's own tick. Rescaling to something rounder would lose the short pulses
#: that matter most for activity.
HEADER = "2.0"


@dataclass(slots=True)
class Activity:
    """One signal's switching, in trace ticks."""

    path: str
    width: int
    t0: int = 0
    t1: int = 0
    tx: int = 0
    tz: int = 0
    #: Toggle count. For a vector this counts *changes of the whole value*, which
    #: is what a simulator's own SAIF reports for a bus.
    tc: int = 0

    @property
    def duration(self) -> int:
        return self.t0 + self.t1 + self.tx + self.tz

    @property
    def held(self) -> float | None:
        """Fraction of the run spent without changing — §8.31's clock gating hint."""
        return None if not self.duration else 1.0 - min(1.0, self.tc / max(1, self.duration))


@dataclass(slots=True)
class Report:
    signals: list[Activity] = field(default_factory=list)
    duration: int = 0
    timescale: str = "1ns"

    def busiest(self, n: int = 20) -> list[Activity]:
        return sorted(self.signals, key=lambda a: -a.tc)[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "timescale": self.timescale,
            "signals": [
                {
                    "path": a.path, "width": a.width, "tc": a.tc,
                    "t0": a.t0, "t1": a.t1, "tx": a.tx, "tz": a.tz,
                }
                for a in self.signals
            ],
        }


def _bucket(bits: str) -> str:
    """Which of SAIF's four states a value counts towards.

    A vector is bucketed by whether it is fully known: SAIF's T0/T1 are about a
    *net*, and for a bus the meaningful split is known vs not. Splitting a 32-bit
    value across T0 and T1 by counting its bits would report a number no tool
    consumes.
    """
    low = bits.lower()
    if "x" in low:
        return "tx"
    if "z" in low:
        return "tz"
    return "t0" if set(low) <= {"0"} else "t1"


def measure(store: Any, limit: int | None = None) -> Report:
    """Time at each level and toggle count, per signal."""
    t0, t1 = store.time_range
    out = Report(duration=max(0, t1 - t0), timescale=str(store.timescale))

    for sig in store.signals()[: limit or None]:
        # A vector dumped element by element (`mem[0]`) is not a net of its own.
        if sig.array_index is not None:
            continue
        a = Activity(path=sig.path, width=sig.width)
        events = store.transitions(sig.handle, t0, t1)
        if not events:
            out.signals.append(a)
            continue
        for i, (at, value) in enumerate(events):
            until = events[i + 1][0] if i + 1 < len(events) else t1
            setattr(a, _bucket(value.bits), getattr(a, _bucket(value.bits)) + max(0, until - at))
        # The first event is the initial value, not a transition.
        a.tc = max(0, len(events) - 1)
        out.signals.append(a)
    return out


def render(report: Report, design: str = "design", divider: str = "/") -> str:
    """The report as a SAIF file.

    Grouped by scope into nested `(INSTANCE ...)` blocks, because that is what
    the format is for: a power tool attributes activity to hierarchy, and a flat
    list of paths gives it nothing to attribute to.
    """
    tree: dict[str, Any] = {}
    for a in report.signals:
        *scopes, name = a.path.split(".")
        node = tree
        for scope in scopes:
            node = node.setdefault(scope, {})
        node.setdefault("", []).append((name, a))

    out = [
        "(SAIFILE",
        f'(SAIFVERSION "{HEADER}")',
        '(DIRECTION "backward")',
        f'(DESIGN "{design}")',
        f'(DATE "{_time.strftime("%Y-%m-%d %H:%M:%S")}")',
        '(VENDOR "VeriTrace")',
        '(PROGRAM_NAME "veritrace")',
        f'(DIVIDER {divider} )',
        f"(TIMESCALE {report.timescale})",
        f"(DURATION {report.duration})",
    ]
    out += _instances(tree, 0)
    out.append(")")
    return "\n".join(out) + "\n"


def _instances(node: dict[str, Any], depth: int) -> list[str]:
    pad = "  " * (depth + 1)
    out: list[str] = []
    nets = node.get("", [])
    for scope, child in node.items():
        if scope == "":
            continue
        out.append(f"{pad}(INSTANCE {scope}")
        out += _instances(child, depth + 1)
        out.append(f"{pad})")
    if nets:
        out.append(f"{pad}(NET")
        for name, a in nets:
            out.append(
                f"{pad}  ({name} (T0 {a.t0}) (T1 {a.t1}) (TX {a.tx}) (TZ {a.tz}) "
                f"(TC {a.tc}) (IG 0))"
            )
        out.append(f"{pad})")
    return out


def gating_candidates(report: Report, threshold: float = 0.9) -> list[Activity]:
    """§8.31's clock-gating hint: registers that hold their value almost always.

    Reported as candidates, never as findings — whether a clock *can* be gated is
    a question about the design's structure, and this only knows that it would
    have been worth it.
    """
    return sorted(
        (a for a in report.signals if a.duration and (a.held or 0) >= threshold and a.tc),
        key=lambda a: -(a.held or 0),
    )
