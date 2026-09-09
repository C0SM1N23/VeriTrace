`timescale 1ns / 1ps

module tb_gen_array;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic [31:0] lane_in = '0;
    logic [31:0] lane_out;

    always #5 clk = ~clk;
    gen_array dut (.*);

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_gen_array);
        #12 rst_n = 1'b1;
        lane_in = 32'h4433_2211;
        repeat (3) @(posedge clk);
        lane_in = 32'h8877_6655;
        repeat (3) @(posedge clk);
        $finish;
    end
endmodule
