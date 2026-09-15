"""Packs, gen-sva and the plugin API — §8.15, §8.22, §13.7.

Prompt 16's acceptance criterion is §16.2's: *write a new plugin from the
generated documentation alone, and it should take under 45 minutes.* A test
cannot hold a stopwatch, so it holds the thing the stopwatch was measuring —
`test_a_plugin_written_from_the_documentation_alone_works` is a plugin written
against `docs/PLUGINS.md` and nothing else, using only names that page mentions,
and it asserts that each of those names exists and does what the page says. If
the API drifts from the documentation, that test fails rather than the
documentation quietly becoming wrong.

The gen-sva tests do the equivalent for §8.22: the portable checker is
**compiled and run**, because "it generated a file" proves nothing about a
feature whose entire purpose is that the file works in a simulator with no SVA
support.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import design_store, make_vtx
from click.testing import CliRunner

from veritrace import simulate
from veritrace.cli import main
from veritrace.export import sva
from veritrace.protocol import pack as pack_mod
from veritrace.tools import run_capture

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / "designs"
PACKS = ROOT / "python" / "veritrace" / "protocol" / "packs"

needs_icarus = pytest.mark.skipif(
    simulate.find_iverilog() is None, reason="Icarus Verilog is not installed"
)

#: Every one of these testbenches ends in `$finish` within a few thousand
#: simulated cycles. A run that reaches this has stopped being a test of the
#: generated checker, and letting it continue costs a CI runner rather than a
#: test — so it fails here, with whatever the simulator had printed.
SIM_TIMEOUT_S = 180.0


# --- §8.15, the packs -------------------------------------------------------

#: Every row of §8.15's table, by the slug the file is named with.
SHIPPED = {
    "axi4", "axi4lite", "axistream", "ahb", "apb", "avalon", "avalonst",
    "wishbone", "sdram", "ddr3", "spi", "i2c", "uart", "handshake",
    "riscv-retire",
}


def test_every_pack_in_the_table_is_shipped():
    """§8.15 is a list of what the repo carries, not a wish list."""
    found = {p.name.removesuffix(".vtp.toml") for p in PACKS.glob("*.vtp.toml")}
    assert SHIPPED <= found, f"missing: {sorted(SHIPPED - found)}"


@pytest.mark.parametrize("path", sorted(PACKS.glob("*.vtp.toml")), ids=lambda p: p.stem)
def test_a_pack_loads_and_its_rules_parse(path):
    """A pack with a typo in a rule is a pack that fails at the worst moment —
    mid-extraction on somebody's design — so every one is parsed here."""
    p = pack_mod.load(path)
    assert p.name and p.version
    assert p.detect.required_suffixes
    for rule in p.rules:
        assert rule.node is not None, rule.id
        assert rule.msg, f"{p.name}/{rule.id} has no message"
    assert p.channels or p.commands, "a pack must describe either channels or commands"


def test_a_handshake_may_be_an_expression():
    """§16.2: "motorul e ce conteaza, nu numarul de packs".

    AHB, Wishbone and the serial packs have no valid/ready pair; their handshake
    is a small expression over the wires they do have. Without that in the
    engine, the table in §8.15 could only ever be filled with protocols shaped
    like AXI.
    """
    ahb = pack_mod.load(PACKS / "ahb.vtp.toml")
    assert ahb.channel("A").valid == "htrans[1]"
    wb = pack_mod.load(PACKS / "wishbone.vtp.toml")
    assert wb.channel("WB").valid == "cyc && stb"
    assert wb.channel("WB").ready == "ack || err || rty"


def test_detection_still_reports_one_bus_once():
    """The identity of an interface is its handshake signals. With an expression
    handshake those are the signals the expression *reads*, or two views of one
    bus would be reported as two buses."""
    from veritrace.protocol.detect import _handshake_names

    wb = pack_mod.load(PACKS / "wishbone.vtp.toml")
    assert set(_handshake_names(wb.channel("WB"))) == {"cyc", "stb", "ack", "err", "rty"}
    axi = pack_mod.load(PACKS / "axi4lite.vtp.toml")
    assert _handshake_names(axi.channel("AW")) == ["awvalid", "awready"]


def test_the_new_packs_do_not_steal_the_existing_designs():
    """A new pack that matched too eagerly would silently change what every
    existing design reports. Specificity is what stops that, and it is checked
    here rather than assumed."""
    from veritrace import TraceStore, clocks
    from veritrace.protocol import engine

    design = DESIGNS / "axi_lite"
    vtx = design_store("axi_lite")
    store = TraceStore(str(vtx))
    analysis = engine.extract(store, vtx, clocks.resolve(store), None, project_root=design)
    packs = {e.interface.pack.name for e in analysis.extractions}
    assert packs == {"AXI4-Lite"}, packs


# --- §8.22, gen-sva ---------------------------------------------------------


@pytest.fixture(scope="module")
def axil():
    return pack_mod.load(PACKS / "axi4lite.vtp.toml")


def test_the_sva_target_emits_properties(axil):
    got = sva.render(axil, iface="tb.dut", target="verilator", clock="aclk", reset="aresetn")
    assert "property p_AXI_AWSTABLE;" in got.code
    assert "@(posedge aclk) disable iff (!aresetn)" in got.code
    assert "(awvalid && !awready) |=> $stable(awaddr);" in got.code
    assert "assert property (p_AXI_AWSTABLE)" in got.code
    # `bind` is right here: Verilator supports it.
    assert "bind tb.dut" in got.code


def test_the_portable_target_emits_no_sva(axil):
    got = sva.render(axil, iface="tb.dut", target="portable", clock="aclk", reset="aresetn")
    for banned in ("property", "assert property", "|=>", "|->", "$stable", "$past"):
        assert banned not in got.code, banned
    # The temporal operators became a register and an `if`.
    assert "reg ant_AXI_AWSTABLE_q;" in got.code
    assert "awaddr_q <= awaddr;" in got.code
    assert "(awaddr == awaddr_q)" in got.code
    # And no `bind`, because Icarus has none — §8.22's target would otherwise
    # fail to compile in the simulator it exists for.
    assert "bind " not in got.code
    assert "module vt_axi4_lite_checker_top;" in got.code


def test_both_targets_check_the_same_rules(axil):
    a = sva.render(axil, iface="tb.dut", target="verilator")
    b = sva.render(axil, iface="tb.dut", target="portable")
    assert a.emitted == b.emitted
    assert a.skipped.keys() == b.skipped.keys()


def test_a_per_transaction_rule_is_skipped_with_its_reason(axil):
    """`bresp == 0` means "the response of this transaction was OKAY", not
    "bresp is zero on every cycle". A cycle-level checker cannot say that, and
    one that checked it every cycle would fire on every idle cycle."""
    got = sva.render(axil, iface="tb.dut", target="portable")
    assert "AXI_SLVERR" in got.skipped
    assert "per transaction" in got.skipped["AXI_SLVERR"]
    assert "AXI_SLVERR" not in got.emitted


def test_an_invariant_written_as_an_implication_is_emitted():
    """A pack that means a genuine per-cycle invariant writes it with `|->`, and
    then it comes out. That is how avalon's read/write exclusion survives."""
    avalon = pack_mod.load(PACKS / "avalon.vtp.toml")
    got = sva.render(avalon, iface="tb.dut", target="portable")
    assert "AVMM_RW_EXCLUSIVE" in got.emitted


def test_a_pack_with_no_rules_says_so():
    sdram = pack_mod.load(PACKS / "sdram.vtp.toml")
    with pytest.raises(sva.SvaError, match="declares no"):
        sva.render(sdram, iface="tb.dut")


def test_an_unknown_target_is_refused(axil):
    with pytest.raises(sva.SvaError, match="unknown target"):
        sva.render(axil, iface="tb.dut", target="uvm")


@needs_icarus
def test_the_portable_checker_compiles_and_runs_in_icarus(tmp_path):
    """**The point of `--target portable`.**

    §8.22's whole argument is that SVA cannot be relied on across the free
    stack, so the portable file has to work in the simulator with the least
    support of all. Asserting that a file was written proves nothing; this
    compiles it against the real design and runs it.
    """
    work = tmp_path / "sva"
    work.mkdir()
    for f in (DESIGNS / "axi_lite").glob("*.sv"):
        (work / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    got = CliRunner().invoke(
        main,
        [
            "gen-sva",
            str(design_store("axi_lite")),
            "--rtl",
            str(DESIGNS / "axi_lite"),
            "--target",
            "portable",
            "-o",
            str(work / "checker.sv"),
        ],
    )
    assert got.exit_code == 0, got.output

    iverilog, vvp = simulate.find_iverilog()
    built = run_capture(
        [iverilog, "-g2012", "-o", str(work / "sim.vvp"), *[str(p) for p in work.glob("*.sv")]],
        cwd=work,
        timeout=SIM_TIMEOUT_S,
    )
    assert built.returncode == 0, built.stderr
    ran = run_capture([vvp, str(work / "sim.vvp")], cwd=work, timeout=SIM_TIMEOUT_S)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    # The design satisfies the four stability rules, so a correct checker is
    # silent about them — and a checker that fired here would be the bug.
    assert "AXI_AWSTABLE" not in ran.stdout


@needs_icarus
def test_the_portable_checker_catches_a_real_violation(tmp_path):
    """Silence on good RTL is half the proof. This is the other half: a design
    that breaks a rule must make the generated checker say so."""
    work = tmp_path / "bad"
    work.mkdir()
    (work / "dut.v").write_text(_UNSTABLE, encoding="utf-8")

    axil = pack_mod.load(PACKS / "axi4lite.vtp.toml")
    checker = sva.render(
        axil,
        iface="tb_bad.dut",
        target="portable",
        widths={"awaddr": 32, "araddr": 32, "wdata": 32, "awvalid": 1, "awready": 1,
                "arvalid": 1, "arready": 1, "wvalid": 1, "wready": 1},
        clock="clk",
        reset="rst_n",
    )
    (work / "checker.sv").write_text(checker.code, encoding="utf-8")

    iverilog, vvp = simulate.find_iverilog()
    built = run_capture(
        [iverilog, "-g2012", "-o", str(work / "sim.vvp"), str(work / "dut.v"),
         str(work / "checker.sv")],
        cwd=work, timeout=SIM_TIMEOUT_S,
    )
    assert built.returncode == 0, built.stderr
    ran = run_capture([vvp, str(work / "sim.vvp")], cwd=work, timeout=SIM_TIMEOUT_S)
    out = ran.stdout + ran.stderr
    assert "AXI_AWSTABLE" in out, out


#: A master that changes `awaddr` while `awvalid` is high and `awready` is low —
#: exactly what AXI_AWSTABLE forbids.
_UNSTABLE = """
`timescale 1ns/1ps
module tb_bad;
  reg clk = 0, rst_n = 0;
  always #5 clk = ~clk;
  wire [31:0] awaddr, araddr, wdata;
  wire awvalid, awready, arvalid, arready, wvalid, wready;
  bad_master dut (.clk(clk), .rst_n(rst_n), .awaddr(awaddr), .awvalid(awvalid),
                  .awready(awready), .araddr(araddr), .arvalid(arvalid),
                  .arready(arready), .wdata(wdata), .wvalid(wvalid), .wready(wready));
  initial begin
    repeat (2) @(posedge clk); rst_n = 1;
    repeat (20) @(posedge clk); $finish;
  end
endmodule

module bad_master(input clk, input rst_n,
                  output reg [31:0] awaddr, output reg awvalid, output awready,
                  output reg [31:0] araddr, output reg arvalid, output arready,
                  output reg [31:0] wdata,  output reg wvalid,  output wready);
  assign awready = 1'b0;   // never accepts, so the address must hold
  assign arready = 1'b1;
  assign wready  = 1'b1;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      awaddr <= 32'd0; awvalid <= 1'b0; araddr <= 32'd0;
      arvalid <= 1'b0; wdata <= 32'd0; wvalid <= 1'b0;
    end else begin
      awvalid <= 1'b1;
      awaddr  <= awaddr + 32'd4;   // THE BUG: changes while it is not accepted
    end
  end
endmodule
"""


# --- §13.7, the plugin API --------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """Plugins register globally, so a test that loads one must not leak it."""
    from veritrace import plugin as plugin_mod

    plugin_mod.clear()
    yield
    plugin_mod.clear()


#: **Prompt 16's acceptance criterion, in the only form a test can take it.**
#:
#: Written against `docs/PLUGINS.md` and nothing else — every name below appears
#: on that page. If the API drifts from the documentation, this stops compiling
#: rather than the page quietly becoming wrong.
_FROM_THE_DOCS = '''
from veritrace.plugin import Analysis, register


@register
class ResetPolarity(Analysis):
    name        = "reset_polarity"
    needs       = ["trace"]
    description = "Resets that are held asserted for most of the run"

    def run(self, ctx):
        rows = []
        for sig in ctx.signals(match="*rst_n*"):
            low = ctx.count_where(sig, 0)
            rows.append([sig.path, low, ctx.total_cycles])
            if low > ctx.total_cycles * 0.5:
                yield ctx.finding(
                    f"{sig.name} is low for {low} of {ctx.total_cycles} cycles",
                    severity="warn",
                    signal=sig.path,
                    detail="An active-low reset held for most of the run usually means "
                           "the testbench never released it.",
                )
        yield ctx.table("Reset polarity", ["signal", "low", "total"], rows)
'''


def test_a_plugin_written_from_the_documentation_alone_works(tmp_path):
    """**Prompt 16's acceptance criterion (§16.2).**

    The plugin above uses `@register`, `Analysis`, `name`, `needs`,
    `description`, `run(ctx)`, `ctx.signals(match=...)`, `ctx.count_where`,
    `ctx.total_cycles`, `ctx.finding` and `ctx.table` — the whole of what
    `docs/PLUGINS.md` documents, and nothing beyond it.
    """
    from veritrace import TraceStore, clocks
    from veritrace import plugin as plugin_mod

    project = tmp_path / "proj"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "reset_polarity.py").write_text(_FROM_THE_DOCS, encoding="utf-8")

    found, errors = plugin_mod.discover(project)
    assert errors == {}, errors
    assert [c.name for c in found] == ["reset_polarity"]
    assert found[0].needs == ["trace"]

    store = TraceStore(str(design_store("fifo_buggy")))
    got = plugin_mod.run_all(found, store=store, clock=clocks.resolve(store))

    assert got.skipped == {}, got.skipped
    assert got.tables and got.tables[0].title == "Reset polarity"
    assert got.tables[0].columns == ["signal", "low", "total"]
    assert got.tables[0].rows, "the design has reset signals; the table should have rows"
    # `rd_rst_n` is tied low for the whole run — that is this design's injected
    # bug, so a plugin looking for a reset held low must find it.
    assert any("rd_rst_n" in f.signal for f in got.findings), [f.title for f in got.findings]
    for f in got.findings:
        assert f.check == "plugin.reset_polarity"
        assert f.group.value == "plugin"


def test_a_plugin_whose_needs_are_unmet_is_not_run_and_says_why(tmp_path):
    """§13.7: running it anyway and finding nothing is indistinguishable from a
    design with nothing wrong (P7)."""
    from veritrace import plugin as plugin_mod

    src = "\n".join(
        [
            "from veritrace.plugin import Analysis, register",
            "@register",
            "class NeedsBus(Analysis):",
            "    name = 'needs_bus'",
            "    needs = ['transactions']",
            "    def run(self, ctx):",
            "        yield ctx.finding('should never appear')",
        ]
    )
    project = tmp_path / "p"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "needs_bus.py").write_text(src, encoding="utf-8")

    found, _ = plugin_mod.discover(project)
    got = plugin_mod.run_all(found, store=object())
    assert got.findings == []
    assert "transactions" in got.skipped["needs_bus"]


def test_project_plugin_replaces_a_user_plugin_with_the_same_name(tmp_path, monkeypatch):
    """Discovery order promised an override but used to run both classes."""
    from veritrace import plugin as plugin_mod

    user = tmp_path / "user"
    project = tmp_path / "project"
    user.mkdir()
    (project / "plugins").mkdir(parents=True)
    template = (
        "from veritrace.plugin import Analysis, register\n"
        "@register\n"
        "class Same(Analysis):\n"
        "    name = 'same'\n"
        "    description = {description!r}\n"
        "    def run(self, ctx): return iter(())\n"
    )
    (user / "same.py").write_text(template.format(description="user"), encoding="utf-8")
    (project / "plugins" / "same.py").write_text(
        template.format(description="project"), encoding="utf-8"
    )
    monkeypatch.setattr(plugin_mod, "USER_DIR", user)

    found, errors = plugin_mod.discover(project)
    assert errors == {}
    assert [(p.name, p.description) for p in found] == [("same", "project")]


def test_discovery_does_not_leak_plugins_into_the_next_project(tmp_path, monkeypatch):
    """A server process may open many unrelated projects in its lifetime."""
    from veritrace import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod, "USER_DIR", tmp_path / "no-user-plugins")
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "plugins").mkdir(parents=True)
    (second / "plugins").mkdir(parents=True)
    (first / "plugins" / "only_first.py").write_text(
        "from veritrace.plugin import Analysis, Exporter, register\n"
        "@register\n"
        "class OnlyFirst(Analysis):\n"
        "    name = 'only-first'\n"
        "    def run(self, ctx): return iter(())\n"
        "@register\n"
        "class FirstFormat(Exporter):\n"
        "    name = 'first-format'\n"
        "    def render(self, ctx): return 'first'\n",
        encoding="utf-8",
    )

    found, errors = plugin_mod.discover(first)
    assert errors == {}
    assert [cls.name for cls in found] == ["only-first"]
    assert [cls.name for cls in plugin_mod.exporters()] == ["first-format"]

    found, errors = plugin_mod.discover(second)
    assert errors == {}
    assert found == []
    assert plugin_mod.exporters() == []


def test_checks_path_does_not_run_a_previous_projects_plugin(tmp_path, monkeypatch):
    """Exercise the production Checks orchestration, not only discovery."""
    from veritrace import TraceStore, clocks
    from veritrace import plugin as plugin_mod
    from veritrace.analysis import checks as checks_mod
    from veritrace.config import Config

    monkeypatch.setattr(plugin_mod, "USER_DIR", tmp_path / "no-user-plugins")
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "plugins").mkdir(parents=True)
    second.mkdir()
    (first / "plugins" / "first_check.py").write_text(
        "from veritrace.plugin import Analysis, register\n"
        "@register\n"
        "class FirstCheck(Analysis):\n"
        "    name = 'first-check'\n"
        "    def run(self, ctx): yield ctx.finding('belongs only to first')\n",
        encoding="utf-8",
    )
    store = TraceStore(str(design_store("fsm")))

    first_report = checks_mod.run_all(
        store,
        clock=clocks.resolve(store),
        config=Config.empty(first),
        project_root=first,
    )
    second_report = checks_mod.run_all(
        store,
        clock=clocks.resolve(store),
        config=Config.empty(second),
        project_root=second,
    )

    assert any(f.check == "plugin.first-check" for f in first_report.findings)
    assert all(f.check != "plugin.first-check" for f in second_report.findings)


def test_failed_plugin_file_cannot_leave_a_registered_ghost(tmp_path, monkeypatch):
    """Registration followed by an import error must roll back the whole file."""
    from veritrace import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod, "USER_DIR", tmp_path / "no-user-plugins")
    project = tmp_path / "project"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "partial.py").write_text(
        "from veritrace.plugin import Analysis, Exporter, register\n"
        "@register\n"
        "class GhostAnalysis(Analysis):\n"
        "    name = 'ghost-analysis'\n"
        "    def run(self, ctx): return iter(())\n"
        "@register\n"
        "class GhostExporter(Exporter):\n"
        "    name = 'ghost-exporter'\n"
        "    def render(self, ctx): return 'must not run'\n"
        "raise RuntimeError('failed after registration')\n",
        encoding="utf-8",
    )

    found, errors = plugin_mod.discover(project)

    assert found == []
    assert plugin_mod.exporters() == []
    assert "failed after registration" in errors["partial.py"]


def test_concurrent_project_discovery_and_export_are_context_isolated(
    tmp_path, monkeypatch
):
    """Two API workers cannot replace each other's catalog mid-operation."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from veritrace import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod, "USER_DIR", tmp_path / "no-user-plugins")
    projects: dict[str, Path] = {}
    template = (
        "from veritrace.plugin import Analysis, Exporter, register\n"
        "@register\n"
        "class ProjectAnalysis(Analysis):\n"
        "    name = 'project-analysis'\n"
        "    description = {label!r}\n"
        "    def run(self, ctx): return iter(())\n"
        "@register\n"
        "class ProjectFormat(Exporter):\n"
        "    name = 'project-format'\n"
        "    description = {label!r}\n"
        "    def render(self, ctx): return {label!r}\n"
    )
    for label in ("alpha", "beta"):
        project = tmp_path / label
        (project / "plugins").mkdir(parents=True)
        (project / "plugins" / "project.py").write_text(
            template.format(label=label), encoding="utf-8"
        )
        projects[label] = project

    entered = Barrier(2)
    imported = Barrier(2)
    real_import = plugin_mod._import

    def overlapping_import(path):
        entered.wait(timeout=5)
        real_import(path)
        # Both files are now registered before either discover() snapshots its
        # catalog. A shared process-wide registry deterministically mixes them.
        imported.wait(timeout=5)

    monkeypatch.setattr(plugin_mod, "_import", overlapping_import)

    def load_and_render(label):
        found, errors = plugin_mod.discover(projects[label])
        exporters = plugin_mod.exporters()
        assert errors == {}
        assert len(found) == 1
        assert len(exporters) == 1
        return found[0].description, plugin_mod.run_exporter(exporters[0])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {label: pool.submit(load_and_render, label) for label in projects}
        results = {label: future.result(timeout=10) for label, future in futures.items()}

    assert results == {"alpha": ("alpha", "alpha"), "beta": ("beta", "beta")}


def test_declared_plugin_capabilities_reach_the_real_checks_path(tmp_path):
    """coverage/memory/performance were advertised but never wired by check()."""
    from veritrace import TraceStore, clocks
    from veritrace.analysis import checks as checks_mod
    from veritrace.config import Config

    project = tmp_path / "project"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "caps.py").write_text(
        "from veritrace.plugin import Analysis, register\n"
        "@register\n"
        "class Caps(Analysis):\n"
        "    name = 'caps'\n"
        "    needs = ['coverage', 'memory', 'performance']\n"
        "    def run(self, ctx):\n"
        "        assert ctx.coverage is not None\n"
        "        assert ctx.memory\n"
        "        assert ctx.performance is not None\n"
        "        yield ctx.finding('all capabilities arrived')\n",
        encoding="utf-8",
    )
    store = TraceStore(str(design_store("fifo_buggy")))
    cfg = Config.empty(project)
    report = checks_mod.run_all(
        store,
        clock=clocks.resolve(store),
        config=cfg,
        project_root=project,
        coverage_report=object(),
        memory_reports=[object()],
        performance_report=object(),
    )
    assert any(f.check == "plugin.caps" for f in report.findings)
    assert "plugin:caps" not in report.skipped


def test_a_plugin_that_raises_is_a_skip_not_a_broken_tab(tmp_path):
    from veritrace import plugin as plugin_mod

    src = "\n".join(
        [
            "from veritrace.plugin import Analysis, register",
            "@register",
            "class Boom(Analysis):",
            "    name = 'boom'",
            "    def run(self, ctx):",
            "        raise ValueError('deliberate')",
            "@register",
            "class Fine(Analysis):",
            "    name = 'fine'",
            "    def run(self, ctx):",
            "        yield ctx.finding('still here')",
        ]
    )
    project = tmp_path / "p"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "two.py").write_text(src, encoding="utf-8")

    found, _ = plugin_mod.discover(project)
    got = plugin_mod.run_all(found)
    assert "deliberate" in got.skipped["boom"]
    assert [f.title for f in got.findings] == ["still here"]


def test_a_file_that_does_not_import_is_named_rather_than_fatal(tmp_path):
    from veritrace import plugin as plugin_mod

    project = tmp_path / "p"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "broken.py").write_text("import nonexistent_module_xyz", encoding="utf-8")
    (project / "plugins" / "good.py").write_text(
        "from veritrace.plugin import Analysis, register\n"
        "@register\n"
        "class Ok(Analysis):\n"
        "    name = 'ok'\n"
        "    def run(self, ctx):\n"
        "        return iter(())\n",
        encoding="utf-8",
    )
    found, errors = plugin_mod.discover(project)
    assert "broken.py" in errors
    assert [c.name for c in found] == ["ok"]


def test_a_plugin_directory_cannot_shadow_the_standard_library(tmp_path):
    """Importing by path rather than by appending to `sys.path`: a `plugins/`
    folder next to somebody's RTL must not be able to replace `json` for the
    rest of the process."""
    import json as before

    from veritrace import plugin as plugin_mod

    project = tmp_path / "p"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "json.py").write_text("raise RuntimeError('should not shadow')", encoding="utf-8")
    plugin_mod.discover(project)

    import json as after

    assert after is before


def test_a_plugin_asking_for_something_that_does_not_exist_is_refused():
    from veritrace import plugin as plugin_mod

    with pytest.raises(plugin_mod.PluginError, match="try one of"):

        @plugin_mod.register
        class Bad(plugin_mod.Analysis):
            name = "bad"
            needs = ["telepathy"]


def test_a_plugin_without_a_name_is_refused():
    from veritrace import plugin as plugin_mod

    with pytest.raises(plugin_mod.PluginError, match="has no `name`"):

        @plugin_mod.register
        class Nameless(plugin_mod.Analysis):
            pass


def test_plugins_reach_the_checks_report(tmp_path):
    """§13.7: "Constatarile aparute apar automat in tab-ul Checks." Through
    `run_all`, so `--fail-on` and suppression work on them like anything else."""
    from veritrace import TraceStore, clocks, config
    from veritrace.analysis import checks as checks_mod

    project = tmp_path / "proj"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "reset_polarity.py").write_text(_FROM_THE_DOCS, encoding="utf-8")
    (project / ".veritrace.toml").write_text('[design]\ntop = "tb"\n', encoding="utf-8")

    store = TraceStore(str(design_store("fifo_buggy")))
    conf = config.load_or_empty(project)
    report = checks_mod.run_all(store, clock=clocks.resolve(store), config=conf)

    names = {f.check for f in report}
    assert "plugin.reset_polarity" in names, names
    assert report.plugin_tables and report.plugin_tables[0].title == "Reset polarity"
    assert "plugin_tables" in report.to_dict()


def test_the_command_lists_what_is_installed(tmp_path):
    project = tmp_path / "proj"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "reset_polarity.py").write_text(_FROM_THE_DOCS, encoding="utf-8")

    got = CliRunner().invoke(main, ["plugins", "--root", str(project)])
    assert got.exit_code == 0, got.output
    assert "reset_polarity" in got.output
    assert "needs trace" in got.output
    assert "Resets that are held asserted" in got.output


def test_the_command_says_when_there_are_none(tmp_path):
    got = CliRunner().invoke(main, ["plugins", "--root", str(tmp_path)])
    assert got.exit_code == 0, got.output
    assert "no plugins found" in got.output


_EXPORTER_FROM_THE_DOCS = '''
import json
from veritrace.plugin import Exporter, register

@register
class SignalJson(Exporter):
    name = "signal-json"
    needs = ["trace"]
    description = "Signal inventory"
    extension = ".json"

    def render(self, ctx):
        return json.dumps({
            "trace_sha256": ctx.metadata["trace_sha256"],
            "query": ctx.query,
            "signals": [s.path for s in ctx.signals()],
        }, sort_keys=True)
'''


def test_exporter_from_documentation_runs_through_real_cli(tmp_path, monkeypatch):
    """§13.7's third extension point was previously only a sentence in SPEC."""
    project = tmp_path / "project"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "signal_json.py").write_text(
        _EXPORTER_FROM_THE_DOCS, encoding="utf-8"
    )
    trace = make_vtx(project)
    output = project / "signals.json"
    monkeypatch.chdir(project)

    got = CliRunner().invoke(
        main,
        ["export", str(trace), "--format", "signal-json", "-o", str(output)],
    )

    assert got.exit_code == 0, got.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["query"] is None
    assert payload["trace_sha256"]
    assert "tb.clk" in payload["signals"]
    assert "signal-json ->" in got.output


def test_exporter_from_documentation_runs_through_real_api(tmp_path, monkeypatch):
    """§13.7's exporter is reachable from the public §10.1 route too."""
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    project = tmp_path / "project"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "signal_json.py").write_text(
        _EXPORTER_FROM_THE_DOCS, encoding="utf-8"
    )
    (project / ".veritrace.toml").write_text("[design]\n", encoding="utf-8")
    trace = make_vtx(project)
    monkeypatch.chdir(project)

    with TestClient(create_app(default_trace=trace)) as client:
        sid = client.get("/", headers={"Accept": "application/json"}).json()[
            "default_session"
        ]
        got = client.post(
            f"/session/{sid}/export", json={"kind": "signal-json"}
        )

    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("application/json")
    assert "attachment" in got.headers["content-disposition"]
    payload = got.json()
    assert payload["trace_sha256"]
    assert "tb.clk" in payload["signals"]


def test_failing_exporter_does_not_clobber_existing_report(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "broken.py").write_text(
        "from veritrace.plugin import Exporter, register\n"
        "@register\n"
        "class Broken(Exporter):\n"
        "    name = 'broken'\n"
        "    needs = ['trace']\n"
        "    def render(self, ctx): raise RuntimeError('deliberate exporter failure')\n",
        encoding="utf-8",
    )
    trace = make_vtx(project)
    output = project / "report.txt"
    output.write_text("previous valid report", encoding="utf-8")
    monkeypatch.chdir(project)

    got = CliRunner().invoke(
        main,
        ["export", str(trace), "--format", "broken", "-o", str(output)],
    )

    assert got.exit_code != 0
    assert "deliberate exporter failure" in got.output
    assert output.read_text(encoding="utf-8") == "previous valid report"


def test_plugins_command_lists_exporters(tmp_path):
    project = tmp_path / "project"
    (project / "plugins").mkdir(parents=True)
    (project / "plugins" / "signal_json.py").write_text(
        _EXPORTER_FROM_THE_DOCS, encoding="utf-8"
    )

    got = CliRunner().invoke(main, ["plugins", "--root", str(project), "--json"])

    assert got.exit_code == 0, got.output
    payload = json.loads(got.output)
    assert payload["plugins"] == []
    assert payload["exporters"] == [
        {
            "name": "signal-json",
            "needs": ["trace"],
            "description": "Signal inventory",
            "extension": ".json",
        }
    ]
