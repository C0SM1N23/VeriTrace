"""Correlation layer — §7.

The acceptance thresholds for this stage live here: fifo_async must reach 98%,
the generate/instance-array design 90%, and the mandatory Verilator flags of
§4.0 must be shown to matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from veritrace import TraceStore, convert
from veritrace.cli import main
from veritrace.correlate.heuristics import normalize, strip_indices, suffixes
from veritrace.correlate.resolver import Method, correlate, format_report
from veritrace.graph.elaborate import elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
FIFO_RTL = [DESIGNS / "fifo_async" / "tb_fifo_sync.sv", DESIGNS / "fifo_async" / "fifo_sync.sv"]
LANES_RTL = [DESIGNS / "lanes" / "tb_lanes.sv", DESIGNS / "lanes" / "lanes.sv"]


def load(tmp_path: Path, vcd: Path, name: str) -> TraceStore:
    out = tmp_path / f"{name}.vtx"
    convert(str(vcd), str(out))
    return TraceStore(str(out))


def run(tmp_path: Path, rtl, vcd: Path, name: str):
    store = load(tmp_path, vcd, name)
    el = elaborate(rtl)
    rep = correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    return el, store, rep


# --- normalisation (§7.1) --------------------------------------------------


def test_normalize_handles_the_hard_names():
    # Escaped identifier: backslash and trailing space are syntax.
    assert normalize("top.\\my$sig ") == "top.my$sig"
    # Verilator's bracket mangling and the `__N` spelling both mean `[N]`.
    assert normalize("top.g_lane__BRA__2__KET__.q") == "top.g_lane[2].q"
    assert normalize("top.g_lane__2.q") == "top.g_lane[2].q"
    assert normalize("TOP.Clk") == "top.clk"


def test_suffixes_are_longest_first():
    assert suffixes("a.b.c") == ["a.b.c", "b.c", "c"]


def test_strip_indices():
    assert strip_indices("g_lane[2].u_fifo.wr_ptr") == "g_lane.u_fifo.wr_ptr"


# --- acceptance thresholds -------------------------------------------------


def test_fifo_async_correlates_at_least_98_percent(tmp_path):
    _, _, rep = run(tmp_path, FIFO_RTL, DESIGNS / "fifo_async" / "dump.vcd", "fifo")
    assert rep.percent >= 98.0, format_report(rep)
    assert not rep.unmatched, rep.unmatched


def test_lanes_correlates_at_least_90_percent(tmp_path):
    """The hard case: for-generate plus an instance array."""
    _, _, rep = run(tmp_path, LANES_RTL, DESIGNS / "lanes" / "dump.vcd", "lanes")
    assert rep.percent >= 90.0, format_report(rep)


def test_lanes_mappings_are_correct_not_merely_present(tmp_path):
    """Twenty mappings checked by identity, not by existence.

    A resolver that matched everything to the first handle would pass a rate
    check; this is what catches it.
    """
    el, store, rep = run(tmp_path, LANES_RTL, DESIGNS / "lanes" / "dump.vcd", "lanes")

    checked = 0
    for sig in el.graph:
        if sig.trace_handle is None:
            continue
        traced = store.signal(sig.trace_handle)
        # The mapped trace signal must be the same signal: same normalised path
        # and the same width.
        assert normalize(traced.path) == normalize(sig.path), f"{sig.path} -> {traced.path}"
        if sig.kind.value not in ("param",):
            assert traced.width == sig.width, f"{sig.path}: {traced.width} != {sig.width}"
        checked += 1
    assert checked >= 20, f"only {checked} mappings verified"

    # Each generate lane must map to its own instance, not all to lane 0.
    for i in range(4):
        sig = el.graph.get(f"tb_lanes.dut.g_lane[{i}].u_fifo.wr_ptr")
        assert sig is not None and sig.trace_handle is not None
        assert store.signal(sig.trace_handle).path == f"tb_lanes.dut.g_lane[{i}].u_fifo.wr_ptr"

    # Same for the instance array.
    handles = {
        el.graph.get(f"tb_lanes.dut.u_cnt[{i}].q").trace_handle for i in range(4)
    }
    assert len(handles) == 4, "instance-array elements collapsed onto one handle"
    assert rep.percent >= 90.0


def test_memories_correlate_element_by_element(tmp_path):
    """A simulator dumps `mem[0]`, never `mem` (§5.6)."""
    el, store, _ = run(tmp_path, FIFO_RTL, DESIGNS / "fifo_async" / "dump.vcd", "fifo")
    mem = el.graph.get("tb_fifo_sync.dut.mem")
    assert mem.trace_handle is None
    assert mem.elements, "memory words were not correlated"
    assert mem.is_traced
    for idx, handle in mem.elements.items():
        assert store.signal(handle).path == f"tb_fifo_sync.dut.mem[{idx}]"


# --- the mandatory Verilator flags (§4.0) ----------------------------------


def test_verilator_flags_change_what_can_be_correlated(tmp_path):
    """§4.0: without the mandated flags the trace loses signals.

    Both dumps come from Verilator 5.032 on the same design, one built with the
    §4.0 flag set and one with defaults. The design carries 64-entry memories,
    deeper than Verilator's default `--trace-max-array` of 32.
    """
    with_flags = DESIGNS / "lanes" / "verilator_with.vcd"
    without = DESIGNS / "lanes" / "verilator_without.vcd"
    if not (with_flags.exists() and without.exists()):
        pytest.skip("Verilator reference dumps not present")

    el_w, store_w, rep_w = run(tmp_path, LANES_RTL, with_flags, "vw")
    el_n, store_n, rep_n = run(tmp_path, LANES_RTL, without, "vn")

    # The flags decide how much of the design is observable at all.
    assert store_w.n_signals > 3 * store_n.n_signals, (
        f"expected far more signals with the flags: {store_w.n_signals} vs {store_n.n_signals}"
    )

    # Concretely: the memories only reach the trace with --trace-max-array.
    mem_w = el_w.graph.get("tb_lanes.dut.g_lane[0].u_fifo.mem")
    mem_n = el_n.graph.get("tb_lanes.dut.g_lane[0].u_fifo.mem")
    assert mem_w.elements, "memory should be traced with --trace-max-array 1024"
    assert not mem_n.elements, "memory should be missing at the default limit of 32"

    # And the correlation rate follows.
    assert rep_w.percent > rep_n.percent, f"{rep_w.percent} vs {rep_n.percent}"
    assert rep_w.percent >= 99.0


def test_flagless_dump_still_degrades_gracefully(tmp_path):
    """A worse trace is a worse trace, not a crash (P7)."""
    without = DESIGNS / "lanes" / "verilator_without.vcd"
    if not without.exists():
        pytest.skip("Verilator reference dump not present")
    _, _, rep = run(tmp_path, LANES_RTL, without, "vn2")
    assert rep.total > 0
    assert 0.0 < rep.rate <= 1.0
    assert "correlated" in format_report(rep)


# --- no RTL (§7.4) ---------------------------------------------------------


def test_correlate_without_rtl_is_a_clear_message_not_a_crash(tmp_path):
    store_dir = tmp_path / "fifo.vtx"
    convert(str(DESIGNS / "fifo_async" / "dump.vcd"), str(store_dir))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["correlate", str(store_dir)])
    assert result.exit_code != 0
    assert "--rtl" in result.output


def test_correlate_cli_reports_the_rate(tmp_path):
    store_dir = tmp_path / "fifo.vtx"
    convert(str(DESIGNS / "fifo_async" / "dump.vcd"), str(store_dir))
    runner = CliRunner()
    result = runner.invoke(
        main, ["correlate", str(store_dir), "--rtl", str(DESIGNS / "fifo_async")]
    )
    assert result.exit_code == 0, result.output
    assert "correlated" in result.output
    assert "100.0%" in result.output


def test_report_warns_below_ninety_percent():
    from veritrace.correlate.resolver import CorrelationReport

    rep = CorrelationReport(total=100, matched=50)
    text = format_report(rep)
    assert "WARNING" in text and "§4.0" in text


def test_unknown_method_counts_are_reported(tmp_path):
    _, _, rep = run(tmp_path, LANES_RTL, DESIGNS / "lanes" / "dump.vcd", "lanes2")
    assert rep.by_method.get(Method.EXACT.value, 0) > 50
    assert sum(rep.by_method.values()) == rep.total
