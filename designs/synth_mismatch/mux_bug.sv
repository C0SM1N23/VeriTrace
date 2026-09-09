`timescale 1ns / 1ps

// One deliberate sim/synth mismatch, of the kind §8.29 exists to prove.
//
// THE BUG is on line 20: the sensitivity list names `sel` and nothing else.
//
//   - **A simulator** does exactly what is written. `y` is recomputed only when
//     `sel` changes, so a change on `a` or `b` alone is not seen and `y` holds a
//     stale value until the next time `sel` moves.
//   - **A synthesiser** has no notion of a sensitivity list for combinational
//     logic. It reads the body, sees a multiplexer, and builds one — sensitive
//     to all three inputs, as the name `always @*` would have given.
//
// The RTL simulation and the silicon therefore disagree, and no amount of
// simulating the RTL can show it: the testbench passes, because the testbench is
// checking the same wrong thing the RTL says. §8.11 flags the *pattern*; this
// design is here so §8.29 can prove the behaviour differs.
module mux_bug (
    input  logic       clk,
    input  logic       sel,
    input  logic [7:0] a,
    input  logic [7:0] b,
    output logic [7:0] y,
    output logic [7:0] y_ok
);

  // BUG: `a` and `b` are missing from the list.
  always @(sel) begin
    y = sel ? a : b;
  end

  // The control also evaluates at time zero: declaration initializers can run
  // before an always @* starts waiting, leaving its output X until an input moves.
  always_comb begin
    y_ok = sel ? a : b;
  end

endmodule
