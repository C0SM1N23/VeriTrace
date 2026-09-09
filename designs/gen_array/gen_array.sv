`timescale 1ns / 1ps

module gen_lane (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [7:0] din,
    output logic [7:0] dout
);
    always_ff @(posedge clk) begin
        if (!rst_n) dout <= '0;
        else dout <= din;
    end
endmodule

// §7.1/§14.1: a clean generate hierarchy.  The reference is intentionally
// bug-free; it catches regressions that flatten every lane onto the same name.
module gen_array (
    input  logic         clk,
    input  logic         rst_n,
    input  logic [31:0]  lane_in,
    output logic [31:0]  lane_out
);
    for (genvar i = 0; i < 4; i++) begin : g_lane
        gen_lane u_lane (
            .clk  (clk),
            .rst_n(rst_n),
            .din  (lane_in[i*8 +: 8]),
            .dout (lane_out[i*8 +: 8])
        );
    end
endmodule
