`timescale 1ns / 1ps
//
// A 16-word AXI4-Lite RAM. Deliberately correct, and in particular *correct
// about `wstrb`*: it writes exactly the byte lanes it is told to and leaves the
// rest alone.
//
// That matters for §8.19. The bug in this design is upstream, in dma_bridge;
// if the memory quietly ignored the byte enables it would paper over it, and
// the scoreboard would have nothing to find.
//
module axil_mem #(
    parameter int WORDS = 16
) (
    input  logic        aclk,
    input  logic        aresetn,

    input  logic        awvalid,
    output logic        awready,
    input  logic [31:0] awaddr,
    input  logic [ 2:0] awprot,

    input  logic        wvalid,
    output logic        wready,
    input  logic [31:0] wdata,
    input  logic [ 3:0] wstrb,

    output logic        bvalid,
    input  logic        bready,
    output logic [ 1:0] bresp,

    input  logic        arvalid,
    output logic        arready,
    input  logic [31:0] araddr,
    input  logic [ 2:0] arprot,

    output logic        rvalid,
    input  logic        rready,
    output logic [31:0] rdata,
    output logic [ 1:0] rresp
);

  localparam logic [1:0] OKAY   = 2'b00;
  localparam logic [1:0] SLVERR = 2'b10;

  logic [31:0] mem[0:WORDS-1];

  wire [3:0] wsel = awaddr[5:2];
  wire [3:0] rsel = araddr[5:2];

  // ---- write --------------------------------------------------------------

  logic wbusy;
  assign awready = !wbusy && awvalid && wvalid;
  assign wready  = !wbusy && awvalid && wvalid;

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      wbusy  <= 1'b0;
      bvalid <= 1'b0;
      bresp  <= OKAY;
      for (int i = 0; i < WORDS; i++) mem[i] <= 32'h0;
    end else if (!wbusy) begin
      if (awvalid && wvalid) begin
        wbusy  <= 1'b1;
        bvalid <= 1'b1;
        if (wsel < WORDS) begin
          // Per byte lane, as a real memory does.
          for (int b = 0; b < 4; b++)
            if (wstrb[b]) mem[wsel][8*b+:8] <= wdata[8*b+:8];
          bresp <= OKAY;
        end else begin
          bresp <= SLVERR;
        end
      end
    end else if (bready) begin
      wbusy  <= 1'b0;
      bvalid <= 1'b0;
    end
  end

  // ---- read ---------------------------------------------------------------

  logic rbusy;
  assign arready = !rbusy && arvalid;

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      rbusy  <= 1'b0;
      rvalid <= 1'b0;
      rdata  <= 32'h0;
      rresp  <= OKAY;
    end else if (!rbusy) begin
      if (arvalid) begin
        rbusy  <= 1'b1;
        rvalid <= 1'b1;
        if (rsel < WORDS) begin
          rdata <= mem[rsel];
          rresp <= OKAY;
        end else begin
          rdata <= 32'h0;
          rresp <= SLVERR;
        end
      end
    end else if (rready) begin
      rbusy  <= 1'b0;
      rvalid <= 1'b0;
    end
  end

endmodule
