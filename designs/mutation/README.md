# `designs/mutation` — a correct design and a testbench with a blind spot

The FIFO here has **no injected bug**. It is correct. The thing under test is
`tb_fifo.sv`, and what §8.28 is about is what a thorough-looking testbench still
fails to observe.

The testbench keeps a reference queue, compares every read against it, and checks
`empty` *and* `full` on all 200 iterations — 534 assertions, all passing. What it
never does is let the FIFO fill: `HIGH_WATER` is 3 against a `DEPTH` of 8.

## What mutation testing says about it

```sh
veritrace mutate --rtl designs/mutation --top tb_fifo
```

```
Mutation score: 79% (26/33)
  33 of 33 sites, seed 0 · iverilog + vvp (top tb_fifo)

Survivors grouped by file:
  fifo.sv:25   (wr_ptr[AW] != rd_ptr[AW]) && ...  ->  '0    stuck-at
  fifo.sv:25   [AW] -> [(AW)+1]                   (x2)      index
  fifo.sv:25   1 -> 2, 0 -> 1                     (x4)      constant
```

**Every survivor is on line 25** — the expression that decides when the FIFO is
full. Line coverage for that line is 100%: it is a continuous assignment, so it
"executes" on every cycle of the run. Coverage cannot see this. Mutation testing
names it in one number.

## The manual check

Raise `HIGH_WATER` from 3 to 8 so the tests reach the full condition, and run it
again:

| | score | survivors |
|---|---|---|
| `HIGH_WATER = 3` — never fills | **79%** | 7, all on line 25 |
| `HIGH_WATER = 8` — fills it | **94%** | 2, both on line 25 |

Five mutants that survived now die, and they die because of a one-line change to
the *testbench*. That is the claim §8.28 makes, checked rather than asserted.

## Why two still survive

Both are `wr_ptr[AW]` → `wr_ptr[(AW)+1]`, an out-of-range bit select. Even at
`HIGH_WATER = 8` the testbench stops writing *at* the point the FIFO becomes
full, so it never attempts a write while `full` is high — and gating writes is
the only thing `full` does. The mutant is observable: it finishes with 401
assertions instead of 534, because the run takes a different path. Nothing
*contradicts* the reference model, so nothing fails.

Closing that needs a directed test, not a longer random one: write while full,
and check that nothing was written. Which is the point — the survivor list is a
list of tests to write.
