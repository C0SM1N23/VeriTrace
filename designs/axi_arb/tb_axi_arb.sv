`timescale 1ns / 1ps
//
// Two AXI4-Lite masters, a fixed-priority arbiter, one slow slave.
//
// This is the design behind the third acceptance criterion of Prompt 9:
// transaction-level why-trace has to cross the arbiter. Master 0 spends most of
// the run unable to issue, and the reason is not in master 0 at all — it is a
// transaction belonging to master 1. A signal-level answer stops at
// `grant != 0`; the transaction-level one names WRITE#n on m1.
//
// Both masters are write-only, so a transaction is exactly AW + W + B and the
// arbiter's lock has an unambiguous end.
//
module tb_axi_arb;

  logic aclk = 1'b0;
  logic aresetn = 1'b0;

  always #5 aclk = ~aclk;  // 100 MHz

  // ---- master 0: blocked by the arbiter most of the time ------------------
  logic        m0_awvalid, m0_awready;
  logic [31:0] m0_awaddr;
  logic [ 2:0] m0_awprot;
  logic        m0_wvalid, m0_wready;
  logic [31:0] m0_wdata;
  logic [ 3:0] m0_wstrb;
  logic        m0_bvalid, m0_bready;
  logic [ 1:0] m0_bresp;
  logic        m0_arvalid, m0_arready;
  logic [31:0] m0_araddr;
  logic [ 2:0] m0_arprot;
  logic        m0_rvalid, m0_rready;
  logic [31:0] m0_rdata;
  logic [ 1:0] m0_rresp;
  logic        m0_busy;

  // ---- master 1: always wins ----------------------------------------------
  logic        m1_awvalid, m1_awready;
  logic [31:0] m1_awaddr;
  logic [ 2:0] m1_awprot;
  logic        m1_wvalid, m1_wready;
  logic [31:0] m1_wdata;
  logic [ 3:0] m1_wstrb;
  logic        m1_bvalid, m1_bready;
  logic [ 1:0] m1_bresp;
  logic        m1_arvalid, m1_arready;
  logic [31:0] m1_araddr;
  logic [ 2:0] m1_arprot;
  logic        m1_rvalid, m1_rready;
  logic [31:0] m1_rdata;
  logic [ 1:0] m1_rresp;
  logic        m1_busy;

  // ---- the shared bus downstream of the arbiter ---------------------------
  logic        s_awvalid, s_awready;
  logic [31:0] s_awaddr;
  logic [ 2:0] s_awprot;
  logic        s_wvalid, s_wready;
  logic [31:0] s_wdata;
  logic [ 3:0] s_wstrb;
  logic        s_bvalid, s_bready;
  logic [ 1:0] s_bresp;
  logic        s_arvalid, s_arready;
  logic [31:0] s_araddr;
  logic [ 2:0] s_arprot;
  logic        s_rvalid, s_rready;
  logic [31:0] s_rdata;
  logic [ 1:0] s_rresp;

  logic [1:0] grant;
  logic       arb_locked;

  arbiter arb (
      .aclk      (aclk),
      .aresetn   (aresetn),
      .m0_awvalid(m0_awvalid),
      .m1_awvalid(m1_awvalid),
      .m1_busy   (m1_busy),
      .s_bvalid  (s_bvalid),
      .s_bready  (s_bready),
      .grant     (grant),
      .locked    (arb_locked)
  );

  // What each master is allowed to start. This is §8.16's `arb_hold`, and the
  // signal the causal chain crosses on its way from m0 to m1.
  wire m0_grant_ok = (grant == 2'd0);
  wire m1_grant_ok = 1'b1;  // highest priority: it asks whenever it has work

  wire        sel1 = (grant == 2'd1);

  // Address/data path: whoever holds the grant drives the slave.
  assign s_awvalid = sel1 ? m1_awvalid : m0_awvalid;
  assign s_awaddr  = sel1 ? m1_awaddr : m0_awaddr;
  assign s_awprot  = sel1 ? m1_awprot : m0_awprot;
  assign s_wvalid  = sel1 ? m1_wvalid : m0_wvalid;
  assign s_wdata   = sel1 ? m1_wdata : m0_wdata;
  assign s_wstrb   = sel1 ? m1_wstrb : m0_wstrb;
  assign s_bready  = sel1 ? m1_bready : m0_bready;
  assign s_arvalid = sel1 ? m1_arvalid : m0_arvalid;
  assign s_araddr  = sel1 ? m1_araddr : m0_araddr;
  assign s_arprot  = sel1 ? m1_arprot : m0_arprot;
  assign s_rready  = sel1 ? m1_rready : m0_rready;

  // Responses go back only to the master that owns the bus.
  assign m0_awready = sel1 ? 1'b0 : s_awready;
  assign m0_wready  = sel1 ? 1'b0 : s_wready;
  assign m0_bvalid  = sel1 ? 1'b0 : s_bvalid;
  assign m0_bresp   = s_bresp;
  assign m0_arready = sel1 ? 1'b0 : s_arready;
  assign m0_rvalid  = sel1 ? 1'b0 : s_rvalid;
  assign m0_rdata   = s_rdata;
  assign m0_rresp   = s_rresp;

  assign m1_awready = sel1 ? s_awready : 1'b0;
  assign m1_wready  = sel1 ? s_wready : 1'b0;
  assign m1_bvalid  = sel1 ? s_bvalid : 1'b0;
  assign m1_bresp   = s_bresp;
  assign m1_arready = sel1 ? s_arready : 1'b0;
  assign m1_rvalid  = sel1 ? s_rvalid : 1'b0;
  assign m1_rdata   = s_rdata;
  assign m1_rresp   = s_rresp;

  arb_master #(
      .N_TXN   (10),
      .BASE    (32'h0000_0000),
      .DO_READS(1'b0)
  ) m0 (
      .aclk    (aclk),
      .aresetn (aresetn),
      .grant_ok(m0_grant_ok),
      .awvalid (m0_awvalid),
      .awready (m0_awready),
      .awaddr  (m0_awaddr),
      .awprot  (m0_awprot),
      .wvalid  (m0_wvalid),
      .wready  (m0_wready),
      .wdata   (m0_wdata),
      .wstrb   (m0_wstrb),
      .bvalid  (m0_bvalid),
      .bready  (m0_bready),
      .bresp   (m0_bresp),
      .arvalid (m0_arvalid),
      .arready (m0_arready),
      .araddr  (m0_araddr),
      .arprot  (m0_arprot),
      .rvalid  (m0_rvalid),
      .rready  (m0_rready),
      .rdata   (m0_rdata),
      .rresp   (m0_rresp),
      .busy    (m0_busy)
  );

  arb_master #(
      .N_TXN   (10),
      .BASE    (32'h0000_0000),
      .DO_READS(1'b0)
  ) m1 (
      .aclk    (aclk),
      .aresetn (aresetn),
      .grant_ok(m1_grant_ok),
      .awvalid (m1_awvalid),
      .awready (m1_awready),
      .awaddr  (m1_awaddr),
      .awprot  (m1_awprot),
      .wvalid  (m1_wvalid),
      .wready  (m1_wready),
      .wdata   (m1_wdata),
      .wstrb   (m1_wstrb),
      .bvalid  (m1_bvalid),
      .bready  (m1_bready),
      .bresp   (m1_bresp),
      .arvalid (m1_arvalid),
      .arready (m1_arready),
      .araddr  (m1_araddr),
      .arprot  (m1_arprot),
      .rvalid  (m1_rvalid),
      .rready  (m1_rready),
      .rdata   (m1_rdata),
      .rresp   (m1_rresp),
      .busy    (m1_busy)
  );

  // Eight wait states: master 1 holds the bus long enough that master 0 is
  // visibly starved rather than merely delayed.
  arb_slave #(
      .WAIT_STATES(8)
  ) slv (
      .aclk   (aclk),
      .aresetn(aresetn),
      .awvalid(s_awvalid),
      .awready(s_awready),
      .awaddr (s_awaddr),
      .awprot (s_awprot),
      .wvalid (s_wvalid),
      .wready (s_wready),
      .wdata  (s_wdata),
      .wstrb  (s_wstrb),
      .bvalid (s_bvalid),
      .bready (s_bready),
      .bresp  (s_bresp),
      .arvalid(s_arvalid),
      .arready(s_arready),
      .araddr (s_araddr),
      .arprot (s_arprot),
      .rvalid (s_rvalid),
      .rready (s_rready),
      .rdata  (s_rdata),
      .rresp  (s_rresp)
  );

  initial begin
    $dumpfile("dump.vcd");
    $dumpvars(0, tb_axi_arb);
    repeat (4) @(posedge aclk);
    aresetn <= 1'b1;
    fork
      begin
        wait (aresetn && !m0_busy && !m1_busy);
        repeat (10) @(posedge aclk);
      end
      repeat (4000) @(posedge aclk);
    join_any
    $display("axi_arb: finished at %0t", $time);
    $finish;
  end

endmodule
