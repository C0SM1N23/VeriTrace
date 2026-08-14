`timescale 1ns / 1ps

// A correct synchronous FIFO. Nothing is injected here — the point of this
// design is the *testbench*, and what it fails to look at (§8.28).
module fifo #(
    parameter int WIDTH = 8,
    parameter int DEPTH = 8
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
  localparam int AW = $clog2(DEPTH);

  logic [WIDTH-1:0] mem[DEPTH];
  logic [    AW:0]  wr_ptr, rd_ptr;

  // One extra bit distinguishes full from empty: the pointers are equal in both
  // cases and only the wrap bit tells them apart.
  assign full    = (wr_ptr[AW] != rd_ptr[AW]) && (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]);
  assign empty   = (wr_ptr == rd_ptr);
  assign rd_data = mem[rd_ptr[AW-1:0]];

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wr_ptr <= '0;
      rd_ptr <= '0;
    end else begin
      if (wr_en && !full) begin
        mem[wr_ptr[AW-1:0]] <= wr_data;
        wr_ptr <= wr_ptr + 1;
      end
      if (rd_en && !empty) begin
        rd_ptr <= rd_ptr + 1;
      end
    end
  end
endmodule
