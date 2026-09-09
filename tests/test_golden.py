"""§14.1/§14.2 — the golden regression suite.

*"Pentru fiecare design de referinta, `why()` trebuie sa ajunga la root cause-ul
din `expected.yaml`. Asta e suita ta de regresie si o rulezi la fiecare commit."*

The file is `expected.toml`, not `.yaml`: every other data file here is TOML,
`tomllib` is in the standard library, and the alternative was hand-rolling a
YAML reader for four shapes of value.

The point of keeping the expectations in data rather than in Python is that
adding a reference design means adding two files and no code — and that what the
tool is supposed to say about a design is reviewable next to the design itself.

Every case here failed before the fixes it guards:

* `fifo_async` — the memory read explained the whole array and blamed `wr_en`
  (§5.6 was not implemented in the walk).
* `lanes` — a legal index into a 64-entry memory was reported as out of range,
  and the chain stopped there.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from veritrace import TraceStore, convert
from veritrace.analysis.checks import run_all
from veritrace.analysis.whytrace import WhyTracer, root_cause
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
CASES = sorted(p.parent.name for p in DESIGNS.glob("*/expected.toml"))


def _spec(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    """One elaborated, correlated design per case, built once."""
    out = {}
    for name in CASES:
        d = DESIGNS / name
        vtx = tmp_path_factory.mktemp(name) / "dump.vtx"
        convert(str(d / "dump.vcd"), str(vtx))
        store = TraceStore(str(vtx))
        el = elaborate(discover(d))
        corr = correlate(
            el.graph, {s.path: s.handle for s in store.signals()}, el.aliases
        )
        out[name] = (el, store, corr, _spec(d / "expected.toml"))
    return out


@pytest.mark.parametrize("name", CASES)
def test_correlation_meets_the_designs_own_bar(name, sessions):
    _el, _store, corr, spec = sessions[name]
    floor = spec.get("correlation_min")
    if floor is None:
        pytest.skip("no correlation bar declared")
    assert corr.percent >= floor, f"{name}: {corr.percent}% correlated, expected >= {floor}%"


@pytest.mark.parametrize("name", CASES)
def test_why_reaches_what_the_design_says_it_should(name, sessions):
    el, store, _corr, spec = sessions[name]
    cases = spec.get("why") or []
    if not cases:
        pytest.skip("no why expectations declared")

    for case in cases:
        result = WhyTracer(el.graph, store).why(*_question(case["question"]))
        nodes = list(result.root.walk())
        paths = {n.signal.path() for n in nodes}
        reasons = {n.reason.value for n in nodes}

        if "root_cause" in case:
            cause = root_cause(result.root)
            assert cause is not None, f"{name}: {case['question']} reached no root cause"
            assert cause.signal.path() == case["root_cause"], (
                f"{name}: root cause was {cause.signal.path()}, "
                f"expected {case['root_cause']}"
            )
            if "reason" in case:
                assert cause.reason.value == case["reason"]
            if "file" in case:
                assert cause.loc and cause.loc.file == case["file"]
            if "line" in case:
                assert cause.loc and cause.loc.line == case["line"]

        if "reaches" in case:
            assert case["reaches"] in paths, (
                f"{name}: {case['question']} never reached {case['reaches']}; "
                f"got {sorted(paths)[:8]}"
            )
        if "not_reason" in case:
            assert case["not_reason"] not in reasons, (
                f"{name}: {case['question']} concluded {case['not_reason']}, "
                "which this design says is wrong"
            )
        if case.get("jumps_back"):
            # §5.6: the element was written before the read, and the node about
            # the element has to sit at the write, not at the question's time.
            elem = next(n for n in nodes if n.signal.path() == case["reaches"])
            assert elem.time < result.root.time, (
                f"{name}: the memory node is at {elem.time}, the read at "
                f"{result.root.time} — no jump back to the write"
            )
        if "detail_contains" in case:
            assert any(case["detail_contains"] in n.detail for n in nodes), (
                f"{name}: no node said {case['detail_contains']!r}"
            )


@pytest.mark.parametrize("name", CASES)
def test_checks_say_what_the_design_says_they_should(name, sessions):
    el, store, _corr, spec = sessions[name]
    want = spec.get("checks") or {}
    if not want:
        pytest.skip("no check expectations declared")

    from veritrace import clocks
    from veritrace.protocol import engine

    # The same path a session takes: transactions first, because the protocol
    # and handshake checks consume them. Passing None here silently skipped
    # every check that needs an interface.
    clock = clocks.resolve(store, el.graph)
    analysis = engine.extract(store, None, clock, None, use_cache=False)
    report = run_all(store, el.graph, el, clock, None, analysis, None, None, None)
    signals = {f.signal for f in report.findings}
    checks = {f.check for f in report.findings}
    groups = {f.group.value if hasattr(f.group, "value") else str(f.group) for f in report.findings}

    for sig in want.get("required_signals") or []:
        assert sig in signals, f"{name}: nothing was reported about {sig}"
    for check in want.get("required_checks") or []:
        assert check in checks, f"{name}: required check {check!r} did not fire; got {sorted(checks)}"
    if want.get("required_locations"):
        required = set(want.get("required_signals") or [])
        relevant = [f for f in report.findings if f.signal in required]
        assert relevant, f"{name}: no required finding was available for source localization"
        for finding in relevant:
            assert finding.loc is not None, (
                f"{name}: {finding.check} on {finding.signal} has no RTL location"
            )
            assert finding.loc.file and finding.loc.line > 0
    for group in want.get("forbidden_groups") or []:
        assert group not in groups, f"{name}: reported {group} on a design with no such bug"


def _question(text: str) -> tuple[str, int]:
    from veritrace.analysis import vtq

    q = vtq.parse(text)
    return q.signal, q.time if q.time is not None else 10**12
