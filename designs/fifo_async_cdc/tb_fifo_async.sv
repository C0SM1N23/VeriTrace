`timescale 1ns / 1ps

module tb_fifo_async;
    logic wr_clk = 1'b0;
    logic rd_clk = 1'b0;
    logic rst_n = 1'b0;
    logic wr_en = 1'b0;
    logic empty;
    logic [3:0] wr_gray_seen;

    always #4 wr_clk = ~wr_clk;
    always #5 rd_clk = ~rd_clk;
    fifo_async dut (.*);

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_fifo_async);
        #13 rst_n = 1'b1;
        #3 wr_en = 1'b1;
        repeat (8) @(posedge wr_clk);
        wr_en = 1'b0;
        repeat (8) @(posedge rd_clk);
        $finish;
    end
endmodule
