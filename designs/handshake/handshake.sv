`timescale 1ns / 1ps

// §8.9's structural row: a `ready` that is a function of `valid` in the same
// cycle. Two AXI4-Lite-ish channels, built from one source:
//
//   * `s_axi_awready` is registered — the correct shape;
//   * `s_axi_wready`  is combinational *from wvalid* — the deadlock risk.
//
// The point of the design is that **the trace shows nothing**. Both channels
// complete every transfer in this run, because the master never waits on ready
// before asserting valid. The day the other side does, `wready` and `wvalid`
// wait for each other forever. A structural check finds it; no amount of
// simulation on this stimulus will.
module handshake_dut (
    input  logic clk,
    input  logic rst_n,

    input  logic s_axi_awvalid,
    output logic s_axi_awready,
    input  logic [31:0] s_axi_awaddr,

    input  logic s_axi_wvalid,
    output logic s_axi_wready,
    input  logic [31:0] s_axi_wdata,

    output logic s_axi_bvalid,
    input  logic s_axi_bready,

    input  logic s_axi_arvalid,
    output logic s_axi_arready,
    input  logic [31:0] s_axi_araddr,

    output logic s_axi_rvalid,
    input  logic s_axi_rready,
    output logic [31:0] s_axi_rdata
);
    logic busy;

    // Correct: ready comes out of a flop, so it cannot depend on this cycle's
    // valid however the logic is written.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) s_axi_awready <= 1'b0;
        else        s_axi_awready <= ~busy;
    end

`ifdef FIX_HANDSHAKE
    assign s_axi_wready = ~busy;
`else
    // The bug: wready is a combinational function of wvalid. Legal Verilog,
    // simulates fine here, and a deadlock against any master that waits for
    // ready before raising valid.
    assign s_axi_wready = s_axi_wvalid & ~busy;
`endif

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy         <= 1'b0;
            s_axi_bvalid <= 1'b0;
            s_axi_rvalid <= 1'b0;
            s_axi_rdata  <= 32'd0;
        end else begin
            busy <= s_axi_awvalid & s_axi_awready;
            s_axi_bvalid <= (s_axi_wvalid & s_axi_wready) ? 1'b1
                          : (s_axi_bvalid & s_axi_bready) ? 1'b0 : s_axi_bvalid;
            s_axi_rvalid <= (s_axi_arvalid & s_axi_arready) ? 1'b1
                          : (s_axi_rvalid & s_axi_rready) ? 1'b0 : s_axi_rvalid;
            if (s_axi_arvalid & s_axi_arready) s_axi_rdata <= s_axi_araddr;
        end
    end

    assign s_axi_arready = ~s_axi_rvalid;
endmodule

module tb_handshake;
    logic clk = 1'b0, rst_n = 1'b0;
    logic awvalid = 0, wvalid = 0, bready = 1, arvalid = 0, rready = 1;
    logic awready, wready, bvalid, arready, rvalid;
    logic [31:0] awaddr = 32'h40, wdata = 32'hA5, araddr = 32'h40, rdata;

    handshake_dut dut (
        .clk(clk), .rst_n(rst_n),
        .s_axi_awvalid(awvalid), .s_axi_awready(awready), .s_axi_awaddr(awaddr),
        .s_axi_wvalid(wvalid), .s_axi_wready(wready), .s_axi_wdata(wdata),
        .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_arvalid(arvalid), .s_axi_arready(arready), .s_axi_araddr(araddr),
        .s_axi_rvalid(rvalid), .s_axi_rready(rready), .s_axi_rdata(rdata)
    );

    always #5 clk = ~clk;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_handshake);
        #12 rst_n = 1'b1;
        // A master that raises valid without waiting: every transfer completes,
        // and the combinational path never shows itself.
        repeat (4) begin
            @(posedge clk) awvalid <= 1'b1;
            @(posedge clk) awvalid <= 1'b0; wvalid <= 1'b1;
            @(posedge clk) wvalid <= 1'b0;
            @(posedge clk) arvalid <= 1'b1;
            @(posedge clk) arvalid <= 1'b0;
        end
        #40 $finish;
    end
endmodule
