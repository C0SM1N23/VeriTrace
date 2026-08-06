`timescale 1ns / 1ps
//
// The §8.20 test design: a minimal SDR SDRAM controller driving a real JEDEC
// command bus, with four timing violations injected — one from each category
// Prompt 11's acceptance criterion names.
//
//     iverilog -g2012 -o sim.vvp *.sv                  && vvp sim.vvp
//         -> dump.vcd     tRCD, tRP, tRFC and tFAW each violated once
//
//     iverilog -g2012 -DNO_VIOLATION -o sim_ok.vvp *.sv && vvp sim_ok.vvp
//         -> dump_ok.vcd  the same RTL, conformant
//
// A compile-time define rather than a runtime flag, for the reason
// designs/deadlock uses one too: the bug is then a property of the elaborated
// design, so it is visible in the graph and reachable from `why`.
//
// 100 MHz, so one clock is 10 ns and the datasheet's nanosecond minima map to
// whole cycles — see sdram_ctrl.sv for the arithmetic. There is no SDRAM model
// on the other end: §8.20 analyses the *command bus*, so the controller
// driving it is the entire device under test.
//
module tb_sdram;

`ifdef NO_VIOLATION
  localparam bit VIOLATE = 1'b0;
`else
  localparam bit VIOLATE = 1'b1;
`endif

  logic clk = 1'b0;
  logic rst_n = 1'b0;

  always #5 clk = ~clk;  // 100 MHz

  logic        cs_n, ras_n, cas_n, we_n;
  logic [ 1:0] ba;
  logic [12:0] a;
  logic        done;

  sdram_ctrl #(
      .VIOLATE(VIOLATE)
  ) ctrl (
      .clk  (clk),
      .rst_n(rst_n),
      .cs_n (cs_n),
      .ras_n(ras_n),
      .cas_n(cas_n),
      .we_n (we_n),
      .ba   (ba),
      .a    (a),
      .done (done)
  );

  initial begin
`ifdef NO_VIOLATION
    $dumpfile("dump_ok.vcd");
`else
    $dumpfile("dump.vcd");
`endif
    $dumpvars(0, tb_sdram);

    repeat (4) @(posedge clk);
    rst_n <= 1'b1;

    wait (done);
    repeat (4) @(posedge clk);
    $display("[tb_sdram] sequence complete (VIOLATE=%0b)", VIOLATE);
    $finish;
  end

endmodule
