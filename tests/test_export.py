"""Viewer save files and probe scripts — §13.3, §8.11b.

These files are read by tools that are not on this machine, so the tests check
the properties that decide whether a real viewer accepts them: the path syntax
each one wants, well-formed XML, no invented signals, and no crash on the
characters a generate block puts in a hierarchical name.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from veritrace import TraceStore
from veritrace._native import convert
from veritrace.analysis import cone
from veritrace.config import Config
from veritrace.export import probes, viewers
from veritrace.graph.elaborate import discover, elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


@pytest.fixture(scope="module")
def sel() -> viewers.Selection:
    return viewers.Selection(
        signals=["tb.dut.ready", "tb.dut.state", "tb.g_lane[2].u_fifo.q"],
        group="Causal chain",
        radix={"tb.dut.state": "hex", "tb.g_lane[2].u_fifo.q": "dec"},
        markers=[viewers.Marker(1247, "root cause")],
        window=(0, 5000),
        timescale="1ns",
        origin="test",
    )


# --- path syntax -----------------------------------------------------------


def test_slash_paths_keep_generate_indices():
    """ModelSim and xsim want `/a/b`, with the index attached to its element."""
    assert viewers.to_slash("tb.dut.ready") == "/tb/dut/ready"
    assert viewers.to_slash("tb.g_lane[2].u_fifo.q") == "/tb/g_lane[2]/u_fifo/q"


# --- GTKWave ---------------------------------------------------------------


def test_gtkw_lists_signals_dotted_and_names_the_group(sel):
    out = viewers.gtkw(sel)
    assert "-Causal chain" in out
    for path in sel.signals:
        assert f"\n{path}\n" in out
    # GTKWave reads dotted paths, so no conversion should have happened.
    assert "/tb/dut/ready" not in out
    assert "[marker] 1247" in out


# --- ModelSim --------------------------------------------------------------


def test_do_matches_the_shape_in_the_spec(sel):
    """§13.3 gives the exact lines this has to produce."""
    out = viewers.do(sel)
    assert 'add wave -group "Causal chain" -radix hexadecimal /tb/dut/state' in out
    assert 'add wave -group "Causal chain" /tb/dut/ready' in out
    assert 'wave cursor add -time 1247ns -name "root cause"' in out


def test_do_uses_modelsim_radix_names(sel):
    out = viewers.do(sel)
    # `hex` is not a ModelSim radix; `hexadecimal` is.
    assert "-radix hexadecimal" in out and "-radix hex " not in out
    assert "-radix unsigned" in out


# --- Vivado ----------------------------------------------------------------


def test_wcfg_is_well_formed_xml(sel):
    root = ET.fromstring(viewers.wcfg(sel))
    names = [o.get("fp_name") for o in root.iter("wvobject")]
    assert "/tb/dut/ready" in names
    assert "/tb/g_lane[2]/u_fifo/q" in names


def test_wcfg_escapes_characters_that_would_break_the_file():
    """A hierarchical name can carry XML metacharacters; string formatting
    would produce a file Vivado refuses to open."""
    sel = viewers.Selection(signals=['tb.a<b>.q', 'tb."quoted".r'], group="G & G")
    root = ET.fromstring(viewers.wcfg(sel))  # would raise if unescaped
    names = [o.get("fp_name") for o in root.iter("wvobject")]
    assert "/tb/a<b>/q" in names


# --- Surfer ----------------------------------------------------------------


def test_surfer_keeps_dotted_paths(sel):
    out = viewers.surfer(sel)
    assert 'name = "tb.dut.ready"' in out
    assert "[[displayed_items]]" in out


# --- the shared entry point ------------------------------------------------


def test_render_rejects_an_unknown_format(sel):
    with pytest.raises(ValueError, match="unknown format"):
        viewers.render("nope", sel)


def test_every_declared_format_renders(sel):
    for name in viewers.FORMATS:
        assert viewers.render(name, sel).strip()


def test_selection_drops_signals_the_trace_does_not_have(tmp_path):
    """A save file naming signals the viewer cannot find is worse than a short
    one: GTKWave drops them silently and ModelSim errors per line."""
    out = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "fifo_async" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    sel = viewers.selection_from_paths(
        ["tb_fifo_sync.dut.wr_ptr", "tb_fifo_sync.dut.does_not_exist"],
        "Cone",
        store=store,
    )
    assert sel.signals == ["tb_fifo_sync.dut.wr_ptr"]


def test_selection_applies_the_radix_globs_from_the_config(tmp_path):
    """§4.3: the exported file should open with the same formatting as the UI."""
    out = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "fifo_async" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    config = Config.empty()
    config.radix_globs = {"*_ptr": "hex"}
    sel = viewers.selection_from_paths(
        ["tb_fifo_sync.dut.wr_ptr", "tb_fifo_sync.dut.full"], "Cone", store=store, config=config
    )
    assert sel.radix == {"tb_fifo_sync.dut.wr_ptr": "hex"}
    assert "-radix hexadecimal" in viewers.do(sel)


# --- probes (§8.11b) -------------------------------------------------------


@pytest.fixture(scope="module")
def checks_graph():
    return elaborate(discover(DESIGNS / "checks")).graph


@pytest.fixture(scope="module")
def plan(checks_graph):
    result = cone.cone(checks_graph, "tb_checks.dut.u_dut.status", depth=3)
    return probes.plan_from_cone(
        result,
        checks_graph,
        clock=probes.guess_clock(checks_graph, result.paths()),
        depth=1024,
    )


def test_probe_plan_comes_from_the_cone(plan):
    assert plan.target == "tb_checks.dut.u_dut.status"
    assert "tb_checks.dut.u_dut.lock_r" in plan.signals
    assert plan.width_of("tb_checks.dut.u_dut.status") == 2


def test_clock_is_inferred_from_the_probed_flops(checks_graph, plan):
    """Sampling on the clock the logic runs on is the only readable choice."""
    assert plan.clock is not None and plan.clock.endswith("clk")


def test_vivado_script_marks_and_connects_every_probe(plan):
    out = probes.vivado(plan)
    assert "create_debug_core u_ila_0 ila" in out
    assert "set_property C_DATA_DEPTH 1024" in out
    for path in plan.signals:
        assert path.replace(".", "/") in out


def test_quartus_script_uses_pipe_separated_nodes(plan):
    out = probes.quartus(plan)
    assert "package require ::quartus::stp" in out
    assert "tb_checks|dut|u_dut|lock_r" in out
    assert "stp_write_file" in out


def test_probe_scripts_warn_rather_than_emit_an_unbuildable_capture(checks_graph):
    """§8.11b: an ILA is a few thousand samples, not 10^8. Too many probes and
    the depth that fits on the device collapses, so say so."""
    plan = probes.ProbePlan(
        signals=[f"tb.s{i}" for i in range(probes.CROWDED + 5)], target="tb.s0"
    )
    assert plan.crowded
    for fmt in probes.FORMATS:
        assert "WARNING" in probes.render(fmt, plan)


def test_missing_clock_is_flagged_not_guessed():
    """P1: no invented values. An unset clock is marked for the user to fill."""
    plan = probes.ProbePlan(signals=["tb.a"], target="tb.a", clock=None)
    out = probes.vivado(plan)
    assert "WARNING: no sampling clock" in out
    assert "<clk>" in out


def test_probe_render_rejects_an_unknown_format(plan):
    with pytest.raises(ValueError, match="unknown probe format"):
        probes.render("altera", plan)
