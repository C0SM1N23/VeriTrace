"""§8.30 timing correlation, §8.31 SAIF, §8.33 WaveDrom.

Three exports that share one property: they are only worth having if the numbers
in them are the trace's numbers. So each is checked against something independent
— the store's own transition list, the design's own signal names, a report whose
answers are known by construction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import design_store

from veritrace import TraceStore, clocks, timing
from veritrace.export import saif, wavedrom

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
AXI = DESIGNS / "axi_lite"


@pytest.fixture(scope="module")
def store():
    return TraceStore(str(design_store("axi_lite")))


# --- §8.31 SAIF -------------------------------------------------------------


def test_the_four_state_times_add_up_to_the_run(store):
    """A net is in exactly one state at a time, so the four must sum to DURATION.

    A SAIF whose times do not add up is one a power tool will either reject or,
    worse, silently scale.
    """
    report = saif.measure(store)
    assert report.duration > 0
    for a in report.signals:
        if a.duration:
            assert a.duration == report.duration, a.path


def test_toggle_counts_match_the_store(store):
    report = saif.measure(store)
    t0, t1 = store.time_range
    by = {a.path: a for a in report.signals}
    for path in ("tb_axi_lite.aclk", "tb_axi_lite.cpu.state", "tb_axi_lite.s_axi_awvalid"):
        expected = max(0, len(store.transitions(store.find(path), t0, t1)) - 1)
        assert by[path].tc == expected, path


def test_a_constant_signal_never_toggles(store):
    report = saif.measure(store)
    by = {a.path: a for a in report.signals}
    assert by["tb_axi_lite.cpu.awprot"].tc == 0


def test_the_saif_nests_by_hierarchy(store):
    """A power tool attributes activity to instances; a flat list gives it none."""
    text = saif.render(saif.measure(store), design="tb_axi_lite")
    assert text.startswith("(SAIFILE")
    assert "(INSTANCE tb_axi_lite" in text and "(INSTANCE cpu" in text
    assert text.count("(SAIFILE") == 1 and text.rstrip().endswith(")")
    assert "(T0 " in text and "(TC " in text


def test_gating_candidates_are_registers_that_hold(store):
    for a in saif.gating_candidates(saif.measure(store)):
        assert a.tc > 0, "a signal that never moves is a tie-off, not a gating candidate"
        assert (a.held or 0) >= 0.9


# --- §8.33 WaveDrom ---------------------------------------------------------


def test_a_repeated_value_becomes_a_continuation(store):
    """`.` is what makes a diagram readable rather than a wall of repeats."""
    d = wavedrom.build(store, ["tb_axi_lite.s_axi_awvalid"], 0, 400_000)
    row = d.rows[-1]
    assert "." in row.wave
    assert set(row.wave) <= set("01xz.=")


def test_a_bus_carries_one_label_per_change(store):
    d = wavedrom.build(store, ["tb_axi_lite.s_axi_awaddr"], 0, 400_000)
    row = d.rows[-1]
    assert row.wave.count("=") == len(row.data), "one label per drawn cell"
    assert all(v.startswith("0x") for v in row.data)


def test_the_json_is_what_wavedrom_expects(store):
    d = wavedrom.build(store, ["tb_axi_lite.s_axi_awvalid", "tb_axi_lite.s_axi_awaddr"], 0, 400_000)
    payload = json.loads(d.to_json())
    assert [r["name"] for r in payload["signal"]] == ["s_axi_awvalid", "s_axi_awaddr"]
    assert all("wave" in r for r in payload["signal"])


def test_the_svg_is_standalone_and_sized(store):
    d = wavedrom.build(store, ["tb_axi_lite.s_axi_awvalid"], 0, 200_000)
    svg = wavedrom.to_svg(d)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "http://www.w3.org/2000/svg" in svg and "viewBox=" in svg
    assert "#0e1116" in svg, "the dark background of §11.2"
    assert "#ffffff" in wavedrom.to_svg(d, light=True)


def test_a_clocked_range_is_sampled_per_cycle(store):
    clock = clocks.resolve(store, None, None)
    d = wavedrom.build(store, ["tb_axi_lite.s_axi_awvalid"], 100_000, 300_000, clock)
    edges = [e for e in clock.edges if 100_000 <= e <= 300_000]
    assert len(d.rows[-1].wave) == len(edges)
    assert d.head.startswith("c")


# --- §8.30 timing -----------------------------------------------------------


@pytest.fixture(scope="module")
def report():
    return timing.load(AXI / "timing_summary.rpt")


def test_the_summary_and_every_path_are_parsed(report):
    assert report.wns == pytest.approx(-0.417)
    assert report.tns == pytest.approx(-1.204)
    assert len(report.paths) == 3
    assert [p.met for p in report.paths] == [False, False, True]
    # Worst slack first, because that is the order anyone reads them in.
    assert report.paths == sorted(report.paths, key=lambda p: p.slack)


def test_a_path_keeps_its_hops_and_their_delays(report):
    worst = report.paths[0]
    assert worst.group == "aclk" and worst.kind == "Setup"
    assert worst.delay_ns == pytest.approx(9.912)
    assert worst.hops, "the per-hop table is where the delay is attributable"
    assert all("/" in h.resource for h in worst.hops)
    assert max(h.cumulative_ns for h in worst.hops) == pytest.approx(9.912)


def test_the_endpoint_pin_is_dropped_but_the_hierarchy_is_not(report):
    assert report.paths[0].source == "regs/rdata_reg[7]"
    assert report.paths[0].destination == "cpu/state_reg[1]"


def test_a_netlist_name_maps_back_to_the_rtl():
    assert timing.rtl_name("regs/rdata_reg[7]") == "regs.rdata"
    assert timing.rtl_name("cpu/state_reg[1]/D") == "cpu.state"
    assert timing.rtl_name("regs/rdata_reg[7]/Q") == "regs.rdata"
    assert timing.rtl_name("") == ""


def test_correlation_finds_the_line_and_the_activity(report, store):
    """§8.30's two joins, and the second is the one no other tool makes."""
    from veritrace.graph.elaborate import discover, elaborate

    el = elaborate(discover(AXI), [], [], "tb_axi_lite")
    out = timing.correlate(timing.load(AXI / "timing_summary.rpt"), el.graph, store)

    worst = out.paths[0]
    assert worst.dest_signal == "tb_axi_lite.cpu.state"
    assert worst.dest_loc.endswith("axil_master.sv:62")
    assert worst.toggles and worst.toggles > 0

    dead = next(p for p in out.paths if p.destination.startswith("regs/awprot"))
    assert dead.toggles == 0 and dead.dead
    assert out.false_priorities == [dead]
    assert "never switched" in dead.note


def test_without_a_trace_nothing_is_claimed_about_what_matters(report):
    out = timing.correlate(timing.load(AXI / "timing_summary.rpt"))
    assert not out.correlated
    assert all(p.toggles is None for p in out.paths)
    assert out.false_priorities == [], "no trace means no claim, not an empty claim"
