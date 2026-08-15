`timescale 1ns / 1ps

// §8.1's CONFLICT terminal and §8.5's hardest X: two drivers fighting over one
// net. Deliberately arranged so the conflict starts *after* reset and clears in
// between, because an X that is only sampled at one instant after reset is the
// case the X scan used to drop silently.
//
// `y` is driven by two continuous assignments. While `a == b` the net resolves
// cleanly; the moment they disagree it is X, and no amount of waiting makes it
// settle. That is a real bug class — two modules driving one wire — and the
// only kind of X whose cause the tool can state with certainty.
module conflict_dut (
    input  logic clk,
    input  logic rst_n,
    input  logic a,
    input  logic b,
    output wire  y,
    output logic tied_off
);
    assign y = a;   // driver 1
    assign y = b;   // driver 2 — same net, on purpose

    // A tie-off, so the CONSTANT terminal has a case here too.
    assign tied_off = 1'b0;
endmodule

module tb_multidriver;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic a = 1'b0;
    logic b = 1'b0;
    wire  y;
    logic tied_off;

    conflict_dut dut (
        .clk(clk),
        .rst_n(rst_n),
        .a(a),
        .b(b),
        .y(y),
        .tied_off(tied_off)
    );

    always #5 clk = ~clk;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_multidriver);
        // Reset window: the drivers agree, so `y` is clean here.
        #20 rst_n = 1'b1;
        // A brief disagreement that clears again — this is what a scan
        // anchored on one sample after reset would call "the design
        // initialising" and drop.
        #10 a = 1'b1;
        #10 b = 1'b1;
        // ...and the real one, well clear of reset, which must be reported.
        #40 a = 1'b0;
        #60 $finish;
    end
endmodule
