`timescale 1ns / 1ps

// Drives checks_top long enough for the stuck detector to have something to
// measure: the default threshold in §8.4 is 100 clock cycles, so the run has to
// be several times that.

module tb_checks;

  localparam int WIDTH = 16;

  logic             clk = 1'b0;
  logic             slow_clk = 1'b0;
  logic             rst_n;
  logic [1:0]       mode;
  logic [WIDTH-1:0] din;
  logic [WIDTH-1:0] dout;
  logic [WIDTH-1:0] acc;
  logic             busy;
  logic [1:0]       status;

  checks_top #(
      .WIDTH(WIDTH)
  ) dut (
      .clk     (clk),
      .rst_n   (rst_n),
      .slow_clk(slow_clk),
      .mode    (mode),
      .din     (din),
      .dout    (dout),
      .acc     (acc),
      .busy    (busy),
      .status  (status)
  );

  always #5 clk = ~clk;
  // A genuinely unrelated domain, so the crossing into `status` is real.
  always #17 slow_clk = ~slow_clk;

  initial begin
    $dumpfile("dump.vcd");
    $dumpvars(0, tb_checks);
  end

  initial begin
    rst_n = 1'b0;
    mode  = 2'b00;
    din   = '0;
    repeat (3) @(posedge clk);
    rst_n = 1'b1;

    // Take lock_r high early and never let it go: it freezes here.
    @(posedge clk);
    mode = 2'b01;
    @(posedge clk);
    mode = 2'b00;

    // Then run quietly for well over the stuck threshold.
    repeat (300) begin
      @(posedge clk);
      din <= din + 16'd1;
    end

    // Late write, so `uninit` is X for most of the run rather than all of it.
    mode = 2'b11;
    repeat (4) @(posedge clk);
    mode = 2'b00;

    repeat (20) @(posedge clk);
    $finish;
  end

endmodule
