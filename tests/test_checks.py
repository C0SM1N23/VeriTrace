"""Automatic checks — §8.4, §8.5, §8.6, §8.11, §8.11c.

The acceptance criterion for Prompt 7 is the first test here: opening a session
on `designs/checks` reports every injected problem with no manual query. The
design is a fixture whose flaws are deliberate; see `designs/checks/README.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import checks, cone, params
from veritrace.analysis.findings import Group, Severity
from veritrace.config import Config
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate
from veritrace.integrity import report as int_report
from veritrace.memory import report as mem_report
from veritrace.perf import report as perf_report
from veritrace.protocol import engine

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """The checks design, elaborated and correlated, as a session would."""
    out = tmp_path_factory.mktemp("checks") / "dump.vtx"
    convert(str(DESIGNS / "checks" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(DESIGNS / "checks"))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, Config.empty())
    # Extraction runs even though this design has no bus: it is what a session
    # does, and "ran and found no interfaces" has to be distinguishable from
    # "never ran" in `report.skipped`.
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    # §8.18's scan runs for the same reason: a design with no agents has no
    # deadlock, and that has to look different from a scan that never ran.
    perf, _edges = perf_report.build(store, analysis.extractions, el.graph, clock)
    # §8.20's scan, same reasoning: a design with no memory interfaces still
    # has to show a *ran* result, not a skip.
    memory = mem_report.build_all(store, analysis.packs, clock, Config.empty())
    # §8.19's scoreboard, same reasoning once more: a design whose packs declare
    # no data path still has to show that the scan ran.
    integrity = int_report.build(analysis, store, clock)
    report = checks.run_all(
        store, el.graph, el, clock, Config.empty(), analysis, perf.liveness, memory,
        integrity,
    )
    return store, el, clock, report


# --- the acceptance criterion ----------------------------------------------

#: Every problem injected into designs/checks, by check name. Opening a session
#: has to surface all of them without a single query being typed.
INJECTED = {
    "inferred_latch",
    "case_no_default",
    "stuck",
    "blocking_in_always_ff",
    "nonblocking_in_always_comb",
    "incomplete_sensitivity",
    "cdc_no_sync",
    "async_reset_no_sync",
    "x_source",
    "x_optimism",
    "initial_value_dependency",
    "parameter_default",
}


def test_opening_a_session_reports_every_injected_problem(session):
    """Prompt 7's acceptance criterion, and the whole point of §1.4."""
    _store, _el, _clock, report = session
    found = set(report.counts())
    assert INJECTED <= found, f"missed: {sorted(INJECTED - found)}"
    assert not report.skipped, report.skipped


def test_every_finding_has_a_source_location(session):
    """The second half of the criterion: an exact location on every row."""
    _store, _el, _clock, report = session
    for f in report:
        assert f.loc is not None, f"{f.check} has no location"
        assert f.loc.file.endswith(".sv") and f.loc.line > 0, f"{f.check}: {f.loc}"


def test_why_is_runnable_wherever_there_is_something_to_explain(session):
    """`[why]` must work, or be absent — never present and broken (§11.4)."""
    from veritrace.analysis import vtq

    _store, el, _clock, report = session
    for f in report:
        if f.why is None:
            # Only allowed where there is no trace value to explain.
            assert f.group is Group.PARAMETERS, f"{f.check} dropped its why()"
            continue
        query = vtq.parse(f.why)
        assert el.graph.get(query.signal) is not None, f"{f.why} names no signal"


def test_findings_are_ordered_worst_first(session):
    _store, _el, _clock, report = session
    severities = [f.severity for f in report.sorted()]
    assert severities == sorted(severities, reverse=True)


def test_ids_are_stable_across_runs(session):
    """Suppression has to survive a re-run, so the id cannot depend on order."""
    store, el, clock, report = session
    again = checks.run_all(store, el.graph, el, clock, Config.empty())
    assert [f.id for f in report.sorted()] == [f.id for f in again.sorted()]


# --- stuck (§8.4) ----------------------------------------------------------


def test_stuck_finds_the_frozen_register(session):
    _store, _el, clock, report = session
    stuck = {f.signal: f for f in report if f.group is Group.STUCK}
    lock = next(v for k, v in stuck.items() if k.endswith("lock_r"))
    assert lock.severity is Severity.WARN
    assert "frozen at 1" in lock.title
    assert clock.cycles_between(lock.time, clock.edges[-1]) > 100


def test_stuck_reports_one_row_per_wire_not_one_per_alias(session):
    """A port connection is one signal under three names (§7.1).

    Three rows for one frozen wire is what makes an automatic report unreadable.
    """
    _store, _el, _clock, report = session
    names = [f.signal for f in report if f.group is Group.STUCK]
    leaves = [n.rsplit(".", 1)[-1] for n in names]
    assert len(leaves) == len(set(leaves)), names


def test_stuck_separates_never_moved_from_froze_late(session):
    """§8.4 excludes design constants; a tie-off is not a deadlock."""
    _store, _el, _clock, report = session
    for f in report:
        if f.group is not Group.STUCK:
            continue
        if f.severity is Severity.INFO:
            assert "never changed" in f.title
        else:
            assert "frozen at" in f.title


def test_stuck_needs_a_clock_and_says_so_when_there_is_none(session):
    """P7: a check that cannot run reports why, rather than looking like a pass."""
    store, el, _clock, _report = session
    report = checks.run_all(store, el.graph, el, None, Config.empty())
    assert Group.STUCK.value in report.skipped
    assert "clock" in report.skipped[Group.STUCK.value]


def test_a_window_wider_than_the_run_is_a_skip_not_a_clean_report(session):
    """The same rule of honesty, for the commonest case of all.

    §8.4's default is 100 cycles and a testbench is routinely shorter than
    that, so on most runs the check is structurally unable to fire — and
    "nothing has been frozen" read as a verdict on the design. Three of the
    four reference designs are in this position.
    """
    from veritrace.analysis import stuck as stuck_mod

    store, el, clock, _report = session
    config = Config.empty()
    config.stuck_cycles = 100_000

    note = stuck_mod.too_short(store, clock, config)
    assert note and "100000 cycles" in note and "--cycles" in note
    assert not list(stuck_mod.scan(store, clock, el.graph, config))

    report = checks.run_all(store, el.graph, el, clock, config)
    assert report.skipped[Group.STUCK.value] == note
    # And the window the design *does* fit reports normally.
    assert stuck_mod.too_short(store, clock, Config.empty()) is None


def test_a_clock_that_stopped_is_the_finding_not_four_hundred_frozen_signals(tmp_path):
    """§8.4: *"singurul finding util e acel fapt"*.

    The scan used to return silently here, which is the one thing this check
    cannot look like: a design that stopped being clocked and a design with
    nothing wrong produced the same empty report.
    """
    from veritrace.analysis import stuck as stuck_mod

    # Twenty cycles of clock, then a long tail with the clock parked low.
    vcd = ["$timescale 1ns $end", "$scope module tb $end",
           "$var reg 1 ! clk $end", "$var reg 8 \" data [7:0] $end",
           "$upscope $end", "$enddefinitions $end", "#0", "0!", "b0 \""]
    t = 0
    for i in range(40):
        t += 5
        vcd += [f"#{t}", f"{i % 2}!"]
    vcd += [f"#{t + 5000}", "b1 \""]

    src = tmp_path / "stopped.vcd"
    src.write_text("\n".join(vcd) + "\n")
    out = tmp_path / "stopped.vtx"
    convert(str(src), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store, None, Config.empty())

    found = list(stuck_mod.scan(store, clock, None, Config.empty(), threshold_cycles=2))
    assert len(found) == 1, [f.title for f in found]
    assert found[0].check == stuck_mod.CHECK_CLOCK_STOPPED
    assert found[0].severity is Severity.ERROR
    assert "the clock stopped at c" in found[0].title
    # It is nameable by `--fail-on stuck`, like the rows it stands in for.
    assert found[0].check in checks.expand_checks(["stuck"])

    # And it survives a threshold wider than the run: a stopped clock is a fact
    # about the run rather than a signal measured against the window, so the
    # scan must not be recorded as unable to say anything.
    assert stuck_mod.too_short(store, clock, Config.empty(), 1_000_000) is None
    wide = list(stuck_mod.scan(store, clock, None, Config.empty(), threshold_cycles=1_000_000))
    assert [f.check for f in wide] == [stuck_mod.CHECK_CLOCK_STOPPED]


# --- X propagation (§8.5) --------------------------------------------------


def test_x_sources_group_many_victims_under_one_cause(session):
    """§8.5: 200 X's usually have 2 causes. Reporting all 200 is useless."""
    _store, _el, _clock, report = session
    xs = [f for f in report if f.group is Group.X_SOURCES]
    assert len(xs) == 1, [f.signal for f in xs]
    source = xs[0]
    assert source.signal.endswith("uninit")
    assert source.check == "x_source"
    # The uninitialised register plus every `acc` it reaches, at all hierarchy
    # levels: the walk crosses port boundaries.
    assert len(source.related) > 1
    assert any(r.endswith("acc") for r in source.related)


def test_x_terminal_is_specific_not_merely_unknown(session):
    """A specialised terminal from the §8.5 table, not a bare "it is X"."""
    from veritrace.analysis.whytrace import Reason, WhyTracer

    store, el, _clock, _report = session
    result = WhyTracer(el.graph, store).why("tb_checks.dut.u_dut.acc", 0)
    reasons = {n.reason for n in result.root.walk()}
    assert Reason.UNINITIALIZED_REG in reasons
    assert Reason.UNKNOWN_X not in reasons


# --- cone / fan-out (§8.6) -------------------------------------------------


def test_cone_walks_backwards_and_crosses_module_boundaries(session):
    _store, el, _clock, _report = session
    result = cone.cone(el.graph, "tb_checks.dut.u_dut.status", depth=4)
    paths = set(result.paths())
    assert "tb_checks.dut.u_dut.status" in paths
    assert "tb_checks.dut.u_dut.lock_r" in paths
    # Out through the port of checks_dut and into the wrapper above it.
    assert any(p.count(".") == 2 for p in paths), paths


def test_fanout_answers_what_breaks_if_this_changes(session):
    _store, el, _clock, _report = session
    result = cone.cone(el.graph, "tb_checks.dut.u_dut.lock_r", depth=2, direction="fanout")
    assert "tb_checks.dut.u_dut.busy" in result.paths()
    assert "tb_checks.dut.u_dut.status" in result.paths()


def test_cone_depth_bounds_the_result(session):
    _store, el, _clock, _report = session
    near = cone.cone(el.graph, "tb_checks.dut.u_dut.status", depth=1)
    far = cone.cone(el.graph, "tb_checks.dut.u_dut.status", depth=4)
    assert len(near.nodes) < len(far.nodes)
    assert max(n.depth for n in near.nodes) == 1


def test_cone_intersected_with_activity_drops_what_never_moved(session):
    """§8.6: intersecting with activity cuts another 60% of the noise."""
    store, el, _clock, _report = session
    t0, t1 = store.time_range
    full = cone.cone(el.graph, "tb_checks.dut.u_dut.status", depth=4)
    # A window late in the run, after lock_r has frozen.
    active = cone.cone(
        el.graph,
        "tb_checks.dut.u_dut.status",
        depth=4,
        store=store,
        window=(t1 - (t1 - t0) // 10, t1),
    )
    assert len(active.nodes) < len(full.nodes)
    assert active.n_inactive > 0
    assert active.nodes[0].path == "tb_checks.dut.u_dut.status"


def test_cone_rejects_an_unknown_signal(session):
    _store, el, _clock, _report = session
    with pytest.raises(KeyError):
        cone.cone(el.graph, "nope.not.here")


# --- lint (§8.11 Group A) --------------------------------------------------


def test_cdc_finding_carries_the_honesty_note(session):
    """§8.11 is explicit: this is structural, not CDC sign-off."""
    _store, _el, _clock, report = session
    cdc = [f for f in report if f.check == "cdc_no_sync"]
    assert cdc
    for f in cdc:
        assert any("not formal CDC sign-off" in n for n in f.notes)


def test_cdc_reports_the_cycles_the_trace_makes_risky(session):
    """The differentiator of §8.11: what a purely static tool cannot say."""
    _store, _el, _clock, report = session
    cdc = [f for f in report if f.check == "cdc_no_sync"]
    sampled = [f for f in cdc if any("risky window" in n for n in f.notes)]
    assert sampled, "no CDC finding carried trace evidence"


def test_a_two_flop_synchroniser_is_not_reported(tmp_path):
    """The check has to be quiet where the design did the right thing."""
    rtl = tmp_path / "sync.sv"
    rtl.write_text(
        """
        module m (input logic a_clk, input logic b_clk, input logic d, output logic q);
          logic src, s1, s2;
          always_ff @(posedge a_clk) src <= d;
          always_ff @(posedge b_clk) s1 <= src;
          always_ff @(posedge b_clk) s2 <= s1;
          assign q = s2;
        endmodule
        """,
        encoding="utf-8",
    )
    el = elaborate([rtl], top="m")
    from veritrace.analysis import lint

    assert not [f for f in lint.scan(el.graph) if f.check == "cdc_no_sync"]


def test_a_crossing_without_a_synchroniser_is_reported(tmp_path):
    rtl = tmp_path / "nosync.sv"
    rtl.write_text(
        """
        module m (input logic a_clk, input logic b_clk, input logic d, output logic q);
          logic src, s1;
          always_ff @(posedge a_clk) src <= d;
          always_ff @(posedge b_clk) s1 <= src;
          assign q = s1;
        endmodule
        """,
        encoding="utf-8",
    )
    el = elaborate([rtl], top="m")
    from veritrace.analysis import lint

    found = [f for f in lint.scan(el.graph) if f.check == "cdc_no_sync"]
    assert len(found) == 1
    assert "src" in found[0].title


def test_assignment_style_checks_name_the_right_signals(session):
    _store, _el, _clock, report = session
    by_check = {f.check: f for f in report}
    assert by_check["blocking_in_always_ff"].signal.endswith("shadow")
    assert by_check["nonblocking_in_always_comb"].signal.endswith("parity")


def test_a_diagnostic_without_a_named_signal_is_still_actionable(session):
    """slang blames the `case` keyword; the block span maps it to a signal."""
    _store, _el, _clock, report = session
    case = next(f for f in report if f.check == "case_no_default")
    assert case.signal is not None and case.signal.endswith("decoded")
    assert case.why is not None


def test_slang_noise_is_not_surfaced_as_findings(session):
    """§8.11: aggregate a front end, do not import its chatter."""
    _store, _el, _clock, report = session
    assert not [f for f in report if "unused" in f.check.lower()]


# --- parameters (§8.11c) ---------------------------------------------------


def test_parameter_left_on_a_default_the_parent_contradicts(session):
    _store, _el, _clock, report = session
    p = next(f for f in report if f.check == "parameter_default")
    assert p.signal.endswith("DEPTH")
    assert "DEPTH = 8" in p.detail


def test_parameter_tree_marks_propagated_and_shadowed(session):
    _store, el, _clock, _report = session
    tree = params.tree(el)
    assert tree is not None and tree.path == "tb_checks"
    flat = {}

    def walk(node):
        for name, (value, overridden, shadowed) in node.params.items():
            flat[f"{node.path}.{name}"] = (value, overridden, shadowed)
        for c in node.children:
            walk(c)

    walk(tree)
    # WIDTH was passed down explicitly; DEPTH was not and disagrees.
    assert flat["tb_checks.dut.u_dut.WIDTH"][1] is True
    assert flat["tb_checks.dut.u_dut.WIDTH"][2] is False
    assert flat["tb_checks.dut.u_dut.DEPTH"][2] is True


# --- configuration (§4.3) --------------------------------------------------


def test_stuck_threshold_is_configurable(session):
    store, el, clock, _report = session
    from veritrace.analysis import stuck

    strict = list(stuck.scan(store, clock, el.graph, threshold_cycles=1))
    loose = list(stuck.scan(store, clock, el.graph, threshold_cycles=10_000))
    assert len(strict) > len(loose) == 0


def test_ignore_globs_exclude_signals_from_checks(session):
    store, el, clock, _report = session
    config = Config.empty()
    config.ignore = ["*lock_r", "tb_checks.dut.u_dut.uninit"]
    report = checks.run_all(store, el.graph, el, clock, config)
    assert not [f for f in report if f.signal and f.signal.endswith("lock_r")]
    assert not [f for f in report if f.group is Group.X_SOURCES]


def test_disabled_checks_are_not_run(session):
    store, el, clock, _report = session
    config = Config.empty()
    config.disabled_checks = ["cdc", "stuck"]
    report = checks.run_all(store, el.graph, el, clock, config)
    assert "cdc_no_sync" not in report.counts()
    assert "stuck" not in report.counts()
    assert "inferred_latch" in report.counts()


def test_fail_on_names_expand_to_checks():
    assert "cdc_no_sync" in checks.expand_checks(["cdc"])
    assert "x_source" in checks.expand_checks(["x"])
    assert checks.expand_checks(["inferred_latch"]) == {"inferred_latch"}


# --- default tab (§11.4b) --------------------------------------------------


def test_default_tab_is_checks_when_there_is_something_to_show(session):
    _store, _el, _clock, report = session
    assert checks.default_tab(report) == "checks"


def test_default_tab_falls_back_to_wave_on_a_clean_design():
    from veritrace.analysis.findings import Report

    assert checks.default_tab(Report()) == "wave"
    assert checks.default_tab(Report(), configured="wave") == "wave"


def test_two_findings_that_differ_only_in_source_get_different_ids(session):
    """Suppressing one crossing must not silently hide the other.

    Both unsynchronised crossings into `status` share a check, a signal and a
    location; only the source signal differs.
    """
    _store, _el, _clock, report = session
    ids = [f.id for f in report]
    assert len(ids) == len(set(ids)), "two findings collide on id"
    cdc = [f for f in report if f.check == "cdc_no_sync"]
    assert len(cdc) == 2
    assert cdc[0].signal == cdc[1].signal and cdc[0].loc == cdc[1].loc
    assert cdc[0].id != cdc[1].id


def test_cdc_does_not_fire_within_one_clock_domain(tmp_path_factory):
    """One clock reaching four instances has four paths and one net.

    Comparing those paths as strings made every crossing between two instances
    look like a CDC — including a signal "crossing into itself". A checker that
    cries wolf on a single-clock design is worse than no checker, because it is
    the reason people stop reading the output.
    """
    from veritrace.protocol import engine

    out = tmp_path_factory.mktemp("arb") / "dump.vtx"
    convert(str(DESIGNS / "axi_arb" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(DESIGNS / "axi_arb"))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, Config.empty())
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    report = checks.run_all(store, el.graph, el, clock, Config.empty(), analysis)

    cdc = [f for f in report if f.check == "cdc_no_sync"]
    assert cdc == [], [f.title for f in cdc]


# --- §8.9's structural row ---------------------------------------------------


def test_a_ready_computed_from_valid_is_reported(tmp_path):
    """§8.9: `ready` combinationally dependent on `valid` is a deadlock risk.

    The trace cannot show it — every transfer in the fixture completes, because
    the master never waits for ready. That is exactly why the check reads the
    graph, and why it says so in the finding.
    """
    from veritrace.analysis import handshake
    from veritrace.correlate.resolver import correlate
    from veritrace.graph.elaborate import discover, elaborate
    from veritrace.protocol import engine

    design = DESIGNS / "handshake"
    vtx = tmp_path / "hs.vtx"
    convert(str(design / "dump.vcd"), str(vtx))
    store = TraceStore(str(vtx))
    el = elaborate(discover(design))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    analysis = engine.extract(store, None, None, None, use_cache=False)

    found = list(handshake.scan(analysis, el.graph))
    assert len(found) == 1, [f.title for f in found]
    (f,) = found
    assert f.signal.endswith("s_axi_wready")
    assert "depends combinationally on" in f.title
    # The path is evidence, not decoration.
    assert any("path:" in n for n in f.notes)
    # And the honesty note: a clean run does not clear a structural finding.
    assert any("not observed in this run" in n for n in f.notes)


def test_a_registered_ready_is_not_reported(tmp_path):
    """The control, in the same design: `awready` comes out of a flop and
    `arready` is a function of `rvalid`. Neither may be flagged, or the check
    is noise."""
    from veritrace.analysis import handshake
    from veritrace.correlate.resolver import correlate
    from veritrace.graph.elaborate import discover, elaborate
    from veritrace.protocol import engine

    design = DESIGNS / "handshake"
    vtx = tmp_path / "hs2.vtx"
    convert(str(design / "dump.vcd"), str(vtx))
    store = TraceStore(str(vtx))
    el = elaborate(discover(design))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    analysis = engine.extract(store, None, None, None, use_cache=False)

    flagged = {f.signal for f in handshake.scan(analysis, el.graph)}
    assert not any(s.endswith(("awready", "arready")) for s in flagged)

    # And fixing the RTL silences it, because the check is about the source.
    fixed = elaborate(discover(design), defines=["FIX_HANDSHAKE"])
    correlate(fixed.graph, {s.path: s.handle for s in store.signals()}, fixed.aliases)
    assert not list(handshake.scan(analysis, fixed.graph))


# --- §8.11: the initial-value check, and the three shapes next to it --------


def test_the_initial_value_check_names_the_register_that_carries_one(session):
    """`cfg` is a clocked register read while it still holds its declaration
    value. It is the thirteenth problem designs/checks injects, and the check
    could not fire at all while `$dumpvars`' write at t=0 counted as a driver
    reaching it."""
    _store, _el, _clock, report = session
    found = [f for f in report if f.check == "initial_value_dependency"]
    assert [f.signal for f in found] == ["tb_checks.dut.u_dut.cfg"], (
        f"expected only cfg, got {[f.signal for f in found]}"
    )
    assert found[0].loc.file == "checks_dut.sv"


def test_the_initial_value_check_leaves_alone_what_is_not_a_flop(session):
    """The neighbours that share the declaration shape without the hazard: a
    localparam is a constant, a clock generator and an unwritten testbench net
    are stimulus. Reporting them buried the one row that is real."""
    _store, _el, _clock, report = session
    signals = {f.signal for f in report if f.check == "initial_value_dependency"}
    assert "tb_checks.slow_clk" not in signals, "a clock generator is not a flop"
    assert not {s for s in signals if s.endswith((".WIDTH", ".DEPTH"))}, (
        "a localparam is a constant, which synthesis honours exactly"
    )
