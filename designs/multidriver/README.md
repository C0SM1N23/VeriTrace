# multidriver — two drivers on one net

The case §8.1 calls `CONFLICT` and §8.5 finds hardest.

```systemverilog
assign y = a;   // driver 1
assign y = b;   // driver 2 — same net
```

While `a == b` the net resolves cleanly. The moment they disagree it is X, and
waiting does not help: nothing will make it settle. Two modules driving one wire
is a real bug class, and it is the **only** kind of X whose cause the tool can
state with certainty rather than trace back to.

## What it is here to catch

The stimulus is arranged deliberately:

| Window | `a`, `b` | `y` |
|---|---|---|
| reset, up to 20 ns | agree | clean |
| 30–40 ns | disagree | **X**, then clears |
| 80 ns onwards | disagree | **X**, and stays |

The middle window is the trap. An X scan that decides "still broken?" by
sampling one instant after reset sees `y` resolved at that moment and drops the
signal — and then the persistent X at 80 ns is never reported at all. §8.5 has
to ask whether the signal is X *anywhere* after the reset window, which is what
`first_x_from` is for.

## Expected

```sh
veritrace check designs/multidriver/dump.vcd --rtl designs/multidriver
#   X SOURCES (1)
#     tb_multidriver.dut.y   more than one driver active at the same time

veritrace why designs/multidriver/dump.vcd --rtl designs/multidriver \
    "why(tb_multidriver.dut.y @ 100000)"
#   tb_multidriver.dut.y = x   [conflict] at c9   multidriver.sv:20
#        2 drivers active at once
```

Both drivers are then explored, because either one could be the one that should
not be there. `tied_off` sits beside them as the `CONSTANT` terminal's case.

`expected.toml` is what `tests/test_golden.py` checks on every run.
