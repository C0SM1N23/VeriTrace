"""§8.27 formal, §8.35 reachability, §8.29 synth-diff.

Everything that can be checked without the toolchain is: the generated files,
the verdict wording, the translation of SBY's output. The three tests that
genuinely need Yosys or SymbiYosys are marked and skip when they are absent —
they are the acceptance criteria, and running them takes a solver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import tools
from veritrace.export import sva
from veritrace.formal import harness
from veritrace.formal.model import Property, Verdict
from veritrace.formal.sby import _translate
from veritrace.graph.elaborate import discover, elaborate
from veritrace.protocol import pack as pack_mod

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

has_yosys = pytest.mark.skipif(tools.find("yosys") is None, reason="needs yosys")
has_sby = pytest.mark.skipif(tools.find("sby") is None, reason="needs SymbiYosys")


@pytest.fixture(scope="module")
def wobble():
    files = discover(DESIGNS / "formal")
    el = elaborate(files, [], [], "axil_wobble")
    return el, files


# --- §8.27's honesty requirement -------------------------------------------


def test_a_verdict_cannot_be_rendered_without_its_depth():
    """§8.27: never `PROVED`, always `PROVED (bounded, depth=N)`."""
    p = Property(id="R", text="", verdict=Verdict.HELD, depth=20)
    assert "depth=20" in str(p)
    assert "PROVED" not in str(p)


def test_properties_the_run_did_not_reach_are_not_reported_as_held():
    """A tool error is not a pass."""
    out = _translate({"R": ""}, "ERROR: something broke", "bmc", 20, Path("."))
    assert out[0].verdict is Verdict.UNKNOWN


def test_a_bounded_run_that_failed_only_claims_the_depth_it_reached():
    """BMC stops at the first broken step, so nothing was checked past it."""
    log = "failed assertion top.u_chk.B at c.sv:9 step 4\nDONE (FAIL, rc=2)"
    out = {p.id: p for p in _translate({"A": "", "B": ""}, log, "bmc", 20, Path("."))}
    assert out["B"].verdict is Verdict.FAILED and out["B"].step == 4
    assert out["A"].verdict is Verdict.HELD
    assert out["A"].depth == 4, "held to the step the search stopped at, not to 20"


def test_unreached_is_not_read_as_reached():
    """"unreached cover statement" contains "reached cover statement".

    Getting this wrong reports dead code as reachable, which costs someone a day
    writing a test for something that cannot happen.
    """
    log = "Unreached cover statement at top.u_dut: vt_reach_0\nDONE (FAIL, rc=2)"
    out = _translate({"vt_reach_0": ""}, log, "cover", 30, Path("."))
    assert out[0].verdict is Verdict.HELD


# --- what gets generated ----------------------------------------------------


def test_the_formal_target_emits_assertions_from_the_same_lowering():
    """§8.27 reuses §8.22's portable lowering; only the emission differs."""
    pack = pack_mod.resolve(["axi4lite"])[0]
    formal = sva.render(pack, "u", target="formal", clock="clk", reset="rst_n")
    portable = sva.render(pack, "u", target="portable", clock="clk", reset="rst_n")
    assert formal.emitted == portable.emitted
    assert "assert (" in formal.code and "$error" not in formal.code
    assert "$error" in portable.code and "assert (" not in portable.code


def test_the_formal_checker_carries_no_bind(wobble):
    """A formal harness drives the design itself; `bind` has nothing to attach to."""
    pack = pack_mod.resolve(["axi4lite"])[0]
    code = sva.render(pack, "u", target="formal", clock="aclk", reset="aresetn").code
    assert "bind " not in code


def test_interfaces_are_found_in_the_rtl_with_no_trace(wobble):
    el, _ = wobble
    found = harness.interfaces(el, pack_mod.resolve(["axi4lite"]))
    assert [i.name for i in found] == ["axil_wobble"]
    assert found[0].clock.endswith("aclk") and found[0].reset.endswith("aresetn")


def test_the_harness_constrains_the_reset(wobble):
    """§8.27 step 2. Without it every property fails at step 1 for no reason."""
    el, _ = wobble
    iface = harness.interfaces(el, pack_mod.resolve(["axi4lite"]))[0]
    dut = harness.instance_of(el, iface.scope)
    pack = iface.pack
    checker = sva.render(pack, iface.name, target="formal", clock="aclk", reset="aresetn")
    from veritrace.formal.prove import _ports

    text = harness.build(dut, iface, checker.module, _ports(checker.code))
    assert "$initstate" in text and "assume (!aresetn)" in text
    # The clock must reach the checker, or its registers are clocked by a constant.
    assert ".aclk(aclk)" in text


def test_the_harness_uses_the_instance_parameters(wobble):
    """A formal result is about a configuration, not about a module's defaults."""
    files = discover(DESIGNS / "axi_lite")
    el = elaborate(files, [], [], "tb_axi_lite")
    iface = harness.interfaces(el, pack_mod.resolve(["axi4lite"]))[0]
    dut = harness.instance_of(el, iface.scope)
    assert ("BAD_AT", "9") in dut.parameters


# --- the acceptance criteria ------------------------------------------------


@has_sby
def test_three_axi_rules_are_proved_to_depth_20(tmp_path):
    """Prompt 17's first criterion, on `designs/axi_lite`."""
    from veritrace.formal.prove import prove

    files = discover(DESIGNS / "axi_lite")
    el = elaborate(files, [], [], "tb_axi_lite")
    iface = harness.interfaces(el, pack_mod.resolve(["axi4lite"]))[0]
    rtl = [f for f in files if "tb_" not in f.name]

    report = prove(el, iface, rtl, tmp_path, depth=20)
    assert len(report.held) >= 3, [str(p) for p in report.properties]
    assert all(p.depth == 20 for p in report.held)


@has_sby
def test_a_counterexample_comes_back_as_a_waveform(tmp_path):
    """Prompt 17's second criterion: the trace exists and VeriTrace can open it."""
    from veritrace import store
    from veritrace.formal.prove import prove

    files = discover(DESIGNS / "formal")
    el = elaborate(files, [], [], "axil_wobble")
    iface = harness.interfaces(el, pack_mod.resolve(["axi4lite"]))[0]

    report = prove(el, iface, files, tmp_path, depth=20)
    assert report.failed, "designs/formal violates a cycle-level AXI rule by construction"
    trace = report.counterexample
    assert trace is not None and trace.exists()

    from veritrace import TraceStore

    opened = TraceStore(str(store.ensure(trace)))
    paths = {s.path for s in opened.signals()}
    # The subject of the broken rule is in it, which is what makes why-trace
    # possible without any manual step.
    assert report.failed[0].subject in paths


@has_sby
def test_reachability_tells_dead_code_from_a_missing_test(tmp_path):
    """§8.35 on the branch that cannot fire: `retry` is 2 bits, so `retry > 3`."""
    from veritrace.formal.prove import reach

    class _C:
        def __init__(self, text):
            self.text = text

    class _H:
        def __init__(self, line, text):
            self.file, self.line, self.label, self.note = "axil_wobble.sv", line, "if", ""
            self.conditions = [_C(text)]

    files = discover(DESIGNS / "formal")
    el = elaborate(files, [], [], "axil_wobble")
    out = reach(
        el,
        [_H(83, "retry > 3"), _H(81, "bvalid && bresp != 2'b00")],
        files, "axil_wobble", tmp_path, depth=30,
    )
    by_line = {r.line: r for r in out}
    assert by_line[83].status == "unreachable"
    assert by_line[81].status == "reachable"


@has_yosys
def test_synth_diff_finds_an_injected_mismatch(tmp_path):
    """§8.29's criterion: an incomplete sensitivity list, proved rather than suspected."""
    from veritrace.simulate import find_iverilog

    if find_iverilog() is None:
        pytest.skip("needs Icarus as well")
    from veritrace.synth import diff as synth
    from veritrace.synth.yosys import dut_of

    root = DESIGNS / "synth_mismatch"
    src, tb = [root / "mux_bug.sv"], [root / "tb_mux.sv"]
    el = elaborate([*src, *tb], [], [], "tb_mux")

    out = synth.run(src, tb, "tb_mux", dut_of(el, "tb_mux"), tmp_path)
    assert not out.matched
    diverging = {d.signal for d in out.report.divergences}
    assert any(d.endswith(".y") for d in diverging)
    # The correctly written mux is compared in the same run and must not diverge:
    # a check that fires on correct RTL as well says nothing.
    assert not any(d.endswith(".y_ok") for d in diverging)


@has_yosys
def test_the_shim_lets_an_unmodified_testbench_drive_the_netlist(tmp_path):
    """§8.29 insists on the *same* testbench, and synthesis erases parameters."""
    from veritrace.synth.yosys import dut_of, shim

    root = DESIGNS / "mutation"
    el = elaborate([root / "fifo.sv", root / "tb_fifo.sv"], [], [], "tb_fifo")
    dut = dut_of(el, "tb_fifo")
    text = shim(dut)
    assert "parameter WIDTH" in text and "parameter DEPTH" in text
    assert "fifo__vt_gate u_gate" in text
