"""First-divergence diff — §8.7, TAB 5.

Prompt 14 sets two acceptance criteria and both are here by name:

* `test_two_runs_in_different_units_land_on_the_same_real_moment` — two runs
  deliberately dumped in **different timescales** (one in ns, one in ps) must
  align, and "the divergence is at c8" must mean *the same femtosecond* in both.
  This is the one §8.7 calls mandatory, and it is the one that fails silently:
  without normalisation the comparison still produces a confident answer, just a
  wrong one. So the test asserts on real time, not on cycle numbers.

* `test_it_finds_the_injected_difference_between_a_clean_and_a_buggy_run` —
  a clean run against one with the bug compiled in. The repo's reference designs
  build both from one source for exactly this (§8.18–§8.20), so the difference
  under test is a real injected bug rather than a fixture.

The timescale pair is built here as raw VCD rather than simulated: Icarus takes
its timescale from a directive in the source, so producing "the same run in two
units" from a simulator means two source files that could drift apart. Writing
the text makes the two runs provably the same events, which is the whole point of
the assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks, convert
from veritrace import diff as diff_mod
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
DEADLOCK = DESIGNS / "deadlock"


# --- the timescale fixture --------------------------------------------------


def write_vcd(path: Path, unit: str, half: int, flip_cycle: int, cycles: int = 20) -> Path:
    """One clock and one signal, in whatever unit is asked for.

    `half` is the half period *in that unit*, so `("ns", 5)` and `("ps", 5000)`
    describe the same physical clock. `flip_cycle` is when `state` goes high —
    the divergence, when the two runs are given different values for it.
    """
    lines = [
        "$timescale 1" + unit + " $end",
        "$scope module tb $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 \" state $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        "0\"",
    ]
    for edge in range(cycles):
        rise = (2 * edge + 1) * half
        lines += [f"#{rise}", "1!"]
        if edge == flip_cycle:
            lines.append("1\"")
        lines += [f"#{rise + half}", "0!"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def open_side(name: str, vcd: Path, out: Path):
    convert(str(vcd), str(out))
    store = TraceStore(str(out))
    return diff_mod.side(name, store, clocks.resolve(store))


@pytest.fixture(scope="module")
def units(tmp_path_factory):
    """The same design, dumped in nanoseconds and in picoseconds.

    Identical up to cycle 8, where the picosecond run raises `state` one cycle
    later. A tool that ignores the timescale sees the ps run as a thousand times
    longer and finds the divergence at cycle 0 or not at all.
    """
    work = tmp_path_factory.mktemp("units")
    ns = write_vcd(work / "run_ns.vcd", "ns", 5, flip_cycle=8)
    ps = write_vcd(work / "run_ps.vcd", "ps", 5000, flip_cycle=9)
    return (
        open_side("ns", ns, work / "ns.vtx"),
        open_side("ps", ps, work / "ps.vtx"),
    )


# --- §8.7's mandatory normalisation ----------------------------------------


def test_a_timescale_becomes_femtoseconds():
    assert diff_mod.fs_per_tick("1ns") == 10**6
    assert diff_mod.fs_per_tick("1ps") == 10**3
    assert diff_mod.fs_per_tick("10ps") == 10**4
    assert diff_mod.fs_per_tick("1fs") == 1


def test_an_unreadable_timescale_is_refused_rather_than_guessed():
    with pytest.raises(diff_mod.AlignError, match="cannot read a time unit"):
        diff_mod.fs_per_tick("per fortnight")


def test_two_runs_in_different_units_land_on_the_same_real_moment(units):
    """**Prompt 14, acceptance criterion 1.**

    The two runs are the same physical clock written in units a thousand apart.
    After alignment, position `k` on the shared axis must be the *same
    femtosecond* in both — which is what makes "first divergence at c8" a fact
    rather than a coincidence of numbering.
    """
    a, b = units
    assert a.fs != b.fs, "the fixture is not testing anything"
    alignment = diff_mod.align(a, b, "cycle")

    for k in range(alignment.matched):
        ta, tb = alignment.time_at(k, "a"), alignment.time_at(k, "b")
        assert a.to_fs(ta) == b.to_fs(tb), (
            f"position c{k} is {a.to_fs(ta)} fs in the ns run "
            f"and {b.to_fs(tb)} fs in the ps run"
        )
        # And the raw timestamps really are different numbers, so the equality
        # above came from the conversion rather than from them being identical.
        assert ta != tb


def test_the_divergence_is_reported_at_the_cycle_it_happened(units):
    a, b = units
    report = diff_mod.compare(diff_mod.align(a, b, "cycle"))
    assert report.compared >= 1
    first = report.first
    assert first is not None, "the two runs differ and the diff said they do not"
    assert first.signal == "state"
    # The ns run raises `state` at c8 and the ps run at c9, so c8 is the first
    # position where the two step functions hold different values.
    assert first.at == 8, f"expected the runs to part at c8, got c{first.at}"
    assert (first.value_a, first.value_b) == ("1", "0")
    # The two timestamps name one moment, in each run's own units.
    assert a.to_fs(first.time_a) == b.to_fs(first.time_b)


def test_identical_runs_have_no_divergence(tmp_path):
    a = open_side("a", write_vcd(tmp_path / "a.vcd", "ns", 5, 8), tmp_path / "a.vtx")
    b = open_side("b", write_vcd(tmp_path / "b.vcd", "ps", 5000, 8), tmp_path / "b.vtx")
    report = diff_mod.compare(diff_mod.align(a, b, "cycle"))
    assert report.divergences == []
    assert report.compared == 2


# --- alignment strategies ---------------------------------------------------


def test_clock_alignment_pairs_edges_positionally(units):
    """Every anchor is the same kind of event, so the n-th of one *is* the n-th
    of the other. Running a sequence match here would cost O(n·m) to rediscover
    cycle numbering."""
    a, b = units
    alignment = diff_mod.align(a, b, "cycle")
    assert alignment.pairs == [(i, i) for i in range(alignment.matched)]
    assert alignment.matched == min(alignment.counts)


def test_a_run_without_a_clock_says_so(units, tmp_path):
    a, _b = units
    lonely = diff_mod.Side(name="x", store=a.store, clock=None, fs=a.fs)
    with pytest.raises(diff_mod.AlignError, match="no primary clock"):
        diff_mod.align(lonely, a, "cycle")


def test_an_unknown_strategy_is_refused(units):
    a, b = units
    with pytest.raises(diff_mod.AlignError, match="unknown alignment strategy"):
        diff_mod.align(a, b, "vibes")


def test_handshake_alignment_needs_interfaces(units):
    a, b = units
    with pytest.raises(diff_mod.AlignError, match="no protocol interfaces"):
        diff_mod.align(a, b, "handshake")


def test_matching_by_content_survives_a_missing_anchor():
    """Where anchor keys carry information, one run skipping an event must shift
    the rest rather than misalign everything after it — §8.7's LCS.

    Pairing positionally instead would match `c`↔`d` and put every later anchor
    one place out, which is the silent way a diff reports the wrong cycle.
    """
    from veritrace.diff.align import Anchor, _pair

    a = [Anchor(i * 10, k) for i, k in enumerate("abcde")]
    b = [Anchor(i * 10, k) for i, k in enumerate("abde")]
    pairs, _note = _pair(a, b)
    assert [x[0] for x in pairs] == [0, 1, 3, 4], "the shared subsequence is a,b,d,e"
    assert [x[1] for x in pairs] == [0, 1, 2, 3]


def test_anchors_matching_nothing_are_reported():
    from veritrace.diff.align import Anchor, _pair

    a = [Anchor(i * 10, k) for i, k in enumerate("abcde")]
    b = [Anchor(i * 10, k) for i, k in enumerate("axcxe")]
    _pairs, note = _pair(a, b)
    assert "matched nothing" in note


# --- the injected bug -------------------------------------------------------


@pytest.fixture(scope="module")
def injected(tmp_path_factory):
    """The deadlock design, built clean and built with the bug (§8.18)."""
    work = tmp_path_factory.mktemp("injected")
    el = elaborate(discover(DEADLOCK))
    sides = []
    for name, dump in (("clean", "dump_ok.vcd"), ("buggy", "dump.vcd")):
        out = work / f"{name}.vtx"
        convert(str(DEADLOCK / dump), str(out))
        store = TraceStore(str(out))
        correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
        sides.append(diff_mod.side(name, store, clocks.resolve(store, el.graph)))
    return el, sides[0], sides[1]


def test_it_finds_the_injected_difference_between_a_clean_and_a_buggy_run(injected):
    """**Prompt 14, acceptance criterion 2.**

    The two dumps come from one source built twice, so everything they disagree
    about traces back to the injected bug. The diff has to find it, order the
    consequences after it, and — since the injection here is a compile-time
    constant — say that it is a build difference rather than presenting a
    parameter as a run-time divergence.
    """
    _el, clean, buggy = injected
    alignment = diff_mod.align(clean, buggy, "cycle")
    assert alignment.matched > 1
    report = diff_mod.compare(alignment)

    assert report.compared > 50
    assert report.only_a == [] and report.only_b == [], "same design, same signals"
    first = report.first
    assert first is not None
    assert first.signal == "HOLD", [d.signal for d in report.divergences[:5]]
    assert (first.value_a, first.value_b) == ("0", "1")
    assert "build difference" in first.detail

    # The consequences come after the cause, and they are the deadlock: the
    # slave stops accepting once the injected hold is in.
    behaviour = [d for d in report.divergences if not d.detail]
    assert behaviour, "nothing but build differences — the runs did not diverge"
    assert behaviour[0].at > first.at
    assert any("s_allowed" in d.signal for d in behaviour[:4])
    assert [d.at for d in report.divergences] == sorted(d.at for d in report.divergences)


def test_transactions_diverge_where_one_run_stopped_issuing(injected):
    """§8.7's other reader: not "which wire", but "which access". A write that
    never happened is the answer more often than a write with wrong data."""
    from veritrace.protocol import engine

    el, clean, buggy = injected
    protocols = [
        engine.extract(s.store, None, s.clock, None, project_root=DEADLOCK)
        for s in (clean, buggy)
    ]
    alignment = diff_mod.align(clean, buggy, "cycle")
    diverging = diff_mod.compare_transactions(alignment, protocols[0], protocols[1])
    assert diverging, "the two runs issued the same transactions throughout"
    first = diverging[0]
    assert first.level == "transaction"
    assert first.ref
    assert first.detail


def test_ignoring_a_signal_takes_it_out_of_the_comparison(injected):
    """§11.4: counters and timestamps legitimately differ, and one of them at the
    top of the list buries the answer."""
    _el, clean, buggy = injected
    alignment = diff_mod.align(clean, buggy, "cycle")
    before = diff_mod.compare(alignment)
    after = diff_mod.compare(alignment, ignore=["HOLD", "*.HOLD"])
    assert "HOLD" not in {d.signal for d in after.divergences}
    assert after.compared < before.compared
    assert "HOLD" in after.ignored


def test_why_runs_on_both_sides_and_says_where_they_part(injected):
    """§8.7 steps 5 and 6 — and §11.4's magenta node is `first_differing`."""
    el, clean, buggy = injected
    alignment = diff_mod.align(clean, buggy, "cycle")
    report = diff_mod.compare(alignment)
    diff_mod.explain(report, el.graph, el.graph)
    assert report.why_a is not None and report.why_b is not None, report.why_error
    assert report.first_differing is not None
    assert report.first_differing >= 0


# --- the CLI and the route --------------------------------------------------


def test_diff_command_reports_the_first_divergence():
    from click.testing import CliRunner

    from veritrace.cli import main

    got = CliRunner().invoke(
        main,
        [
            "diff",
            str(DEADLOCK / "dump_ok.vcd.vtx"),
            str(DEADLOCK / "dump.vcd.vtx"),
            "--rtl",
            str(DEADLOCK),
        ],
    )
    assert got.exit_code == 0, got.output
    assert "FIRST DIVERGENCE" in got.output
    assert "aligned on cycle" in got.output
    assert "normalised before comparing" in got.output


def test_diff_route_opens_the_second_trace_as_its_own_session():
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    client = TestClient(create_app(str(DEADLOCK / "dump_ok.vcd.vtx"), [str(DEADLOCK)]))
    sid = client.get("/", headers={"Accept": "application/json"}).json()["default_session"]

    got = client.post(
        f"/session/{sid}/diff", json={"trace": str(DEADLOCK / "dump.vcd.vtx")}
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["divergences"], "the two runs differ"
    assert body["alignment"]["strategy"] == "cycle"
    assert body["alignment"]["timescale_a"] and body["alignment"]["timescale_b"]

    # Both traces are now open, which is what the two-trace selector lists.
    names = {s["name"] for s in client.get("/sessions").json()["sessions"]}
    assert names == {"dump_ok.vcd.vtx", "dump.vcd.vtx"}


def test_diffing_a_trace_against_itself_is_refused():
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    client = TestClient(create_app(str(DEADLOCK / "dump_ok.vcd.vtx"), [str(DEADLOCK)]))
    sid = client.get("/", headers={"Accept": "application/json"}).json()["default_session"]
    got = client.post(
        f"/session/{sid}/diff", json={"trace": str(DEADLOCK / "dump_ok.vcd.vtx")}
    )
    assert got.status_code == 400
    assert "same trace" in got.json()["detail"]
