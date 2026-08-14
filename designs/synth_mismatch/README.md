# `designs/synth_mismatch` — a mismatch that only a netlist can show

One module, two multiplexers, one difference: `y` is written from an `always`
block whose sensitivity list names only `sel`, and `y_ok` is written from
`always @*`.

A simulator obeys the list, so `y` is recomputed only when `sel` changes. A
synthesiser has no such notion for combinational logic — it reads a mux and
builds one. The RTL and the gates therefore behave differently, and **no amount
of simulating the RTL can reveal it**: every run agrees with itself.

## What §8.29 says about it

```sh
veritrace synth-diff --rtl designs/synth_mismatch --tb designs/synth_mismatch/tb_mux.sv --top tb_mux
```

```
yosys (WSL (Ubuntu)) · 6 top-level ports compared

FIRST DIVERGENCE at c0
  dut.y     rtl = x     gate = 1
```

`dut.y_ok` is compared in the same run and never diverges. That is the control:
a check that fires on correct RTL as well as broken RTL says nothing, so the
correct mux is part of the fixture.

## Why `x` and not a wrong number

`sel` is initialised to 0 and does not move until halfway through the run, so the
buggy block never executes at all — `y` is still uninitialised while `b` is being
driven. The netlist's mux follows `b` from the first cycle. The disagreement is
therefore visible from c0, which is the earliest a divergence can be reported and
the easiest kind to act on.

§8.11 already flags this pattern statically, as a *suspicion*. This is the same
bug **demonstrated**: two waveforms, one testbench, one signal that differs.
