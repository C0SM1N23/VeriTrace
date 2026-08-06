// Correlation stress design: the two shapes that break naive name matching
// (§7.1) — a for-generate block and an instance array. Both produce indexed
// hierarchy in the trace (`g_lane[2].u_fifo.wr_ptr`, `u_cnt[1].q`) that only
// exists after elaboration.
`timescale 1ns / 1ps

module lane_fifo #(
    parameter int WIDTH = 8,
    parameter int DEPTH = 4
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             push,
    input  logic [WIDTH-1:0] din,
    output logic [WIDTH-1:0] dout,
    output logic             full
);
    localparam int AW = $clog2(DEPTH);
    logic [WIDTH-1:0] mem [DEPTH];
    logic [AW:0]      wr_ptr;

    assign full = wr_ptr[AW];
    assign dout = mem[wr_ptr[AW-1:0]];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) wr_ptr <= '0;
        else if (push && !full) begin
            mem[wr_ptr[AW-1:0]] <= din;
            wr_ptr <= wr_ptr + 1'b1;
        end
    end
endmodule

// Instantiated as an array below, to exercise the `u_cnt[1].q` form.
module counter #(
    parameter int W = 4
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         en,
    output logic [W-1:0] q
);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)   q <= '0;
        else if (en)  q <= q + 1'b1;
    end
endmodule

module lanes #(
    parameter int NLANES = 4,
    parameter int WIDTH  = 8
) (
    input  logic                     clk,
    input  logic                     rst_n,
    input  logic [NLANES-1:0]        push,
    input  logic [WIDTH-1:0]         din,
    output logic [NLANES-1:0]        full,
    output logic [WIDTH-1:0]         dout0,
    output logic [NLANES-1:0]        tick
);
    // for-generate: hierarchy becomes lanes.g_lane[i].u_fifo.*
    logic [WIDTH-1:0] lane_dout [NLANES];
    genvar i;
    generate
        for (i = 0; i < NLANES; i++) begin : g_lane
            logic busy;
            // 64 entries on purpose: deeper than Verilator's default
            // --trace-max-array of 32, so the memory only reaches the trace
            // when the mandatory flags of §4.0 are used.
            lane_fifo #(.WIDTH(WIDTH), .DEPTH(64)) u_fifo (
                .clk   (clk),
                .rst_n (rst_n),
                .push  (push[i]),
                .din   (din),
                .dout  (lane_dout[i]),
                .full  (full[i])
            );
            assign busy = full[i] & push[i];
        end
    endgenerate

    // Instance array: hierarchy becomes lanes.u_cnt[i].*. The output is one
    // packed vector, which the array splits 4 bits per instance — the idiom
    // instance arrays exist for, and the one simulators agree on.
    logic [NLANES*4-1:0] cnt_q;
    counter #(.W(4)) u_cnt [NLANES-1:0] (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (push),
        .q     (cnt_q)
    );

    assign dout0 = lane_dout[0];
    generate
        for (i = 0; i < NLANES; i++) begin : g_tick
            assign tick[i] = (cnt_q[i*4 +: 4] == 4'hF);
        end
    endgenerate
endmodule
