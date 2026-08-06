`timescale 1ns / 1ps

// Drives fifo_buggy until the symptom appears: `full` goes high and never
// clears, even though reads are requested continuously.
module tb_fifo_buggy;
    localparam int WIDTH = 8;
    localparam int DEPTH = 16;

    logic             clk = 1'b0;
    logic             rst_n;
    logic             wr_en, rd_en;
    logic [WIDTH-1:0] wr_data;
    logic [WIDTH-1:0] rd_data;
    logic             full, empty;

    fifo_buggy #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (
        .clk(clk), .rst_n(rst_n),
        .wr_en(wr_en), .wr_data(wr_data),
        .rd_en(rd_en), .rd_data(rd_data),
        .full(full), .empty(empty)
    );

    always #5 clk = ~clk;

    // A liveness check, of the shape a real testbench has: once reads are
    // being requested, the FIFO must drain. With the injected bug it never
    // does, so this fires repeatedly and fills the log — which is what
    // `veritrace triage` is given (§8.10b).
    //
    // The message is deliberately in the ordinary "assertion ... failed at
    // time ..." shape, so the default patterns of §8.10b recognise it with no
    // configuration. `$time` is in this module's time unit, hence the `ns`.
    int stall;
    always @(posedge clk) begin
        if (!rst_n) begin
            stall <= 0;
        end else if (rd_en && full) begin
            stall <= stall + 1;
            if (stall > 4) begin
                $display("ERROR: assertion \"p_fifo_drains\" failed at time %0dns", $time);
                $display("       tb_fifo_buggy.dut.full");
            end
        end else begin
            stall <= 0;
        end
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_fifo_buggy);
        // Dumping unpacked-array elements needs an explicit element select,
        // which the LRM does not allow as a $dumpvars argument. Icarus accepts
        // it as an extension; slang and ModelSim reject it. Icarus predefines
        // __ICARUS__, so this stays an Icarus-only path and every other tool
        // reads the file cleanly.
`ifdef __ICARUS__
        $dumpvars(1, dut.mem[0], dut.mem[1]);
`endif
    end

    initial begin
        rst_n   = 1'b0;
        wr_en   = 1'b0;
        rd_en   = 1'b0;
        wr_data = '0;
        repeat (2) @(posedge clk);
        rst_n = 1'b1;

        // Fill it.
        for (int i = 0; i < DEPTH + 4; i++) begin
            @(negedge clk);
            wr_en   = 1'b1;
            wr_data = 8'h20 + i[7:0];
        end

        // Ask for reads that will never be honoured: rd_ptr is stuck in reset,
        // so `full` stays high from here to the end of the run.
        @(negedge clk);
        wr_en = 1'b0;
        rd_en = 1'b1;
        repeat (20) @(negedge clk);

        repeat (4) @(posedge clk);
        $finish;
    end
endmodule
