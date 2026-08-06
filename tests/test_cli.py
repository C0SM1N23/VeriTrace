import tomllib

from click.testing import CliRunner

from veritrace import __version__
from veritrace.cli import main

SAMPLE_FIFO = """\
module fifo_sync (
    input  logic clk,
    input  logic rst_n,
    input  logic wr_en,
    output logic full
);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) full <= 1'b0;
        else if (wr_en) full <= 1'b1;
    end
endmodule
"""


def test_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_init_writes_valid_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fifo_sync.sv").write_text(SAMPLE_FIFO)

    runner = CliRunner()
    result = runner.invoke(main, ["init"], input="y\ny\ny\n")
    assert result.exit_code == 0, result.output

    config_path = tmp_path / ".veritrace.toml"
    assert config_path.exists()

    data = tomllib.loads(config_path.read_text())
    assert data["design"]["top"] == "fifo_sync"
    assert data["clocks"]["primary"] == "clk"
    assert data["reset"]["signal"] == "rst_n"
    assert data["reset"]["active"] == "low"


def test_init_fails_without_rtl_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code != 0


# --- the analysis commands (§13, §13.3, §8.10b) ----------------------------

import shutil
from pathlib import Path

import pytest

from veritrace._native import convert

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


@pytest.fixture(scope="module")
def buggy(tmp_path_factory):
    """A copy of the buggy FIFO with its store built, so the CLI has a target."""
    work = tmp_path_factory.mktemp("cli-buggy")
    shutil.copytree(DESIGNS / "fifo_buggy", work / "fifo_buggy")
    design = work / "fifo_buggy"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    return design


def run(args):
    return CliRunner().invoke(main, args)


def test_why_prints_the_chain_ending_at_the_root_cause(buggy):
    r = run([
        "why", str(buggy / "dump.vtx"),
        "why(tb_fifo_buggy.dut.full)", "--rtl", str(buggy),
    ])
    assert r.exit_code == 0, r.output
    assert "tb_fifo_buggy.dut.full" in r.output
    assert "rd_rst_n" in r.output and "constant" in r.output


def test_why_json_is_machine_readable(buggy):
    import json

    r = run([
        "why", str(buggy / "dump.vtx"),
        "why(tb_fifo_buggy.dut.full)", "--rtl", str(buggy), "--json",
    ])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["root"]["signal"] == "tb_fifo_buggy.dut.full"
    assert body["stats"]["nodes"] > 1


def test_why_needs_rtl_and_says_so(buggy):
    r = run(["why", str(buggy / "dump.vtx"), "why(tb_fifo_buggy.dut.full)"])
    assert r.exit_code != 0
    assert "--rtl" in r.output


@pytest.mark.parametrize(
    "flag,marker",
    [
        ("--do", "add wave -group"),
        ("--gtkw", "[timestart]"),
        ("--wcfg", "<wave_config"),
        ("--surfer", "[[displayed_items]]"),
    ],
)
def test_why_writes_a_save_file_for_every_viewer(buggy, flag, marker):
    """§13.3: the same analysis, in the format of the viewer you already use."""
    r = run([
        "why", str(buggy / "dump.vtx"),
        "why(tb_fifo_buggy.dut.full)", "--rtl", str(buggy), flag,
    ])
    assert r.exit_code == 0, r.output
    assert marker in r.output
    assert "rd_rst_n" in r.output


def test_why_writes_to_a_file_when_asked(buggy, tmp_path):
    out = tmp_path / "cone.do"
    r = run([
        "why", str(buggy / "dump.vtx"),
        "why(tb_fifo_buggy.dut.full)", "--rtl", str(buggy), "--do", "-o", str(out),
    ])
    assert r.exit_code == 0, r.output
    assert "add wave" in out.read_text()


def test_two_formats_at_once_is_refused(buggy):
    r = run([
        "why", str(buggy / "dump.vtx"),
        "why(tb_fifo_buggy.dut.full)", "--rtl", str(buggy), "--do", "--gtkw",
    ])
    assert r.exit_code != 0
    assert "pick one format" in r.output


def test_cone_narrows_the_design(buggy):
    r = run([
        "cone", str(buggy / "dump.vtx"), "tb_fifo_buggy.dut.full",
        "--rtl", str(buggy), "--depth", "2",
    ])
    assert r.exit_code == 0, r.output
    assert "fanin cone of tb_fifo_buggy.dut.full" in r.output
    assert "rd_ptr" in r.output


def test_cone_fanout_answers_the_other_question(buggy):
    r = run([
        "cone", str(buggy / "dump.vtx"), "tb_fifo_buggy.dut.rd_rst_n",
        "--rtl", str(buggy), "--direction", "fanout", "--depth", "2",
    ])
    assert r.exit_code == 0, r.output
    assert "rd_ptr" in r.output


def test_cone_rejects_an_unknown_signal(buggy):
    r = run(["cone", str(buggy / "dump.vtx"), "nope", "--rtl", str(buggy)])
    assert r.exit_code != 0
    assert "unknown signal" in r.output


def test_stuck_reports_the_frozen_signals(buggy):
    r = run(["stuck", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--cycles", "5"])
    assert r.exit_code == 0, r.output
    assert "frozen signal(s)" in r.output


def test_check_reports_every_group(buggy):
    r = run(["check", str(buggy / "dump.vtx"), "--rtl", str(buggy)])
    assert r.exit_code == 0, r.output
    assert "LINT" in r.output


def test_check_fail_on_is_the_ci_gate(buggy):
    """§13.9: a failing build carries the cause, not a dump nobody will open."""
    ok = run(["check", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--fail-on", "coverage"])
    assert ok.exit_code == 0
    gated = run(["check", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--fail-on", "lint"])
    assert gated.exit_code == 1


def test_probes_needs_only_the_graph(buggy):
    """§8.11b: useful before the bug, when the capture is being configured."""
    r = run([
        "probes", "tb_fifo_buggy.dut.full", "--rtl", str(buggy), "--depth", "2",
    ])
    assert r.exit_code == 0, r.output
    assert "create_debug_core" in r.output
    assert "tb_fifo_buggy/dut/rd_ptr" in r.output


def test_probes_quartus_format(buggy):
    r = run([
        "probes", "tb_fifo_buggy.dut.full", "--rtl", str(buggy),
        "--format", "quartus", "--depth", "2",
    ])
    assert r.exit_code == 0, r.output
    assert "::quartus::stp" in r.output
    assert "tb_fifo_buggy|dut|rd_ptr" in r.output


def test_triage_reaches_the_root_cause_from_the_log(buggy):
    """The v0.5 acceptance criterion, through the command a user actually runs."""
    r = run([
        "triage", str(buggy / "sim.log"),
        "--trace", str(buggy / "dump.vtx"), "--rtl", str(buggy),
    ])
    assert r.exit_code == 0, r.output
    assert "root cause(s)" in r.output
    assert "rd_rst_n" in r.output
    assert "fifo_buggy.sv" in r.output


def test_triage_can_write_a_save_file_for_the_root_causes(buggy):
    r = run([
        "triage", str(buggy / "sim.log"),
        "--trace", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--do",
    ])
    assert r.exit_code == 0, r.output
    assert "add wave" in r.output and "rd_rst_n" in r.output


def test_triage_needs_a_trace(buggy, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = run(["triage", str(buggy / "sim.log"), "--rtl", str(buggy)])
    assert r.exit_code != 0
    assert "trace.default" in r.output


def test_init_runs_the_checks_on_a_trace_it_finds(tmp_path, monkeypatch):
    """§13.4: the first impression must be "the tool already knows something"."""
    shutil.copytree(DESIGNS / "checks", tmp_path / "checks")
    monkeypatch.chdir(tmp_path / "checks")
    for stale in (tmp_path / "checks").glob("dump.vtx*"):
        shutil.rmtree(stale, ignore_errors=True)
        stale.unlink(missing_ok=True)

    # Accept every guess, which is what §13.4's flow is: three Enters.
    r = CliRunner().invoke(main, ["init"], input="y\ny\ny\n")
    assert r.exit_code == 0, r.output
    assert "Trace found: dump.vcd" in r.output
    assert "correlation:" in r.output
    # The findings are on screen before the server has even been started.
    assert "stuck" in r.output.lower()
    assert "veritrace serve" in r.output

    data = tomllib.loads((tmp_path / "checks" / ".veritrace.toml").read_text())
    assert data["trace"]["default"] == "dump.vcd"
    assert data["checks"]["stuck_cycles"] == 100


# --- transactions (§8.13-8.16) ---------------------------------------------


@pytest.fixture(scope="module")
def arb(tmp_path_factory):
    work = tmp_path_factory.mktemp("cli-arb")
    shutil.copytree(DESIGNS / "axi_arb", work / "axi_arb")
    design = work / "axi_arb"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    return design


def test_txn_lists_the_interfaces_and_states_the_rate(arb):
    r = run(["txn", str(arb / "dump.vtx"), "--rtl", str(arb)])
    assert r.exit_code == 0, r.output
    assert "3 interface(s)" in r.output and "40 transactions" in r.output
    for name in ("m0", "m1", "slv"):
        assert name in r.output
    # §8.14's acceptance criterion is a printed number, not an implication.
    assert "channel events correlated, 100%" in r.output
    assert "AXI4-Lite" in r.output


def test_txn_runs_a_query(arb):
    r = run(["txn", str(arb / "dump.vtx"), "txn(m0, type=WRITE) | slowest(3)", "--rtl", str(arb)])
    assert r.exit_code == 0, r.output
    assert "3 of 10 transaction(s)" in r.output
    assert "m0.WRITE[" in r.output


def test_txn_json_is_machine_readable(arb):
    import json

    r = run(["txn", str(arb / "dump.vtx"), "txn(m1)", "--rtl", str(arb), "--json"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n"] == 10
    assert body["transactions"][0]["kind"] == "WRITE"
    assert body["transactions"][0]["metrics"]["latency"] > 0


def test_txn_names_the_interfaces_when_the_query_names_none(arb):
    r = run(["txn", str(arb / "dump.vtx"), "txn(nope)", "--rtl", str(arb)])
    assert r.exit_code != 0
    assert "detected: m0, m1, slv" in r.output


def test_why_answers_at_transaction_level(arb):
    """Prompt 9's third acceptance criterion, through the CLI a user types."""
    r = run([
        "why", str(arb / "dump.vtx"),
        "why(txn.m0.WRITE[0].not_issued)", "--rtl", str(arb),
    ])
    assert r.exit_code == 0, r.output
    assert "WRITE#0 on m0" in r.output
    assert "[txn] m1.WRITE[" in r.output, "the chain never crossed the arbiter"
    assert "grant" in r.output


def test_txn_writes_a_viewer_save_file(arb, tmp_path):
    """§13.3: any command that selects signals can hand them to a viewer."""
    out = tmp_path / "iface.gtkw"
    r = run([
        "txn", str(arb / "dump.vtx"), "txn(m0)", "--rtl", str(arb),
        "--gtkw", "-o", str(out),
    ])
    assert r.exit_code == 0, r.output
    text = out.read_text(encoding="utf-8")
    assert "awvalid" in text and "Transactions" in text


def test_a_design_with_no_bus_says_so_instead_of_printing_nothing(buggy):
    r = run(["txn", str(buggy / "dump.vtx"), "--rtl", str(buggy)])
    assert r.exit_code == 0, r.output
    assert "no protocol interfaces detected" in r.output
    assert "write a pack" in r.output


def test_check_gates_on_protocol_violations(arb):
    """§13.9: `--fail-on protocol` makes a broken bus fail the build."""
    lite = DESIGNS / "axi_lite"
    r = run(["check", str(lite / "dump.vcd"), "--rtl", str(lite), "--fail-on", "protocol"])
    assert r.exit_code == 1, r.output
    assert "PROTOCOL" in r.output


# --- veritrace perf (§8.17, §8.18) -----------------------------------------


@pytest.fixture(scope="module")
def dl(tmp_path_factory):
    work = tmp_path_factory.mktemp("cli-deadlock")
    shutil.copytree(DESIGNS / "deadlock", work / "deadlock")
    design = work / "deadlock"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    convert(str(design / "dump_ok.vcd"), str(design / "dump_ok.vtx"))
    return design


def test_perf_prints_the_attribution_and_says_it_sums_to_one_hundred(arb):
    r = run(["perf", str(arb / "dump.vtx"), "--rtl", str(arb)])
    assert r.exit_code == 0, r.output
    assert "cycles sampled" in r.output
    # `designs/axi_arb/packs/axi4lite.vtp.toml` shadows the shipped pack and
    # adds a rung reading a wire none of the interfaces owns, so the blocked
    # cycles are attributed to the slave's wait states rather than to a generic
    # "the address was refused" — which is the point of the cascade living in
    # the pack (§8.17).
    assert "slave_wait_states" in r.output
    assert "other" not in r.output
    # §8.17's claim, printed where a reader can check it rather than trust it.
    assert "= total          100.0%" in r.output


def test_perf_reports_the_deadlock_the_way_section_8_18_writes_it(dl):
    r = run(["perf", str(dl / "dump.vtx"), "--rtl", str(dl)])
    assert r.exit_code == 0, r.output
    assert "DEADLOCK at" in r.output
    assert "waits on" in r.output and "held by" in r.output
    assert "`-- cycle" in r.output
    assert "Cycle of 2 agents" in r.output
    assert "[why] why(" in r.output


def test_perf_finds_nothing_on_the_same_design_without_the_bug(dl):
    r = run(["perf", str(dl / "dump_ok.vtx"), "--rtl", str(dl)])
    assert r.exit_code == 0, r.output
    assert "no deadlock, livelock or starvation found" in r.output


def test_perf_runs_a_query(dl):
    import json

    r = run(["perf", str(dl / "dump.vtx"), "deadlock()", "--rtl", str(dl)])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["kind"] == "deadlock" and len(body["deadlocks"]) == 1


def test_check_fails_the_build_on_a_deadlock(dl):
    """§13.9: the CI gate carries the cause in the log."""
    r = run(["check", str(dl / "dump.vtx"), "--rtl", str(dl), "--fail-on", "deadlock"])
    assert r.exit_code != 0
    assert "deadlock" in r.output.lower()


def test_perf_without_rtl_says_why_the_graph_is_incomplete(dl):
    r = run(["perf", str(dl / "dump.vtx")])
    assert r.exit_code == 0, r.output
    assert "--rtl" in r.output


# --- veritrace memory (§8.20) ----------------------------------------------


@pytest.fixture(scope="module")
def sdram(tmp_path_factory):
    work = tmp_path_factory.mktemp("cli-sdram")
    shutil.copytree(DESIGNS / "sdram", work / "sdram")
    design = work / "sdram"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    convert(str(design / "dump_ok.vcd"), str(design / "dump_ok.vtx"))
    return design


def test_memory_reports_every_injected_violation(sdram):
    r = run(["memory", str(sdram / "dump.vtx")])
    assert r.exit_code == 0, r.output
    assert "mt48lc16m16a2" in r.output and "4 banks" in r.output
    for constraint in ("tRCD", "tRP", "tRFC", "tFAW"):
        assert f"! {constraint} violated 1 time(s)" in r.output, constraint
    # §8.20's report names both commands and the required minimum.
    assert "ACTIVATE@c8 -> READ@c9" in r.output
    assert "min 2" in r.output


def test_memory_lists_the_constraints_that_held(sdram):
    """"conforme" is a stated fact in §8.20's report, not an absent line."""
    r = run(["memory", str(sdram / "dump.vtx")])
    assert "conformant" in r.output
    assert "tRAS" in r.output


def test_memory_finds_nothing_on_the_same_design_compiled_clean(sdram):
    r = run(["memory", str(sdram / "dump_ok.vtx")])
    assert r.exit_code == 0, r.output
    assert "violated" not in r.output
    assert "13 commands" in r.output


def test_memory_runs_a_query(sdram):
    import json

    r = run(["memory", str(sdram / "dump.vtx"), "banks(ctrl)"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["kind"] == "banks" and body["n_banks"] == 4


def test_memory_can_be_checked_against_another_chip(sdram):
    """`--chip` picks a different `packs/timing/` file (§8.20)."""
    r = run(["memory", str(sdram / "dump.vtx"), "--chip", "nope"])
    assert r.exit_code == 0, r.output
    assert "no timing file named" in r.output


def test_check_fails_the_build_on_a_timing_violation(sdram):
    """§13.9: the CI gate carries the cause in the log."""
    r = run(["check", str(sdram / "dump.vtx"), "--fail-on", "memory"])
    assert r.exit_code != 0
    assert "MEMORY" in r.output
