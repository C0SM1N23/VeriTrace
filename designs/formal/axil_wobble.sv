`timescale 1ns / 1ps

// An AXI4-Lite master with one protocol violation, and one branch that cannot
// be reached. Both exist to be found without simulating: the first by §8.27's
// bounded model check, the second by §8.35's reachability classification.
//
// **The violation (line 44).** AXI requires that once `awvalid` is asserted, the
// address holds until `awready` accepts it. Here `awaddr` is driven straight
// from a counter that keeps running, so a slave that stalls for even one cycle
// sees the address move underneath it. Whether that ever happens depends on the
// slave, which is exactly why it survives simulation against a well-behaved one
// — and why a solver, free to choose `awready`, finds it in two steps.
//
// **The dead branch (line 62).** `retry` is two bits, so `retry > 3` is not
// something the design can produce. §8.35's own example, and the point is that
// no test will ever close that coverage hole because there is nothing there.
module axil_wobble (
    input  logic        aclk,
    input  logic        aresetn,
    input  logic        start,

    output logic        awvalid,
    input  logic        awready,
    output logic [31:0] awaddr,
    output logic [ 2:0] awprot,

    output logic        wvalid,
    input  logic        wready,
    output logic [31:0] wdata,
    output logic [ 3:0] wstrb,

    input  logic        bvalid,
    output logic        bready,
    input  logic [ 1:0] bresp,

    output logic        arvalid,
    input  logic        arready,
    output logic [31:0] araddr,
    output logic [ 2:0] arprot,

    input  logic        rvalid,
    output logic        rready,
    input  logic [31:0] rdata,
    input  logic [ 1:0] rresp,

    output logic        alarm
);

  logic [31:0] tick;
  logic [ 1:0] retry;
  logic        busy;

  assign awprot  = 3'b000;
  assign arprot  = 3'b000;
  assign wstrb   = 4'hF;
  assign wdata   = tick;
  assign araddr  = 32'h0;
  assign arvalid = 1'b0;
  assign rready  = 1'b1;
  assign bready  = 1'b1;

  assign awvalid = busy;
  assign wvalid  = busy;
  // THE BUG: the address is the free-running counter, not a value captured when
  // the transfer was issued. It moves while `awvalid` is high.
  assign awaddr  = {tick[27:0], 4'h0};

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      tick  <= '0;
      retry <= '0;
      busy  <= 1'b0;
      alarm <= 1'b0;
    end else begin
      tick <= tick + 1'b1;

      if (!busy && start) busy <= 1'b1;
      else if (busy && awready && wready) busy <= 1'b0;

      if (bvalid && bresp != 2'b00) retry <= retry + 1'b1;

      // DEAD: `retry` is two bits wide, so it cannot exceed 3.
      if (retry > 3) alarm <= 1'b1;
    end
  end

endmodule
