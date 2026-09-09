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

from conftest import design_store

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


def test_lcs_is_exact_even_when_greedy_matching_blocks_are_shorter():
    from itertools import product
    from veritrace.diff.align import Anchor, _pair

    def reference(a, b):
        row = [0] * (len(b) + 1)
        for key in a:
            previous = row
            row = [0]
            for j, other in enumerate(b):
                row.append(previous[j] + 1 if key == other else max(row[-1], previous[j + 1]))
        return row[-1]

    words = ["".join(w) for n in range(5) for w in product("ab", repeat=n)]
    for a in words:
        for b in words:
            pairs, _ = _pair([Anchor(i, k) for i, k in enumerate(a)],
                             [Anchor(i, k) for i, k in enumerate(b)])
            assert len(pairs) == reference(a, b), (a, b, pairs)
            assert all(a[i] == b[j] for i, j in pairs)
            assert all(i < k and j < l for (i, j), (k, l) in zip(pairs, pairs[1:]))


def test_long_anchor_sequences_never_fall_back_to_wrong_positional_matches():
    from veritrace.diff.align import Anchor, _pair

    a = [Anchor(i, str(i % 3)) for i in range(20002)]
    b = a[:10000] + a[10001:]
    pairs, note = _pair(a, b)
    assert len(pairs) == len(b)
    assert all(a[i].key == b[j].key for i, j in pairs)
    assert "paired by position" not in note


def test_subcycle_glitches_and_the_tail_after_the_last_anchor_are_compared(tmp_path):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.cli import main
    import json

    header = ('$timescale 1ns $end\n$scope module tb $end\n'
              '$var wire 1 ! clk $end\n$var wire 1 " state $end\n'
              '$upscope $end\n$enddefinitions $end\n#0\n0!\n0"\n')
    a, b = tmp_path / "a.vcd", tmp_path / "b.vcd"
    a.write_text(header + '#5\n1!\n#7\n1"\n#8\n0"\n#10\n0!\n#15\n1!\n#20\n0!\n')
    b.write_text(header + '#5\n1!\n#10\n0!\n#15\n1!\n#20\n0!\n')
    for glitch in (7, 18):
        if glitch == 18:
            a.write_text(header + '#5\n1!\n#10\n0!\n#15\n1!\n#18\n1"\n#19\n0"\n#20\n0!\n')
        cli = CliRunner().invoke(main, ["diff", str(a), str(b), "--json"])
        assert cli.exit_code == 0, cli.output
        d = json.loads(cli.stdout)["divergences"][0]
        assert (d["time_a"], d["time_b"], d["value_a"], d["value_b"]) == (glitch, glitch, "1", "0")
        wave = json.loads(cli.stdout)["wave"]
        assert wave["signal"] == "state"
        different = [s for s in wave["segments"] if s["different"]]
        assert len(different) == 1
        assert (different[0]["time_a"], different[0]["time_b"], different[0]["a"], different[0]["b"]) == (glitch, glitch, "1", "0")
        assert different[0]["end"] - different[0]["start"] == pytest.approx(0.1)
        with TestClient(create_app(default_trace=a)) as c:
            sid = c.get("/").json()["default_session"]
            actual = c.post(f"/session/{sid}/diff", json={"trace": str(b)}).json()["divergences"][0]
            assert actual == d


def test_manual_anchors_reach_the_actual_comparator_through_cli_api_and_vtq(tmp_path):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.cli import main
    import json

    a = write_vcd(tmp_path / "a.vcd", "ns", 5, flip_cycle=8)
    b = write_vcd(tmp_path / "b.vcd", "ps", 5000, flip_cycle=9)
    cli = CliRunner().invoke(main, ["diff", str(a), str(b), "--align", "manual",
        "--mark", "0", "0", "--mark", "200", "200000", "--json"])
    assert cli.exit_code == 0, cli.output
    expected = json.loads(cli.stdout)
    assert expected["alignment"]["matched"] == 2
    assert expected["divergences"][0]["time_a"] == 85
    with TestClient(create_app(default_trace=a)) as c:
        sid = c.get("/").json()["default_session"]
        body = {"trace": str(b), "strategy": "manual", "times_a": [0, 200], "times_b": [0, 200000]}
        direct = c.post(f"/session/{sid}/diff", json=body)
        assert direct.status_code == 200, direct.text
        assert direct.json()["divergences"] == expected["divergences"]
        vtq = f'diff("{b.as_posix()}", align=manual, marks="0:0,200:200000")'
        queried = c.post(f"/session/{sid}/query", json={"vtq": vtq})
        assert queried.status_code == 200, queried.text
        assert queried.json()["divergences"] == expected["divergences"]
        for invalid in ([0, 0], [200, 0], [0, 201], [0]):
            response = c.post(f"/session/{sid}/diff", json={**body, "times_a": invalid})
            assert response.status_code == 409, response.text
        assert c.post(f"/session/{sid}/diff", json={**body, "times_a": [False, 200]}).status_code == 422


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
    # Initialization itself can differ at t=0 (the buggy node starts at X).
    # Do not discard those genuine events just to place every effect in a later cycle.
    assert behaviour[0].at >= first.at
    assert any(d.at > first.at for d in behaviour)
    for d in behaviour:
        assert d.value_a != d.value_b
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


def test_an_empty_comparison_cannot_be_reported_as_agreement(injected):
    _el, clean, buggy = injected
    alignment = diff_mod.align(clean, buggy, "cycle")
    with pytest.raises(diff_mod.AlignError, match="no common signals"):
        diff_mod.compare(alignment, ignore=["*"])
    with pytest.raises(diff_mod.AlignError, match="missing from"):
        diff_mod.compare(alignment, only=["port_that_was_not_dumped"])
    for limit in [0, -1]:
        with pytest.raises(diff_mod.AlignError, match="limit must"):
            diff_mod.compare(alignment, limit=limit)


def test_explicit_synthesis_ports_override_default_counter_ignore(tmp_path):
    a = write_vcd(tmp_path / "a.vcd", "ns", 5, flip_cycle=3)
    b = write_vcd(tmp_path / "b.vcd", "ns", 5, flip_cycle=7)
    for path in (a, b):
        path.write_text(path.read_text().replace('" state $end', '" byte_cnt $end'))
    sides = [open_side(p.stem, p, tmp_path / (p.stem + ".vtx")) for p in (a, b)]
    alignment = diff_mod.align(*sides, "cycle")
    report = diff_mod.compare(alignment, only=["byte_cnt"])
    assert report.compared == 1
    assert report.first is not None and report.first.signal == "byte_cnt"


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
            str(design_store("deadlock", "dump_ok.vcd")),
            str(design_store("deadlock")),
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

    client = TestClient(create_app(str(design_store("deadlock", "dump_ok.vcd")), [str(DEADLOCK)]))
    sid = client.get("/", headers={"Accept": "application/json"}).json()["default_session"]

    got = client.post(
        f"/session/{sid}/diff", json={"trace": str(design_store("deadlock"))}
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["divergences"], "the two runs differ"
    assert body["alignment"]["strategy"] == "cycle"
    assert body["alignment"]["timescale_a"] and body["alignment"]["timescale_b"]

    # Both traces are now open, which is what the two-trace selector lists.
    names = {s["name"] for s in client.get("/sessions").json()["sessions"]}
    assert names == {"dump_ok.vcd.vtx", "dump.vcd.vtx"}


def test_diff_vtq_uses_the_same_production_comparison():
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    client = TestClient(
        create_app(str(design_store("deadlock", "dump_ok.vcd")), [str(DEADLOCK)])
    )
    sid = client.get("/", headers={"Accept": "application/json"}).json()[
        "default_session"
    ]
    other = str(design_store("deadlock")).replace("\\", "/")
    got = client.post(
        f"/session/{sid}/query", json={"vtq": f"diff('{other}', align=cycle)"}
    )
    assert got.status_code == 200, got.text
    assert got.json()["kind"] == "diff"
    assert got.json()["divergences"]
    assert got.json()["alignment"]["strategy"] == "cycle"


def test_focusing_a_divergence_updates_real_waves_and_both_causal_chains():
    import json
    from click.testing import CliRunner
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.cli import main

    a, b = design_store("deadlock", "dump_ok.vcd"), design_store("deadlock")
    with TestClient(create_app(str(a), [str(DEADLOCK)])) as client:
        sid = client.get("/").json()["default_session"]
        initial = client.post(f"/session/{sid}/diff", json={"trace": str(b)}).json()
        chosen = next(d for d in initial["divergences"] if not d["detail"] and d["time_a"] > 0)
        got = client.post(f"/session/{sid}/diff", json={"trace": str(b), "focus": chosen["signal"]})
        assert got.status_code == 200, got.text
        focused = got.json()
        assert focused["focus"] == focused["wave"]["signal"] == chosen["signal"]
        for side in ("a", "b"):
            assert focused[f"why_{side}"]["signal"].endswith("." + chosen["signal"])
            assert focused[f"why_{side}"]["time"] == chosen[f"time_{side}"]
        at = next(s for s in focused["wave"]["segments"] if s["time_a"] == chosen["time_a"])
        assert at["different"] and (at["a"], at["b"]) == (chosen["value_a"], chosen["value_b"])
        invalid = client.post(f"/session/{sid}/diff", json={"trace": str(b), "focus": "nonexistent"})
        assert invalid.status_code == 409
    cli = CliRunner().invoke(main, ["diff", str(a), str(b), "--rtl", str(DEADLOCK),
        "--focus", chosen["signal"], "--json"])
    assert cli.exit_code == 0, cli.output
    result = json.loads(cli.stdout)
    assert result["wave"] == focused["wave"]
    assert result["why_a"]["signal"] == focused["why_a"]["signal"]


def test_diffing_a_trace_against_itself_is_refused():
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    client = TestClient(create_app(str(design_store("deadlock", "dump_ok.vcd")), [str(DEADLOCK)]))
    sid = client.get("/", headers={"Accept": "application/json"}).json()["default_session"]
    got = client.post(
        f"/session/{sid}/diff", json={"trace": str(design_store("deadlock", "dump_ok.vcd"))}
    )
    assert got.status_code == 400
    assert "same trace" in got.json()["detail"]


def test_empty_diff_and_cli_limits_propagate_through_production_entrypoints():
    from click.testing import CliRunner
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.cli import main
    import json

    a, b = design_store("deadlock", "dump_ok.vcd"), design_store("deadlock")
    with TestClient(create_app(str(a), [str(DEADLOCK)])) as client:
        sid = client.get("/").json()["default_session"]
        got = client.post(f"/session/{sid}/diff", json={"trace": str(b), "ignore": ["*"]})
        assert got.status_code == 409, got.text
        assert "no common signals" in got.json()["detail"]
    run = CliRunner().invoke(main, ["diff", str(a), str(b), "--ignore", "*"])
    assert run.exit_code != 0 and "no common signals" in run.output
    run = CliRunner().invoke(main, ["diff", str(a), str(b), "--limit", "1", "--json"])
    assert run.exit_code == 0, run.output
    assert len(json.loads(run.output)["divergences"]) == 1
    invalid = CliRunner().invoke(main, ["diff", str(a), str(b), "--limit", "0"])
    assert invalid.exit_code != 0
