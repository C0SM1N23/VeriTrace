"""`veritrace run` — §13.4b, a folder of SystemVerilog to a verdict.

The acceptance criterion is the one §13.4 sets for the whole onboarding flow:
*nothing typed in between*. Given only `.sv` files, one command has to produce a
waveform, convert it, and report what is wrong — and it must report exactly what
`veritrace check` reports on the same waveform, or `run` would be a second,
quietly different answer to the same question.

Everything that needs Icarus is skipped when Icarus is not installed. The two
things that do not need it — how the top module is chosen, and what happens when
no simulator is there — are tested unconditionally, because those are the paths
a first-time user hits before anything else works.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from veritrace import simulate
from veritrace.cli import main

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

needs_icarus = pytest.mark.skipif(
    simulate.find_iverilog() is None, reason="Icarus Verilog is not installed"
)


@pytest.fixture
def dropped(tmp_path):
    """What someone actually does: copy some `.sv` files into a folder.

    No dump, no `.veritrace.toml`, no Makefile — the state every project is in
    the first time it meets this tool.
    """
    work = tmp_path / "mine"
    work.mkdir()
    for f in (DESIGNS / "fifo_buggy").glob("*.sv"):
        shutil.copy(f, work)
    return work


# --- the paths that need no simulator ---------------------------------------


def test_a_folder_with_no_sources_says_so(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    got = CliRunner().invoke(main, ["run", str(empty)])
    assert got.exit_code != 0
    assert "No .sv/.v files" in got.output


def test_another_simulator_prints_its_recipe_instead_of_guessing(dropped):
    """§4.0 holds those flags and the Makefile implements them; a second
    implementation here would be a second thing to keep in step."""
    got = CliRunner().invoke(main, ["run", str(dropped), "--sim", "verilator"])
    assert got.exit_code != 0
    assert "make sim-verilator" in got.output
    assert "tb_fifo_buggy" in got.output


def test_the_top_module_is_the_one_nobody_instantiates(dropped):
    from veritrace.cli import find_rtl_files, guess_top_module

    assert guess_top_module(find_rtl_files(dropped)) == "tb_fifo_buggy"


def test_the_generated_dumper_names_the_top_and_nothing_else(tmp_path):
    """It has to reach the whole design without touching the sources."""
    path = simulate.write_dumper(tmp_path, "tb_cpu", "dump.vcd")
    text = path.read_text(encoding="utf-8")
    assert "$dumpvars(0, tb_cpu);" in text
    assert '$dumpfile("dump.vcd");' in text
    assert f"module {simulate.DUMPER_MODULE};" in text


def test_a_testbench_that_already_dumps_is_left_alone(dropped):
    """The reference testbench calls `$dumpfile` itself, so the generated
    module must not be compiled in — two `$dumpfile` calls is a broken run."""
    sources = sorted(dropped.glob("*.sv"))
    assert any("$dumpfile" in f.read_text(encoding="utf-8") for f in sources)


# --- the whole flow ---------------------------------------------------------


@needs_icarus
def test_one_command_turns_a_folder_of_sv_into_findings(dropped):
    got = CliRunner().invoke(main, ["run", str(dropped)], catch_exceptions=False)
    assert got.exit_code == 0, got.output
    assert "top module 'tb_fifo_buggy'" in got.output
    assert "simulated with Icarus Verilog" in got.output
    # The correlation rate is the number that says whether any of it can be
    # trusted (§4.0), so it is on screen without being asked for.
    assert "correlation:" in got.output
    assert (dropped / ".veritrace" / "dump.vcd").is_file()


@needs_icarus
def test_it_reports_exactly_what_check_reports(dropped):
    """Two commands, one answer. `run` that disagreed with `check` would be a
    second opinion nobody asked for."""
    from veritrace import TraceStore, clocks
    from veritrace._native import convert
    from veritrace.analysis import checks as checks_mod
    from veritrace.config import Config
    from veritrace.correlate.resolver import correlate
    from veritrace.graph.elaborate import discover, elaborate

    CliRunner().invoke(main, ["run", str(dropped)], catch_exceptions=False)
    dump = dropped / ".veritrace" / "dump.vcd"

    out = dropped / "again.vtx"
    convert(str(dump), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(dropped))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, Config.empty())
    expected = checks_mod.run_all(store, el.graph, el, clock, Config.empty())

    got = CliRunner().invoke(
        main, ["run", str(dropped), "--json"], catch_exceptions=False
    )
    import json

    # stdout only: the conversion notice goes to stderr precisely so that
    # `--json` stays a document something else can read.
    assert sorted(json.loads(got.stdout)["counts"]) == sorted(expected.counts())


@needs_icarus
def test_it_writes_a_config_so_the_next_command_needs_no_arguments(dropped):
    """`init`'s promise, kept without asking for `init` (§13.4b)."""
    CliRunner().invoke(main, ["run", str(dropped)], catch_exceptions=False)
    conf = dropped / ".veritrace.toml"
    assert conf.is_file()
    text = conf.read_text(encoding="utf-8")
    assert "tb_fifo_buggy" in text


@needs_icarus
def test_an_existing_config_is_never_overwritten(dropped):
    """A project that configured itself decided things this cannot re-derive."""
    conf = dropped / ".veritrace.toml"
    conf.write_text("# mine\n", encoding="utf-8")
    CliRunner().invoke(main, ["run", str(dropped)], catch_exceptions=False)
    assert conf.read_text(encoding="utf-8") == "# mine\n"


@needs_icarus
def test_a_testbench_with_no_dumpfile_still_produces_a_waveform(tmp_path):
    """The case that makes "drop the files in a folder" true rather than nearly
    true: most testbenches have no `$dumpfile` until somebody needs one."""
    work = tmp_path / "quiet"
    work.mkdir()
    (work / "dut.sv").write_text(
        "module dut(input logic clk, output logic [3:0] q);\n"
        "  always_ff @(posedge clk) q <= q + 1'b1;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (work / "tb.sv").write_text(
        "module tb;\n"
        "  logic clk = 0;\n"
        "  logic [3:0] q;\n"
        "  dut u(.clk(clk), .q(q));\n"
        "  always #5 clk = ~clk;\n"
        "  initial begin repeat (40) @(posedge clk); $finish; end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    got = CliRunner().invoke(main, ["run", str(work)], catch_exceptions=False)
    assert got.exit_code == 0, got.output
    assert "no $dumpfile in your sources" in got.output
    assert (work / ".veritrace" / "dump.vcd").is_file()
    assert "signals)" in got.output


@needs_icarus
def test_a_compile_error_is_reported_as_one(tmp_path):
    """Not as a traceback, and not as "no waveform found"."""
    work = tmp_path / "broken"
    work.mkdir()
    (work / "bad.sv").write_text("module bad; this is not verilog endmodule\n", encoding="utf-8")
    got = CliRunner().invoke(main, ["run", str(work), "--top", "bad"])
    assert got.exit_code != 0
    assert "compilation failed" in got.output


@needs_icarus
def test_a_testbench_that_never_finishes_is_given_up_on(tmp_path):
    """A missing `$finish` is a mistake, not a reason to hang forever."""
    work = tmp_path / "forever"
    work.mkdir()
    (work / "tb.sv").write_text(
        "module tb;\n  logic clk = 0;\n  always #5 clk = ~clk;\nendmodule\n", encoding="utf-8"
    )
    got = CliRunner().invoke(main, ["run", str(work), "--timeout", "3"])
    assert got.exit_code != 0
    assert "did not finish" in got.output


@needs_icarus
def test_it_is_also_the_ci_gate(dropped):
    """§13.9 without a Makefile: the same `--fail-on` names, the same verdict."""
    got = CliRunner().invoke(main, ["run", str(dropped), "--fail-on", "lint"])
    assert got.exit_code == 1, got.output
    # And it stays quiet about what it did not find: this design has no
    # deadlock, so gating on one must not fail the build.
    clean = CliRunner().invoke(main, ["run", str(dropped), "--fail-on", "deadlock"])
    assert clean.exit_code == 0, clean.output
