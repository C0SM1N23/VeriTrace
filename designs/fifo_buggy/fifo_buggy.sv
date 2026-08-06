`timescale 1ns / 1ps

// fifo_sync with one injected bug, for the why-trace golden test.
//
// THE BUG: the read pointer is held in reset by `rd_rst_n`, which is tied low
// and never deasserted. rd_ptr therefore stays at 0, the FIFO fills and `full`
// sticks high forever. This is the §8.1 shape: a symptom several hops away from
// a signal that simply never moved.
//
// Everything else is identical to designs/fifo_async/fifo_sync.sv.
module fifo_buggy #(
    parameter int WIDTH = 8,
    parameter int DEPTH = 16
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             wr_en,
    input  logic [WIDTH-1:0] wr_data,
    input  logic             rd_en,
    output logic [WIDTH-1:0] rd_data,
    output logic             full,
    output logic             empty
);
    localparam int ADDR_W = $clog2(DEPTH);
    logic [WIDTH-1:0] mem [DEPTH];
    logic [ADDR_W:0]  wr_ptr, rd_ptr;

    // BUG: intended as a synchronised release of the read-side reset, but the
    // deassert was never written, so it is a constant 0.
    logic rd_rst_n;
    assign rd_rst_n = 1'b0;

    assign full    = wr_ptr[ADDR_W] != rd_ptr[ADDR_W] &&
                      wr_ptr[ADDR_W-1:0] == rd_ptr[ADDR_W-1:0];
    assign empty   = wr_ptr == rd_ptr;
    assign rd_data = mem[rd_ptr[ADDR_W-1:0]];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= '0;
        end else if (wr_en && !full) begin
            mem[wr_ptr[ADDR_W-1:0]] <= wr_data;
            wr_ptr <= wr_ptr + 1'b1;
        end
    end

    always_ff @(posedge clk or negedge rd_rst_n) begin
        if (!rd_rst_n) rd_ptr <= '0;
        else if (rd_en && !empty) rd_ptr <= rd_ptr + 1'b1;
    end
endmodule
