`timescale 1ns / 1ps

// Drives `cpu_top` through a short RV32I program and dumps the register file
// word by word — Icarus needs an explicit `$dumpvars` per element for unpacked
// arrays (§5.6), and without it the regfile is invisible to the tool.
module tb_cpu_top;
    logic clk = 1'b0;
    logic rst_n = 1'b0;

    logic [31:0] pc, instr, wb_data;
    logic [4:0]  wb_addr;
    logic        wb_en, retire_valid;

    cpu_top dut (
        .clk(clk),
        .rst_n(rst_n),
        .pc(pc),
        .instr(instr),
        .wb_data(wb_data),
        .wb_addr(wb_addr),
        .wb_en(wb_en),
        .retire_valid(retire_valid)
    );

    always #5 clk = ~clk;

    // A program that exercises every injected bug:
    //
    //   0: addi x0, x0, 7      x0 must stay 0 (BUG_X0_WRITABLE)
    //   4: addi x1, x0, 5      x1 = 5      — reads x0, so it inherits bug 1
    //   8: addi x2, x0, 3      x2 = 3
    //   c: add  x3, x1, x2     x3 = 8      — two regfile reads at once
    //  10: addi x4, x0, 64     x4 = 64     — a word address for the store
    //  14: sw   x3, 0(x4)      dmem[16] = 8 (BUG_STORE_INDEX)
    //  18: lw   x5, 0(x4)      x5 = 8      — the load that reads it back
    //  1c: addi x6, x0, 1      x6 = 1
    //  20: bne  x5, x6, -4     loop while x5 != x6 (BUG_BRANCH_POLARITY)
    //  24: addi x7, x0, 9
    initial begin
        dut.imem[0]  = 32'h00700013;  // addi x0, x0, 7
        dut.imem[1]  = 32'h00500093;  // addi x1, x0, 5
        dut.imem[2]  = 32'h00300113;  // addi x2, x0, 3
        dut.imem[3]  = 32'h002081b3;  // add  x3, x1, x2
        dut.imem[4]  = 32'h04000213;  // addi x4, x0, 64
        dut.imem[5]  = 32'h00322023;  // sw   x3, 0(x4)
        dut.imem[6]  = 32'h00022283;  // lw   x5, 0(x4)
        dut.imem[7]  = 32'h00800313;  // addi x6, x0, 8
        dut.imem[8]  = 32'hfe629ee3;  // bne  x5, x6, -4
        dut.imem[9]  = 32'h00900393;  // addi x7, x0, 9
        for (int i = 10; i < 32; i++) dut.imem[i] = 32'h00000013;  // nop
        for (int i = 0; i < 32; i++) dut.dmem[i] = 32'h0;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_cpu_top);
`ifdef __ICARUS__
        // §5.6: unpacked-array elements need naming one by one, and only Icarus
        // accepts it. Eight words is enough for the program above and keeps the
        // dump small.
        $dumpvars(1, dut.regfile[0], dut.regfile[1], dut.regfile[2], dut.regfile[3],
                     dut.regfile[4], dut.regfile[5], dut.regfile[6], dut.regfile[7]);
        // Both ends of the store bug: `sw x3, 0(x4)` with x4 = 64 should write
        // word 16, and the broken index writes word 0 instead.
        $dumpvars(1, dut.dmem[0], dut.dmem[1], dut.dmem[2], dut.dmem[3]);
        $dumpvars(1, dut.dmem[16], dut.dmem[17], dut.dmem[18]);
`endif
        #12 rst_n = 1'b1;
        #400 $finish;
    end
endmodule
