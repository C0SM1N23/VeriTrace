"""The §10.1 performance commands.

    stalls(iface)              outstanding(iface)
    latency(iface, by=master)  fairness()
    throughput(iface)          deadlock()   livelock()   starvation()

A dispatch table over the generic `Call` the VTQ parser already produces, which
is what §10.1's grammar was built generically for: adding a command here is a
dict entry, not a parser change.

Every command answers about a session that has *already* been measured. Running
the scan on demand would make the same query mean different things depending on
whether the tab had been opened, and §8.18 is explicit that these run when the
session opens.
"""

from __future__ import annotations

from typing import Any, Callable

from veritrace.analysis.vtq import Call, Pipeline, QueryError
from veritrace.perf import metrics
from veritrace.perf.model import PerfReport

#: Commands that need an interface named, so the error can say so up front
#: rather than after the lookup fails.
NEEDS_IFACE = ("stalls", "latency", "throughput", "outstanding")

COMMANDS = (*NEEDS_IFACE, "fairness", "deadlock", "livelock", "starvation")


def _iface(report: PerfReport, analysis: Any, call: Call) -> tuple[str, Any]:
    """Resolve the argument to an interface, or explain what was available."""
    known = [i.iface for i in report.interfaces]
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
    ex = analysis.get(name) if analysis is not None else None
    resolved = ex.interface.name if ex is not None else name
    perf = report.get(resolved)
    if perf is None:
        raise QueryError(
            f"no interface named `{name}`; detected: {', '.join(known) or 'none'}"
        )
    return resolved, ex


def run(
    report: PerfReport | None,
    analysis: Any,
    wait_for: list,
    pipeline: Pipeline,
) -> dict[str, Any]:
    """Execute one parsed performance command."""
    call = pipeline.source
    if pipeline.stages:
        raise QueryError(f"`{call.name}()` does not take a `| stage`")
    if report is None:
        raise QueryError("performance analysis did not run for this session")

    fn: Callable[[], dict[str, Any]] | None = _DISPATCH.get(call.name)
    if fn is None:
        raise QueryError(
            f"`{call.name}()` is not a performance command; try "
            f"{', '.join(COMMANDS)}"
        )
    return {"kind": call.name, **fn(report, analysis, wait_for, call)}


def _stalls(report: PerfReport, analysis: Any, _: list, call: Call) -> dict[str, Any]:
    name, _ex = _iface(report, analysis, call)
    prof = report.get(name).stalls
    if prof is None:
        raise QueryError(
            f"no per-cycle profile for `{name}`; "
            f"{report.get(name).notes.get('perf', 'the extraction produced none')}"
        )
    return {"iface": name, "stalls": prof.to_dict()}


def _latency(report: PerfReport, analysis: Any, _: list, call: Call) -> dict[str, Any]:
    name, ex = _iface(report, analysis, call)
    by = call.kwargs.get("by")
    if ex is None:
        return {"iface": name, "latency": report.get(name).latency.to_dict()}
    got = metrics.latency(ex, str(by) if by else None)
    return {
        "iface": name,
        "by": by,
        "latency": got["all"].to_dict(),
        "groups": {k: v.to_dict() for k, v in (got.get("groups") or {}).items()},
    }


def _throughput(report: PerfReport, analysis: Any, _: list, call: Call) -> dict[str, Any]:
    name, _ex = _iface(report, analysis, call)
    per = report.get(name)
    return {
        "iface": name,
        "throughput": per.throughput.to_dict() if per.throughput else None,
        "bytes": per.bytes_moved,
        "peak_bytes_per_cycle": per.peak_bytes_per_cycle,
        "burst_efficiency": per.burst_efficiency,
    }


def _outstanding(report: PerfReport, analysis: Any, _: list, call: Call) -> dict[str, Any]:
    name, _ex = _iface(report, analysis, call)
    per = report.get(name)
    return {
        "iface": name,
        "outstanding": per.outstanding.to_dict() if per.outstanding else None,
        "peak": per.outstanding_peak,
        "plateau": per.outstanding_plateau,
    }


def _fairness(report: PerfReport, _a: Any, _w: list, _c: Call) -> dict[str, Any]:
    return {"fairness": report.fairness.to_dict() if report.fairness else None}


def _deadlock(report: PerfReport, _a: Any, wait_for: list, _c: Call) -> dict[str, Any]:
    return {
        "deadlocks": [d.to_dict() for d in report.liveness.deadlocks],
        # The whole graph, not just the cycles: §8.18 asks for the wait-for
        # graph, and an agent stuck on a slow slave is worth seeing even when it
        # is nobody's deadlock.
        "wait_for": [e.to_dict() for e in wait_for],
        "skipped": dict(report.liveness.skipped),
    }


def _livelock(report: PerfReport, _a: Any, _w: list, _c: Call) -> dict[str, Any]:
    return {"livelocks": [x.to_dict() for x in report.liveness.livelocks]}


def _starvation(report: PerfReport, _a: Any, _w: list, _c: Call) -> dict[str, Any]:
    return {"starvation": [s.to_dict() for s in report.liveness.starvation]}


_DISPATCH: dict[str, Any] = {
    "stalls": _stalls,
    "latency": _latency,
    "throughput": _throughput,
    "outstanding": _outstanding,
    "fairness": _fairness,
    "deadlock": _deadlock,
    "livelock": _livelock,
    "starvation": _starvation,
}
