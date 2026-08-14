`timescale 1ns / 1ps

// Drives `a` and `b` while `sel` stays put, which is the one stimulus that tells
// the two apart. Nothing here checks anything: §8.29's claim is that the *diff*
// between two runs finds the mismatch, and a testbench that already knew what to
// assert would not be demonstrating that.
module tb_mux;
  logic       clk = 0;
  logic       sel = 0;
  logic [7:0] a = 8'h00;
  logic [7:0] b = 8'h00;
  logic [7:0] y, y_ok;

  mux_bug dut (
      .clk(clk), .sel(sel), .a(a), .b(b), .y(y), .y_ok(y_ok)
  );

  always #5 clk = ~clk;

  int i;
  initial begin
    // `sel` is held low for the whole first half: only `b` reaches `y`, and only
    // if the design is sensitive to it.
    for (i = 0; i < 20; i++) begin
      @(posedge clk);
      b <= 8'(i + 1);
      a <= 8'(200 - i);
    end

    @(posedge clk);
    sel <= 1;

    for (i = 0; i < 20; i++) begin
      @(posedge clk);
      a <= 8'(100 + i);
      b <= 8'(i);
    end

    repeat (4) @(posedge clk);
    $display("tb_mux: done");
    $finish;
  end
endmodule
