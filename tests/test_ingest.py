"""§8.34 transaction ingestion, and §7.4b's black-box boundary.

The cocotb fixture is a *real* log: `designs/cocotb/axi_mon.py` was run against
`axil_slave` and `sim.log` is what it printed. Parsing a log somebody wrote by
hand would test the regex against itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace.graph.elaborate import elaborate, is_protected
from veritrace.ingest import cocotb, merge, read, uvm

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
LOG = DESIGNS / "cocotb" / "sim.log"

#: A `uvm_text_tr_database`, in the format UVM's free recorder writes.
UVM_DB = """\
CREATE_STREAM @0 {NAME:mon STREAM:2 SCOPE:uvm_test_top.env.agt TYPE:TVM}
BEGIN @1000 {TXH:7 STREAM:2 NAME:write}
SET_ATTR @1000 {TXH:7 NAME:addr VALUE:'h1000 RADIX:UVM_HEX BITS:32}
SET_ATTR @1000 {TXH:7 NAME:len VALUE:4}
BEGIN @1100 {TXH:8 STREAM:2 NAME:read}
SET_ATTR @1100 {TXH:8 NAME:addr VALUE:'h2004}
END @1500 {TXH:7}
END @1800 {TXH:8}
"""


# --- §8.34 cocotb ------------------------------------------------------------


def test_the_real_monitor_log_produces_paired_transactions():
    streams = cocotb.parse(LOG.read_text(encoding="utf-8", errors="replace"), timescale="1ps")
    assert len(streams) == 1
    e = streams[0]
    assert e.interface.name == "axi_mon"
    kinds = {}
    for t in e.transactions:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
    assert kinds == {"WRITE": 8, "READ": 8}
    assert not e.open_transactions, "every phase in this log is closed"


def test_a_paired_transaction_has_a_latency_nobody_computed():
    """§8.34's payoff: latency comes free, because the model is the same one."""
    e = cocotb.parse(LOG.read_text(errors="replace"), timescale="1ps")[0]
    durations = {t.duration() for t in e.transactions}
    assert durations == {20_000}, "two clock periods at 1ps per tick"


def test_fields_are_lifted_from_the_line():
    e = cocotb.parse(LOG.read_text(errors="replace"), timescale="1ps")[0]
    write = next(t for t in e.transactions if t.kind == "WRITE")
    assert set(write.fields) >= {"addr", "data", "strb", "resp"}
    assert isinstance(write.fields["addr"], int), "0x4 is a number, not a string"


def test_a_status_line_is_not_a_transaction():
    """The monitor also logs `DONE`. A transaction carries data; a status line
    does not, and a catch-all pattern that swallows both is worse than none."""
    e = cocotb.parse(LOG.read_text(errors="replace"), timescale="1ps")[0]
    assert not any(t.kind == "DONE" for t in e.transactions)


def test_a_single_line_monitor_gives_completed_transactions():
    """Whether the *pattern* has an `end` group decides what a line means."""
    text = "  100.00ns INFO  cocotb.dut  mon WRITE addr=0x10 data=0x5\n"
    e = cocotb.parse(text, {"single": cocotb.DEFAULTS["single"]}, timescale="1ns")[0]
    assert len(e.transactions) == 1
    assert e.transactions[0].closed, "one line per transaction means it already finished"


def test_a_bad_regex_is_reported_not_swallowed():
    with pytest.raises(ValueError, match="not a valid regex"):
        cocotb.parse("x", {"broken": "(unclosed"})


def test_times_are_converted_into_the_traces_ticks():
    """A log is in wall-clock time and a trace is in ticks; ingesting without
    converting puts every transaction at the wrong cycle."""
    text = "  1.00ns INFO  cocotb.dut  mon WRITE addr=0x0\n"
    ns = cocotb.parse(text, {"single": cocotb.DEFAULTS["single"]}, timescale="1ns")[0]
    ps = cocotb.parse(text, {"single": cocotb.DEFAULTS["single"]}, timescale="1ps")[0]
    assert ns.transactions[0].start_time == 1
    assert ps.transactions[0].start_time == 1000


# --- §8.34 UVM ---------------------------------------------------------------


def test_the_uvm_database_is_read_straight_through():
    streams = uvm.parse(UVM_DB)
    assert len(streams) == 1
    e = streams[0]
    assert e.interface.name == "uvm_test_top.env.agt", "the scope names the interface"
    assert [t.kind for t in e.transactions] == ["WRITE", "READ"]
    assert e.transactions[0].fields == {"addr": 0x1000, "len": 4}
    assert e.transactions[0].duration() == 500


def test_interleaved_uvm_transactions_stay_apart():
    """UVM has several open at once; TXH is what keeps them apart."""
    e = uvm.parse(UVM_DB)[0]
    read = next(t for t in e.transactions if t.kind == "READ")
    assert read.fields == {"addr": 0x2004}
    assert read.start_time == 1100 and read.end_time == 1800


# --- the model everything downstream reads -----------------------------------


def test_an_ingested_stream_is_an_ordinary_extraction():
    """§8.34's whole point: latency, deadlock and why(txn) work unchanged."""
    e = read(cocotb_log=LOG, timescale="1ps")[0]
    assert e.correlation == 100.0, "every transaction the monitor recorded is matched"
    assert e.sampled_cycles == 0, "no signal scan ran, and the report says so"
    assert e.interface.pack.channels == (), "an ingested stream has no wires to declare"
    assert e.transactions[0].ref.startswith("axi_mon.")


def test_indexes_are_renumbered_in_start_order():
    e = read(cocotb_log=LOG, timescale="1ps")[0]
    writes = [t for t in e.transactions if t.kind == "WRITE"]
    assert [t.index for t in writes] == list(range(len(writes)))
    assert writes == sorted(writes, key=lambda t: t.start_time)


def test_a_monitor_replaces_the_pack_for_the_same_interface():
    """§8.34: where both describe one interface, the monitor wins — and the
    reason is recorded rather than the pack silently disappearing."""
    from veritrace.protocol.engine import ProtocolAnalysis

    derived = ProtocolAnalysis()
    derived.extractions = read(cocotb_log=LOG, timescale="1ps")  # same name
    ingested = read(cocotb_log=LOG, timescale="1ps")
    out = merge(derived, ingested)
    assert len(out.extractions) == 1
    assert any("axi_mon" in e and "§8.34" in e for e in out.errors)


# --- §7.4b black-box IP ------------------------------------------------------


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    phy = tmp_path / "secret_phy.v"
    phy.write_text(
        "`pragma protect begin_protected\n"
        '`pragma protect encrypt_agent = "XILINX"\n'
        "`pragma protect data_block\n"
        "kQ8vZ3nR1mYt4Lp0aWx7Hq2Bf5Jd6Ec9Gs3Nu8Iv0Kw1Ly2Mz3Ob4Pc5Qd6Re7Sf8Tg9Uh0Vi1Wj2X\n"
        "`pragma protect end_protected\n",
        encoding="utf-8",
    )
    top = tmp_path / "top.sv"
    top.write_text(
        "module top (input logic clk, input logic rst_n, output logic ready);\n"
        "  logic phy_ready;\n"
        "  secret_phy u_phy (.clk(clk), .rst_n(rst_n), .ready(phy_ready));\n"
        "  always_ff @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) ready <= 1'b0; else ready <= phy_ready;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    return phy, top


def test_an_encrypted_block_is_detected(tmp_path):
    phy, top = _fixture(tmp_path)
    assert is_protected(phy)
    assert not is_protected(top)


def test_encrypted_ip_is_a_boundary_not_an_error(tmp_path):
    """§7.4b: the rest of the design stays completely analysable."""
    phy, top = _fixture(tmp_path)
    el = elaborate([phy, top], [], [], "top")

    assert el.protected == [phy]
    assert el.graph.blackboxes == {"top.u_phy": "secret_phy"}
    assert not el.errors, "a design that uses encrypted IP must not open in red"
    assert any("BLACKBOX_IP" in d.message for d in el.diagnostics)
    # And the design around it is still there.
    assert {"top.ready", "top.phy_ready"} <= {s.path for s in el.graph}


def test_only_what_the_ip_drives_is_marked(tmp_path):
    """A clock goes *into* the IP. Marking it would stop why-trace at the clock,
    which explains nothing."""
    phy, top = _fixture(tmp_path)
    el = elaborate([phy, top], [], [], "top")
    assert el.graph.blackbox_driven == {"top.phy_ready"}
