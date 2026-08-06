"""One call that produces everything TAB 10 shows — §8.20.

The seam the CLI, the API and the tests all go through, exactly the role
`perf.report.build` plays for §8.17-8.18 — so "what the Memory tab shows" has
one definition, and a headless run and a browser cannot disagree about it.
"""

from __future__ import annotations

import time as _time
from typing import Any

from veritrace import clocks as clocks_mod
from veritrace.clocks import Clock, to_trace_units
from veritrace.memory import banks, decode, metrics, timing
from veritrace.memory.model import MemoryReport
from veritrace.memory.timing import TimingError
from veritrace.protocol import detect as detect_mod, expr
from veritrace.protocol.channels import Sampler
from veritrace.protocol.model import Interface
from veritrace.protocol.pack import Pack


def detect_interfaces(store: Any, packs: list[Pack], config: Any = None) -> list[Interface]:
    """Every memory (§8.20) interface in the trace.

    The same detection pass §8.14 runs for protocol packs, restricted to the
    ones that declare `[[command]]` instead of `[[channel]]` — `detect.detect`
    already treats the two families identically (§8.14's `[detect]` does not
    care which), so nothing here is memory-specific except the filter.
    """
    memory_packs = [p for p in packs if p.is_memory]
    if not memory_packs:
        return []
    return detect_mod.detect(store, memory_packs, config)


def address_map_ranges(pack: Pack) -> dict[str, tuple[int, int]]:
    """§8.20's `[address_map]` as plain `[hi, lo]` bit ranges.

    Only a bare `name[hi:lo]` reduces to a range; anything more elaborate is
    left out rather than approximated, so the inspector shows the fields it can
    decompose exactly and says nothing about the rest (P1).
    """
    out: dict[str, tuple[int, int]] = {}
    am = pack.address_map
    if am is None:
        return out
    for field_name in ("bank", "row", "col"):
        src = getattr(am, field_name, None)
        if not src:
            continue
        node = expr.parse(src)
        if (
            isinstance(node, expr.Slice)
            and isinstance(node.hi, expr.Const)
            and isinstance(node.lo, expr.Const)
            and isinstance(node.hi.value, int)
            and isinstance(node.lo.value, int)
        ):
            out[field_name] = (node.hi.value, node.lo.value)
    return out


def _n_banks(commands: list) -> int:
    """Bank count as observed, not assumed — a pack states no such number, and
    a design under-using a wider bank field should not be padded with banks
    that never appear."""
    seen = {c.fields.get("bank") for c in commands if isinstance(c.fields.get("bank"), int)}
    return (max(seen) + 1) if seen else 0


def build(
    store: Any,
    iface: Interface,
    session_clock: Clock | None = None,
    chip: str = "mt48lc16m16a2",
    project_root: Any = None,
    config: Any = None,
) -> MemoryReport:
    """§8.20's whole pipeline for one interface: decode, track, check, measure.

    Failure degrades the same way protocol extraction does (P7): a bad chip
    file or an unusable clock is recorded on the report rather than raised, so
    one broken interface does not take the tab down with it.
    """
    started = _time.perf_counter()
    out = MemoryReport(
        iface=iface.name,
        chip=chip,
        n_banks=0,
        signals=dict(iface.signals),
        address_map=address_map_ranges(iface.pack),
    )

    clock = clocks_mod.clock_at(store, iface.clock) if iface.clock else None
    clock = clock or session_clock
    if clock is None or not clock.edges:
        out.skipped["decode"] = "no usable clock for this interface"
        out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
        return out

    try:
        chip_timing = timing.find(chip, project_root)
    except TimingError as e:
        out.skipped["timing"] = str(e)
        chip_timing = None

    sampler = Sampler(store, clock.edges)
    commands, decode_notes = decode.decode(iface, sampler, config)
    out.commands = commands
    out.skipped.update({f"command {k}": v for k, v in decode_notes.items()})
    out.n_banks = _n_banks(commands)

    run_start, run_end = clock.edges[0], clock.edges[-1]

    if out.n_banks == 0:
        out.skipped["banks"] = "no command carried a decodable bank number"
    elif chip_timing is None:
        out.skipped["banks"] = "no chip timing available"
    else:
        rcd = _units(chip_timing.tRCD, store.timescale) or 0
        rp = _units(chip_timing.tRP, store.timescale) or 0
        out.segments = banks.segments(commands, out.n_banks, rcd, rp, run_start, run_end)

        violations, checked, skipped = banks.check_timing(
            commands, out.n_banks, chip_timing, store.timescale, clock
        )
        out.violations = violations
        out.checked = checked
        out.skipped.update(skipped)

        rfc_units = _units(chip_timing.tRFC, store.timescale) or 0
        out.efficiency = metrics.measure(
            commands, out.segments, out.n_banks, rfc_units, run_start, run_end, clock.period
        )

    out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return out


def _units(ns: float, timescale: str) -> int | None:
    return to_trace_units(ns, "ns", timescale)


def build_all(
    store: Any,
    packs: list[Pack],
    session_clock: Clock | None = None,
    config: Any = None,
    project_root: Any = None,
    chip: str | None = None,
) -> list[MemoryReport]:
    """Every memory interface, extracted independently — one bad interface
    must not lose the others (P7), the same rule §8.14's `engine.extract`
    follows for protocol packs."""
    interfaces = detect_interfaces(store, packs, config)
    out: list[MemoryReport] = []
    for iface in interfaces:
        picked_chip = chip or getattr(config, "memory_chip", None) or "mt48lc16m16a2"
        try:
            out.append(build(store, iface, session_clock, picked_chip, project_root, config))
        except Exception as e:  # noqa: BLE001 - one interface must not lose the rest
            r = MemoryReport(iface=iface.name, chip=picked_chip, n_banks=0, signals=dict(iface.signals))
            r.skipped["extraction"] = str(e)
            out.append(r)
    out.sort(key=lambda r: r.iface)
    return out
