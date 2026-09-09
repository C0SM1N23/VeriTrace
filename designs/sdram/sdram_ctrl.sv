`timescale 1ns / 1ps
//
// A minimal SDR SDRAM controller — the §8.20 test design.
//
// It drives a real JEDEC command bus (CS#/RAS#/CAS#/WE#, BA, A) through a
// scripted sequence, and the whole point of it is `VIOLATE`: with that
// parameter set, three delays violate tRCD, tRP and tRFC. A fourth shortened
// gap ends at the fourth ACTIVATE: that is legal for tFAW, which restricts the
// fifth ACTIVATE. The original golden incorrectly expected four violations.
//
// Written as a flat step counter rather than an FSM on purpose: the exact
// cycle numbers *are* the thing under test, so they should be readable
// straight off the source rather than inferred from state transitions.
//
// At the 100 MHz clock the testbench uses, one step is 10 ns, and the
// MT48LC16M16A2 -7E minima (packs/timing/mt48lc16m16a2.toml) become:
//
//     tRCD 20 ns -> 2 steps     tRP  20 ns -> 2 steps
//     tRAS 44 ns -> 5 steps     tRC  66 ns -> 7 steps
//     tRRD 15 ns -> 2 steps     tRFC 66 ns -> 7 steps
//     tFAW 75 ns -> 8 steps
//
// The shortened gaps are each exactly `- BUG` short of those. Every other
// gap in the sequence was checked by hand to sit clear of *every* constraint,
// so the run produces three intended violations and no collateral ones —
// which is what makes the acceptance test able to assert an exact set.
//
module sdram_ctrl #(
    // 1 injects the timing bugs; 0 is the same controller, correct.
    parameter bit VIOLATE = 1'b1
) (
    input  logic        clk,
    input  logic        rst_n,

    output logic        cs_n,
    output logic        ras_n,
    output logic        cas_n,
    output logic        we_n,
    output logic [ 1:0] ba,
    output logic [12:0] a,

    output logic        done
);

  // ---- command encodings (JEDEC SDR SDRAM) --------------------------------
  // {cs_n, ras_n, cas_n, we_n}. CS# high is "deselected", which is the absence
  // of a command rather than a command of its own — hence no NOP row in the
  // protocol pack either.
  localparam logic [3:0] C_NOP       = 4'b1111;
  localparam logic [3:0] C_ACTIVATE  = 4'b0011;
  localparam logic [3:0] C_READ      = 4'b0101;
  localparam logic [3:0] C_WRITE     = 4'b0100;
  localparam logic [3:0] C_PRECHARGE = 4'b0010;
  localparam logic [3:0] C_REFRESH   = 4'b0001;

  // ---- datasheet minima, in clock cycles at 100 MHz -----------------------
  localparam int T_RCD = 2;
  localparam int T_RP  = 2;
  localparam int T_RRD = 2;
  localparam int T_RFC = 7;
  localparam int T_FAW = 8;

  // How long a row is held open before precharging. Comfortably above tRAS
  // (5 steps) so that tRAS never fires, and chosen so ACTIVATE -> PRECHARGE ->
  // ACTIVATE still clears tRC (7 steps) once the injected tRP bug has pulled
  // the second ACTIVATE one cycle earlier: 6 + 2 - 1 = 7. Getting this wrong
  // is how an injected tRP bug quietly becomes a tRC bug as well.
  localparam int ROW_OPEN = 6;

  // The injected bug: one cycle short of the minimum, in four places.
  localparam int BUG = VIOLATE ? 1 : 0;

  // ---- the scripted sequence ----------------------------------------------
  //
  //   VIOLATION 1 — tRCD: ACTIVATE b0 -> READ b0
  //   VIOLATION 2 — tRP : PRECHARGE b0 -> ACTIVATE b0
  //   VIOLATION 3 — tRFC: REFRESH -> ACTIVATE
  //   LEGAL tFAW CASE: the 4th ACTIVATE inside the rolling window
  //
  localparam int S_ACT0 = 4;                        // ACTIVATE b0, row 0x1A4
  localparam int S_RD0  = S_ACT0 + T_RCD - BUG;     // (1) tRCD
  localparam int S_PRE0 = S_ACT0 + ROW_OPEN;
  localparam int S_ACT1 = S_PRE0 + T_RP - BUG;      // (2) tRP
  localparam int S_PRE1 = S_ACT1 + ROW_OPEN;
  localparam int S_REF  = S_PRE1 + T_RP;
  localparam int S_ACT2 = S_REF + T_RFC - BUG;      // (3) tRFC
  localparam int S_WR   = S_ACT2 + T_RCD;           // conformant, exercises WRITE
  localparam int S_PRE2 = S_ACT2 + ROW_OPEN;

  // Four ACTIVATEs to four banks. The first three are tRRD apart; the fourth
  // lands inside tFAW of the first when the bug is injected, and just outside
  // it when it is not — while staying clear of tRRD either way. Both cases
  // satisfy tFAW: a fifth activation would be needed to exceed its limit.
  localparam int S_FAW0 = S_PRE2 + T_RP;
  localparam int S_FAW1 = S_FAW0 + T_RRD;
  localparam int S_FAW2 = S_FAW1 + T_RRD;
  localparam int S_FAW3 = S_FAW0 + T_FAW - BUG;     // legal fourth ACTIVATE

  localparam int N_STEPS = S_FAW3 + 12;

  logic [7:0]  step;
  logic [3:0]  cmd;
  logic [1:0]  cmd_ba;
  logic [12:0] cmd_a;

  assign {cs_n, ras_n, cas_n, we_n} = cmd;
  assign ba   = cmd_ba;
  assign a    = cmd_a;
  assign done = (step >= N_STEPS[7:0]);

  always_comb begin
    cmd    = C_NOP;
    cmd_ba = 2'd0;
    cmd_a  = 13'd0;
    case (step)
      S_ACT0:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd0; cmd_a = 13'h1A4; end
      S_RD0:   begin cmd = C_READ;      cmd_ba = 2'd0; cmd_a = 13'h030; end
      S_PRE0:  begin cmd = C_PRECHARGE; cmd_ba = 2'd0; cmd_a = 13'd0;   end
      S_ACT1:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd0; cmd_a = 13'h0C8; end
      S_PRE1:  begin cmd = C_PRECHARGE; cmd_ba = 2'd0; cmd_a = 13'd0;   end
      S_REF:   begin cmd = C_REFRESH;                                    end
      S_ACT2:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd1; cmd_a = 13'h200; end
      S_WR:    begin cmd = C_WRITE;     cmd_ba = 2'd1; cmd_a = 13'h018; end
      S_PRE2:  begin cmd = C_PRECHARGE; cmd_ba = 2'd1; cmd_a = 13'd0;   end
      S_FAW0:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd0; cmd_a = 13'h010; end
      S_FAW1:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd1; cmd_a = 13'h011; end
      S_FAW2:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd2; cmd_a = 13'h012; end
      S_FAW3:  begin cmd = C_ACTIVATE;  cmd_ba = 2'd3; cmd_a = 13'h013; end
      default: ;
    endcase
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) step <= 8'd0;
    else if (step < N_STEPS[7:0]) step <= step + 8'd1;
  end

endmodule
