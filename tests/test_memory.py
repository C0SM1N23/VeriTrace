"""SDRAM controller analysis — §8.20, TAB 10.

The two acceptance criteria of Prompt 11 are the first two sections:

1. the real injected timing violations are detected with the exact cycle and
   the correct constraint — and the *same RTL compiled clean* produces none,
   which is the half that proves the checker measures the design rather than
   announcing itself;
2. the bank timeline shows the right state for every bank across a sequence
   worked out by hand.

The second is built from a hand-written command list rather than a dump, for
the same reason `test_perf` builds its stall trace by hand: checking a timeline
against a trace it was derived from proves only self-consistency, while
checking it against a sequence whose answer was written down in advance proves
the state machine is right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import checks, memory as memory_findings, vtq
from veritrace.analysis.findings import Group
from veritrace.config import Config
from veritrace.memory import banks, metrics, query as mem_query, report as mem_report, timing
from veritrace.memory.model import ACTIVATING, ACTIVE, IDLE, PRECHARGING, CmdEvent
from veritrace.protocol import engine, pack

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

#: The fixture's four-ACTIVATE burst is legal for tFAW. It used to be a
#: false-positive golden; real fifth-activation violations are tested below.
INJECTED = {"tRCD", "tRP", "tRFC"}


def _open(vcd: str, tmp_path_factory):
    out = tmp_path_factory.mktemp("sdram") / "dump.vtx"
    convert(str(DESIGNS / "sdram" / vcd), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    reports = mem_report.build_all(store, analysis.packs, clock, Config.empty())
    return store, clock, analysis, reports


@pytest.fixture(scope="module")
def violating(tmp_path_factory):
    return _open("dump.vcd", tmp_path_factory)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    """The same RTL with `VIOLATE` compiled out — the control for every claim."""
    return _open("dump_ok.vcd", tmp_path_factory)


def cmd(t: int, name: str, **fields) -> CmdEvent:
    return CmdEvent(time=t, name=name, fields=fields)


# --- acceptance 1: every injected violation, with the exact cycle ----------


def test_the_command_bus_is_detected_as_one_interface(violating):
    """A controller's ports and the testbench wires they drive are one command
    bus, not two — the same collapse §8.14 does for a master/slave port pair."""
    _store, _clock, _analysis, reports = violating
    assert len(reports) == 1, [r.iface for r in reports]
    assert reports[0].n_banks == 4


def test_every_command_in_the_pack_is_decoded(violating):
    """§8.20's decode table, end to end: five command types out of four
    control pins plus a multiplexed address bus."""
    _store, _clock, _analysis, reports = violating
    names = {c.name for c in reports[0].commands}
    assert names == {"ACTIVATE", "READ", "WRITE", "PRECHARGE", "REFRESH"}
    # And the args came out: an ACTIVATE carries the row it opened.
    act = next(c for c in reports[0].commands if c.name == "ACTIVATE")
    assert act.fields["bank"] == 0 and act.fields["row"] == 0x1A4
    # A READ's column is a bit-slice of the same address bus (§8.20's `a[9:0]`).
    rd = next(c for c in reports[0].commands if c.name == "READ")
    assert rd.fields["col"] == 0x030 and rd.fields["ap"] == 0


def test_injected_violations_are_found_without_blaming_four_legal_activations(violating):
    """Prompt 11's first acceptance criterion."""
    _store, _clock, _analysis, reports = violating
    found = {v.constraint for v in reports[0].violations}
    assert found == INJECTED, sorted(found)
    # Exactly one of each: a collateral second hit would mean the design's
    # other gaps are not as clear of the constraints as they are meant to be.
    for name in INJECTED:
        hits = [v for v in reports[0].violations if v.constraint == name]
        assert len(hits) == 1, (name, hits)


@pytest.mark.parametrize(
    "constraint,at_cycle,measured,limit,bank",
    [
        ("tRCD", 9, 1, 2, 0),
        ("tRP", 15, 1, 2, 0),
        ("tRFC", 29, 6, 7, None),
    ],
)
def test_each_violation_names_the_exact_cycle_and_constraint(
    violating, constraint, at_cycle, measured, limit, bank
):
    """*Toate cele 4 violari injectate sunt detectate cu ciclul exact si
    constrangerea corecta.* Not "something looks wrong near here"."""
    _store, clock, _analysis, reports = violating
    v = next(v for v in reports[0].violations if v.constraint == constraint)
    assert clock.cycle_of(v.at) == at_cycle
    assert v.measured_cycles == measured
    assert v.limit_cycles == limit
    assert v.bank == bank
    # Both ends of the measurement are named, so the report can say what the
    # gap was actually between.
    assert v.first is not None and v.second is not None
    assert v.second.time == v.at


def test_the_same_design_compiled_clean_has_no_violations(clean):
    """The half that makes the checker worth having: "none found" has to be as
    trustworthy as a positive finding."""
    _store, _clock, _analysis, reports = clean
    assert len(reports) == 1
    assert reports[0].violations == []
    # And it is not that nothing was analysed: the same 13 commands are there.
    assert len(reports[0].commands) == 13


def test_a_constraint_with_no_violations_is_reported_as_checked(clean):
    """§8.20's report ends with "✓ tRP, tRAS, tRC, tFAW: conforme" — a stated
    fact, not the absence of a line."""
    _store, _clock, _analysis, reports = clean
    checked = reports[0].checked
    assert INJECTED <= set(checked)
    assert all(n == 0 for n in checked.values())


def test_cl_and_cwl_say_why_they_were_not_checked(violating):
    """P7. This pack decodes the command bus and nothing else, so data latency
    is unmeasurable from it — which has to look different from "conformant"."""
    _store, _clock, _analysis, reports = violating
    assert "CL" in reports[0].skipped and "CWL" in reports[0].skipped
    assert "data-bus" in reports[0].skipped["CL"]
    assert "CL" not in reports[0].checked


# --- acceptance 2: the bank timeline ----------------------------------------


def test_the_bank_timeline_matches_a_hand_worked_sequence():
    """Prompt 11's second acceptance criterion, on a sequence whose answer was
    written down before the code ran.

    One bank, one row opened at t=10 and closed at t=50, with tRCD=4 and
    tRP=6 — so the states are idle, activating (4 long), active, precharging
    (6 long), idle, and the whole run is covered with no gaps.
    """
    segs = banks.segments(
        [cmd(10, "ACTIVATE", bank=0, row=5), cmd(50, "PRECHARGE", bank=0)],
        n_banks=1,
        tRCD=4,
        tRP=6,
        run_start=0,
        run_end=100,
    )
    assert [(s.state, s.t0, s.t1, s.row) for s in segs] == [
        (IDLE, 0, 10, None),
        (ACTIVATING, 10, 14, 5),
        (ACTIVE, 14, 50, 5),
        (PRECHARGING, 50, 56, 5),
        (IDLE, 56, 100, None),
    ]


def test_the_timeline_covers_the_whole_run_with_no_gaps(violating):
    """A timeline with silent holes is not one whose shape can be trusted —
    the same reasoning §8.17 applies to stall attribution summing to 100%."""
    _store, clock, _analysis, reports = violating
    r = reports[0]
    for bank in range(r.n_banks):
        segs = [s for s in r.segments if s.bank == bank]
        assert segs, bank
        assert segs[0].t0 == clock.edges[0]
        assert segs[-1].t1 == clock.edges[-1]
        for a, b in zip(segs, segs[1:]):
            assert a.t1 == b.t0, (bank, a, b)


def test_a_bank_reports_the_row_it_has_open(violating):
    """The timeline's whole point per §8.20: *vezi dintr-o privire ce banc e
    deschis pe ce rand*."""
    _store, _clock, _analysis, reports = violating
    active = [s for s in reports[0].segments if s.state == ACTIVE and s.bank == 0]
    assert active, reports[0].segments
    assert any(s.row == 0x1A4 for s in active)
    # An idle bank has no open row to report, and says so rather than keeping
    # the last one.
    assert all(s.row is None for s in reports[0].segments if s.state == IDLE)


def test_a_command_arriving_early_cuts_the_previous_segment_short():
    """On a design that violates tRP the next ACTIVATE lands while the bank is
    still precharging. The timeline shows what the controller *did* — so that
    segment is truncated, not left overlapping the one after it, and not
    stretched to stay legal. The too-early command is the finding, and the
    timing checker reports it separately."""
    segs = banks.segments(
        [cmd(0, "ACTIVATE", bank=0, row=1), cmd(10, "PRECHARGE", bank=0), cmd(14, "ACTIVATE", bank=0, row=2)],
        n_banks=1,
        tRCD=2,
        tRP=20,  # would run to t=30, but the next ACTIVATE arrives at t=14
        run_start=0,
        run_end=40,
    )
    precharging = next(s for s in segs if s.state == PRECHARGING)
    assert (precharging.t0, precharging.t1) == (10, 14)
    for a, b in zip(segs, segs[1:]):
        assert a.t1 == b.t0, (a, b)


def test_precharge_all_closes_every_bank():
    """`a[10]` on a PRECHARGE means "all banks", and every real controller uses
    it. Filing such a command under `ba` alone would leave the other banks
    believing their rows were still open."""
    cmds = [
        cmd(0, "ACTIVATE", bank=0, row=1),
        cmd(10, "ACTIVATE", bank=1, row=2),
        cmd(40, "PRECHARGE", bank=0, all=1),
    ]
    segs = banks.segments(cmds, n_banks=2, tRCD=2, tRP=4, run_start=0, run_end=60)
    for bank in (0, 1):
        states = [s.state for s in segs if s.bank == bank]
        assert PRECHARGING in states, (bank, states)
        assert states[-1] == IDLE, (bank, states)


# --- the constraint checker, per constraint ---------------------------------


def test_trc_is_checked_across_the_precharge_between_two_activates():
    """tRC is ACTIVATE -> ACTIVATE on one bank, and in any normal sequence
    that spans a PRECHARGE. Clearing the tracker on PRECHARGE — the obvious
    implementation — makes the constraint silently never fire."""
    u = banks._Units(tRC=50, tRP=20, tRAS=25)
    seq = [
        cmd(0, "ACTIVATE", bank=0, row=1),
        cmd(25, "PRECHARGE", bank=0),
        cmd(45, "ACTIVATE", bank=0, row=2),
    ]
    hits = [v for v in banks._same_bank_violations(seq, 0, u, None) if v.constraint == "tRC"]
    assert len(hits) == 1 and hits[0].measured_ticks == 45
    assert hits[0].measured_cycles is None  # No clock, no fabricated cycles.


def test_trrd_measures_against_the_last_different_bank_activate():
    """Comparing against the immediately preceding command instead would skip
    the check whenever two same-bank ACTIVATEs sit in between."""
    u = banks._Units(tRRD=3)
    acts = [
        cmd(0, "ACTIVATE", bank=0),
        cmd(1, "ACTIVATE", bank=0),
        cmd(2, "ACTIVATE", bank=1),
    ]
    hits = [v for v in banks._device_wide_violations(acts, u, None) if v.constraint == "tRRD"]
    assert len(hits) == 1 and hits[0].measured_ticks == 1
    assert hits[0].measured_cycles is None


def test_fractional_limits_do_not_round_away_a_real_timing_violation():
    from dataclasses import replace

    chip = replace(timing.find("mt48lc16m16a2"), tRCD=20.5, tREFI=100.5)
    clock = clocks.Clock("tb.clk", 0, list(range(0, 201, 10)))
    commands = [cmd(0, "ACTIVATE", bank=0), cmd(20, "READ", bank=0),
                cmd(40, "REFRESH"), cmd(141, "REFRESH")]
    violations, _, _ = banks.check_timing(commands, 1, chip, "1ns", clock)
    rcd = next(v for v in violations if v.constraint == "tRCD")
    assert (rcd.measured_ticks, rcd.limit_ticks, rcd.limit_cycles) == (20, 21, 3)
    refi = next(v for v in violations if v.constraint == "tREFI")
    assert (refi.measured_ticks, refi.limit_ticks, refi.limit_cycles) == (101, 100, 10)


def test_trefi_is_a_maximum_not_a_minimum():
    """Every other constraint is "at least"; tREFI is "at most", and reporting
    it the same way round would invert the finding."""
    u = banks._Units(tREFI=1000, tRFC=1)
    hits = [
        v
        for v in banks._device_wide_violations(
            [cmd(0, "REFRESH"), cmd(2000, "REFRESH")], u, None
        )
        if v.constraint == "tREFI"
    ]
    assert len(hits) == 1 and hits[0].is_maximum


def test_a_command_with_an_undecodable_bank_is_counted_not_dropped():
    """P1: an X on `ba` means that command took part in no per-bank check, and
    a silent drop reads exactly like a clean design."""
    chip = timing.find("mt48lc16m16a2")
    cmds = [cmd(0, "ACTIVATE", bank=None, row=1), cmd(100, "ACTIVATE", bank=0, row=1)]
    _v, _checked, skipped = banks.check_timing(cmds, 4, chip, "1ns", None)
    assert "bank" in skipped and "1 command" in skipped["bank"]


# --- efficiency metrics ------------------------------------------------------


def test_row_hits_classify_the_reopen_as_a_conflict():
    """An ACTIVATE that follows a PRECHARGE on the same bank had to close
    another row first — that is the conflict §8.20 says costs bandwidth,
    distinct from a compulsory first-touch miss."""
    cmds = [
        cmd(0, "ACTIVATE", bank=0, row=1),
        cmd(5, "READ", bank=0, col=0),
        cmd(10, "READ", bank=0, col=1),
        cmd(20, "PRECHARGE", bank=0),
        cmd(30, "ACTIVATE", bank=0, row=2),
    ]
    total, per_bank = metrics.row_hits(cmds, n_banks=1)
    assert (total.misses, total.conflicts, total.hits) == (1, 1, 1)
    assert per_bank[0].total == 3


def test_metrics_are_counted_in_cycles_not_trace_units(violating):
    """A dump in picoseconds and the same design in nanoseconds must report
    the same numbers — a ratio against raw trace units would differ by 1000."""
    _store, _clock, _analysis, reports = violating
    e = reports[0].efficiency
    assert 0.0 < e.bus_utilization <= 1.0
    # Two data commands about twenty cycles apart, not two hundred thousand.
    assert e.turnaround_events == 1
    assert 0 < e.turnaround_cycles < 100


def test_every_efficiency_metric_is_a_series_not_only_a_total(violating):
    """*Toate afisate ca serie temporala, nu doar ca numar total — asa vezi
    cand se degradeaza.*"""
    _store, _clock, _analysis, reports = violating
    e = reports[0].efficiency
    for series in (e.row_hit_series, e.bus_utilization_series, e.bank_parallelism_series):
        assert series is not None and series.points


# --- findings, §10.1 commands and the default tab ---------------------------


def test_timing_violations_appear_in_checks_like_any_other_finding(violating):
    """§11.4: a violated tRCD is not a separate kind of news."""
    store, clock, analysis, reports = violating
    report = checks.run_all(
        store, None, None, clock, Config.empty(), analysis, None, reports
    )
    found = [f for f in report if f.group is Group.MEMORY]
    assert len(found) == len(INJECTED)
    assert {f.check for f in found} == {memory_findings.CHECK}
    assert Group.MEMORY.value not in report.skipped
    # Every row carries a runnable `why`, the same contract §11.4 puts on the
    # rest of the tab.
    for f in found:
        assert f.why and vtq.parse(f.why).signal


def test_a_memory_design_opens_on_the_memory_tab(violating):
    """§11.4b: *daca s-au detectat interfete de memorie -> Memory*, ahead of
    the interface count."""
    from veritrace.analysis.findings import Report

    assert checks.default_tab(Report(), n_memory_interfaces=1) == "memory"
    # And it outranks the Transactions rule rather than racing it.
    assert checks.default_tab(Report(), n_interfaces=5, n_memory_interfaces=1) == "memory"
    assert checks.default_tab(Report(), n_interfaces=5, n_memory_interfaces=0) == "transactions"
    # Still overridable (§4.3).
    assert checks.default_tab(Report(), n_memory_interfaces=1, configured="wave") == "wave"


@pytest.mark.parametrize(
    "query,key",
    [
        ("cmds(ctrl)", "commands"),
        ("banks(ctrl)", "segments"),
        ("timing(ctrl)", "violations"),
        ("rowhits(ctrl)", "efficiency"),
    ],
)
def test_every_section_10_1_memory_command_runs(violating, query, key):
    _store, _clock, _analysis, reports = violating
    got = mem_query.run(reports, vtq.parse_pipeline(query))
    assert key in got


def test_timing_can_be_re_checked_against_another_chip(violating, tmp_path):
    """*Selectezi chip-ul din dropdown... sau incarci propriile valori.* Only
    the timing half depends on the chip, so only that half is recomputed."""
    store, clock, _analysis, reports = violating
    fast = tmp_path / "packs" / "timing"
    fast.mkdir(parents=True)
    # A part fast enough that none of the injected gaps is short for it.
    (fast / "faster.toml").write_text(
        "name = \"FASTER\"\n"
        + "\n".join(
            f"{k} = 1"
            for k in ("tRCD", "tRP", "tRAS", "tRC", "tRRD", "tWR", "tWTR", "tRTP", "tFAW", "tRFC")
        )
        + "\ntREFI = 100000\nCL = 2\nCWL = 1\n",
        encoding="utf-8",
    )
    got = mem_query.run(
        reports, vtq.parse_pipeline("timing(ctrl, chip=faster)"), store, clock, tmp_path
    )
    assert got["chip"] == "faster"
    assert got["violations"] == []


def test_an_unknown_memory_interface_says_what_was_detected(violating):
    _store, _clock, _analysis, reports = violating
    with pytest.raises(vtq.QueryError, match="ctrl"):
        mem_query.run(reports, vtq.parse_pipeline("cmds(nope)"))


# --- the pack and the chip timing files -------------------------------------


def test_the_shipped_sdram_pack_decodes_the_section_8_20_table():
    """The encodings are the datasheet's, so a typo here would silently decode
    the wrong command for every SDRAM design there will ever be."""
    p = next(x for x in pack.discover() if x.slug == "sdram")
    assert p.is_memory and not p.channels and not p.transactions
    by_name = {c.name: c for c in p.commands}
    assert set(by_name) == {"ACTIVATE", "READ", "WRITE", "PRECHARGE", "REFRESH"}
    assert by_name["ACTIVATE"].encode == {"cs_n": 0, "ras_n": 0, "cas_n": 1, "we_n": 1}
    assert by_name["READ"].encode == {"cs_n": 0, "ras_n": 1, "cas_n": 0, "we_n": 1}
    assert by_name["WRITE"].encode == {"cs_n": 0, "ras_n": 1, "cas_n": 0, "we_n": 0}
    assert by_name["PRECHARGE"].encode == {"cs_n": 0, "ras_n": 0, "cas_n": 1, "we_n": 0}
    assert by_name["REFRESH"].encode == {"cs_n": 0, "ras_n": 0, "cas_n": 0, "we_n": 1}
    assert p.address_map is not None


def test_the_shipped_chip_file_carries_every_constraint():
    """A missing parameter would mean a constraint silently never checked."""
    chip = timing.find("mt48lc16m16a2")
    for name in (*timing.NS_FIELDS, *timing.CYCLE_FIELDS):
        assert getattr(chip, name) is not None
    assert chip.tRCD == 20 and chip.tRP == 20 and chip.CL == 3


def test_a_chip_file_missing_a_parameter_is_refused():
    with pytest.raises(timing.TimingError, match="missing timing parameter"):
        timing.loads('name = "X"\ntRCD = 20\n')


@pytest.mark.parametrize("field,value", [("tRCD", "nan"), ("tRFC", "inf"),
                                        ("CL", "2.5"), ("CWL", "true")])
def test_invalid_timing_values_are_rejected(field, value):
    source = timing.find("mt48lc16m16a2").path.read_text()
    import re
    changed = re.sub(rf"(?m)^{field}\s*=.*$", f"{field} = {value}", source)
    with pytest.raises(timing.TimingError, match="finite|whole number"):
        timing.loads(changed)


def test_tfaw_allows_four_activates_and_checks_the_fifth_at_the_exact_boundary():
    limits = banks._Units(tFAW=100)
    four = [cmd(t, "ACTIVATE", bank=i) for i, t in enumerate([0, 10, 20, 30])]
    assert not [v for v in banks._device_wide_violations(four, limits, None) if v.constraint == "tFAW"]
    for at, expected in [(99, 1), (100, 0)]:
        five = four + [cmd(at, "ACTIVATE", bank=0)]
        hits = [v for v in banks._device_wide_violations(five, limits, None) if v.constraint == "tFAW"]
        assert len(hits) == expected
        if hits:
            assert hits[0].first.time == 0 and hits[0].second.time == 99


def test_missing_command_pairs_are_not_reported_as_conformant():
    chip = timing.find("mt48lc16m16a2")
    violations, checked, skipped = banks.check_timing([cmd(0, "ACTIVATE", bank=0)], 1,
                                                     chip, "1ns", None)
    assert not violations and not checked
    assert set(timing.NS_FIELDS) <= skipped.keys()
    assert "no qualifying" in skipped["tREFI"]


def test_command_only_write_recovery_and_auto_precharge_are_qualified():
    chip = timing.find("mt48lc16m16a2")
    commands = [cmd(0, "ACTIVATE", bank=0), cmd(100, "WRITE", bank=0, ap=1),
                cmd(1000, "PRECHARGE", bank=0)]
    violations, checked, skipped = banks.check_timing(commands, 1, chip, "1ns", None)
    assert not violations
    assert "tWR" not in checked
    assert "last write data beat" in skipped["tWR"]
    assert "auto-precharge" in skipped
