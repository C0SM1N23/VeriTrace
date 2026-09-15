`timescale 1ns/1ps
// Two asynchronous read ports; synchronous byte-lane writes on either port.
module dual_sram #(parameter ADDR_W = 10)(
  input wire clk_i,
  input wire [ADDR_W-1:0] a_addr_i, b_addr_i,
  input wire [31:0] a_wdata_i, b_wdata_i,
  input wire [3:0] a_wstrb_i, b_wstrb_i,
  input wire a_write_grant_i, b_write_grant_i,
  output wire [31:0] a_rdata_o, b_rdata_o
);
  reg [31:0] mem [0:255];
  wire [7:0] a_word_addr = a_addr_i[ADDR_W-1:2];
  wire [7:0] b_word_addr = b_addr_i[ADDR_W-1:2];
  assign a_rdata_o = mem[a_word_addr];
  assign b_rdata_o = mem[b_word_addr];
  integer lane;
  always @(posedge clk_i) begin
    for (lane = 0; lane < 4; lane = lane + 1) begin
      if (a_write_grant_i && a_wstrb_i[lane])
        mem[a_word_addr][lane*8 +: 8] <= a_wdata_i[lane*8 +: 8];
      if (b_write_grant_i && b_wstrb_i[lane])
        mem[b_word_addr][lane*8 +: 8] <= b_wdata_i[lane*8 +: 8];
    end
  end
endmodule

module tb;
  reg clk_i = 0;
  always #5 clk_i = ~clk_i;
  reg [9:0] a_addr_i = 12, b_addr_i = 1020;
  reg [31:0] a_wdata_i = 32'h11223344, b_wdata_i = 32'hdeadbeef;
  reg [3:0] a_wstrb_i = 15, b_wstrb_i = 15;
  reg a_write_grant_i = 1, b_write_grant_i = 1;
  wire [31:0] a_rdata_o, b_rdata_o;
  dual_sram dut(.*);
  initial begin
    #6;
    if (a_rdata_o !== 32'h11223344 || b_rdata_o !== 32'hdeadbeef) $fatal(1, "dual write/read");
    #4; a_wdata_i = 32'haabbccdd; a_wstrb_i = 5; b_write_grant_i = 0;
    #6;
    if (a_rdata_o !== 32'h11bb33dd || b_rdata_o !== 32'hdeadbeef) $fatal(1, "byte lanes");
    #4; a_write_grant_i = 0; a_addr_i = 1020;
    #1;
    if (a_rdata_o !== 32'hdeadbeef) $fatal(1, "asynchronous read");
    #9; $finish;
  end
endmodule
