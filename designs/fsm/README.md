# `designs/fsm` — one deliberate flaw per §8.8 check

Six state machines: five with an injected fault, one clean.

| Module | Flaw | Check that finds it |
|---|---|---|
| `fsm_good` | **none** — the control | nothing should fire |
| `fsm_dead` | `S_TRAP` self-loops forever | `fsm_dead_state` |
| `fsm_unreachable` | nothing transitions into `S_ORPHAN` | `fsm_unreachable_state` |
| `fsm_typo` | `if (ready && !ready)` | `fsm_impossible_transition` |
| `fsm_partial` | `S_WAIT` has one exit and no `else` | `fsm_incomplete_guards` |
| `fsm_unreset` | `count` is written in the clocked block and never reset | `fsm_incomplete_reset` |

## The point

**The testbench never reaches most of them.** `err` and `grant` are tied low, so
`S_TRAP`, `S_GO` and `S_ORPHAN` are never entered; the run passes and prints
`tb_fsm: done, no assertion fired`.

That is §8.8's whole argument:

> Un FSM cu o stare moarta e un bug garantat pe care nicio simulare nu-l gaseste
> daca stimulii n-au ajuns acolo.

So the checks read the **RTL**, not the waveform:

```sh
veritrace fsm --rtl designs/fsm          # no trace anywhere in this command
```

```
6 finding(s), from the RTL alone:
  [error] fsm_dead_state   S_TRAP is a dead state: nothing leaves it but reset
      fsm_dut.sv:71
  [error] fsm_dead_state   S_WAIT is a dead state: nothing leaves it but reset
      fsm_dut.sv:128
  [warn] fsm_incomplete_reset   1 register(s) of this machine are never reset: count
      fsm_dut.sv:176
  [warn] fsm_impossible_transition   S_WAIT -> S_IDLE can never be taken
      fsm_dut.sv:128
  [warn] fsm_unreachable_state   S_ORPHAN cannot be reached from S_IDLE
      fsm_dut.sv:99
  [info] fsm_incomplete_guards   S_WAIT holds when none of its 1 exit condition(s) is true
      fsm_dut.sv:159
```

Six findings for five flaws, and the sixth is right: `fsm_typo`'s only exit is
the contradictory guard, so the state genuinely cannot be left. Two true
statements about one bug, each pointing somewhere useful.

`fsm_good` produces **nothing**. A static check that fires on correct RTL is one
people switch off, and then the real findings go with it — so the control
machine is as much a part of the fixture as the broken ones.

## The dump

`dump.vcd` exists so §8.8 step 5's overlay has something to colour, and so this
design looks like the others. No check depends on it:

```sh
veritrace fsm designs/fsm/dump.vcd tb_fsm.bad_dead.state --rtl designs/fsm
```

```
    S_IDLE         reset  19 visit(s)  20 cycles
    S_WORK         18 visit(s)  18 cycles
    S_TRAP         NEVER VISITED
```

## `plugins/`

`plugins/state_dwell.py` is §13.7's worked example: cycles spent in each state,
as a table, plus an `info` finding for every state the run never entered. It is
discovered because it sits here — no install step, no registration.

```sh
veritrace plugins --root designs/fsm
```

## Rebuilding

```sh
cd designs/fsm && iverilog -g2012 -o sim.vvp *.sv && vvp sim.vvp
```
