`timescale 1ns / 1ps

// One deliberate flaw per §8.8 check, the way designs/checks does it for §8.11.
//
// Every flaw here is found **without a trace**. That is the whole claim of §8.8:
// "un FSM cu o stare moarta e un bug garantat pe care nicio simulare nu-l
// gaseste daca stimulii n-au ajuns acolo". The testbench beside this file
// deliberately never drives the machine into most of them, so a run that passes
// is not evidence of anything — and the checks still report all five.
//
//   1. dead state          `bad_dead.state`, S_TRAP: enter and never leave
//   2. unreachable state   `bad_unreach.state`, S_ORPHAN: nothing goes there
//   3. impossible guard    `bad_typo.state`, a condition that contradicts itself
//   4. incomplete guards   `bad_partial.state`, S_WAIT: one exit, no else
//   5. incomplete reset    `bad_reset.state`, the companion counter never resets
//
// `good.state` is the control: a clean four-state machine that must produce no
// findings at all. A check that fires on it is a false positive, and a false
// positive is what gets a static check switched off.

// --- the control: nothing should be reported here ---------------------------
module fsm_good (
    input  logic clk,
    input  logic rst_n,
    input  logic go,
    input  logic ack,
    output logic busy
);
    localparam logic [1:0] S_IDLE = 2'd0, S_RUN = 2'd1, S_DONE = 2'd2;
    logic [1:0] state;
    logic [3:0] count;

    assign busy = (state != S_IDLE);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            count <= 4'd0;
        end else begin
            case (state)
                S_IDLE:  state <= go  ? S_RUN  : S_IDLE;
                S_RUN:   begin state <= ack ? S_DONE : S_RUN; count <= count + 4'd1; end
                S_DONE:  state <= S_IDLE;
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule

// --- 1. dead state ----------------------------------------------------------
module fsm_dead (
    input  logic clk,
    input  logic rst_n,
    input  logic err,
    output logic trapped
);
    localparam logic [1:0] S_IDLE = 2'd0, S_WORK = 2'd1, S_TRAP = 2'd2;
    logic [1:0] state;

    assign trapped = (state == S_TRAP);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) state <= S_IDLE;
        else begin
            case (state)
                S_IDLE:  state <= S_WORK;
                S_WORK:  state <= err ? S_TRAP : S_IDLE;
                // THE BUG: S_TRAP has no way out. Entering it stops the machine
                // until the chip is reset. Nothing here is wrong syntactically
                // and no simulation finds it unless `err` happens to go high.
                S_TRAP:  state <= S_TRAP;
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule

// --- 2. unreachable state ---------------------------------------------------
module fsm_unreachable (
    input  logic clk,
    input  logic rst_n,
    input  logic go,
    output logic active
);
    localparam logic [1:0] S_IDLE = 2'd0, S_RUN = 2'd1, S_ORPHAN = 2'd2;
    logic [1:0] state;

    assign active = (state == S_RUN) || (state == S_ORPHAN);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) state <= S_IDLE;
        else begin
            case (state)
                S_IDLE:   state <= go ? S_RUN : S_IDLE;
                S_RUN:    state <= S_IDLE;
                // THE BUG: S_ORPHAN is written and reasoned about, but no
                // transition ever enters it. Either dead code, or the edge into
                // it was forgotten.
                S_ORPHAN: state <= S_IDLE;
                default:  state <= S_IDLE;
            endcase
        end
    end
endmodule

// --- 3. impossible transition ----------------------------------------------
module fsm_typo (
    input  logic clk,
    input  logic rst_n,
    input  logic ready,
    output logic done
);
    localparam logic [1:0] S_IDLE = 2'd0, S_SEND = 2'd1, S_WAIT = 2'd2;
    logic [1:0] state;

    assign done = (state == S_IDLE);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) state <= S_IDLE;
        else begin
            case (state)
                S_IDLE: state <= ready ? S_SEND : S_IDLE;
                S_SEND: state <= S_WAIT;
                S_WAIT: begin
                    // THE BUG: a mistyped condition. `ready && !ready` is never
                    // true, so the machine can never leave S_WAIT this way —
                    // the branch is dead and the state is stuck with it.
                    if (ready && !ready) state <= S_IDLE;
                    else                 state <= S_WAIT;
                end
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule

// --- 4. incomplete guards ---------------------------------------------------
module fsm_partial (
    input  logic clk,
    input  logic rst_n,
    input  logic start,
    input  logic grant,
    output logic waiting
);
    localparam logic [1:0] S_IDLE = 2'd0, S_WAIT = 2'd1, S_GO = 2'd2;
    logic [1:0] state;

    assign waiting = (state == S_WAIT);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) state <= S_IDLE;
        else begin
            case (state)
                S_IDLE: state <= start ? S_WAIT : S_IDLE;
                // THE BUG (a mild one, reported at info): only the happy path is
                // written. With no `else` and no self-transition, an unexpected
                // input leaves the machine where it is — intended for a state
                // that waits, a hang for one that should not.
                S_WAIT: if (grant) state <= S_GO;
                S_GO:   state <= S_IDLE;
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule

// --- 5. incomplete reset ----------------------------------------------------
module fsm_unreset (
    input  logic clk,
    input  logic rst_n,
    input  logic tick,
    output logic [3:0] ticks
);
    localparam logic S_IDLE = 1'b0, S_COUNT = 1'b1;
    logic       state;
    logic [3:0] count;

    assign ticks = count;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            // THE BUG: `count` is written in this block beside the state and is
            // *not* reset. On the board it starts at whatever the flops power up
            // to, so the machine's first pass is non-deterministic — and the
            // simulator hides it, because the testbench initialises memory.
        end else begin
            case (state)
                S_IDLE:  state <= tick ? S_COUNT : S_IDLE;
                S_COUNT: begin state <= S_IDLE; count <= count + 4'd1; end
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule
