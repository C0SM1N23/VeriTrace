`timescale 1ns / 1ps
//
// A fixed-priority arbiter between two AXI4-Lite write masters.
//
// Master 1 wins whenever it is asking, and keeps the bus until its write
// response has been taken. Master 0 therefore only issues in the gaps, which is
// the contention §8.16 walks through: "arb_hold = 1 (blocked since c1187)" ->
// "grant_owner = 2" -> "a correlated transaction on another master is holding
// the arbiter".
//
// The grant is deliberately *observable* — a combinational function of the two
// masters' `awvalid` and of the locked owner — because that is what makes the
// causal chain cross from one master's signal into the other master's
// transaction rather than stopping at an opaque state machine.
//
module arbiter (
    input  logic       aclk,
    input  logic       aresetn,
    input  logic       m0_awvalid,
    input  logic       m1_awvalid,
    // High while master 1 still has work to do, so the grant does not flap in
    // the gap between one of its transactions and the next. Without it master 0
    // would slip in every few cycles and there would be no starvation to trace.
    input  logic       m1_busy,
    // The shared write-response handshake: the bus is free again once it fires.
    input  logic       s_bvalid,
    input  logic       s_bready,
    output logic [1:0] grant,
    output logic       locked
);

  logic       lock;
  logic [1:0] owner;

  // Fixed priority, not round-robin: starvation is the point of this design.
  wire [1:0] pick = (m1_awvalid || m1_busy) ? 2'd1 : 2'd0;

  assign grant  = lock ? owner : pick;
  assign locked = lock;

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      lock  <= 1'b0;
      owner <= 2'd0;
    end else if (!lock && (m0_awvalid || m1_awvalid)) begin
      lock  <= 1'b1;
      owner <= pick;
    end else if (lock && s_bvalid && s_bready) begin
      lock <= 1'b0;
    end
  end

endmodule
