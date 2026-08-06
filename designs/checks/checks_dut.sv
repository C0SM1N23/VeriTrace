`timescale 1ns / 1ps

// A deliberately flawed control block.
//
// Every problem below is injected on purpose, so the automatic checks of §8.4,
// §8.5, §8.11 and §8.11c have something real to find. Opening a session on this
// design must report all of them without a single manual query.
//
// See designs/checks/README.md for the full list and what each one is called.
//
// Do not "fix" anything here. This file is a fixture.

module checks_dut #(
    parameter int WIDTH = 8,
    // The parent sets WIDTH but forgets DEPTH, so this stays on its default
    // while checks_top says 8 — the parameterisation bug of §8.11c.
    parameter int DEPTH = 4
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             slow_clk,
    input  logic [1:0]       mode,
    input  logic [WIDTH-1:0] din,
    output logic [WIDTH-1:0] dout,
    output logic [WIDTH-1:0] acc,
    output logic             busy,
    output logic [1:0]       status
);

  // Inferred latch: dout keeps its value when mode is 2'b10 or 2'b11, which
  // synthesis builds as a latch and simulation never shows.
  always_comb begin
    if (mode == 2'b00) dout = din;
    else if (mode == 2'b01) dout = ~din;
  end

  // Case with no default: the 2'b11 arm is uncovered, so synthesis is free to
  // choose something other than "hold".
  logic [1:0] decoded;
  always_comb begin
    case (mode)
      2'b00: decoded = 2'd0;
      2'b01: decoded = 2'd1;
      2'b10: decoded = 2'd2;
    endcase
  end

  // Freezes: the release arm can never be true, because it needs rst_n both
  // low and high. Once taken, lock_r is stuck at 1 for the rest of the run.
  logic lock_r;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) lock_r <= 1'b0;
    else if (mode == 2'b11 && !rst_n) lock_r <= 1'b0;
    else if (mode != 2'b00) lock_r <= 1'b1;
  end
  assign busy = lock_r;

  // Blocking assignment inside always_ff.
  logic [WIDTH-1:0] shadow;
  always_ff @(posedge clk) begin
    shadow = din ^ {WIDTH{decoded[0]}};
  end

  // Non-blocking assignment inside always_comb.
  logic parity;
  always_comb begin
    parity <= ^shadow;
  end

  // Sensitivity list misses rst_n, so simulation holds gated across a reset
  // that synthesis would honour immediately.
  logic [1:0] gated;
  always @(mode) begin
    gated = mode & {2{rst_n}};
  end

  // Clock-domain crossing straight into a flop on the other clock, with no
  // two-flop synchroniser: parity and lock_r both belong to clk.
  always_ff @(posedge slow_clk) begin
    status <= {parity, lock_r};
  end

  // No reset and no write until late in the run, so acc is X for the first
  // stretch and everything downstream of it goes X with it.
  logic [WIDTH-1:0] uninit;
  always_ff @(posedge clk) begin
    if (mode == 2'b11) uninit <= din + {{(WIDTH - 2) {1'b0}}, gated};
  end
  assign acc = uninit ^ {WIDTH{lock_r}};

  // X-optimism: while uninit is X the simulator falls through to the default
  // arm and produces a clean 3, so nothing looks wrong. The hardware has a
  // real selector and can take the other branch.
  logic [1:0] xsel;
  always_comb begin
    case (uninit[1:0])
      2'b00:   xsel = 2'd0;
      default: xsel = 2'd3;
    endcase
  end

  // A declared initial value that the design reads before writing it: fine in
  // simulation, X on an ASIC, whatever the bitstream says on an FPGA.
  logic [1:0] cfg = 2'b10;
  always_ff @(posedge clk) begin
    if (mode == 2'b10) cfg <= xsel;
  end

  // DEPTH is only here so the parameter is used and slang keeps it.
  logic [$clog2(DEPTH+1)-1:0] depth_probe;
  assign depth_probe = DEPTH[$clog2(DEPTH+1)-1:0] | {{($clog2(DEPTH+1)-2){1'b0}}, cfg};

endmodule


// Wrapper that sets WIDTH and DEPTH but only passes WIDTH down.
module checks_top #(
    parameter int WIDTH = 16,
    parameter int DEPTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             slow_clk,
    input  logic [1:0]       mode,
    input  logic [WIDTH-1:0] din,
    output logic [WIDTH-1:0] dout,
    output logic [WIDTH-1:0] acc,
    output logic             busy,
    output logic [1:0]       status
);

  checks_dut #(
      .WIDTH(WIDTH)
  ) u_dut (
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

endmodule
