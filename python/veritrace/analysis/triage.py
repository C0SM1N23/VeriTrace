"""Triage from a simulation log — §8.10b.

The gap this closes, in the spec's own words: *we had an excellent tool for
somebody who already knew which signal to query — but nobody starts that way.*
A real session starts with a log full of failures, and without a bridge from
the log to a query the answer is "open GTKWave and look at everything".

    veritrace triage sim.log --trace dump.vtx --rtl rtl/

Four steps, per §8.10b:

1. **Parse** the log with configurable patterns (`[triage] patterns` in
   `.veritrace.toml`, §4.3).
2. **Derive the question** each failure implies. An assertion asks why the
   signals in its condition are what they are; a timeout asks what stopped
   moving in the window before it.
3. **Run them in parallel**, because a log with forty failures otherwise takes
   forty times as long as it needs to.
4. **Group by root cause**, and report ordered by how many failures each cause
   explains.

Step 4 is what makes triage worth doing. *Forty-seven failures look like a
catastrophe; three causes look like a morning's work.* And per §8.10b's rule of
honesty, failures that could not be explained are reported explicitly with the
reason — a tool that hides what it did not understand is not one to trust.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from veritrace.analysis.whytrace import CausalNode, Reason, WhyTracer, root_cause
from veritrace.clocks import Clock, UNIT_FS, to_trace_units

#: How far back a timeout looks for something that stopped moving, in cycles.
TIMEOUT_WINDOW_CYCLES = 200


@dataclass(slots=True)
class LogFailure:
    """One failure line, as read from the log."""

    kind: str  # "assertion" | "uvm" | "timeout" | "error" | a user pattern name
    line_no: int
    text: str
    #: Simulation time in trace units, when the log gave one.
    time: int | None = None
    #: Property or component name, when the log gave one.
    name: str | None = None
    #: Continuation lines the simulator printed under the failure. §8.10b's own
    #: example puts the hierarchical path there rather than on the header line,
    #: and that path is usually the only thing naming a signal.
    context: list[str] = field(default_factory=list)

    @property
    def is_timeout(self) -> bool:
        return self.kind == "timeout" or "timeout" in self.text.lower()

    @property
    def full_text(self) -> str:
        return "\n".join([self.text, *self.context])


@dataclass(slots=True)
class Cause:
    """A root cause, and every failure it accounts for."""

    signal: str
    reason: Reason
    time: int
    loc: Any = None
    value: str = ""
    failures: list[LogFailure] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "reason": self.reason.value,
            "time": self.time,
            "value": self.value,
            "loc": (
                {"file": self.loc.file, "line": self.loc.line} if self.loc else None
            ),
            "n_failures": self.n,
            "failures": [
                {"line": f.line_no, "text": f.text, "time": f.time, "kind": f.kind}
                for f in self.failures
            ],
        }


@dataclass(slots=True)
class TriageReport:
    causes: list[Cause] = field(default_factory=list)
    #: (failure, why it could not be explained) — §8.10b's rule of honesty.
    unexplained: list[tuple[LogFailure, str]] = field(default_factory=list)
    n_failures: int = 0

    @property
    def n_explained(self) -> int:
        return sum(c.n for c in self.causes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_failures": self.n_failures,
            "n_causes": len(self.causes),
            "causes": [c.to_dict() for c in self.causes],
            "unexplained": [
                {"line": f.line_no, "text": f.text, "reason": why}
                for f, why in self.unexplained
            ],
        }


# --- step 1: parse the log -------------------------------------------------


def _to_trace_time(value: str, unit: str | None, timescale: str) -> int | None:
    """A log time like `12470ns` in the trace's own units.

    Getting this wrong points every query at the wrong moment, so an unknown
    unit yields `None` — no time — rather than a number in the wrong scale.
    """
    try:
        magnitude = float(value)
    except ValueError:
        return None
    if not unit:
        # No unit in the log: it is already in the trace's units. `to_trace_units`
        # always expects one, so this case is handled here rather than there.
        scale_fs = UNIT_FS.get(timescale.lstrip("0123456789 ").lower())
        return int(magnitude) if scale_fs is not None else None
    return to_trace_units(magnitude, unit, timescale)


def parse_log(
    text: str, patterns: dict[str, str], timescale: str = "1ns"
) -> list[LogFailure]:
    """Every failure line, in the order the log printed them.

    A line is attributed to the first pattern that matches, so the specific
    patterns (assertion, UVM, timeout) win over the generic `$error` catch-all.
    Patterns are compiled once, and one bad user regex is reported rather than
    taking the whole command down (P7).
    """
    compiled: list[tuple[str, re.Pattern]] = []
    for name, pattern in patterns.items():
        try:
            compiled.append((name, re.compile(pattern)))
        except re.error as e:
            raise ValueError(f"pattern {name!r} is not a valid regex: {e}") from None

    out: list[LogFailure] = []
    for i, line in enumerate(text.splitlines(), start=1):
        matched = False
        for name, rx in compiled:
            m = rx.search(line)
            if m is None:
                continue
            groups = m.groupdict()
            at = groups.get("time")
            out.append(
                LogFailure(
                    kind=name,
                    line_no=i,
                    text=line.strip(),
                    time=_to_trace_time(at, groups.get("unit"), timescale) if at else None,
                    name=groups.get("name"),
                )
            )
            matched = True
            break
        # An indented line that is not itself a failure belongs to the one
        # above it. This is how simulators print the hierarchical path of a
        # failed assertion, and that path is usually the only signal named.
        if not matched and out and line[:1].isspace() and line.strip():
            out[-1].context.append(line.strip())
    return out


# --- step 2: derive the question -------------------------------------------


def signals_for(failure: LogFailure, graph: Any) -> list[str]:
    """Which signals a failure is about.

    An assertion names its property; the property is a `bind` in the AST, so
    the signals in its condition are known. Failing that — and for a plain
    `$error` — the fallback is any hierarchical path the message itself
    mentions, which house-format logs almost always print.
    """
    out: list[str] = []
    if failure.name and graph is not None:
        sig = graph.get(failure.name)
        if sig is not None:
            out.append(sig.path)
        else:
            # `top.dut.checker.p_axi_stable` — the property lives in a scope
            # whose signals are the ones it constrains.
            scope = failure.name.rsplit(".", 1)[0]
            out += [s.path for s in graph if s.path.startswith(scope + ".")][:4]
    for token in re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b", failure.full_text):
        if graph is not None and graph.get(token) is not None and token not in out:
            out.append(token)
    return out


def _timeout_suspects(store: Any, graph: Any, clock: Clock | None, at: int, config: Any) -> list[str]:
    """§8.10b: a timeout asks what froze in the window before it."""
    from veritrace.analysis import stuck

    if clock is None:
        return []
    window_start = at - TIMEOUT_WINDOW_CYCLES * (clock.period or 1)
    frozen = [
        f
        for f in stuck.scan(store, clock, graph, config, threshold_cycles=8)
        if f.time is not None and window_start <= f.time <= at
    ]
    frozen.sort(key=lambda f: f.time or 0)
    return [f.signal for f in frozen[:4] if f.signal]


# --- step 3 and 4: run, then group -----------------------------------------


def triage(
    log_text: str,
    store: Any,
    graph: Any = None,
    clock: Clock | None = None,
    config: Any = None,
    max_workers: int = 8,
) -> TriageReport:
    """Parse, derive, run and group — the whole of §8.10b."""
    patterns = config.patterns() if config is not None else {}
    if not patterns:
        from veritrace.config import DEFAULT_LOG_PATTERNS

        patterns = DEFAULT_LOG_PATTERNS
    failures = parse_log(log_text, patterns, store.timescale)
    report = TriageReport(n_failures=len(failures))
    if not failures:
        return report

    if graph is None:
        report.unexplained = [(f, "no RTL loaded: why() needs the design graph") for f in failures]
        return report

    _t0, t_end = store.time_range

    def investigate(failure: LogFailure) -> tuple[LogFailure, CausalNode | None, str]:
        at = failure.time if failure.time is not None else t_end
        at = max(0, min(at, t_end))
        if failure.is_timeout:
            targets = _timeout_suspects(store, graph, clock, at, config)
            if not targets:
                return failure, None, "timeout, but nothing froze in the window before it"
        else:
            targets = signals_for(failure, graph)
            if not targets:
                return failure, None, "no signal in the message could be matched to the design"
        # A tracer per failure: the memo caches are per-question, and sharing
        # one across threads would need a lock on the hot path for no gain.
        tracer = WhyTracer(graph, store)
        best: CausalNode | None = None
        for target in targets:
            root = root_cause(tracer.why(target, at).root)
            if root is not None and (best is None or root.time < best.time):
                best = root
        if best is None:
            return failure, None, "the causal chain did not reach a root cause"
        return failure, best, ""

    # §8.10b step 3. The work is graph walking plus Parquet reads, and the Rust
    # store releases the GIL for those, so threads genuinely overlap here.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(investigate, failures))

    # §8.10b step 4. The cause is *what is wrong and where*, not when it was
    # noticed: one tie-off observed from forty failing cycles is one cause,
    # which is the whole point — "forty-seven failures look like a catastrophe;
    # three causes look like a morning's work". The reported time is the
    # earliest, so the report points at the first moment it went wrong.
    causes: dict[tuple[str, Reason], Cause] = {}
    for failure, root, why_not in results:
        if root is None:
            report.unexplained.append((failure, why_not))
            continue
        key = (root.signal.path(), root.reason)
        cause = causes.get(key)
        if cause is None:
            cause = causes[key] = Cause(
                signal=root.signal.path(),
                reason=root.reason,
                time=root.time,
                loc=root.loc,
                value=root.value,
            )
        elif root.time < cause.time:
            cause.time, cause.value, cause.loc = root.time, root.value, root.loc
        cause.failures.append(failure)

    # §8.10b step 5: most explanatory first. Ties broken by time then name, so
    # two runs on the same log produce the same report (P1).
    report.causes = sorted(causes.values(), key=lambda c: (-c.n, c.time, c.signal))
    return report


def format_report(report: TriageReport, clock: Clock | None = None) -> str:
    """The console form of §8.10b, including what it could not explain."""
    if report.n_failures == 0:
        return "no failures found in the log"

    def at(t: int) -> str:
        return f"c{clock.cycle_of(t)}" if clock is not None else str(t)

    # Plain ASCII: this goes to a Windows console and into CI logs, where a
    # box-drawing character turns into mojibake and hides the report.
    lines = [
        f"{report.n_failures} failure(s) in log -> "
        f"{len(report.causes)} root cause(s)"
        + (f", {len(report.unexplained)} unexplained" if report.unexplained else "")
    ]
    for i, cause in enumerate(report.causes, start=1):
        where = f"   {cause.loc}" if cause.loc else ""
        lines.append("")
        lines.append(
            f"[{i}] {cause.n} failure(s)  -  {cause.signal} = {cause.value} "
            f"[{cause.reason.value}] at {at(cause.time)}{where}"
        )
        for f in cause.failures[:3]:
            lines.append(f"      log:{f.line_no}  {f.text[:90]}")
        if cause.n > 3:
            lines.append(f"      ... and {cause.n - 3} more")
        lines.append(f"      why({cause.signal} @ {cause.time})")

    # The rule of honesty: name what was not understood, and why.
    if report.unexplained:
        lines.append("")
        lines.append(f"[?] {len(report.unexplained)} unexplained:")
        for f, why_not in report.unexplained[:5]:
            lines.append(f"      log:{f.line_no}  {f.text[:70]}")
            lines.append(f"            {why_not}")
        if len(report.unexplained) > 5:
            lines.append(f"      ... and {len(report.unexplained) - 5} more")
    return "\n".join(lines)


def read_log(path: str | Path) -> str:
    """Read a log without letting an encoding surprise abort the command."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def failure_signals(report: TriageReport) -> Iterable[str]:
    """Every signal named by a root cause, for exporting a save file."""
    return [c.signal for c in report.causes]
