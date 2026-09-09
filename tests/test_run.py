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
import tomllib
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
    assert "No .sv/.v/.f files" in got.output


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


def test_multiple_roots_are_not_silently_sorted_into_the_wrong_top(tmp_path):
    """Two valid roots are ambiguity, even though either one compiles cleanly."""
    (tmp_path / "a.sv").write_text("module a; initial $finish; endmodule\n", encoding="utf-8")
    (tmp_path / "b.sv").write_text("module b; initial $finish; endmodule\n", encoding="utf-8")
    got = CliRunner().invoke(main, ["run", str(tmp_path)])
    assert got.exit_code != 0
    assert "Multiple uninstantiated top modules" in got.output
    assert "a, b" in got.output and "--top" in got.output


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
def test_real_gated_clock_fsm_keeps_exact_intervals_through_the_api(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app

    project = tmp_path / "gated clock design"
    project.mkdir()
    (project / "tb.sv").write_text("""`timescale 1ns/1ps
module tb;
  reg slow = 0;
  reg fast = 0;
  always #2 fast = ~fast;
  initial begin
    #5 slow = 1; #5 slow = 0;
    #20 slow = 1; #5 slow = 0;
    #40 slow = 1; #5 slow = 0;
    #20 $finish;
  end
  toggle slow_fsm(slow);
  toggle fast_fsm(fast);
endmodule
module toggle(input clk);
  localparam IDLE = 0, BUSY = 1;
  reg state = IDLE;
  always @(posedge clk)
    case (state)
      IDLE: state <= BUSY;
      BUSY: state <= IDLE;
    endcase
endmodule
""")
    result = CliRunner().invoke(main, ["run", str(project), "--top", "tb", "--json"])
    assert result.exit_code == 0, result.output
    dump = Path(json.loads(result.stdout)["dump"])
    assert dump.is_file()
    with TestClient(create_app(default_trace=dump, rtl=[str(project)], top="tb")) as c:
        sid = c.get("/").json()["default_session"]
        endpoint = f"/session/{sid}/fsm/tb.slow_fsm.state"
        result = c.get(endpoint)
        assert result.status_code == 200, result.text
        machine = result.json()
        assert machine["clock_path"] == "tb.slow_fsm.clk"
        assert machine["intervals"] == [[0, 5000, 0], [5000, 30000, 1],
                                        [30000, 75000, 0], [75000, 100000, 1]]
        assert machine["cycles_in"] == {"0": 2, "1": 1}
        assert machine["visits"] == {"0": 2, "1": 2}
        assert c.get(endpoint).json() == machine
        # Cursor values are read by the exact same route Source/Wave use.
        signals = c.get(f"/session/{sid}/signals").json()["signals"]
        handle = next(s["handle"] for s in signals if s["path"] == machine["signal"])
        for start, end, state in machine["intervals"]:
            values = c.post(f"/session/{sid}/values",
                            json={"time": (start + end) // 2, "handles": [handle]}).json()
            assert int(values["values"][0]["value"], 2) == state


@needs_icarus
def test_one_command_turns_a_folder_of_sv_into_findings(dropped):
    got = CliRunner().invoke(main, ["run", str(dropped)], catch_exceptions=False)
    # The reference testbench deliberately prints assertion failures.  The
    # waveform must still be analysed, but the command must preserve that
    # failed-test verdict instead of turning useful diagnostics into success.
    assert got.exit_code == 1, got.output
    assert "reported a testbench failure" in got.output
    assert "top module 'tb_fifo_buggy'" in got.output
    assert "simulated with Icarus Verilog" in got.output
    # The correlation rate is the number that says whether any of it can be
    # trusted (§4.0), so it is on screen without being asked for.
    assert "correlation:" in got.output
    # This testbench calls `$dumpfile("dump.vcd")` itself, so the waveform lands
    # where *it* says — next to the sources, exactly as running the simulation
    # by hand would. The build products still go to the work directory.
    assert (dropped / "dump.vcd").is_file()
    assert (dropped / ".veritrace" / "sim.vvp").is_file()


@needs_icarus
@pytest.mark.parametrize("activates,expected", [(4, 0), (5, 1)])
def test_real_command_bus_tfaw_only_flags_the_fifth_activate(tmp_path, activates, expected):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app

    project = tmp_path / "memory commands"
    project.mkdir()
    (project / "tb.sv").write_text(f"""`timescale 1ns/1ps
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg cs_n = 1, ras_n = 1, cas_n = 1, we_n = 1;
  reg [1:0] ba = 0;
  reg [12:0] a = 0;
  integer i;
  initial begin
    for (i = 0; i < {activates}; i = i + 1) begin
      @(negedge clk);
      cs_n = 0; ras_n = 0; cas_n = 1; we_n = 1;
      ba = i % 4; a = i;
    end
    @(negedge clk); cs_n = 1;
    repeat (4) @(posedge clk);
    $finish;
  end
endmodule
""")
    result = CliRunner().invoke(main, ["run", str(project), "--top", "tb", "--json"])
    assert result.exit_code == 0, result.output
    dump = Path(json.loads(result.stdout)["dump"])
    with TestClient(create_app(default_trace=dump, rtl=[str(project)], top="tb")) as c:
        sid = c.get("/").json()["default_session"]
        report = c.get(f"/session/{sid}/memory").json()["interfaces"][0]
        assert report["n_commands"] == activates
        hits = [v for v in report["violations"] if v["constraint"] == "tFAW"]
        assert len(hits) == expected
        if hits:
            assert hits[0]["first"]["time"] == 15000
            assert hits[0]["second"]["time"] == 55000
        assert "tREFI" not in report["checked"]
        assert "no qualifying" in report["skipped"]["tREFI"]


@needs_icarus
def test_refresh_only_bus_still_checks_device_timing_and_exposes_real_intervals(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app

    source = tmp_path / "tb.sv"
    source.write_text("""`timescale 1ns/1ps
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg cs_n = 1, ras_n = 0, cas_n = 0, we_n = 1;
  reg [1:0] ba = 0;
  reg [12:0] a = 0;
  initial begin
    @(negedge clk); cs_n = 0;
    @(negedge clk); cs_n = 1;
    repeat (1600) @(negedge clk);
    cs_n = 0;
    @(negedge clk); cs_n = 1;
    repeat (5) @(posedge clk);
    $finish;
  end
endmodule
""")
    result = CliRunner().invoke(main, ["run", str(tmp_path), "--top", "tb", "--json"])
    assert result.exit_code == 0, result.output
    dump = Path(json.loads(result.stdout)["dump"])
    with TestClient(create_app(default_trace=dump, rtl=[str(source)], top="tb")) as c:
        sid = c.get("/").json()["default_session"]
        report = c.get(f"/session/{sid}/memory").json()["interfaces"][0]
        assert report["n_commands"] == 2 and report["n_banks"] == 0
        assert report["refresh_intervals"] == [{"t0": 15000, "t1": 16025000, "elapsed": 16010000}]
        # This SDR part has 4096 refresh rows per 64 ms, not the DDR 8192-row limit.
        assert report["refresh_limit"] == 15625000
        assert report["checked"]["tREFI"] == 1
        assert report["violations"][0]["constraint"] == "tREFI"


@needs_icarus
def test_memory_commands_keep_their_own_gated_clock_through_cli_and_api(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.memory import timing

    source = tmp_path / "tb.sv"
    source.write_text("""`timescale 1ns/1ps
module dram;
  reg clk = 0;
  initial begin
    #5 clk=1; #5 clk=0; #90 clk=1; #5 clk=0;
    #5 clk=1; #5 clk=0; #5 clk=1;
  end
  reg cs_n=0, ras_n=0, cas_n=1, we_n=1;
  reg [1:0] ba=0;
  reg [12:0] a=0;
  initial begin
    #10 ras_n=1; cas_n=0;
    #95 ras_n=0; cas_n=1; we_n=0;
    #10 cs_n=1;
  end
endmodule
module tb;
  reg clk=0;
  always #1 clk=~clk;
  dram mem();
  initial #130 $finish;
endmodule
""")
    (tmp_path / "timing").mkdir()
    alternate = timing.find("mt48lc16m16a2").path.read_text().replace("tRTP  = 15", "tRTP  = 16")
    (tmp_path / "timing" / "alternate.toml").write_text(alternate)
    result = CliRunner().invoke(main, ["run", str(tmp_path), "--top", "tb", "--json"])
    assert result.exit_code == 0, result.output
    dump = Path(json.loads(result.stdout)["dump"])
    checked = CliRunner().invoke(main, ["memory", str(dump)])
    assert "READ@c1 -> PRECHARGE@c2" in checked.output
    with TestClient(create_app(default_trace=dump)) as c:
        sid = c.get("/").json()["default_session"]
        base = f"/session/{sid}"
        report = c.get(base + "/memory").json()["interfaces"][0]
        assert report["clock_path"] == "tb.mem.clk"
        cmds = c.post(base + "/query", json={"vtq": f"cmds({report['iface']})"}).json()["commands"]
        assert [(x["time"], x["cycle"]) for x in cmds] == [(5000, 0), (100000, 1), (110000, 2)]
        v = report["violations"][0]
        assert v["constraint"] == "tRTP" and v["measured_cycles"] == 1
        assert (v["measured_ticks"], v["limit_ticks"]) == (10000, 15000)
        assert v["limit_cycles"] is None  # A gated clock cannot convert a wall-time limit.
        other = c.post(base + "/query", json={"vtq": f"timing({report['iface']}, chip=alternate)"})
        assert other.status_code == 200, other.text
        v = other.json()["violations"][0]
        assert v["measured_cycles"] == 1 and v["limit_cycles"] is None
        assert v["limit_ticks"] == 16000


@needs_icarus
@pytest.mark.parametrize("reset_value", ["0", "1'bx"])
def test_asserted_or_unknown_reset_never_fabricates_commands_or_transactions(tmp_path, reset_value):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app

    (tmp_path / "tb.sv").write_text(f"""`timescale 1ns/1ps
module tb;
  reg clk=0;
  always #5 clk=~clk;
  reg rst_n={reset_value};
  reg cs_n=0, ras_n=0, cas_n=1, we_n=1;
  reg [1:0] ba=0;
  reg [12:0] a=0;
  reg stream_valid=1, stream_ready=1;
  reg [7:0] stream_data=42;
  initial #100 $finish;
endmodule
""")
    result = CliRunner().invoke(main, ["run", str(tmp_path), "--top", "tb", "--json"])
    assert result.exit_code == 0, result.output
    dump = Path(json.loads(result.stdout)["dump"])
    for _ in range(2):  # Fresh API process and cached protocol extraction must agree.
        with TestClient(create_app(default_trace=dump)) as c:
            sid = c.get("/").json()["default_session"]
            base = f"/session/{sid}"
            memory = c.get(base + "/memory").json()["interfaces"][0]
            assert memory["n_commands"] == 0 and memory["checked"] == {}
            assert "no commands sampled" in memory["skipped"]["command reset"]
            interfaces = c.get(base + "/transactions").json()["interfaces"]
            assert len(interfaces) == 1
            stream = interfaces[0]
            assert stream["interface"]["prefix"] == "stream_"
            assert "no transactions sampled" in stream["skipped"]["reset"]
            txns = c.post(base + "/query", json={"vtq": f"txn({stream['interface']['name']})"}).json()
            assert txns["transactions"] == []


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

    first = CliRunner().invoke(
        main, ["run", str(dropped), "--json"], catch_exceptions=False
    )
    import json

    dump = Path(json.loads(first.stdout)["dump"])
    assert dump.is_file()

    out = dropped / "again.vtx"
    convert(str(dump), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(dropped))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, Config.empty())
    expected = checks_mod.run_all(store, el.graph, el, clock, Config.empty())

    # stdout only: the conversion notice goes to stderr precisely so that
    # `--json` stays a document something else can read.
    assert sorted(json.loads(first.stdout)["counts"]) == sorted(expected.counts())


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
def test_a_commented_dumpvars_does_not_disable_the_real_dumper(tmp_path):
    source = tmp_path / "tb.sv"
    source.write_text(
        "module tb;\n"
        "  // $dumpvars(0, tb); this old code is not executable\n"
        "  initial begin #1; $finish; end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    result = simulate.icarus([source], "tb", tmp_path / "work", run_dir=tmp_path)
    assert result.injected
    assert result.dump.is_file() and result.dump.stat().st_size > 0


@needs_icarus
def test_a_stale_dump_cannot_turn_a_no_output_run_into_success(tmp_path):
    stale = tmp_path / "old.vcd"
    stale.write_text("this is from yesterday\n", encoding="utf-8")
    source = tmp_path / "tb.sv"
    source.write_text(
        "module tb;\n"
        "  initial begin\n"
        '    if (1\'b0) begin $dumpfile("old.vcd"); $dumpvars(0, tb); end\n'
        "    #1; $finish;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    with pytest.raises(simulate.SimulationError, match="no new waveform|older run"):
        simulate.icarus([source], "tb", tmp_path / "work", run_dir=tmp_path)
    assert stale.read_text(encoding="utf-8") == "this is from yesterday\n"


@needs_icarus
def test_dumpfile_without_dumpvars_is_completed_and_lands_in_work(tmp_path):
    source = tmp_path / "tb.sv"
    source.write_text(
        "module tb;\n"
        '  initial begin $dumpfile("mine.vcd"); #1; $finish; end\n'
        "endmodule\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    result = simulate.icarus([source], "tb", work, run_dir=tmp_path)
    assert result.injected, "$dumpfile alone selects no signals"
    assert result.dump.parent == work.resolve()
    assert result.dump.is_file() and result.dump.stat().st_size > 0


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
def test_a_zero_status_system_error_is_still_a_failed_simulation(tmp_path):
    """Icarus prints ``ERROR:`` for `$error` but exits zero.

    A waveform and a zero process status must not overrule the testbench's own
    explicit failure.
    """
    source = tmp_path / "tb.sv"
    source.write_text(
        "module tb;\n"
        "  initial begin #1; $error(\"injected failure\"); $finish; end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    got = CliRunner().invoke(main, ["run", str(tmp_path)])
    assert got.exit_code != 0
    assert "reported a testbench failure" in got.output
    assert "injected failure" in got.output


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
    # A non-matching analysis gate cannot erase an underlying failed testbench.
    # This design has no deadlock, but its own assertion still fired.
    failed_test = CliRunner().invoke(
        main, ["run", str(dropped), "--fail-on", "deadlock"]
    )
    assert failed_test.exit_code == 1, failed_test.output
    assert "reported a testbench failure" in failed_test.output


# --- a real project's shape: filelists, and running where it runs -----------


def test_a_filelist_is_read_for_the_graph_and_passed_to_the_compiler(tmp_path):
    """`.f` files are how a real project already describes its build. The
    compiler gets `-f`; this reader exists only so pyslang, which has no notion
    of a command file, elaborates the same sources."""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.v").write_text("module a; endmodule\n", encoding="utf-8")
    (tmp_path / "inc").mkdir()
    fl = tmp_path / "build.f"
    fl.write_text(
        "// a comment\n+incdir+inc\n+define+WIDTH=8\nrtl/a.v\n", encoding="utf-8"
    )
    files, incdirs, defines = simulate.read_filelist(fl)
    assert files == [(tmp_path / "rtl" / "a.v").resolve()]
    assert incdirs == [(tmp_path / "inc").resolve()]
    assert defines == ["WIDTH=8"]


@needs_icarus
def test_explicit_sources_and_filelists_keep_the_callers_order(tmp_path):
    """Compilation-unit order and paths containing spaces both reach Icarus."""
    project = tmp_path / "project with spaces"
    project.mkdir()
    first = project / "first.sv"
    middle = project / "middle.sv"
    filelist = project / "middle.f"
    last = project / "tb.sv"
    first.write_text("`define ORDERED\n", encoding="utf-8")
    middle.write_text("`undef ORDERED\n", encoding="utf-8")
    filelist.write_text("middle.sv\n", encoding="utf-8")
    last.write_text(
        "`ifdef ORDERED\nthis_is_a_compile_error\n`endif\n"
        "module tb; initial begin #1; $finish; end endmodule\n",
        encoding="utf-8",
    )

    got = CliRunner().invoke(
        main, ["run", str(first), str(filelist), str(last), "--top", "tb"]
    )
    assert got.exit_code == 0, got.output


@needs_icarus
def test_relative_include_directory_uses_the_simulators_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "inc").mkdir(parents=True)
    (project / "inc" / "value.vh").write_text("`define VALUE 1\n", encoding="utf-8")
    source = project / "tb.sv"
    source.write_text(
        '`include "value.vh"\n'
        "module tb; initial begin #1; $finish; end endmodule\n",
        encoding="utf-8",
    )
    caller = tmp_path / "unrelated"
    caller.mkdir()
    monkeypatch.chdir(caller)

    result = simulate.icarus(
        [source], "tb", project / "work", incdirs=["inc"], run_dir=project
    )
    assert result.dump.is_file()


def test_a_nested_filelist_is_followed_once(tmp_path):
    inner = tmp_path / "inner.f"
    outer = tmp_path / "outer.f"
    (tmp_path / "a.v").write_text("module a; endmodule\n", encoding="utf-8")
    inner.write_text("a.v\n-f outer.f\n", encoding="utf-8")
    outer.write_text("-f inner.f\n", encoding="utf-8")
    files, _i, _d = simulate.read_filelist(outer)
    # Once, and without recursing forever on the cycle.
    assert files == [(tmp_path / "a.v").resolve()]


@needs_icarus
def test_the_simulation_runs_where_the_project_runs_it(tmp_path):
    """The case every real testbench depends on: `$readmemh("program.hex")` is
    relative to the directory the flow runs from, not to wherever the build
    products are put. Getting this wrong loads no program and silently
    simulates a design that does nothing."""
    work = tmp_path / "proj"
    work.mkdir()
    (work / "program.hex").write_text("0000002a\n", encoding="utf-8")
    (work / "tb.sv").write_text(
        "module tb;\n"
        "  logic clk = 0;\n"
        "  logic [31:0] mem [0:0];\n"
        "  always #5 clk = ~clk;\n"
        "  initial begin\n"
        '    $readmemh("program.hex", mem);\n'
        "    repeat (10) @(posedge clk);\n"
        '    $display("loaded %0d", mem[0]);\n'
        "    $finish;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    got = CliRunner().invoke(main, ["run", str(work)], catch_exceptions=False)
    assert got.exit_code == 0, got.output
    assert "loaded 42" in (work / ".veritrace" / "sim.log").read_text(encoding="utf-8")


@needs_icarus
def test_a_missing_include_names_the_flag_that_fixes_it(tmp_path):
    """Icarus says `Include file x.vh not found`; on its own that is a wall of
    output. The one thing to do next is worth saying."""
    work = tmp_path / "inc"
    work.mkdir()
    (work / "tb.sv").write_text(
        '`include "elsewhere.vh"\nmodule tb; initial $finish; endmodule\n', encoding="utf-8"
    )
    got = CliRunner().invoke(main, ["run", str(work)])
    assert got.exit_code != 0
    assert "elsewhere.vh" in got.output
    assert "--incdir" in got.output


@needs_icarus
def test_run_passes_configured_sources_includes_defines_and_top_to_icarus(tmp_path, monkeypatch):
    """The graph and the actual compiler must see the same §4.3 build."""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "width.vh").write_text("`define WIDTH 4\n", encoding="utf-8")
    (tmp_path / "rtl" / "tb.sv").write_text(
        '`include "width.vh"\n'
        "module configured_tb;\n"
        "`ifndef ENABLED\n"
        "  this_would_not_compile missing_define;\n"
        "`endif\n"
        "  logic [`WIDTH-1:0] value;\n"
        "  initial begin value = 'hA; #1; $finish; end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / ".veritrace.toml").write_text(
        '[design]\n'
        'top = "configured_tb"\n'
        'rtl = ["rtl/*.sv"]\n'
        'incdirs = ["include"]\n'
        '[design.defines]\n'
        'ENABLED = 1\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    got = CliRunner().invoke(main, ["run"], catch_exceptions=False)
    assert got.exit_code == 0, got.output
    command = (tmp_path / ".veritrace" / "sim.command").read_text(encoding="utf-8")
    assert "ENABLED=1" in command and str(tmp_path / "include") in command


@needs_icarus
def test_json_run_preserves_exact_build_for_cli_rerun_and_api_elaboration(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app

    project = tmp_path / "project with spaces"
    project.mkdir()
    include = project / "headers"
    include.mkdir()
    (include / "width.vh").write_text("`define WIDTH 8\n")
    definitions = project / "first.sv"
    definitions.write_text("`define INITIAL 42\n")
    source = project / "tb.sv"
    source.write_text('''`include "width.vh"
module tb;
  reg clk=0;
  always #5 clk=~clk;
  reg [`WIDTH-1:0] data=`INITIAL;
  `ifndef ENABLED
    missing_module would_fail();
  `endif
  initial #50 $finish;
endmodule
''')
    (project / "unselected.sv").write_text("this is deliberately invalid HDL")
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    first = CliRunner().invoke(main, ["run", str(definitions), str(source),
        "--incdir", str(include), "-D", "ENABLED=1", "--top", "tb", "--json"])
    assert first.exit_code == 0, first.output
    dump = Path(json.loads(first.stdout)["dump"])
    assert "could not elaborate" not in first.stderr
    conf = tomllib.loads((project / ".veritrace.toml").read_text())
    assert conf["design"]["rtl"] == ["first.sv", "tb.sv"]
    assert conf["design"]["incdirs"] == ["headers"]
    assert conf["design"]["defines"] == {"ENABLED": "1"}
    assert not (caller / ".veritrace.toml").exists()
    # No explicit RTL: a new process must consume the build it just persisted.
    with TestClient(create_app(default_trace=dump)) as c:
        sid = c.get("/").json()["default_session"]
        status = c.get(f"/session/{sid}/status").json()
        assert status["rtl_error"] == "", status
        assert status["has_rtl"] is True
    monkeypatch.chdir(project)
    second = CliRunner().invoke(main, ["run", "--json"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["top"] == "tb"
    command = (project / ".veritrace" / "sim.command").read_text()
    assert "unselected.sv" not in command and "ENABLED=1" in command


@needs_icarus
def test_run_overrides_survive_fresh_cli_and_api_analysis_without_rewriting_config(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.cli import _load

    (tmp_path / "headers").mkdir()
    (tmp_path / "headers/width.vh").write_text("`define WIDTH 8\n")
    source = tmp_path / "tb.sv"
    source.write_text('''`timescale 1ns/1ps
`ifdef OVERRIDE
  `include "width.vh"
`else
  `define WIDTH 4
`endif
module tb;
  reg clk=0;
  always #5 clk=~clk;
  reg [`WIDTH-1:0] data=`INITIAL;
  initial #25 $finish;
endmodule
''')
    config = tmp_path / ".veritrace.toml"
    config.write_text('[design]\ntop="tb"\nrtl=["tb.sv"]\n[design.defines]\nINITIAL=1\n')
    original = config.read_bytes()
    monkeypatch.chdir(tmp_path)
    run = CliRunner().invoke(main, ["run", "--incdir", "headers", "-D", "OVERRIDE", "-D", "INITIAL=42", "--json"])
    assert run.exit_code == 0, run.output
    dump = Path(json.loads(run.stdout)["dump"])
    assert config.read_bytes() == original
    # A real second command must not elaborate the old 4-bit/default branch.
    ctx = _load(dump, need_rtl=True)
    assert ctx.graph.get("tb.data").width == 8
    assert "OVERRIDE" in ctx.config.defines and ctx.config.defines[-1] == "INITIAL=42"
    why = CliRunner().invoke(main, ["why", str(dump), "why(tb.data @ 8000)", "--json"])
    assert why.exit_code == 0, why.output
    assert json.loads(why.stdout)["root"]["value"] == "00101010"
    with TestClient(create_app(default_trace=dump)) as client:
        sid = client.get("/").json()["default_session"]
        session = client.app.state.registry.get(sid)
        assert session.graph.get("tb.data").width == 8
        got = client.post(f"/session/{sid}/query", json={"vtq": "why(tb.data @ 8000)"})
        assert got.status_code == 200, got.text
        assert got.json()["root"]["value"] == "00101010"
    moved = tmp_path.parent / (tmp_path.name + " moved")
    shutil.copytree(tmp_path, moved)
    monkeypatch.chdir(moved)
    moved_dump = moved / dump.relative_to(tmp_path)
    relocated = CliRunner().invoke(main, ["why", str(moved_dump), "why(tb.data @ 8000)", "--json"])
    assert relocated.exit_code == 0, relocated.output
    assert json.loads(relocated.stdout)["root"]["value"] == "00101010"
    monkeypatch.chdir(tmp_path)
    # A later default run uses the user's original config, not the old override.
    fresh = CliRunner().invoke(main, ["run", "--json"])
    assert fresh.exit_code == 0, fresh.output
    latest = _load(Path(json.loads(fresh.stdout)["dump"]), need_rtl=True)
    assert latest.graph.get("tb.data").width == 4
    assert "OVERRIDE" not in latest.config.defines
    assert config.read_bytes() == original


@needs_icarus
def test_reopening_a_new_build_uses_its_sources_and_top_not_the_previous_run(tmp_path, monkeypatch):
    import json
    import time
    from fastapi.testclient import TestClient
    from veritrace.api import create_app
    from veritrace.api.sessions import SessionRegistry

    first, second = tmp_path / "first.sv", tmp_path / "second.sv"
    first.write_text("module first; reg data=0; initial #25 $finish; endmodule\n")
    second.write_text("module second; reg [7:0] data=42; initial #25 $finish; endmodule\n")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    run = runner.invoke(main, ["run", str(first), "--top", "first", "--json"])
    assert run.exit_code == 0, run.output
    raw = Path(json.loads(run.stdout)["dump"])
    sync = SessionRegistry()
    sync.open(raw)
    with TestClient(create_app(default_trace=raw)) as client:
        sid = client.get("/").json()["default_session"]
        rerun = runner.invoke(main, ["run", str(second), "--top", "second", "--json"])
        assert rerun.exit_code == 0, rerun.output
        assert Path(json.loads(rerun.stdout)["dump"]) == raw
        reopened = client.post("/session", json={"trace_path": str(raw)})
        assert reopened.json()["session_id"] == sid
        deadline = time.monotonic() + 15
        while (status := client.get(f"/session/{sid}/status").json())["phase"] != "ready":
            assert status["phase"] != "error", status
            assert time.monotonic() < deadline, status
            time.sleep(0.01)
        assert status["top"] == "second" and status["has_rtl"], status
        assert client.app.state.registry.get(sid).graph.get("second.data").width == 8
    reopened = sync.open(raw)
    assert reopened.top == "second" and reopened.graph.get("second.data").width == 8


@needs_icarus
def test_the_config_lands_where_the_next_command_will_look_for_it(tmp_path, monkeypatch):
    """`veritrace run rtl/` — the shape of every real project.

    `config.find` searches upward, so a file written beside the sources is
    invisible from the directory the command was run in. That made the very
    promise printed on the line above it false: the next command answered "No
    trace given", and so did the one after that.
    """
    project = tmp_path / "project"
    (project / "rtl").mkdir(parents=True)
    for f in (DESIGNS / "fifo_buggy").glob("*.sv"):
        shutil.copy(f, project / "rtl")

    monkeypatch.chdir(project)
    CliRunner().invoke(main, ["run", "rtl"], catch_exceptions=False)

    conf = project / ".veritrace.toml"
    assert conf.is_file(), "the config went somewhere the next command cannot see"
    assert not (project / "rtl" / ".veritrace.toml").exists()

    body = tomllib.loads(conf.read_text(encoding="utf-8"))
    # Every path is resolved against the config's own directory, so that is what
    # they have to be written relative to. `rtl/rtl/dump.vcd` was the bug.
    assert (project / body["trace"]["default"]).is_file(), body["trace"]["default"]
    for pattern in body["design"]["rtl"]:
        assert list(project.glob(pattern)) or pattern.endswith(".v"), pattern

    # And the promise itself: no arguments, from the project root.
    out = CliRunner().invoke(main, ["check"], catch_exceptions=False)
    assert out.exit_code == 0, out.output
    assert "No trace given" not in out.output


@needs_icarus
def test_a_folder_run_from_elsewhere_configures_itself(tmp_path, monkeypatch):
    """The other shape: `veritrace run /somewhere/else`. There is no project
    around the caller, so the sources' own directory is the only sensible home
    for the config — and it must not be written into whatever directory the
    user happened to be standing in."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for f in (DESIGNS / "fifo_buggy").glob("*.sv"):
        shutil.copy(f, elsewhere)
    caller = tmp_path / "caller"
    caller.mkdir()

    monkeypatch.chdir(caller)
    CliRunner().invoke(main, ["run", str(elsewhere)], catch_exceptions=False)

    assert (elsewhere / ".veritrace.toml").is_file()
    assert not (caller / ".veritrace.toml").exists(), "configured the wrong directory"
