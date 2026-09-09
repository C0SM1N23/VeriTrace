`timescale 1ns / 1ps

module tb_arbiter;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic request = 1'b0;
    logic release_req = 1'b0;
    logic grant;
    logic lock_r;

    always #5 clk = ~clk;

    arbiter dut (.*);

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_arbiter);
        #12 rst_n = 1'b1;       // deliberately between active clock edges
        #13 request = 1'b1;
        #10 request = 1'b0;
        #10 release_req = 1'b1;
        #10 release_req = 1'b0;
        repeat (130) @(posedge clk);
        if (lock_r !== 1'b1) $fatal(1, "injected lock bug did not reproduce");
        $finish;
    end
endmodule
