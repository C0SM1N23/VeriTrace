`timescale 1ns / 1ps

// §14.1 reference bug: a Gray write pointer crosses directly into the read
// clock domain through only one flop.  The first stage is also not reset, so
// the consumer observes X before its first sample.  A correct async FIFO uses
// two destination-domain synchroniser flops before comparing pointers.
module fifo_async (
    input  logic wr_clk,
    input  logic rd_clk,
    input  logic rst_n,
    input  logic wr_en,
    output logic empty,
    output logic [3:0] wr_gray_seen
);
    logic [3:0] wr_bin;
    logic [3:0] wr_gray;
    logic [3:0] rd_gray;

    assign wr_gray = (wr_bin >> 1) ^ wr_bin;
    assign empty = (wr_gray_seen == rd_gray);

    always_ff @(posedge wr_clk or negedge rst_n) begin
        if (!rst_n) wr_bin <= '0;
        else if (wr_en) wr_bin <= wr_bin + 1'b1;
    end

    always_ff @(posedge rd_clk or negedge rst_n) begin
        if (!rst_n) rd_gray <= '0;
        else rd_gray <= rd_gray;
    end

    // BUG: one unreset capture stage, used directly by read-domain logic.
    always_ff @(posedge rd_clk) begin
        wr_gray_seen <= wr_gray;
    end
endmodule
