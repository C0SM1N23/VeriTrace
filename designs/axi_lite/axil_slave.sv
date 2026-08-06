`timescale 1ns / 1ps
//
// An AXI4-Lite slave with two 32-bit registers, and a configurable number of
// wait states so a trace has something other than zero-latency handshakes in it.
//
// Deliberately ordinary: this design exists to be *correct*, so that anything
// the transaction engine reports about it can be checked by hand against the
// source. The flawed designs live in designs/checks and designs/fifo_buggy.
//
module axil_slave #(
    // Cycles the slave makes a master wait before accepting an address phase.
    parameter int WAIT_STATES = 0
) (
    input  logic        aclk,
    input  logic        aresetn,

    // write address
    input  logic        awvalid,
    output logic        awready,
    input  logic [31:0] awaddr,
    input  logic [ 2:0] awprot,

    // write data
    input  logic        wvalid,
    output logic        wready,
    input  logic [31:0] wdata,
    input  logic [ 3:0] wstrb,

    // write response
    output logic        bvalid,
    input  logic        bready,
    output logic [ 1:0] bresp,

    // read address
    input  logic        arvalid,
    output logic        arready,
    input  logic [31:0] araddr,
    input  logic [ 2:0] arprot,

    // read data
    output logic        rvalid,
    input  logic        rready,
    output logic [31:0] rdata,
    output logic [ 1:0] rresp
);

  localparam logic [1:0] OKAY   = 2'b00;
  localparam logic [1:0] SLVERR = 2'b10;

  logic [31:0] regs [0:1];

  // ---- write channel ------------------------------------------------------

  localparam logic W_IDLE = 1'b0, W_RESP = 1'b1;
  logic       wstate;
  logic [3:0] wcnt;

  // AXI permits ready to depend on valid, which keeps a two-register slave to
  // one state bit instead of a skid buffer.
  wire w_settled = (wstate == W_IDLE) && (wcnt >= WAIT_STATES[3:0]);
  assign awready = w_settled && awvalid && wvalid;
  assign wready  = w_settled && awvalid && wvalid;

  wire [3:0] waddr_sel = awaddr[5:2];

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      wstate  <= W_IDLE;
      wcnt    <= '0;
      bvalid  <= 1'b0;
      bresp   <= OKAY;
      regs[0] <= 32'h0;
      regs[1] <= 32'h0;
    end else begin
      case (wstate)
        W_IDLE:
          if (awvalid && wvalid) begin
            if (wcnt >= WAIT_STATES[3:0]) begin
              wcnt   <= '0;
              bvalid <= 1'b1;
              // Only 0x0 and 0x4 are mapped; anything else answers SLVERR,
              // which is what the pack's response rule is there to catch.
              if (waddr_sel < 4'd2) begin
                regs[waddr_sel[0]] <= wdata;
                bresp <= OKAY;
              end else begin
                bresp <= SLVERR;
              end
              wstate <= W_RESP;
            end else begin
              wcnt <= wcnt + 1'b1;
            end
          end else begin
            wcnt <= '0;
          end
        W_RESP:
          if (bready) begin
            bvalid <= 1'b0;
            wstate <= W_IDLE;
          end
        default: wstate <= W_IDLE;
      endcase
    end
  end

  // ---- read channel -------------------------------------------------------

  localparam logic R_IDLE = 1'b0, R_DATA = 1'b1;
  logic       rstate;
  logic [3:0] rcnt;

  wire r_settled = (rstate == R_IDLE) && (rcnt >= WAIT_STATES[3:0]);
  assign arready = r_settled && arvalid;

  wire [3:0] raddr_sel = araddr[5:2];

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      rstate <= R_IDLE;
      rcnt   <= '0;
      rvalid <= 1'b0;
      rdata  <= 32'h0;
      rresp  <= OKAY;
    end else begin
      case (rstate)
        R_IDLE:
          if (arvalid) begin
            if (rcnt >= WAIT_STATES[3:0]) begin
              rcnt   <= '0;
              rvalid <= 1'b1;
              if (raddr_sel < 4'd2) begin
                rdata <= regs[raddr_sel[0]];
                rresp <= OKAY;
              end else begin
                rdata <= 32'hDEAD_BEEF;
                rresp <= SLVERR;
              end
              rstate <= R_DATA;
            end else begin
              rcnt <= rcnt + 1'b1;
            end
          end else begin
            rcnt <= '0;
          end
        R_DATA:
          if (rready) begin
            rvalid <= 1'b0;
            rstate <= R_IDLE;
          end
        default: rstate <= R_IDLE;
      endcase
    end
  end

endmodule
