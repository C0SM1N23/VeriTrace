# cpu_top — a two-stage RV32I subset

The realistic design of §14.1, and the only one here with the three structures
that make a processor hard to debug:

- **a register file read by a dynamic index** — `regfile[rs1]`, which is §5.6's
  own worked example. Asking why a value is wrong has to resolve the index,
  name one word, and jump back to the instruction that wrote it;
- **a data memory**, addressed by a computed value;
- **a branch**, so control flow itself can be the bug.

The committed `dump.vcd` is built with `BUG_STORE_INDEX`. The other two bugs
build from the same source.

## The injected bugs

Three, each behind a `define`, each with one exact root cause. They are injected
by this project — not taken from anyone else's bug list.

| Define | Symptom | Root cause |
|---|---|---|
| `BUG_STORE_INDEX` | `x5` is 0 after `lw`, when the `sw` before it stored 8 | `cpu_top.sv` — the store uses `mem_addr[4:0]` as a **word** index, so it writes word 0 instead of word 16. The load reads word 16, which nothing ever wrote. |
| `BUG_X0_WRITABLE` | every register is wrong from the second instruction on; `x0` reads 7 | `wb_en` is missing its `rd != 0` term, so `addi x0, x0, 7` really writes `x0` |
| `BUG_BRANCH_POLARITY` | `x7` never gets written and `pc` stops advancing at 0x20 | `BNE` is decoded with `BEQ`'s polarity, so a branch that should fall through loops forever |

```sh
cd designs/cpu_top
iverilog -g2012 -DBUG_X0_WRITABLE -o sim.vvp cpu_top.sv tb_cpu_top.sv && vvp sim.vvp
iverilog -g2012 -o sim.vvp cpu_top.sv tb_cpu_top.sv && vvp sim.vvp   # clean
```

Clean, the program ends with `x = [0, 5, 3, 8, 64, 8, 8, 9]` and `dmem[16] = 8`.

## The program

```
 0: addi x0, x0, 7      x0 must stay 0                    <- BUG_X0_WRITABLE
 4: addi x1, x0, 5      x1 = 5
 8: addi x2, x0, 3      x2 = 3
 c: add  x3, x1, x2     x3 = 8   — two dynamic reads at once
10: addi x4, x0, 64     x4 = 64  — a byte address
14: sw   x3, 0(x4)      dmem[16] = 8                      <- BUG_STORE_INDEX
18: lw   x5, 0(x4)      x5 = 8   — reads it back
1c: addi x6, x0, 8      x6 = 8
20: bne  x5, x6, -4     equal, so falls through           <- BUG_BRANCH_POLARITY
24: addi x7, x0, 9      x7 = 9
```

## What it is here to catch

```sh
veritrace why designs/cpu_top/dump.vcd --rtl designs/cpu_top \
    "why(tb_cpu_top.dut.load_data @ 65000)"
#  tb_cpu_top.dut.load_data = 0   [assigned] at c6   cpu_top.sv:107
#       dmem[mem_addr[6:2]]
#    -> tb_cpu_top.dut.dmem[16] = 0   [assigned] at c0
#    -> tb_cpu_top.dut.mem_addr = 64  [assigned] at c6
```

The index is evaluated, the *word* is named, and the chain lands on the fact
that word 16 was last touched at c0 — nothing wrote it during the run. Before
§5.6 was implemented this walked into the array as a whole, found it
unreadable, and blamed whichever guard happened to be false.

`$dumpvars` names the first eight registers and seven memory words one by one:
Icarus is the only simulator that accepts that for unpacked arrays, and without
it the register file is invisible to any tool (§5.6's own warning).

`expected.toml` is what `tests/test_golden.py` checks on every run.
