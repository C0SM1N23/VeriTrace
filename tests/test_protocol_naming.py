"""Real HDL naming variants must reach transactions and functional coverage."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from veritrace import TraceStore, clocks, simulate
from veritrace._native import convert
from veritrace.api import create_app
from veritrace.cli import main
from veritrace.config import Config
from veritrace.protocol import detect, engine, pack

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


@pytest.mark.skipif(simulate.find_iverilog() is None, reason="Icarus Verilog is not installed")
def test_direction_suffixes_reach_real_cli_native_transactions_and_coverage(tmp_path):
    project = tmp_path / "bus with spaces"
    project.mkdir()
    outputs = {"awvalid", "awaddr", "awprot", "wvalid", "wdata", "wstrb", "bready",
               "arvalid", "araddr", "arprot", "rready"}
    inputs = {"awready", "wready", "bvalid", "bresp", "arready", "rvalid", "rdata", "rresp"}

    def rename(match):
        name = match.group()
        if name == "aclk":
            return "clk_i"
        if name == "aresetn":
            return "rst_n_i"
        leaf = name.removeprefix("s_axi_")
        if leaf in outputs | inputs:
            prefix = "dbus_axi_" if name.startswith("s_axi_") else ""
            return prefix + leaf + ("_o" if leaf in outputs else "_i")
        return name

    for source in (DESIGNS / "axi_lite").glob("*.sv"):
        (project / source.name).write_text(re.sub(r"\b\w+\b", rename, source.read_text()), encoding="utf-8")
    result = CliRunner().invoke(main, ["run", str(project), "--top", "tb_axi_lite", "--json"])
    assert result.exit_code == 0, result.output
    dump = Path(json.loads(result.stdout)["dump"])
    assert dump.is_file()
    # Open the result exactly as the application does, including the native
    # parser, build manifest, clock selection and extraction cache.
    with TestClient(create_app(default_trace=dump)) as client:
        sid = client.get("/").json()["default_session"]
        session = client.app.state.registry.get(sid)
        store = session.store
        clock = clocks.resolve(store)
        analysis = engine.extract(store, session.trace_path, clock, Config.empty(), use_cache=False)
        assert len(analysis.interfaces) == 1, analysis.errors
        ex = analysis.extractions[0]
        assert ex.interface.pack.slug == "axi4lite"
        assert any("dbus_axi" in alias for alias in ex.interface.aliases)
        assert ex.interface.clock.endswith(".clk_i")
        assert ex.interface.reset.endswith(".rst_n_i")
        assert ex.n_events == ex.n_matched == 60
        assert len(ex.transactions) == 24
        for transaction in ex.transactions:
            for beat in transaction.events:
                valid = ex.interface.signals[beat.channel.lower() + "valid"]
                assert store.value_before(store.find(valid), beat.time).to_int() == 1
        coverage = client.get(f"/session/{sid}/coverage")
        assert coverage.status_code == 200, coverage.text
        functional = coverage.json()["functional"]
        assert len(functional) == 1
        assert functional[0]["n_transactions"] == 24
        assert any(p["name"] == "error_response" and p["covered"] == 1 for p in functional[0]["points"])
        cached = engine.extract(store, session.trace_path, clock, Config.empty(), use_cache=True)
        assert {k: v for k, v in cached.extractions[0].to_dict().items() if k != "ms"} == {
            k: v for k, v in ex.to_dict().items() if k != "ms"
        }
        assert [t.to_dict() for t in cached.extractions[0].transactions] == [
            t.to_dict() for t in ex.transactions
        ]


def _handshake_store(tmp_path, declarations):
    source = tmp_path / "wires.vcd"
    source.write_text("$timescale 1ns $end\n$scope module tb $end\n" + declarations +
                      "\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n#10\n0!\n")
    output = tmp_path / "wires.vtx"
    convert(str(source), str(output))
    return TraceStore(str(output))


@pytest.mark.parametrize("alias", [False, True])
def test_ambiguous_direction_suffixes_are_refused_unless_native_aliases(tmp_path, alias):
    store = _handshake_store(tmp_path, '$var wire 1 ! clk_i $end\n'
                            '$var wire 1 " bus_valid_i $end\n'
                            f'$var wire 1 {chr(34) if alias else chr(35)} bus_valid_o $end\n'
                            '$var wire 1 % bus_ready_o $end')
    errors = []
    interfaces = detect.detect(store, pack.resolve(["handshake"]), diagnostics=errors)
    assert len(interfaces) == int(alias)
    if alias:
        assert interfaces[0].signals["valid"] == "tb.bus_valid_i"
    else:
        assert any("ambiguous bus_valid" in error for error in errors)


def test_distinct_local_clocks_do_not_silently_use_session_clock(tmp_path):
    store = _handshake_store(tmp_path, '$var wire 1 ! clk_i $end\n'
                            '$var wire 1 & clk_o $end\n'
                            '$var wire 1 " bus_valid_i $end\n'
                            '$var wire 1 % bus_ready_o $end')
    errors = []
    assert not detect.detect(store, pack.resolve(["handshake"]), diagnostics=errors)
    assert any("ambiguous clock/reset" in error for error in errors)


@pytest.mark.skipif(simulate.find_iverilog() is None, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("ids", ["absent", "unknown", "partial"])
def test_real_full_axi_burst_is_not_misclassified_as_single_beat_lite(tmp_path, ids):
    source = tmp_path / "tb.sv"
    source.write_text("""`timescale 1ns/1ps
module tb;
  reg clk_i=0, rst_n_i=0;
  always #5 clk_i=~clk_i;
  reg dbus_awvalid_o=0, dbus_awready_i=1;
  reg dbus_wvalid_o=0, dbus_wready_i=1, dbus_wlast_o=0;
  reg dbus_bvalid_i=0, dbus_bready_o=1;
  reg dbus_arvalid_o=0, dbus_arready_i=1;
  reg dbus_rvalid_i=0, dbus_rready_o=1, dbus_rlast_i=0;
  reg [31:0] dbus_awaddr_o=32'h40, dbus_araddr_o=32'h80;
  reg [31:0] dbus_wdata_o=32'h11, dbus_rdata_i=32'h33;
  reg [7:0] dbus_awlen_o=1, dbus_arlen_o=1;
  reg [3:0] dbus_wstrb_o=15;
  reg [1:0] dbus_bresp_i=0, dbus_rresp_i=0;
  initial begin
    #12; rst_n_i=1;
    @(negedge clk_i); dbus_awvalid_o=1; dbus_arvalid_o=1;
                     dbus_wvalid_o=1; dbus_rvalid_i=1;
    @(negedge clk_i); dbus_awvalid_o=0; dbus_arvalid_o=0;
                     dbus_wlast_o=1; dbus_rlast_i=1;
                     dbus_wdata_o=32'h22; dbus_rdata_i=32'h44;
    @(negedge clk_i); dbus_wvalid_o=0; dbus_rvalid_i=0; dbus_bvalid_i=1;
    @(negedge clk_i); dbus_bvalid_i=0;
    #6; $finish;
  end
endmodule
""", encoding="utf-8")
    if ids != "absent":
        declarations = "reg [1:0] dbus_awid_o=2'bxx, dbus_arid_o=2'bxx;\n"
        if ids == "unknown":
            declarations += "reg [1:0] dbus_bid_i=2'bxx, dbus_rid_i=2'bxx;\n"
        source.write_text(source.read_text().replace("  initial begin", declarations + "  initial begin"))
    got = CliRunner().invoke(main, ["run", str(tmp_path), "--json"])
    assert got.exit_code == 0, got.output
    dump = Path(json.loads(got.stdout)["dump"])
    with TestClient(create_app(default_trace=dump)) as client:
        sid = client.get("/").json()["default_session"]
        session = client.app.state.registry.get(sid)
        analysis = engine.extract(session.store, session.trace_path,
                                  clocks.resolve(session.store), Config.empty(), use_cache=False)
        assert len(analysis.extractions) == 1
        ex = analysis.extractions[0]
        assert ex.interface.pack.slug == "axi4"
        if ids != "absent":
            # X values and partly dumped IDs are not an ID-less design. Do
            # not invent matches merely because only one request is open.
            assert ex.n_events == 7 and ex.n_matched == 4
            assert all(not t.closed for t in ex.transactions)
            assert not any("ID pins are absent" in note for note in ex.skipped.values())
            return
        assert ex.n_events == ex.n_matched == 7
        assert len(ex.transactions) == 2
        writes = [t for t in ex.transactions if t.kind == "WRITE"]
        reads = [t for t in ex.transactions if t.kind == "READ"]
        assert len(writes) == len(reads) == 1
        assert writes[0].closed and reads[0].closed
        assert [b.fields["wdata"] for b in writes[0].beats("W")] == [0x11, 0x22]
        assert [b.fields["rdata"] for b in reads[0].beats("R")] == [0x33, 0x44]
        assert writes[0].end_time == 45000
        assert reads[0].end_time == 35000
        assert "ID pins are absent" in ex.skipped["WRITE IDs"]
        assert "ID pins are absent" in ex.skipped["READ IDs"]


def test_configured_reset_accepts_aliases_and_direction_suffix_without_renaming(tmp_path):
    store = _handshake_store(tmp_path, '$var wire 1 ! clk $end\n'
                            '$var wire 1 " rst_n_i $end\n'
                            '$var wire 1 " rst_n_o $end')
    assert detect.configured_signal(store, "rst_n") == "tb.rst_n_i"
    assert detect.configured_signal(store, "tb.rst_n_o") == "tb.rst_n_o"


def test_absent_ids_do_not_guess_between_multiple_outstanding_requests():
    from veritrace.protocol.assemble import assemble
    from veritrace.protocol.channels import ChannelScan
    from veritrace.protocol.model import ChannelEvent, Interface

    p = pack.resolve(["axi4"])[0]
    iface = Interface("bus", "tb", "", p, {})
    scans = {
        "AW": ChannelScan("AW", [ChannelEvent("AW", t, t, 0, {"awaddr": t, "awlen": 0}) for t in (10, 20)]),
        "W": ChannelScan("W", [ChannelEvent("W", t, t, 0, {"wdata": t, "wlast": 1}) for t in (12, 22)]),
        "B": ChannelScan("B", [ChannelEvent("B", 30, 30, 0, {"bresp": 0})]),
    }
    out = assemble(iface, scans, None)
    assert out.n_events == 5 and out.n_matched == 4
    assert len(out.transactions) == 2 and all(not t.closed for t in out.transactions)
    assert "one outstanding" in out.skipped["WRITE IDs"]
