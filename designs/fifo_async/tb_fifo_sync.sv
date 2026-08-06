`timescale 1ns / 1ps

// Testbench for fifo_sync. Deliberately exercises the cases the trace core
// has to get right: X values before reset, combinational settling (full/empty
// glitch on the same timestamp as a pointer update), and array elements that
// only appear in the dump when named explicitly (§5.6).
module tb_fifo_sync;
    localparam int WIDTH = 8;
    localparam int DEPTH = 16;

    logic             clk = 1'b0;
    logic             rst_n;
    logic             wr_en, rd_en;
    logic [WIDTH-1:0] wr_data;
    logic [WIDTH-1:0] rd_data;
    logic             full, empty;

    fifo_sync #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (
        .clk(clk), .rst_n(rst_n),
        .wr_en(wr_en), .wr_data(wr_data),
        .rd_en(rd_en), .rd_data(rd_data),
        .full(full), .empty(empty)
    );

    always #5 clk = ~clk;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_fifo_sync);
        // Dumping unpacked-array elements needs an explicit element select,
        // which the LRM does not allow as a $dumpvars argument. Icarus accepts
        // it as an extension; slang and ModelSim reject it. Icarus predefines
        // __ICARUS__, so this stays an Icarus-only path and every other tool
        // reads the file cleanly.
`ifdef __ICARUS__
        $dumpvars(1, dut.mem[0], dut.mem[1], dut.mem[2], dut.mem[3]);
`endif
    end

    initial begin
        rst_n   = 1'b0;
        wr_en   = 1'b0;
        rd_en   = 1'b0;
        wr_data = '0;
        repeat (2) @(posedge clk);
        rst_n = 1'b1;

        // Fill past full to exercise the full flag.
        for (int i = 0; i < DEPTH + 2; i++) begin
            @(negedge clk);
            wr_en   = 1'b1;
            wr_data = 8'hA0 + i[7:0];
        end
        @(negedge clk);
        wr_en = 1'b0;

        // Drain past empty to exercise the empty flag.
        for (int i = 0; i < DEPTH + 2; i++) begin
            @(negedge clk);
            rd_en = 1'b1;
        end
        @(negedge clk);
        rd_en = 1'b0;

        // Simultaneous read+write, and back-to-back single-cycle pulses.
        repeat (4) begin
            @(negedge clk);
            wr_en   = 1'b1;
            rd_en   = 1'b1;
            wr_data = wr_data + 8'h11;
        end
        @(negedge clk);
        wr_en = 1'b0;
        rd_en = 1'b0;

        repeat (4) @(posedge clk);
        $finish;
    end
endmodule
