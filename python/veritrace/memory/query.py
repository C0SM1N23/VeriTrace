"""The §10.1 memory commands.

    cmds(sdram_iface)      banks(sdram_iface)
    timing(sdram_iface, chip=mt48lc16m16a2)
    rowhits(sdram_iface)

The same dispatch-table shape `perf.query` uses for §8.17-8.18: one entry per
command, so a new one is a dict line, not a parser change.

`timing()` is the one command that can do more than read back what session-open
already computed: passing `chip=` for a chip other than the one the session
picked re-runs just the timing checker against a different `packs/timing/`
file, which is what "what would this trace look like against a faster part"
actually needs — nothing else in the report changes with the chip, so nothing
else is recomputed.
"""

from __future__ import annotations

from typing import Any, Callable

from veritrace.analysis.vtq import Call, Pipeline, QueryError
from veritrace.memory import banks as banks_mod
from veritrace.memory.model import MemoryReport
from veritrace.memory.timing import TimingError, find as find_chip

NEEDS_IFACE = ("cmds", "banks", "timing", "rowhits")
COMMANDS = NEEDS_IFACE


def _report(reports: list[MemoryReport], call: Call) -> MemoryReport:
    known = [r.iface for r in reports]
    if not call.args:
        if len(known) == 1:
            name = known[0]
        else:
            raise QueryError(
                f"`{call.name}()` needs an interface; this session has "
                f"{', '.join(known) or 'none'}"
            )
    else:
        name = str(call.args[0])
    r = next((r for r in reports if r.iface == name), None)
    if r is None:
        raise QueryError(f"no memory interface named `{name}`; detected: {', '.join(known) or 'none'}")
    return r


def run(
    reports: list[MemoryReport] | None,
    pipeline: Pipeline,
    store: Any = None,
    clock: Any = None,
    project_root: Any = None,
) -> dict[str, Any]:
    """Execute one parsed memory command."""
    call = pipeline.source
    if pipeline.stages:
        raise QueryError(f"`{call.name}()` does not take a `| stage`")
    if not reports:
        raise QueryError("memory analysis did not run for this session, or found no interface")

    fn: Callable[..., dict[str, Any]] | None = _DISPATCH.get(call.name)
    if fn is None:
        raise QueryError(f"`{call.name}()` is not a memory command; try {', '.join(COMMANDS)}")
    return {"kind": call.name, **fn(reports, call, store, clock, project_root)}


def _cmds(reports: list[MemoryReport], call: Call, *_a: Any) -> dict[str, Any]:
    r = _report(reports, call)
    return {"iface": r.iface, "commands": [c.to_dict() for c in r.commands]}


def _banks(reports: list[MemoryReport], call: Call, *_a: Any) -> dict[str, Any]:
    r = _report(reports, call)
    return {"iface": r.iface, "n_banks": r.n_banks, "segments": [s.to_dict() for s in r.segments]}


def _rowhits(reports: list[MemoryReport], call: Call, *_a: Any) -> dict[str, Any]:
    r = _report(reports, call)
    return {"iface": r.iface, "efficiency": r.efficiency.to_dict()}


def _timing(
    reports: list[MemoryReport], call: Call, store: Any, clock: Any, project_root: Any
) -> dict[str, Any]:
    r = _report(reports, call)
    chip = call.kwargs.get("chip")
    if not chip or str(chip) == r.chip:
        return {
            "iface": r.iface,
            "chip": r.chip,
            "violations": [v.to_dict() for v in r.violations],
            "checked": dict(r.checked),
            "skipped": dict(r.skipped),
        }
    # A different chip: only the timing half depends on it, so only that half
    # is recomputed.
    if store is None:
        raise QueryError("comparing against another chip needs the trace open, not just the report")
    try:
        chip_timing = find_chip(str(chip), project_root)
    except TimingError as e:
        raise QueryError(str(e)) from e
    violations, checked, skipped = banks_mod.check_timing(
        r.commands, r.n_banks, chip_timing, store.timescale, clock
    )
    return {
        "iface": r.iface,
        "chip": chip_timing.slug,
        "violations": [v.to_dict() for v in violations],
        "checked": checked,
        "skipped": skipped,
    }


_DISPATCH: dict[str, Any] = {
    "cmds": _cmds,
    "banks": _banks,
    "timing": _timing,
    "rowhits": _rowhits,
}
