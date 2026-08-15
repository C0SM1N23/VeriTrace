`timescale 1ns / 1ps

// A two-stage RV32I subset — the realistic design of §14.1.
//
// Small on purpose, but it has the three structures that make a CPU hard to
// debug and that no other reference design here contains:
//
//   * a **register file read by a dynamic index** (`regfile[rs1]`), which is
//     §5.6's own example and the case where a causal chain has to jump back in
//     time to the instruction that wrote the value;
//   * a **pipeline register**, so a value read in one cycle was computed in the
//     one before it (§5.5's NBA rule, on a design where getting it wrong is not
//     obvious);
//   * **three deliberately injected bugs**, listed in README.md with the exact
//     root cause each one has. They are injected by this project, not taken
//     from anyone's bug list.
//
// Every bug is behind a `define` so the design can be built correct or broken
// from the same source: `-DFIX_ALL` makes it a clean CPU.

module cpu_top #(
    parameter int IMEM_WORDS = 32,
    parameter int DMEM_WORDS = 32
) (
    input  logic        clk,
    input  logic        rst_n,
    output logic [31:0] pc,
    output logic [31:0] instr,
    output logic [31:0] wb_data,
    output logic [4:0]  wb_addr,
    output logic        wb_en,
    output logic        retire_valid
);
    // ---- architectural state ------------------------------------------
    logic [31:0] regfile [32];
    logic [31:0] imem    [IMEM_WORDS];
    logic [31:0] dmem    [DMEM_WORDS];

    // ---- fetch ---------------------------------------------------------
    logic [31:0] pc_q;
    assign pc    = pc_q;
    assign instr = imem[pc_q[6:2]];

    localparam logic [6:0] OP_IMM = 7'b0010011;
    localparam logic [6:0] OP_REG = 7'b0110011;
    localparam logic [6:0] OP_LOAD = 7'b0000011;
    localparam logic [6:0] OP_STORE = 7'b0100011;
    localparam logic [6:0] OP_BRANCH = 7'b1100011;

    // ---- decode --------------------------------------------------------
    logic [6:0]  opcode;
    logic [4:0]  rs1, rs2, rd;
    logic [2:0]  funct3;
    logic [31:0] imm_i, imm_s, imm_b;

    assign opcode = instr[6:0];
    assign rd     = instr[11:7];
    assign funct3 = instr[14:12];
    assign rs1    = instr[19:15];
    assign rs2    = instr[24:20];
    assign imm_i  = {{20{instr[31]}}, instr[31:20]};
    assign imm_s  = {{20{instr[31]}}, instr[31:25], instr[11:7]};
    assign imm_b  = {{20{instr[31]}}, instr[7], instr[30:25], instr[11:8], 1'b0};

    // §5.6: two dynamically indexed reads. This is the whole reason this design
    // exists — asking why `rs1_data` is wrong has to resolve the index, name
    // one word, and jump to the cycle that wrote it.
    logic [31:0] rs1_data, rs2_data;
    assign rs1_data = regfile[rs1];
    assign rs2_data = regfile[rs2];

    // ---- execute -------------------------------------------------------
    logic [31:0] alu_b, alu_out;
    // A store carries its offset in the S-type fields, not the I-type ones —
    // using `imm_i` here would displace every store by a few words, which is a
    // bug this design is *not* trying to inject.
    always_comb begin
        if (opcode == OP_REG || opcode == OP_BRANCH) alu_b = rs2_data;
        else if (opcode == OP_STORE)                 alu_b = imm_s;
        else                                         alu_b = imm_i;
    end

    always_comb begin
        unique case (funct3)
            3'b000:  alu_out = (opcode == OP_REG && instr[30]) ? rs1_data - alu_b
                                                              : rs1_data + alu_b;
            3'b111:  alu_out = rs1_data & alu_b;
            3'b110:  alu_out = rs1_data | alu_b;
            default: alu_out = rs1_data + alu_b;
        endcase
    end

    logic take_branch;
`ifdef BUG_BRANCH_POLARITY
    // BUG 2 — BNE is decoded with the polarity of BEQ, so the loop in the test
    // program exits one iteration early. Root cause: this line.
    assign take_branch = (opcode == OP_BRANCH) && (rs1_data == rs2_data);
`else
    assign take_branch = (opcode == OP_BRANCH) &&
                         ((funct3 == 3'b000) ? (rs1_data == rs2_data)
                                             : (rs1_data != rs2_data));
`endif

    // ---- memory --------------------------------------------------------
    logic [31:0] mem_addr, load_data;
    assign mem_addr  = alu_out;
    assign load_data = dmem[mem_addr[6:2]];

    // ---- write-back ----------------------------------------------------
    assign wb_addr = rd;
    assign wb_data = (opcode == OP_LOAD) ? load_data : alu_out;
`ifdef BUG_X0_WRITABLE
    // BUG 1 — x0 is not hardwired to zero, so the first `addi x0, x0, 0` in
    // the program leaves a value behind and every later read of x0 is wrong.
    // Root cause: the missing `rd != 0` term here.
    assign wb_en = (opcode == OP_IMM || opcode == OP_REG || opcode == OP_LOAD);
`else
    assign wb_en = (opcode == OP_IMM || opcode == OP_REG || opcode == OP_LOAD) && (rd != 5'd0);
`endif

    assign retire_valid = (opcode != 7'b0);

    // ---- sequential ----------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pc_q <= 32'd0;
        end else if (take_branch) begin
            pc_q <= pc_q + imm_b;
        end else begin
            pc_q <= pc_q + 32'd4;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int i = 0; i < 32; i++) regfile[i] <= 32'd0;
        end else if (wb_en) begin
            regfile[wb_addr] <= wb_data;
        end
    end

    always_ff @(posedge clk) begin
        if (rst_n && opcode == OP_STORE) begin
`ifdef BUG_STORE_INDEX
            // BUG 3 — the store uses the *byte* address as a word index, so it
            // writes four words too far and the load that follows reads a word
            // that was never written. Root cause: the missing `[6:2]` slice.
            dmem[mem_addr[4:0]] <= rs2_data;
`else
            dmem[mem_addr[6:2]] <= rs2_data;
`endif
        end
    end
endmodule
