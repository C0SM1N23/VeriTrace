`timescale 1ns / 1ps
//
// One AXI4-Lite master against one two-register slave: the reference design for
// the §8.14 extraction engine.
//
// 12 writes and 12 read-backs, one of them aimed at an unmapped address so the
// pack's response rules have something real to fire on. Small enough that every
// transaction the tool reports can be checked by hand against this file, which
// is what the acceptance criterion of Prompt 9 asks for.
//
module tb_axi_lite;

  logic aclk = 1'b0;
  logic aresetn = 1'b0;

  always #5 aclk = ~aclk;  // 100 MHz

  // The bus. Named with a prefix so the interface is also detectable at this
  // level, not only inside the two instances.
  logic        s_axi_awvalid, s_axi_awready;
  logic [31:0] s_axi_awaddr;
  logic [ 2:0] s_axi_awprot;
  logic        s_axi_wvalid, s_axi_wready;
  logic [31:0] s_axi_wdata;
  logic [ 3:0] s_axi_wstrb;
  logic        s_axi_bvalid, s_axi_bready;
  logic [ 1:0] s_axi_bresp;
  logic        s_axi_arvalid, s_axi_arready;
  logic [31:0] s_axi_araddr;
  logic [ 2:0] s_axi_arprot;
  logic        s_axi_rvalid, s_axi_rready;
  logic [31:0] s_axi_rdata;
  logic [ 1:0] s_axi_rresp;

  logic        cpu_busy;

  axil_master #(
      .N_TXN (12),
      .BASE  (32'h0000_0000),
      .BAD_AT(9)
  ) cpu (
      .aclk    (aclk),
      .aresetn (aresetn),
      .grant_ok(1'b1),
      .awvalid (s_axi_awvalid),
      .awready (s_axi_awready),
      .awaddr  (s_axi_awaddr),
      .awprot  (s_axi_awprot),
      .wvalid  (s_axi_wvalid),
      .wready  (s_axi_wready),
      .wdata   (s_axi_wdata),
      .wstrb   (s_axi_wstrb),
      .bvalid  (s_axi_bvalid),
      .bready  (s_axi_bready),
      .bresp   (s_axi_bresp),
      .arvalid (s_axi_arvalid),
      .arready (s_axi_arready),
      .araddr  (s_axi_araddr),
      .arprot  (s_axi_arprot),
      .rvalid  (s_axi_rvalid),
      .rready  (s_axi_rready),
      .rdata   (s_axi_rdata),
      .rresp   (s_axi_rresp),
      .busy    (cpu_busy)
  );

  axil_slave #(
      .WAIT_STATES(2)
  ) regs (
      .aclk   (aclk),
      .aresetn(aresetn),
      .awvalid(s_axi_awvalid),
      .awready(s_axi_awready),
      .awaddr (s_axi_awaddr),
      .awprot (s_axi_awprot),
      .wvalid (s_axi_wvalid),
      .wready (s_axi_wready),
      .wdata  (s_axi_wdata),
      .wstrb  (s_axi_wstrb),
      .bvalid (s_axi_bvalid),
      .bready (s_axi_bready),
      .bresp  (s_axi_bresp),
      .arvalid(s_axi_arvalid),
      .arready(s_axi_arready),
      .araddr (s_axi_araddr),
      .arprot (s_axi_arprot),
      .rvalid (s_axi_rvalid),
      .rready (s_axi_rready),
      .rdata  (s_axi_rdata),
      .rresp  (s_axi_rresp)
  );

  initial begin
    $dumpfile("dump.vcd");
    $dumpvars(0, tb_axi_lite);
    repeat (4) @(posedge aclk);
    aresetn <= 1'b1;
    // Run until the master is done, with a hard stop so a bug here cannot turn
    // into an unbounded dump.
    fork
      begin
        wait (aresetn && !cpu_busy);
        repeat (10) @(posedge aclk);
      end
      repeat (4000) @(posedge aclk);
    join_any
    $display("axi_lite: finished at %0t", $time);
    $finish;
  end

endmodule
