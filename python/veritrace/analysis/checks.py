"""Every automatic check, run in one place — §1.4, §13.4.

*Findings that appear on their own when a session opens, with no question
asked.* §1.4 calls this the engine of daily adoption, and §13.4 makes it the
first impression: the tab you land on already knows something you did not.

The contract is deliberately thin so adding a detector is adding one line here:
a detector is any callable that yields `Finding`s. Each runs behind a guard, so
a detector that raises degrades to a recorded skip rather than an empty Checks
tab (P7) — a check that silently does nothing is indistinguishable from a
design with no problems, which is the one failure mode this feature cannot
afford.
"""

from __future__ import annotations

import time as _time
from typing import Any, Callable, Iterator

from veritrace.analysis import lint, liveness, memory, params, protocol, stuck, xprop
from veritrace.analysis.findings import Finding, Group, Report
from veritrace.clocks import Clock

#: Every check name the tool can produce, for `--fail-on` and `checks.disable`.
ALL_CHECKS: dict[str, str] = {
    stuck.CHECK: "signal frozen for longer than the threshold",
    xprop.CHECK: "root cause of an X",
    params.CHECK: "parameter left on a default the parent contradicts",
    protocol.CHECK: "protocol rule from a pack that did not hold (§8.14)",
    **liveness.CHECKS,
    **memory.CHECKS,
    **lint.CHECKS,
}

#: Group a check belongs to, so `--fail-on stuck,x,cdc` can name either.
GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "stuck": (stuck.CHECK,),
    "x": (xprop.CHECK, "x_optimism"),
    "lint": tuple(lint.CHECKS),
    "cdc": ("cdc_no_sync",),
    "parameters": (params.CHECK,),
    "protocol": (protocol.CHECK,),
    "liveness": tuple(liveness.CHECKS),
    "deadlock": (liveness.CHECK_DEADLOCK,),
    "memory": tuple(memory.CHECKS),
}


def expand_checks(names: Iterator[str] | list[str] | tuple[str, ...]) -> set[str]:
    """Resolve `--fail-on stuck,x,cdc` into concrete check names."""
    out: set[str] = set()
    for name in names:
        n = name.strip()
        if not n:
            continue
        out.update(GROUP_ALIASES.get(n, (n,)))
    return out


def run_all(
    store: Any,
    graph: Any = None,
    elaboration: Any = None,
    clock: Clock | None = None,
    config: Any = None,
    analysis: Any = None,
    liveness_report: Any = None,
    memory_reports: Any = None,
) -> Report:
    """Run every applicable check.

    Only `store` is required: §7.4 makes "a dump and no RTL" a supported mode,
    and the stuck detector still works there. Checks that need the graph record
    why they were skipped instead of quietly contributing nothing.
    """
    report = Report()
    started = _time.perf_counter()

    disabled = set(getattr(config, "disabled_checks", ()) or ())
    disabled = expand_checks(disabled) if disabled else set()

    def run(name: str, needs: str | None, fn: Callable[[], Iterator[Finding]]) -> None:
        if needs:
            report.skipped[name] = needs
            return
        try:
            report.findings.extend(f for f in fn() if f.check not in disabled)
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the rest
            report.skipped[name] = f"failed: {e}"

    no_clock = None if clock is not None else "no clock could be identified in this trace"
    no_rtl = None if graph is not None else "no RTL loaded"

    run(
        Group.STUCK.value,
        no_clock,
        lambda: stuck.scan(store, clock, graph, config),
    )
    run(
        Group.X_SOURCES.value,
        no_rtl,
        lambda: xprop.scan(store, graph, clock, config),
    )
    # An extraction that found no interfaces is a *result*, not a skip: most
    # designs have no bus and saying "not run: protocol" on every one of them
    # would train people to ignore the line that matters. Only a caller that
    # never extracted at all gets a skip.
    run(
        Group.PROTOCOL.value,
        None if analysis is not None else "protocol extraction was not run",
        lambda: protocol.scan(analysis, clock, config),
    )
    # §8.18, on the same terms: a design with no interfaces has no agents, which
    # is a result and not a skip. Only a caller that never ran the scan is told
    # it did not run.
    run(
        Group.LIVENESS.value,
        None if liveness_report is not None else "the liveness scan was not run",
        lambda: liveness.scan(liveness_report, clock, config),
    )
    # §8.20, same terms again: no memory interfaces is a result, no scan at
    # all is a skip.
    run(
        Group.MEMORY.value,
        None if memory_reports is not None else "the memory scan was not run",
        lambda: memory.scan(memory_reports, config),
    )
    run(
        Group.LINT.value,
        no_rtl,
        lambda: lint.scan(
            graph,
            elaboration.diagnostics if elaboration is not None else (),
            store,
            clock,
            config,
        ),
    )
    run(
        Group.PARAMETERS.value,
        None if elaboration is not None else "no RTL loaded",
        lambda: params.scan(elaboration, config),
    )

    report.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return report


def default_tab(
    report: Report,
    n_interfaces: int = 0,
    configured: str | None = None,
    n_memory_interfaces: int = 0,
) -> str:
    """Which tab to open on — §11.4b.

    *Memory interfaces -> Memory; two or more interfaces -> Transactions;
    otherwise Checks.* Memory outranks the interface count, on the reasoning
    §11.4b gives for the whole rule: SDRAM/DDR has a vocabulary of its own —
    banks, rows, commands — that neither Wave nor a generic Transactions Gantt
    expresses well, so a design that has one goes straight to the tab built
    for it. Below that, the refinement §13.4 asks for: land on Checks when
    there is something there and on Wave when there is not, because an empty
    Checks tab is exactly the blank canvas §13.4 wants to avoid.
    """
    if configured:
        return configured
    if n_memory_interfaces >= 1:
        return "memory"
    if n_interfaces >= 2:
        return "transactions"
    return "checks" if len(report) else "wave"
