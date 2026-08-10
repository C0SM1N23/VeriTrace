`timescale 1ns / 1ps
//
// §8.19's worked example, as a design: write a block through the DMA path, read
// it back, and see what arrived.
//
// Eight words go in at 0x00..0x1C, the first of them 0xDEADBEEF — §8.19's own
// value — and the same eight addresses are read back. The bridge in between
// drops two byte lanes, so every read-back returns its top half and zeros where
// its bottom half should be. Nothing here asserts anything: the point is that
// the tool finds it from the trace alone, with no testbench checking code, which
// is what "a free scoreboard" means.
//
// Two dumps come out of this one file: dump.vcd with the bug and dump_ok.vcd
// with -DNO_CORRUPTION, so "no corruption found" is as checkable as "found".
//
module tb_dma;

  localparam int N = 8;

  logic aclk = 1'b0;
  logic aresetn = 1'b0;

  always #5 aclk = ~aclk;  // 100 MHz

  // The CPU-facing bus, driven by this testbench.
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

  // The memory-facing bus, driven by the bridge.
  logic        m_axi_awvalid, m_axi_awready;
  logic [31:0] m_axi_awaddr;
  logic [ 2:0] m_axi_awprot;
  logic        m_axi_wvalid, m_axi_wready;
  logic [31:0] m_axi_wdata;
  logic [ 3:0] m_axi_wstrb;
  logic        m_axi_bvalid, m_axi_bready;
  logic [ 1:0] m_axi_bresp;
  logic        m_axi_arvalid, m_axi_arready;
  logic [31:0] m_axi_araddr;
  logic [ 2:0] m_axi_arprot;
  logic        m_axi_rvalid, m_axi_rready;
  logic [31:0] m_axi_rdata;
  logic [ 1:0] m_axi_rresp;

  dma_bridge dma (
      .aclk         (aclk),
      .aresetn      (aresetn),
      .s_axi_awvalid(s_axi_awvalid),
      .s_axi_awready(s_axi_awready),
      .s_axi_awaddr (s_axi_awaddr),
      .s_axi_awprot (s_axi_awprot),
      .s_axi_wvalid (s_axi_wvalid),
      .s_axi_wready (s_axi_wready),
      .s_axi_wdata  (s_axi_wdata),
      .s_axi_wstrb  (s_axi_wstrb),
      .s_axi_bvalid (s_axi_bvalid),
      .s_axi_bready (s_axi_bready),
      .s_axi_bresp  (s_axi_bresp),
      .s_axi_arvalid(s_axi_arvalid),
      .s_axi_arready(s_axi_arready),
      .s_axi_araddr (s_axi_araddr),
      .s_axi_arprot (s_axi_arprot),
      .s_axi_rvalid (s_axi_rvalid),
      .s_axi_rready (s_axi_rready),
      .s_axi_rdata  (s_axi_rdata),
      .s_axi_rresp  (s_axi_rresp),
      .m_axi_awvalid(m_axi_awvalid),
      .m_axi_awready(m_axi_awready),
      .m_axi_awaddr (m_axi_awaddr),
      .m_axi_awprot (m_axi_awprot),
      .m_axi_wvalid (m_axi_wvalid),
      .m_axi_wready (m_axi_wready),
      .m_axi_wdata  (m_axi_wdata),
      .m_axi_wstrb  (m_axi_wstrb),
      .m_axi_bvalid (m_axi_bvalid),
      .m_axi_bready (m_axi_bready),
      .m_axi_bresp  (m_axi_bresp),
      .m_axi_arvalid(m_axi_arvalid),
      .m_axi_arready(m_axi_arready),
      .m_axi_araddr (m_axi_araddr),
      .m_axi_arprot (m_axi_arprot),
      .m_axi_rvalid (m_axi_rvalid),
      .m_axi_rready (m_axi_rready),
      .m_axi_rdata  (m_axi_rdata),
      .m_axi_rresp  (m_axi_rresp)
  );

  axil_mem #(
      .WORDS(16)
  ) mem (
      .aclk   (aclk),
      .aresetn(aresetn),
      .awvalid(m_axi_awvalid),
      .awready(m_axi_awready),
      .awaddr (m_axi_awaddr),
      .awprot (m_axi_awprot),
      .wvalid (m_axi_wvalid),
      .wready (m_axi_wready),
      .wdata  (m_axi_wdata),
      .wstrb  (m_axi_wstrb),
      .bvalid (m_axi_bvalid),
      .bready (m_axi_bready),
      .bresp  (m_axi_bresp),
      .arvalid(m_axi_arvalid),
      .arready(m_axi_arready),
      .araddr (m_axi_araddr),
      .arprot (m_axi_arprot),
      .rvalid (m_axi_rvalid),
      .rready (m_axi_rready),
      .rdata  (m_axi_rdata),
      .rresp  (m_axi_rresp)
  );

  // Word i, with 0xDEADBEEF first and a distinct low byte after that, so no two
  // words are equal — §8.19's reason for tracking (address, sequence) instead of
  // the value is easier to see when the values happen to be unique anyway.
  function automatic logic [31:0] pattern(input int i);
    return {16'hDEAD, 8'hBE, 8'hEF - i[7:0]};
  endfunction

  task automatic axil_write(input logic [31:0] addr, input logic [31:0] data);
    begin
      @(posedge aclk);
      s_axi_awvalid <= 1'b1;
      s_axi_awaddr  <= addr;
      s_axi_awprot  <= 3'b000;
      s_axi_wvalid  <= 1'b1;
      s_axi_wdata   <= data;
      s_axi_wstrb   <= 4'b1111;
      s_axi_bready  <= 1'b1;
      @(posedge aclk);
      while (!(s_axi_awready && s_axi_wready)) @(posedge aclk);
      s_axi_awvalid <= 1'b0;
      s_axi_wvalid  <= 1'b0;
      while (!s_axi_bvalid) @(posedge aclk);
      @(posedge aclk);
      s_axi_bready <= 1'b0;
    end
  endtask

  task automatic axil_read(input logic [31:0] addr, output logic [31:0] data);
    begin
      @(posedge aclk);
      s_axi_arvalid <= 1'b1;
      s_axi_araddr  <= addr;
      s_axi_arprot  <= 3'b000;
      s_axi_rready  <= 1'b1;
      @(posedge aclk);
      while (!s_axi_arready) @(posedge aclk);
      s_axi_arvalid <= 1'b0;
      while (!s_axi_rvalid) @(posedge aclk);
      data = s_axi_rdata;
      @(posedge aclk);
      s_axi_rready <= 1'b0;
    end
  endtask

  logic [31:0] got;

  initial begin
`ifdef NO_CORRUPTION
    $dumpfile("dump_ok.vcd");
`else
    $dumpfile("dump.vcd");
`endif
    $dumpvars(0, tb_dma);

    s_axi_awvalid = 1'b0;
    s_axi_wvalid  = 1'b0;
    s_axi_bready  = 1'b0;
    s_axi_arvalid = 1'b0;
    s_axi_rready  = 1'b0;
    s_axi_awaddr  = 32'h0;
    s_axi_araddr  = 32'h0;
    s_axi_wdata   = 32'h0;
    s_axi_wstrb   = 4'b1111;
    s_axi_awprot  = 3'b000;
    s_axi_arprot  = 3'b000;

    repeat (4) @(posedge aclk);
    aresetn <= 1'b1;
    repeat (2) @(posedge aclk);

    for (int i = 0; i < N; i++) axil_write(i * 4, pattern(i));
    for (int i = 0; i < N; i++) begin
      axil_read(i * 4, got);
      $display("dma: read 0x%08h from 0x%02h (wrote 0x%08h)", got, i * 4, pattern(i));
    end

    repeat (10) @(posedge aclk);
    $display("dma: finished at %0t", $time);
    $finish;
  end

endmodule
