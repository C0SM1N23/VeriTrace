`timescale 1ns / 1ps

// A self-checking testbench with one honest blind spot.
//
// It keeps a reference queue and compares every read against it, so the data
// path, the pointers and the `empty` logic are all genuinely verified — mutate
// any of them and this fails. What it never does is **fill the FIFO**: writes
// stop while three entries are outstanding, so `full` is 0 for the entire run
// and nothing here ever reads it.
//
// That is the shape §8.28 describes: line coverage is high, the tests look
// thorough, and an entire output is unobserved. Mutation testing is what says so.
module tb_fifo;
  localparam int WIDTH = 8;
  localparam int DEPTH = 8;
  //: Deliberately far below DEPTH. Raise it to 8 and the survivors below die,
  //: which is the exercise this design exists for.
  localparam int HIGH_WATER = 3;

  logic clk = 0;
  logic rst_n = 0;
  logic wr_en = 0, rd_en = 0;
  logic [WIDTH-1:0] wr_data = 0;
  logic [WIDTH-1:0] rd_data;
  logic full, empty;

  fifo #(
      .WIDTH(WIDTH),
      .DEPTH(DEPTH)
  ) dut (
      .clk(clk), .rst_n(rst_n), .wr_en(wr_en), .wr_data(wr_data),
      .rd_en(rd_en), .rd_data(rd_data), .full(full), .empty(empty)
  );

  always #5 clk = ~clk;

  // The reference: a plain queue, and the count that keeps the DUT away from
  // its own full condition.
  logic [WIDTH-1:0] model[$];
  int errors = 0;
  int checks = 0;
  int outstanding = 0;

  task automatic check(input string what, input int got, input int want);
    checks++;
    if (got !== want) begin
      errors++;
      $display("FAIL @%0t %s: got %0d, want %0d", $time, what, got, want);
    end
  endtask

  int i;
  bit do_wr, do_rd;
  logic [WIDTH-1:0] popped;
  initial begin
    repeat (3) @(posedge clk);
    rst_n <= 1;
    @(posedge clk);

    // `empty` is checked directly, so anything that breaks it is caught.
    check("empty after reset", empty, 1);

    for (i = 0; i < 200; i++) begin
      // Everything is driven and sampled on the falling edge, where the DUT's
      // outputs are settled. Sampling on the rising edge would race the very
      // non-blocking assignments under test.
      @(negedge clk);
      check("empty", empty, (outstanding == 0));
      // `full` *is* checked — but `outstanding` never reaches DEPTH, so the
      // expected value is 0 on every one of these 200 iterations and the
      // assertion only ever confirms that `full` is not spuriously high. The
      // half of the expression that decides when it goes high is untested.
      check("full", full, (outstanding == DEPTH));

      do_wr   = (outstanding < HIGH_WATER);
      do_rd   = (outstanding > 0) && (i % 3 != 0);
      // `rd_data` is combinational from `rd_ptr`, so the head is readable now —
      // and it is what the read issued at the next edge will consume.
      if (do_rd) check("rd_data", rd_data, model[0]);

      wr_en   <= do_wr;
      wr_data <= WIDTH'(i);
      rd_en   <= do_rd;

      @(posedge clk);
      if (do_wr && !full) begin
        model.push_back(WIDTH'(i));
        outstanding++;
      end
      if (do_rd && !empty) begin
        // Icarus has no `void'()`, so the discarded value goes somewhere.
        popped = model.pop_front();
        outstanding--;
      end
    end

    @(negedge clk);
    wr_en <= 0;
    rd_en <= 0;
    @(posedge clk);

    if (errors != 0) begin
      $display("tb_fifo: %0d/%0d checks failed", errors, checks);
      $fatal(1, "tb_fifo: FAILED");
    end
    $display("tb_fifo: %0d checks passed", checks);
    $finish;
  end

  // A mutant that deadlocks must not hang the whole run.
  initial begin
    #100000;
    $fatal(1, "tb_fifo: timed out");
  end
endmodule
