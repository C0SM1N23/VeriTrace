import json
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


def test_stuck_says_when_the_window_is_wider_than_the_run(buggy):
    """The default is 100 cycles and this run is 46 — a fact about the window.

    "nothing has been frozen" reads as a verdict on the design, which is the
    one thing §8.4's detector must not do by accident.
    """
    r = run(["stuck", str(buggy / "dump.vtx"), "--rtl", str(buggy)])
    assert r.exit_code == 0, r.output
    assert "the whole run is 46" in r.output
    assert "--cycles" in r.output


def test_stuck_json_carries_the_window_it_used(buggy):
    """P6. Without the threshold in the payload an empty list is unreadable."""
    r = run(["stuck", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["threshold_cycles"] == 100
    assert data["findings"] == []
    assert "the whole run is 46" in data["unreachable"]

    tight = json.loads(
        run(["stuck", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--cycles", "5", "--json"]).output
    )
    assert tight["unreachable"] is None
    assert tight["findings"] and tight["findings"][0]["check"] == "stuck"


def test_cone_json_lists_the_nodes_with_their_depth(buggy):
    r = run([
        "cone", str(buggy / "dump.vtx"), "tb_fifo_buggy.dut.full",
        "--rtl", str(buggy), "--depth", "2", "--json",
    ])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["signal"] == "tb_fifo_buggy.dut.full"
    assert any(n["path"].endswith("rd_ptr") for n in data["nodes"])
    assert all(n["depth"] <= 2 for n in data["nodes"])


# --- P6: "daca nu se poate scripta, nu e terminat" (§13) --------------------

#: Commands with no report to serialise, and why. Everything else must offer
#: `--json`, so a new reporting command cannot be added without one — which is
#: how six of them (cone, stuck, correlate, packs, plugins, saif) came to be
#: unscriptable without anyone noticing.
NOT_A_REPORT = {
    # Actions: they change something and say what they did.
    "init", "serve", "convert", "record", "restore", "share", "note",
    "reproduce", "import-capture",
    # Emit a file in a format that is already specified — JSON would be a
    # different artefact, not a machine-readable view of the same one.
    "export", "gen-sva", "probes", "wavedrom",
}


def test_every_reporting_command_can_be_scripted():
    """§13: *"Daca nu se poate scripta, nu e terminat."*"""
    import click

    from veritrace.cli import main as cli

    ctx = click.Context(cli)
    missing = [
        name
        for name in cli.list_commands(ctx)
        if name not in NOT_A_REPORT
        and not any(p.name == "as_json" for p in cli.get_command(ctx, name).params)
    ]
    assert not missing, f"no --json on: {', '.join(missing)}"
    # And the exemption list stays honest: a name that no longer exists in it
    # is a stale entry that would hide a real gap.
    assert NOT_A_REPORT <= set(cli.list_commands(ctx))


def _json_out(result):
    """The JSON a `--json` command printed, past anything on stderr.

    CliRunner folds stderr into `output`, and a command that has to convert a
    dump first announces it there — so a payload that parses on a machine with
    the store already built fails on a clean checkout. Taking the document from
    its first brace is what makes the assertion about the JSON rather than
    about what else happened to be on the terminal.
    """
    text = result.output
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    assert start >= 0, f"no JSON in output: {text!r}"
    return json.loads(text[start:])


def test_packs_and_plugins_are_machine_readable():
    packs = _json_out(run(["packs", "--root", str(DESIGNS / "axi_lite"), "--json"]))
    assert packs["errors"] == []
    assert "AXI4-Lite" in [p["name"] for p in packs["packs"]]

    matched = _json_out(
        run([
            "packs", "--root", str(DESIGNS / "axi_lite"),
            "--trace", str(DESIGNS / "axi_lite" / "dump.vcd"), "--json",
        ])
    )
    assert [m["pack"] for m in matched["matched"]] == ["AXI4-Lite"]

    plugins = _json_out(run(["plugins", "--root", str(DESIGNS / "fsm"), "--json"]))
    assert plugins["errors"] == {}
    assert "state_dwell" in [p["name"] for p in plugins["plugins"]]


def test_saif_json_carries_both_modes(buggy):
    data = json.loads(run(["saif", str(buggy / "dump.vtx"), "--json"]).output)
    assert data["duration"] > 0
    assert any(s["tc"] > 0 for s in data["signals"])
    # `held` is the only thing --gating adds on top of the toggle counts.
    assert all("held" in c for c in data["gating_candidates"])


# --- finding a signal by name (§7.2's rule, applied to what was typed) ------


def test_signals_finds_a_path_without_rtl(buggy):
    """The terminal's answer to "what is this signal actually called?".

    Every other command wants a hierarchical path; until this existed the CLI
    was the only surface with no way to discover one (§13's P6).
    """
    r = run(["signals", str(buggy / "dump.vtx"), "wr_ptr"])
    assert r.exit_code == 0, r.output
    assert "tb_fifo_buggy.dut.wr_ptr" in r.output


def test_signals_json_and_a_miss_that_says_so(buggy):
    data = json.loads(run(["signals", str(buggy / "dump.vtx"), "full", "--json"]).output)
    assert data["total"] == 28
    assert "tb_fifo_buggy.dut.full" in [s["path"] for s in data["signals"]]

    r = run(["signals", str(buggy / "dump.vtx"), "zzzz"])
    assert r.exit_code == 0
    assert "nothing in this trace matches" in r.output


def test_why_accepts_a_unique_suffix(buggy):
    """§7.2 matches RTL to trace on longest unique suffix; so does the prompt."""
    r = run(["why", str(buggy / "dump.vtx"), "why(dut.full @ c45)", "--rtl", str(buggy)])
    assert r.exit_code == 0, r.output
    assert "tb_fifo_buggy.dut.full" in r.output


def test_an_ambiguous_suffix_lists_the_candidates_rather_than_guessing(buggy):
    """Ambiguity means no answer, not a guess — the same rule §7.2 follows.

    `full` is both the testbench wire and the DUT output here. Picking one
    silently would answer a question that was not asked.
    """
    r = run(["why", str(buggy / "dump.vtx"), "why(full @ c45)", "--rtl", str(buggy)])
    assert r.exit_code != 0
    assert "ambiguous" in r.output
    assert "tb_fifo_buggy.dut.full" in r.output
    assert "tb_fifo_buggy.full" in r.output


def test_an_unknown_signal_suggests_the_names_that_do_exist(buggy):
    r = run(["why", str(buggy / "dump.vtx"), "why(rdptr @ c45)", "--rtl", str(buggy)])
    assert r.exit_code != 0
    assert "unknown signal" in r.output
    assert "did you mean" in r.output
    assert "rd_ptr" in r.output


# --- correlation as a gate (§7.2) -------------------------------------------


def test_correlate_json_and_fail_under(buggy):
    """§7.2 calls the rate first-class, which is only true if CI can read it.

    Grepping a percentage out of prose is one reworded line away from passing
    on a broken run.
    """
    data = json.loads(
        run(["correlate", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--json"]).output
    )
    assert data["percent"] >= 90
    assert data["matched"] <= data["total"]
    assert data["summary"].endswith(f"({data['percent']}%)")

    ok = run(["correlate", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--fail-under", "90"])
    assert ok.exit_code == 0, ok.output
    # The case the gate exists for: RTL that has nothing to do with the dump.
    gated = run([
        "correlate", str(buggy / "dump.vtx"), "--rtl", str(DESIGNS / "axi_lite"),
        "--fail-under", "90",
    ])
    assert gated.exit_code != 0
    assert "below the required 90%" in gated.output


def test_check_leads_with_the_correlation_it_produced_the_findings_under(buggy):
    """RTL that does not match the dump gives confident answers about another
    design, and pointing `--rtl` at the wrong directory is the commoner mistake
    (§7.2). Nothing else in `check`'s output mentioned correlation at all."""
    r = run(["check", str(buggy / "dump.vtx"), "--rtl", str(DESIGNS / "axi_lite")])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    # Positions, not line numbers: CliRunner folds stderr in, so a provenance
    # warning may or may not sit above depending on what ran before.
    rate = next(i for i, ln in enumerate(lines) if ln.startswith("RTL: 0/"))
    assert "(0.0%)" in lines[rate]
    assert "below" in r.output and "90%" in r.output
    assert rate < next(i for i, ln in enumerate(lines) if "finding(s)" in ln)

    data = json.loads(
        run(["check", str(buggy / "dump.vtx"), "--rtl", str(buggy), "--json"]).output
    )
    assert data["correlation"]["percent"] >= 90


def test_what_did_not_run_is_printed_above_the_verdict(buggy):
    """P7. "no automatic findings" over a list of checks that never happened
    reads as a clean bill of health for a scan that mostly did not."""
    r = run(["check", str(buggy / "dump.vtx")])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.endswith("check(s) did not run:"))
    verdict = next(
        i for i, ln in enumerate(lines) if ln == "no automatic findings from the checks that ran"
    )
    assert header < verdict
    assert "no RTL loaded" in r.output


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


# --- §4.3: the trace and the query are named once, and told apart ----------


def test_a_query_is_not_mistaken_for_a_trace(tmp_path):
    """`veritrace txn "txn(m0)"` and `veritrace txn dump.vtx` are both valid,
    and click cannot tell them apart once the trace is optional — a query is
    never a path that exists, which is what decides it."""
    from veritrace.cli import _trace_and_query

    assert _trace_and_query(Path("txn(m0)"), None) == (None, "txn(m0)")
    real = tmp_path / "dump.vtx"
    real.mkdir()
    assert _trace_and_query(real, None) == (real, None)
    assert _trace_and_query(real, "txn(m0)") == (real, "txn(m0)")
    assert _trace_and_query(None, None) == (None, None)


def test_why_without_a_question_says_what_to_type():
    from click.testing import CliRunner

    from veritrace.cli import main

    got = CliRunner().invoke(main, ["why"])
    assert got.exit_code != 0
    assert "why needs a question" in got.output


# --- §5.7 in the terminal ----------------------------------------------------


def test_analysis_commands_warn_when_the_rtl_moved_on(tmp_path):
    """§5.7's check, on the path a CI job and a script actually take.

    The session had it; `veritrace why` did not, so the one place a stale dump
    survives longest was also the one place nothing said so. A warning, never a
    refusal — the spec is explicit that the answer is still offered.
    """
    from veritrace.api.sessions import LayoutFile, sha256_files

    src = DESIGNS / "fifo_buggy"
    work = tmp_path / "proj"
    work.mkdir()
    for name in ("dump.vcd", "fifo_buggy.sv", "tb_fifo_buggy.sv"):
        shutil.copy(src / name, work / name)
    vtx = work / "dump.vcd.vtx"
    convert(str(work / "dump.vcd"), str(vtx))

    rtl = sorted(work.glob("*.sv"))
    from veritrace import TraceStore

    layout = LayoutFile.for_trace(vtx)
    data = layout.load()
    data["provenance"] = {
        "trace_sha256": TraceStore(str(vtx)).source_sha256,
        "rtl_sha256": sha256_files(rtl),
    }
    layout.save(data)

    runner = CliRunner()
    args = [str(vtx), "--rtl", str(work), "why(tb_fifo_buggy.dut.full @ 455000)"]
    clean = runner.invoke(main, ["why", *args])
    assert clean.exit_code == 0
    assert "has changed" not in clean.output

    (work / "fifo_buggy.sv").write_text(
        (work / "fifo_buggy.sv").read_text() + "\n// edited after the run\n"
    )
    stale = runner.invoke(main, ["why", *args])
    assert stale.exit_code == 0, "a stale dump is a warning, not a refusal (§5.7)"
    assert "the RTL has changed since this trace was made" in stale.output
    # And the answer is still there, which is the half §5.7 insists on keeping.
    assert "tb_fifo_buggy.dut.full" in stale.output


def test_a_question_about_a_value_that_never_occurred_says_so(tmp_path):
    vtx = tmp_path / "d.vtx"
    convert(str(DESIGNS / "fifo_buggy" / "dump.vcd"), str(vtx))
    runner = CliRunner()
    out = runner.invoke(
        main,
        [
            "why",
            str(vtx),
            "--rtl",
            str(DESIGNS / "fifo_buggy"),
            "why(tb_fifo_buggy.dut.full == 0 @ c45)",
        ],
    )
    assert out.exit_code == 0
    assert "the question assumed" in out.output


def test_the_rest_api_accepts_a_raw_dump(tmp_path):
    """§13: every entry point converts on the way in. The REST route was the
    one that did not, and it reported a real file as missing."""
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    raw = tmp_path / "dump.vcd"
    shutil.copy(DESIGNS / "fifo_buggy" / "dump.vcd", raw)
    with TestClient(create_app()) as c:
        r = c.post("/session", json={"trace_path": str(raw)})
        assert r.status_code == 200, r.text
        assert r.json()["n_signals"] > 0


# --- §4.3 config precedence -------------------------------------------------
#
# `config.load()` searches upward, so a `.veritrace.toml` in a repository root
# is found by a command run anywhere below it — including one pointed at a
# different design with `--rtl`. Taking `design.top` from that config elaborated
# the given sources under a top module they do not contain, which produced an
# empty graph and answers that were confidently wrong rather than refused.


def _stray_config(root, top="tb_somewhere_else"):
    (root / ".veritrace.toml").write_text(
        f'[design]\ntop = "{top}"\nrtl = ["other/*.sv"]\n', encoding="utf-8"
    )


def test_a_config_for_another_design_does_not_capture_an_explicit_rtl(tmp_path, monkeypatch):
    from veritrace.cli import _resolve_rtl

    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "other.sv").write_text("module other_mod; endmodule\n")
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "fifo_sync.sv").write_text(SAMPLE_FIFO)
    _stray_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    files, _incdirs, _defines, top = _resolve_rtl((mine,), None)
    assert [f.name for f in files] == ["fifo_sync.sv"]
    assert top is None, "an unrelated config supplied its top to an explicit --rtl"


def test_a_config_for_this_design_still_fills_in_what_it_knows(tmp_path, monkeypatch):
    """The fix must not cost a real project its own defaults."""
    from veritrace.cli import _resolve_rtl

    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "fifo_sync.sv").write_text(SAMPLE_FIFO)
    (tmp_path / ".veritrace.toml").write_text(
        '[design]\ntop = "fifo_sync"\nrtl = ["rtl/*.sv"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    _files, _incdirs, _defines, top = _resolve_rtl((tmp_path / "rtl",), None)
    assert top == "fifo_sync"


def test_a_top_that_is_not_in_the_sources_is_refused(tmp_path):
    """The second half: elaborating under an absent top gave an empty graph."""
    import pytest

    from veritrace.graph.elaborate import elaborate

    src = tmp_path / "fifo_sync.sv"
    src.write_text(SAMPLE_FIFO)
    with pytest.raises(ValueError, match="not in these sources"):
        elaborate([src], (), (), "tb_somewhere_else")
    # And the name it does contain is offered.
    with pytest.raises(ValueError, match="fifo_sync"):
        elaborate([src], (), (), "tb_somewhere_else")
