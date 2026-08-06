"""What §8.20's analysis produces — the SDRAM/DDR equivalent of `perf.model`.

Nothing here runs a simulator or knows a chip by name: everything is derived
from a decoded command stream and the timing parameters a `[chip].toml` states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A decoded field value: an integer when known, `None` when the cycle that
#: produced it could not be decided (an X on the address bus).
FieldValue = int | None

#: The states §8.20's bank diagram names, in the order a bank actually visits
#: them. Kept as plain strings rather than an enum: every consumer (JSON, the
#: frontend) wants the label, and there is nothing else to attach to a state.
IDLE = "idle"
ACTIVATING = "activating"
ACTIVE = "active"
PRECHARGING = "precharging"


@dataclass(slots=True)
class CmdEvent:
    """One decoded command — a cycle where a `[[command]]` row's `encode`
    matched."""

    time: int
    name: str
    #: `args`, evaluated — e.g. `{"bank": 2, "row": 420}` for an ACTIVATE.
    fields: dict[str, FieldValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"time": self.time, "name": self.name, "fields": dict(self.fields)}


@dataclass(slots=True)
class BankSegment:
    """One stretch of one bank's timeline, for TAB 10's bank-per-row view.

    Segments are contiguous and cover the whole run with no gaps — the same
    reasoning §8.17 applies to stall attribution: a timeline with silent holes
    is not one you can trust the shape of.
    """

    bank: int
    state: str
    t0: int
    t1: int
    #: The open row, while one is open; `None` in `idle`.
    row: FieldValue = None

    def to_dict(self) -> dict[str, Any]:
        return {"bank": self.bank, "state": self.state, "t0": self.t0, "t1": self.t1, "row": self.row}


@dataclass(slots=True)
class TimingViolation:
    """One §8.20 constraint that did not hold, with both commands it was
    measured between — everything `why()` and the UI need to point at it."""

    constraint: str
    #: `None` for a device-wide constraint (tFAW, tRFC, tREFI); the bank
    #: number for a same-bank one.
    bank: int | None
    #: Trace time of the second (violating) command — where a click lands.
    at: int
    measured_cycles: int
    limit_cycles: int
    #: True only for tREFI: a violation there means the interval grew past the
    #: limit, not that it fell short of one.
    is_maximum: bool = False
    first: CmdEvent | None = None
    second: CmdEvent | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint": self.constraint,
            "bank": self.bank,
            "at": self.at,
            "measured_cycles": self.measured_cycles,
            "limit_cycles": self.limit_cycles,
            "is_maximum": self.is_maximum,
            "first": self.first.to_dict() if self.first else None,
            "second": self.second.to_dict() if self.second else None,
        }


@dataclass(slots=True)
class Series:
    """A time series, sampled on fixed windows — the same shape `perf.model`
    uses, so the frontend's chart code does not need a second variant."""

    name: str
    unit: str
    window: int = 0
    points: list[tuple[int, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "window": self.window,
            "points": [{"t": t, "v": round(v, 4)} for t, v in self.points],
        }


@dataclass(slots=True)
class RowStats:
    """§8.20's row hit/miss/conflict totals for one bank, plus the series."""

    hits: int = 0
    misses: int = 0
    conflicts: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses + self.conflicts

    @property
    def hit_rate(self) -> float | None:
        return None if not self.total else self.hits / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "conflicts": self.conflicts,
            "hit_rate": None if self.hit_rate is None else round(self.hit_rate, 4),
        }


@dataclass(slots=True)
class Efficiency:
    """§8.20's efficiency metrics, as time series plus their totals."""

    row_hits: RowStats = field(default_factory=RowStats)
    #: Per-bank breakdown of the same totals.
    per_bank: dict[int, RowStats] = field(default_factory=dict)
    row_hit_series: Series | None = None
    bus_utilization: float | None = None
    bus_utilization_series: Series | None = None
    refresh_overhead: float | None = None
    #: Cycles lost to a read<->write direction change, and how many happened.
    turnaround_cycles: int = 0
    turnaround_events: int = 0
    #: Average simultaneously-active banks.
    bank_parallelism: float | None = None
    bank_parallelism_series: Series | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_hits": self.row_hits.to_dict(),
            "per_bank": {str(b): s.to_dict() for b, s in self.per_bank.items()},
            "row_hit_series": self.row_hit_series.to_dict() if self.row_hit_series else None,
            "bus_utilization": self.bus_utilization,
            "bus_utilization_series": (
                self.bus_utilization_series.to_dict() if self.bus_utilization_series else None
            ),
            "refresh_overhead": self.refresh_overhead,
            "turnaround_cycles": self.turnaround_cycles,
            "turnaround_events": self.turnaround_events,
            "bank_parallelism": self.bank_parallelism,
            "bank_parallelism_series": (
                self.bank_parallelism_series.to_dict() if self.bank_parallelism_series else None
            ),
        }


@dataclass(slots=True)
class MemoryReport:
    """Everything TAB 10 shows for one detected memory interface."""

    iface: str
    chip: str
    n_banks: int
    #: Pack-relative name -> full trace path, the same shape `Interface.signals`
    #: uses — carried here so a violation's `why()` and the UI's "load these
    #: into Wave" both have somewhere to get a real path from.
    signals: dict[str, str] = field(default_factory=dict)
    #: §8.20's address map, resolved from the pack's expressions to plain
    #: `[hi, lo]` bit ranges. Resolved server-side on purpose: the pack writes
    #: `addr[24:12]`, and re-implementing the expression engine in the browser
    #: to read three constant slices would cost far more than it saves — while
    #: hard-coding the shipped pack's ranges there would silently ignore a
    #: project that overrode them. A field whose expression is not a simple
    #: slice is absent rather than guessed at.
    address_map: dict[str, tuple[int, int]] = field(default_factory=dict)
    commands: list[CmdEvent] = field(default_factory=list)
    segments: list[BankSegment] = field(default_factory=list)
    violations: list[TimingViolation] = field(default_factory=list)
    efficiency: Efficiency = field(default_factory=Efficiency)
    #: `constraint -> count`, including zero for a constraint never violated —
    #: so "conforme" is a stated fact, not an absence.
    checked: dict[str, int] = field(default_factory=dict)
    #: Why a constraint could not be checked (e.g. no data signal for CL/CWL).
    skipped: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self, with_commands: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "iface": self.iface,
            "chip": self.chip,
            "n_banks": self.n_banks,
            "signals": dict(self.signals),
            "address_map": {k: list(v) for k, v in self.address_map.items()},
            "n_commands": len(self.commands),
            "segments": [s.to_dict() for s in self.segments],
            "violations": [v.to_dict() for v in self.violations],
            "efficiency": self.efficiency.to_dict(),
            "checked": dict(self.checked),
            "skipped": dict(self.skipped),
            "ms": round(self.elapsed_ms, 2),
        }
        if with_commands:
            out["commands"] = [c.to_dict() for c in self.commands]
        return out
