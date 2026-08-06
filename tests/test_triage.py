"""Triage from a simulation log — §8.10b.

The acceptance criterion for the whole of v0.5 is the first test here: a design
with an injected bug, a log full of failed assertions, and a root cause reached
with no manual intervention.

`designs/fifo_buggy` is that design — `rd_rst_n` is tied low, so the FIFO never
drains — and `sim.log` is the real Icarus output from running it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import triage
from veritrace.analysis.whytrace import Reason
from veritrace.config import Config
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
BUGGY = DESIGNS / "fifo_buggy"


@pytest.fixture(scope="module")
def buggy(tmp_path_factory):
    out = tmp_path_factory.mktemp("triage") / "dump.vtx"
    convert(str(BUGGY / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(BUGGY))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, Config.empty())
    return store, el.graph, clock


@pytest.fixture(scope="module")
def report(buggy):
    store, graph, clock = buggy
    return triage.triage(
        triage.read_log(BUGGY / "sim.log"), store, graph, clock, Config.empty()
    )


# --- the v0.5 acceptance criterion -----------------------------------------


def test_triage_reaches_the_injected_bug_with_no_manual_intervention(report):
    """The criterion for the whole of v0.5 (§8.10b, Prompt 8).

    A log, a trace, some RTL. No query typed, no signal named by hand.
    """
    assert report.n_failures > 1
    assert len(report.causes) == 1, [c.signal for c in report.causes]

    cause = report.causes[0]
    assert cause.signal == "tb_fifo_buggy.dut.rd_rst_n"
    assert cause.reason is Reason.CONSTANT
    assert cause.value == "0"
    # Localised to the injected line, not just to the module.
    assert cause.loc.file == "fifo_buggy.sv"
    assert "rd_rst_n" in (BUGGY / "fifo_buggy.sv").read_text().splitlines()[cause.loc.line - 1]


def test_grouping_is_what_makes_the_report_readable(report):
    """§8.10b: forty-seven failures look like a catastrophe, three causes look
    like a morning's work."""
    assert report.n_explained == report.n_failures
    assert len(report.causes) < report.n_failures
    assert report.causes[0].n == report.n_failures


def test_the_report_names_the_query_that_reproduces_it(report):
    text = triage.format_report(report)
    assert "why(tb_fifo_buggy.dut.rd_rst_n @" in text
    assert "root cause" in text or "constant" in text


def test_triage_is_deterministic(buggy):
    """P1: the same log and trace produce the same report, every time."""
    store, graph, clock = buggy
    log = triage.read_log(BUGGY / "sim.log")
    a = triage.triage(log, store, graph, clock, Config.empty())
    b = triage.triage(log, store, graph, clock, Config.empty())
    assert a.to_dict() == b.to_dict()


# --- log parsing (§8.10b step 1) -------------------------------------------


def parse(text: str, timescale: str = "1ns"):
    from veritrace.config import DEFAULT_LOG_PATTERNS

    return triage.parse_log(text, DEFAULT_LOG_PATTERNS, timescale)


def test_parses_the_three_formats_the_spec_shows():
    """The exact lines from §8.10b."""
    failures = parse(
        'ERROR: assertion "p_axi_stable" failed at time 12470ns\n'
        "       top.dut.axi_checker.p_axi_stable\n"
        "UVM_ERROR @ 8900ns [SCOREBOARD] expected 0xDEADBEEF got 0xDEAD0000\n"
        "$fatal: timeout waiting for done at 45000ns\n"
    )
    assert [f.kind for f in failures] == ["assertion", "uvm", "timeout"]
    assert failures[0].name == "p_axi_stable"
    assert failures[0].time == 12470
    assert failures[1].time == 8900
    assert failures[2].is_timeout


def test_a_continuation_line_belongs_to_the_failure_above_it():
    """§8.10b's own example puts the hierarchical path on the second line, and
    that path is usually the only thing naming a signal."""
    failures = parse(
        'ERROR: assertion "p_axi_stable" failed at time 12470ns\n'
        "       top.dut.axi_checker.p_axi_stable\n"
    )
    assert len(failures) == 1
    assert failures[0].context == ["top.dut.axi_checker.p_axi_stable"]
    assert "axi_checker" in failures[0].full_text


def test_times_are_converted_into_the_trace_timescale():
    """A log in ns against a trace in ps: getting this wrong points every query
    at the wrong moment."""
    assert parse("ERROR at time 275ns", "1ps")[0].time == 275_000
    assert parse("ERROR at time 275ns", "1ns")[0].time == 275
    assert parse("ERROR at time 5us", "1ns")[0].time == 5_000
    # No unit in the log: already in the trace's own units.
    assert parse("ERROR at time 4200", "1ps")[0].time == 4200


def test_lines_that_are_not_failures_are_ignored():
    failures = parse("VCD info: dumpfile dump.vcd opened for output.\nrunning...\n")
    assert failures == []


def test_a_custom_pattern_adds_to_the_built_ins_rather_than_replacing_them():
    """§4.3: a house format is an addition — a log usually mixes it with the
    standard ones."""
    config = Config.empty()
    config.log_patterns = {"house": r"XX-FAIL (?P<name>\w+) @(?P<time>\d+)(?P<unit>ns)"}
    patterns = config.patterns()
    failures = triage.parse_log(
        "XX-FAIL my_check @1200ns\n$error: something at time 90ns\n", patterns, "1ns"
    )
    assert [f.kind for f in failures] == ["house", "error"]
    assert failures[0].name == "my_check" and failures[0].time == 1200


def test_a_broken_user_pattern_is_reported_not_swallowed():
    with pytest.raises(ValueError, match="not a valid regex"):
        triage.parse_log("anything", {"bad": "(unclosed"}, "1ns")


# --- deriving the question (§8.10b step 2) ---------------------------------


def test_a_signal_named_in_the_message_is_found(buggy):
    _store, graph, _clock = buggy
    failure = parse("ERROR: bad value on tb_fifo_buggy.dut.full at time 100ns")[0]
    assert triage.signals_for(failure, graph) == ["tb_fifo_buggy.dut.full"]


def test_a_timeout_asks_what_froze_rather_than_why_a_value_is_wrong(buggy):
    """§8.10b: assertion -> why(); timeout -> stuck() in the window before it."""
    store, graph, clock = buggy
    _t0, t1 = store.time_range
    suspects = triage._timeout_suspects(store, graph, clock, t1, Config.empty())
    assert suspects, "a timeout with a frozen design should name suspects"
    assert any("rd_ptr" in s or "full" in s or "rd_rst_n" in s for s in suspects)


def test_a_timeout_in_the_log_is_triaged_through_the_stuck_detector(buggy):
    store, graph, clock = buggy
    report = triage.triage(
        "$fatal: timeout waiting for done at 455ns\n", store, graph, clock, Config.empty()
    )
    assert report.n_failures == 1
    assert report.causes or report.unexplained


# --- honesty (§8.10b) ------------------------------------------------------


def test_a_failure_that_cannot_be_explained_says_so_with_the_reason(buggy):
    """§8.10b's rule: a tool that hides what it did not understand is not one
    to trust."""
    store, graph, clock = buggy
    report = triage.triage(
        "ERROR: assertion \"p_unknown\" failed at time 100ns\n"
        "       some.module.that.does.not.exist\n",
        store,
        graph,
        clock,
        Config.empty(),
    )
    assert not report.causes
    assert len(report.unexplained) == 1
    _failure, why_not = report.unexplained[0]
    assert "could be matched" in why_not
    assert "unexplained" in triage.format_report(report)


def test_no_rtl_is_reported_rather_than_producing_an_empty_report(buggy):
    store, _graph, clock = buggy
    report = triage.triage(
        triage.read_log(BUGGY / "sim.log"), store, None, clock, Config.empty()
    )
    assert not report.causes
    assert report.unexplained
    assert "no RTL" in report.unexplained[0][1]


def test_an_empty_log_is_not_a_failure(buggy):
    store, graph, clock = buggy
    report = triage.triage("all tests passed\n", store, graph, clock, Config.empty())
    assert report.n_failures == 0
    assert triage.format_report(report) == "no failures found in the log"


def test_the_root_cause_does_not_depend_on_which_simulator_made_the_dump(tmp_path):
    """The same bug must give the same answer from an Icarus or a ModelSim dump.

    They differ in whether a register shows an initial x->0 transition, which
    decides whether `is_constant` holds over the whole run. Terminating on that
    would make the reported root cause a property of the simulator rather than
    of the design, so the constant terminal only fires where the RTL says the
    signal cannot change (§8.1).
    """
    from veritrace.graph.model import Kind

    _store, graph, _clock = None, elaborate(discover(BUGGY)).graph, None
    rd_rst_n = graph.get("tb_fifo_buggy.dut.rd_rst_n")
    rd_ptr = graph.get("tb_fifo_buggy.dut.rd_ptr")
    # `assign rd_rst_n = 1'b0` cannot change; rd_ptr merely never did.
    assert rd_rst_n.is_tie_off
    assert not rd_ptr.is_tie_off
    assert rd_ptr.kind is not Kind.PARAM
