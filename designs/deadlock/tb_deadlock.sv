`timescale 1ns / 1ps
//
// Two mailbox nodes writing to each other over AXI4-Lite — the §8.18 design.
//
//     node0.master ---- link A ----> node1.slave
//     node1.master ---- link B ----> node0.slave
//
// Two links, so two AXI4-Lite interfaces, so two agents. With the bug injected
// each node refuses the incoming write while its own outgoing one is still
// open, and the two wait on each other for the rest of the run:
//
//     link A waits on  node1's awready   held by  link B
//     link B waits on  node0's awready   held by  link A
//                                                   `-- cycle
//
// Injecting and disabling it is one define, because "no deadlock found" has to
// be as checkable as "deadlock found":
//
//     iverilog -g2012 -o sim.vvp *.sv                   -> dump.vcd     deadlocked
//     iverilog -g2012 -DNO_DEADLOCK -o sim_ok.vvp *.sv  -> dump_ok.vcd  correct
//
// A define rather than a runtime flag on purpose: the bug is then a property of
// the elaborated design, so it is visible in the graph and in `why`, which is
// where someone debugging it would actually meet it.
//
// Both nodes start on the same cycle: a deadlock needs the requests to cross,
// and staggering them would let one finish first and hide the bug behind timing.
//
module tb_deadlock;

`ifdef NO_DEADLOCK
  localparam bit HOLD = 1'b0;
`else
  localparam bit HOLD = 1'b1;
`endif

  // Long enough that the blockage is unambiguously past the hundred-cycle
  // threshold §8.18 uses, short enough to stay a small dump.
  localparam int TIMEOUT_CYCLES = 600;

  logic aclk = 1'b0;
  logic aresetn = 1'b0;

  always #5 aclk = ~aclk;  // 100 MHz

  // ---- link A: node0's master port into node1's slave port ---------------
  logic        a_awvalid, a_awready;
  logic [31:0] a_awaddr;
  logic [ 2:0] a_awprot;
  logic        a_wvalid, a_wready;
  logic [31:0] a_wdata;
  logic [ 3:0] a_wstrb;
  logic        a_bvalid, a_bready;
  logic [ 1:0] a_bresp;
  logic        a_arvalid, a_arready;
  logic [31:0] a_araddr;
  logic [ 2:0] a_arprot;
  logic        a_rvalid, a_rready;
  logic [31:0] a_rdata;
  logic [ 1:0] a_rresp;

  // ---- link B: node1's master port into node0's slave port ---------------
  logic        b_awvalid, b_awready;
  logic [31:0] b_awaddr;
  logic [ 2:0] b_awprot;
  logic        b_wvalid, b_wready;
  logic [31:0] b_wdata;
  logic [ 3:0] b_wstrb;
  logic        b_bvalid, b_bready;
  logic [ 1:0] b_bresp;
  logic        b_arvalid, b_arready;
  logic [31:0] b_araddr;
  logic [ 2:0] b_arprot;
  logic        b_rvalid, b_rready;
  logic [31:0] b_rdata;
  logic [ 1:0] b_rresp;

  logic n0_busy, n1_busy;

  // node1 starts late so the run has real traffic before it seizes up: node0
  // gets several writes through while node1 is still idle, and the deadlock
  // then happens on a bus that had been working. A trace that is dead from the
  // first cycle would let a detector "find" it by noticing nothing ever moved.
  mailbox_node #(
      .N_TXN      (16),
      .WAIT_STATES(2),
      .PEER_BASE  (32'h0000_0000),
      .START_DELAY(0),
      .HOLD       (HOLD)
  ) node0 (
      .aclk     (aclk),
      .aresetn  (aresetn),
      .m_awvalid(a_awvalid),
      .m_awready(a_awready),
      .m_awaddr (a_awaddr),
      .m_awprot (a_awprot),
      .m_wvalid (a_wvalid),
      .m_wready (a_wready),
      .m_wdata  (a_wdata),
      .m_wstrb  (a_wstrb),
      .m_bvalid (a_bvalid),
      .m_bready (a_bready),
      .m_bresp  (a_bresp),
      .m_arvalid(a_arvalid),
      .m_arready(a_arready),
      .m_araddr (a_araddr),
      .m_arprot (a_arprot),
      .m_rvalid (a_rvalid),
      .m_rready (a_rready),
      .m_rdata  (a_rdata),
      .m_rresp  (a_rresp),
      .s_awvalid(b_awvalid),
      .s_awready(b_awready),
      .s_awaddr (b_awaddr),
      .s_awprot (b_awprot),
      .s_wvalid (b_wvalid),
      .s_wready (b_wready),
      .s_wdata  (b_wdata),
      .s_wstrb  (b_wstrb),
      .s_bvalid (b_bvalid),
      .s_bready (b_bready),
      .s_bresp  (b_bresp),
      .s_arvalid(b_arvalid),
      .s_arready(b_arready),
      .s_araddr (b_araddr),
      .s_arprot (b_arprot),
      .s_rvalid (b_rvalid),
      .s_rready (b_rready),
      .s_rdata  (b_rdata),
      .s_rresp  (b_rresp),
      .busy     (n0_busy)
  );

  mailbox_node #(
      .N_TXN      (16),
      .WAIT_STATES(2),
      .PEER_BASE  (32'h0000_0000),
      .START_DELAY(20),
      .HOLD       (HOLD)
  ) node1 (
      .aclk     (aclk),
      .aresetn  (aresetn),
      .m_awvalid(b_awvalid),
      .m_awready(b_awready),
      .m_awaddr (b_awaddr),
      .m_awprot (b_awprot),
      .m_wvalid (b_wvalid),
      .m_wready (b_wready),
      .m_wdata  (b_wdata),
      .m_wstrb  (b_wstrb),
      .m_bvalid (b_bvalid),
      .m_bready (b_bready),
      .m_bresp  (b_bresp),
      .m_arvalid(b_arvalid),
      .m_arready(b_arready),
      .m_araddr (b_araddr),
      .m_arprot (b_arprot),
      .m_rvalid (b_rvalid),
      .m_rready (b_rready),
      .m_rdata  (b_rdata),
      .m_rresp  (b_rresp),
      .s_awvalid(a_awvalid),
      .s_awready(a_awready),
      .s_awaddr (a_awaddr),
      .s_awprot (a_awprot),
      .s_wvalid (a_wvalid),
      .s_wready (a_wready),
      .s_wdata  (a_wdata),
      .s_wstrb  (a_wstrb),
      .s_bvalid (a_bvalid),
      .s_bready (a_bready),
      .s_bresp  (a_bresp),
      .s_arvalid(a_arvalid),
      .s_arready(a_arready),
      .s_araddr (a_araddr),
      .s_arprot (a_arprot),
      .s_rvalid (a_rvalid),
      .s_rready (a_rready),
      .s_rdata  (a_rdata),
      .s_rresp  (a_rresp),
      .busy     (n1_busy)
  );

  int cycles = 0;
  always_ff @(posedge aclk) cycles <= cycles + 1;

  initial begin
`ifdef NO_DEADLOCK
    $dumpfile("dump_ok.vcd");
`else
    $dumpfile("dump.vcd");
`endif
    $dumpvars(0, tb_deadlock);

    repeat (4) @(posedge aclk);
    aresetn <= 1'b1;

    fork
      begin
        wait (aresetn && !n0_busy && !n1_busy);
        @(posedge aclk);
        $display("[tb_deadlock] both nodes finished at cycle %0d", cycles);
      end
      begin
        // The deadlocking run never finishes, so it has to be cut off. Saying
        // so in the log matters: a dump that simply ends is indistinguishable
        // from one where the simulation crashed.
        repeat (TIMEOUT_CYCLES) @(posedge aclk);
        $display("[tb_deadlock] TIMEOUT at cycle %0d: n0_busy=%0b n1_busy=%0b",
                 cycles, n0_busy, n1_busy);
        $display("[tb_deadlock] link A: awvalid=%0b awready=%0b   link B: awvalid=%0b awready=%0b",
                 a_awvalid, a_awready, b_awvalid, b_awready);
      end
    join_any

    repeat (4) @(posedge aclk);
    $finish;
  end

endmodule
