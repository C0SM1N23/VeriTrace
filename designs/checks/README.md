# designs/checks — a fixture with problems on purpose

This design is broken **deliberately**. It exists so the automatic checks of
§8.4, §8.5, §8.11 and §8.11c have something real to find, and so the acceptance
criterion for the Checks tab can be tested rather than asserted:

> Opening a session on the test design reports all the injected problems
> automatically, with no manual query.

Do not fix anything here. `tests/test_checks.py` and `web/tests/checks.spec.ts`
both assert that every item below is still reported.

## What is wrong, and where

| Check | Where | What was done |
|---|---|---|
| `inferred_latch` | `checks_dut.sv` `dout` | `always_comb` that assigns on only two of four paths |
| `inferred_latch` | `checks_dut.sv` `decoded` | the incomplete case below infers one too |
| `case_no_default` | `checks_dut.sv` `decoded` | `case (mode)` covers 3 of 4 values, no `default` |
| `stuck` | `checks_dut.sv` `lock_r` | the release arm needs `rst_n` both low and high, so it never fires |
| `blocking_in_always_ff` | `checks_dut.sv` `shadow` | `=` inside `always_ff` |
| `nonblocking_in_always_comb` | `checks_dut.sv` `parity` | `<=` inside `always_comb` |
| `incomplete_sensitivity` | `checks_dut.sv` `gated` | `always @(mode)` also reads `rst_n` |
| `cdc_no_sync` | `checks_dut.sv` `status` | `parity` and `lock_r` (clk) sampled by a flop on `slow_clk` |
| `async_reset_no_sync` | `checks_dut.sv` `lock_r` | `rst_n` is async and its release is not registered |
| `x_source` | `checks_dut.sv` `uninit` | no reset and no write until late, so `acc` is X for most of the run |
| `x_optimism` | `checks_dut.sv` `xsel` | `case` falls through to `default` while its selector is X |
| `initial_value_dependency` | `checks_dut.sv` `cfg` | `logic [1:0] cfg = 2'b10;` read before anything writes it |
| `parameter_default` | `checks_dut.sv` `DEPTH` | `checks_top` sets `DEPTH = 8` but passes only `WIDTH` down |

## Why the run is long

The stuck detector's default threshold is 100 clock cycles (§8.4), so the
testbench runs for well over 300. `lock_r` freezes at cycle 4 and stays frozen
for the remaining 324, which is what makes it a finding rather than a signal
that merely happens to be quiet.

`slow_clk` has a deliberately unrelated period (17 ns against 5 ns) so the
clock-domain crossing is real, and so the "sampled in the risky window"
evidence on the CDC finding comes from the trace rather than from the
structure.

## Regenerating

```sh
make sim-icarus convert DESIGN=designs/checks TOP=tb_checks
```

Icarus warns about the non-blocking assignment in `always_comb`, which is the
point — that warning is one of the injected problems.
