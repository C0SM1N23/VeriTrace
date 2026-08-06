`timescale 1ns / 1ps
//
// An AXI4-Lite master that writes a register and reads it back, N times.
//
// `grant_ok` gates only the *decision to start* a transaction, never a valid
// that is already asserted — withdrawing valid before ready is the protocol
// error the packs' NODROP rule exists to find, and a test design must not
// commit it by accident.
//
module arb_master #(
    parameter int          N_TXN     = 12,
    parameter logic [31:0] BASE      = 32'h0000_0000,
    // 0 lets a master issue writes only, which is what makes one master hold an
    // arbiter for a visible stretch in designs/axi_arb.
    parameter bit          DO_READS  = 1'b1,
    // One transaction aimed at an unmapped address, so a run exercises the
    // response-code rules as well as the happy path. 0 disables it.
    parameter int          BAD_AT    = -1
) (
    input  logic        aclk,
    input  logic        aresetn,
    // High when the arbiter (if any) allows this master to start.
    input  logic        grant_ok,

    output logic        awvalid,
    input  logic        awready,
    output logic [31:0] awaddr,
    output logic [ 2:0] awprot,

    output logic        wvalid,
    input  logic        wready,
    output logic [31:0] wdata,
    output logic [ 3:0] wstrb,

    input  logic        bvalid,
    output logic        bready,
    input  logic [ 1:0] bresp,

    output logic        arvalid,
    input  logic        arready,
    output logic [31:0] araddr,
    output logic [ 2:0] arprot,

    input  logic        rvalid,
    output logic        rready,
    input  logic [31:0] rdata,
    input  logic [ 1:0] rresp,

    // High from reset until the last transaction has completed.
    output logic        busy
);

  typedef enum logic [2:0] {
    IDLE,
    WRITE_A,
    WRITE_R,
    READ_A,
    READ_D,
    FINISHED
  } state_e;

  state_e     state;
  logic [7:0] cnt;
  logic       aw_done, w_done;

  wire        bad = (BAD_AT >= 0) && (cnt == BAD_AT[7:0]);

  assign awvalid = (state == WRITE_A) && !aw_done;
  assign wvalid  = (state == WRITE_A) && !w_done;
  assign bready  = (state == WRITE_R);
  assign arvalid = (state == READ_A);
  assign rready  = (state == READ_D);

  assign awprot  = 3'b000;
  assign arprot  = 3'b000;
  assign wstrb   = 4'hF;
  assign awaddr  = BASE + (bad ? 32'h20 : (cnt[0] ? 32'h4 : 32'h0));
  assign araddr  = awaddr;
  assign wdata   = {24'h0, cnt};
  assign busy    = (state != FINISHED);

  wire aw_ok = aw_done || (awvalid && awready);
  wire w_ok  = w_done  || (wvalid  && wready);

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      state   <= IDLE;
      cnt     <= '0;
      aw_done <= 1'b0;
      w_done  <= 1'b0;
    end else begin
      case (state)
        IDLE: begin
          if (cnt >= N_TXN[7:0]) state <= FINISHED;
          else if (grant_ok) state <= WRITE_A;
        end
        WRITE_A: begin
          if (awvalid && awready) aw_done <= 1'b1;
          if (wvalid && wready) w_done <= 1'b1;
          if (aw_ok && w_ok) begin
            state   <= WRITE_R;
            aw_done <= 1'b0;
            w_done  <= 1'b0;
          end
        end
        WRITE_R:
          if (bvalid) state <= DO_READS ? READ_A : IDLE;
        READ_A:
          if (arready) state <= READ_D;
        READ_D:
          if (rvalid) state <= IDLE;
        FINISHED: state <= FINISHED;
        default:  state <= IDLE;
      endcase

      // One transaction is one write, plus its read-back when reads are on.
      if ((state == WRITE_R && bvalid && !DO_READS) || (state == READ_D && rvalid))
        cnt <= cnt + 1'b1;
    end
  end

endmodule
