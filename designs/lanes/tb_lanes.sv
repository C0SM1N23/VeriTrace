`timescale 1ns / 1ps

module tb_lanes;
    localparam int NLANES = 4;
    localparam int WIDTH = 8;

    logic clk = 1'b0;
    logic rst_n;
    logic [NLANES-1:0] push;
    logic [WIDTH-1:0]  din;
    logic [NLANES-1:0] full, tick;
    logic [WIDTH-1:0]  dout0;

    lanes #(.NLANES(NLANES), .WIDTH(WIDTH)) dut (
        .clk(clk), .rst_n(rst_n), .push(push), .din(din),
        .full(full), .dout0(dout0), .tick(tick)
    );

    always #5 clk = ~clk;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_lanes);
        // Generate blocks and instance arrays put memories behind indexed
        // scopes; Icarus needs each element named explicitly.
        // Dumping unpacked-array elements needs an explicit element select,
        // which the LRM does not allow as a $dumpvars argument. Icarus accepts
        // it as an extension; slang and ModelSim reject it. Icarus predefines
        // __ICARUS__, so this stays an Icarus-only path and every other tool
        // reads the file cleanly.
`ifdef __ICARUS__
        $dumpvars(1, dut.g_lane[0].u_fifo.mem[0], dut.g_lane[1].u_fifo.mem[0]);
`endif
    end

    initial begin
        rst_n = 1'b0;
        push  = '0;
        din   = 8'h00;
        repeat (2) @(posedge clk);
        rst_n = 1'b1;

        for (int i = 0; i < 24; i++) begin
            @(negedge clk);
            push = NLANES'(i);
            din  = 8'h10 + i[7:0];
        end

        @(negedge clk);
        push = '1;
        repeat (20) @(negedge clk);
        push = '0;

        repeat (4) @(posedge clk);
        $finish;
    end
endmodule
