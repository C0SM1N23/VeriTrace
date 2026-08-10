"""The §10.1 data-integrity and coverage commands.

    track(addr=0x4000)   track(data=0xDEADBEEF)   track(from=src, to=sink)
    scoreboard(iface)    fcov(iface)              uncovered()

The same dispatch-table shape `perf.query` and `memory.query` use: a new command
is a dict entry, not a parser change. Both §8.19's and §8.21's commands live
here because they read the two halves of the same session state and splitting
them across two modules would only move the imports around.
"""

from __future__ import annotations

from typing import Any, Callable

from veritrace.analysis.vtq import Call, Pipeline, QueryError
from veritrace.integrity import order, track as track_mod
from veritrace.integrity.model import IntegrityReport

INTEGRITY_COMMANDS = ("track", "scoreboard")
COVERAGE_COMMANDS = ("fcov", "uncovered")
COMMANDS = INTEGRITY_COMMANDS + COVERAGE_COMMANDS


def _int(call: Call, key: str) -> int | None:
    v = call.kwargs.get(key)
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v), 0)
    except ValueError as e:
        raise QueryError(f"`{key}=` needs a number, got {v!r}") from e


def _track(report: IntegrityReport, call: Call) -> dict[str, Any]:
    addr, data = _int(call, "addr"), _int(call, "data")
    src, dst = call.kwargs.get("from"), call.kwargs.get("to")

    if src or dst:
        if not (src and dst):
            raise QueryError("`track(from=…, to=…)` needs both ends of the stream")
        beats = {name: [b for b in report.beats if b.iface == name] for name in (str(src), str(dst))}
        for name, got in beats.items():
            if not got:
                known = ", ".join(sorted({b.iface for b in report.beats})) or "none"
                raise QueryError(f"no tracked interface named `{name}`; tracked: {known}")
        result = order.compare(str(src), beats[str(src)], str(dst), beats[str(dst)])
        return {"order": result.to_dict()}

    if addr is not None:
        iface = call.kwargs.get("iface")
        return track_mod.by_addr(report, addr, str(iface) if iface else None).to_dict()
    if data is not None:
        return track_mod.by_data(report, data).to_dict()
    raise QueryError(
        "`track()` needs `addr=`, `data=`, or `from=`/`to=` for a stream (§8.19)"
    )


def _scoreboard(report: IntegrityReport, call: Call) -> dict[str, Any]:
    name = str(call.args[0]) if call.args else None
    known = [i.iface for i in report.interfaces]
    if name is not None and name not in known:
        raise QueryError(
            f"no tracked interface named `{name}`; tracked: {', '.join(known) or 'none'}"
        )
    keep = [m for m in report.mismatches if name is None or name in m.where]
    return {
        "iface": name,
        "interfaces": [i.to_dict() for i in report.interfaces if name in (None, i.iface)],
        "compared_paths": [
            {"a": a, "b": b, "writes": n}
            for a, b, n in report.compared_paths
            if name in (None, a, b)
        ],
        "mismatches": [m.to_dict() for m in keep],
        "skipped": dict(report.skipped),
    }


def _fcov(coverage: Any, call: Call) -> dict[str, Any]:
    known = [f.iface for f in coverage.functional]
    name = str(call.args[0]) if call.args else None
    if name is not None and name not in known:
        raise QueryError(
            f"no interface named `{name}`; measured: {', '.join(known) or 'none'}"
        )
    return {
        "functional": [
            f.to_dict() for f in coverage.functional if name in (None, f.iface)
        ]
    }


def _uncovered(coverage: Any, call: Call) -> dict[str, Any]:
    """§8.12: the uncovered points, with the conditions that would close them."""
    holes = coverage.holes
    if call.args:
        stem = str(call.args[0])
        holes = [h for h in holes if stem in h.file or stem == str(h.line)]
    return {
        "holes": [h.to_dict() for h in holes],
        "code": coverage.code.to_dict() if coverage.code else None,
        "skipped": dict(coverage.skipped),
    }


def run(
    integrity_report: IntegrityReport | None,
    coverage_report: Any,
    pipeline: Pipeline,
) -> dict[str, Any]:
    """Execute one parsed §8.19/§8.21 command."""
    call = pipeline.source
    if pipeline.stages:
        raise QueryError(f"`{call.name}()` does not take a `| stage`")

    if call.name in INTEGRITY_COMMANDS:
        if integrity_report is None:
            raise QueryError("the data-integrity scan did not run for this session")
        fn: Callable[..., dict[str, Any]] = _INTEGRITY[call.name]
        return {"kind": call.name, **fn(integrity_report, call)}

    if coverage_report is None:
        raise QueryError("coverage did not run for this session")
    return {"kind": call.name, **_COVERAGE[call.name](coverage_report, call)}


_INTEGRITY: dict[str, Any] = {"track": _track, "scoreboard": _scoreboard}
_COVERAGE: dict[str, Any] = {"fcov": _fcov, "uncovered": _uncovered}
