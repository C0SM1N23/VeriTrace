`timescale 1ns / 1ps

// §14.1 reference bug: the reset is asserted asynchronously, but its release
// was never synchronised.  `release_armed` is meant to become one after reset;
// the impossible `!rst_n` branch below leaves it at zero.  Once a request sets
// `lock_r`, the normal release condition can therefore never clear it.
module arbiter (
    input  logic clk,
    input  logic rst_n,
    input  logic request,
    input  logic release_req,
    output logic grant,
    output logic lock_r
);
    logic release_armed;

    assign grant = request && !lock_r;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            lock_r       <= 1'b0;
            release_armed <= 1'b0;
        end else begin
            // BUG: this condition is unreachable in the deasserted branch.
            // A two-flop reset-release synchroniser should drive this state.
            if (!rst_n)
                release_armed <= 1'b1;

            if (request)
                lock_r <= 1'b1;
            else if (release_req && release_armed)
                lock_r <= 1'b0;
        end
    end
endmodule
