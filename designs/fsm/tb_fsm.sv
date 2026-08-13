`timescale 1ns / 1ps

// Drives the six machines of `fsm_dut.sv` gently, and that is the point.
//
// §8.8's claim is that the static checks find what a simulation cannot: the
// stimulus below deliberately never asserts `err`, never asserts `grant`, and
// never reaches the second half of any machine. The run passes, the waveform
// looks healthy — and `veritrace check --rtl designs/fsm` still reports all
// five injected flaws, because it reads the RTL rather than the run.
//
// The dump exists so the §8.8 step 5 overlay has something to colour, and so
// the reference designs stay uniform. No check here depends on it.
module tb_fsm;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    always #5 clk = ~clk;

    logic go = 1'b0, ack = 1'b0, ready = 1'b0, start = 1'b0, tick = 1'b0;
    // Never asserted: the states they would reach are the ones the checks find
    // without ever going there.
    logic err = 1'b0, grant = 1'b0;

    logic busy, trapped, active, done, waiting;
    logic [3:0] ticks;

    fsm_good        good       (.clk, .rst_n, .go, .ack, .busy);
    fsm_dead        bad_dead   (.clk, .rst_n, .err, .trapped);
    fsm_unreachable bad_unreach(.clk, .rst_n, .go, .active);
    fsm_typo        bad_typo   (.clk, .rst_n, .ready, .done);
    fsm_partial     bad_partial(.clk, .rst_n, .start, .grant, .waiting);
    fsm_unreset     bad_reset  (.clk, .rst_n, .tick, .ticks);

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_fsm);

        repeat (3) @(posedge clk);
        rst_n <= 1'b1;

        repeat (2) @(posedge clk);
        go    <= 1'b1;
        ready <= 1'b1;
        start <= 1'b1;
        tick  <= 1'b1;

        repeat (4) @(posedge clk);
        ack <= 1'b1;

        repeat (30) @(posedge clk);
        $display("tb_fsm: done, no assertion fired");
        $finish;
    end
endmodule
