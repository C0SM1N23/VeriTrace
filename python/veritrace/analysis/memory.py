"""SDRAM timing violations as automatic findings — §8.20, §11.4.

*Toate trei sunt scan-uri... deci ruleaza automat la deschiderea sesiunii* is
§8.18's line about liveness, and §8.20's timing checker earns the identical
treatment for the identical reason: a violated `tRCD` is the "clasa numarul
unu de bug la bring-up de memorie" precisely because it corrupts data
silently, on a schedule nobody is watching for. Surfacing it unasked, next to
the stuck signals and the protocol violations, is what makes it findable at
all.
"""

from __future__ import annotations

from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity, location_of

CHECK = "memory_timing"

CHECKS: dict[str, str] = {
    CHECK: "an SDRAM timing constraint that did not hold (§8.20)",
}


def _cyc(n: int) -> str:
    return f"{n} cycle" + ("" if n == 1 else "s")


def scan(reports: Any, config: Any = None, graph: Any = None) -> Iterator[Finding]:
    """Turn every §8.20 `MemoryReport`'s violations into findings."""
    if not reports:
        return
    for r in reports:
        # `cs_n` is the one signal every memory pack's [detect] requires, so it
        # is always resolvable — the "a command is happening" wire, and asking
        # why *it* is asserted here traces back through whatever in the
        # controller decided to issue the violating command too soon.
        cs_n = r.signals.get("cs_n")
        for v in r.violations:
            direction = "exceeded" if v.is_maximum else "was too short"
            measured = _cyc(v.measured_cycles) if v.limit_cycles is not None else f"{v.measured_ticks} ticks"
            limit = _cyc(v.limit_cycles) if v.limit_cycles is not None else f"{v.limit_ticks} ticks"
            title = (
                f"{r.iface}: {v.constraint} {direction} "
                f"({measured}, {'max' if v.is_maximum else 'min'} {limit})"
            )
            bank_note = f"bank {v.bank}" if v.bank is not None else "device-wide"
            first = f"{v.first.name}@{v.first.time}" if v.first else "?"
            second = f"{v.second.name}@{v.second.time}" if v.second else "?"
            yield Finding(
                group=Group.MEMORY,
                severity=Severity.WARN,
                check=CHECK,
                title=title,
                signal=cs_n,
                loc=location_of(graph, cs_n),
                time=v.at,
                detail=f"{bank_note}: {first} -> {second}",
                why=f"why({cs_n} @ {v.at})" if cs_n else None,
                related=(v.constraint,),
            )
