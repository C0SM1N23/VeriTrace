# designs/deadlock — two nodes that wait for each other

The design behind Prompt 10's first acceptance criterion: **a real deadlock,
injectable and switchable off**, so that "no deadlock found" is as checkable as
"deadlock found".

```
tb_deadlock
├── node0   mailbox_node  N_TXN=16, HOLD, starts at once
└── node1   mailbox_node  N_TXN=16, HOLD, starts 20 cycles later

node0.master ──── link A ───> node1.slave
node1.master ──── link B ───> node0.slave
```

Each node has an AXI4-Lite master port pointed at its peer's mailbox and a slave
port the peer writes to. Two links, so two AXI4-Lite interfaces, so **two
agents** in §8.18's sense.

## The bug, in one wire

```systemverilog
wire s_allowed = !HOLD || !out_pending;
```

With `HOLD` set, a node will not accept an incoming write while one of its own
is still outstanding — one state machine serving both directions, which is among
the most common ways a real interconnect seizes up. It is *hold-and-wait*, the
second Coffman condition, in a single term.

node0 gets four writes through while node1 is still in its start delay. Then
node1 begins, both nodes are `out_pending` at once, each refuses the other, and
neither can retire its own write because the peer will not take it.

## Two dumps from one source

```sh
cd designs/deadlock
iverilog -g2012 -o sim.vvp *.sv                  && vvp sim.vvp     # dump.vcd
iverilog -g2012 -DNO_DEADLOCK -o sim_ok.vvp *.sv && vvp sim_ok.vvp  # dump_ok.vcd
```

A compile-time define rather than a runtime flag, so the bug is a property of
the elaborated design: it is visible in the graph and reachable from `why`,
which is where someone debugging it would actually meet it.

| | `dump.vcd` | `dump_ok.vcd` |
|---|---|---|
| transactions | 5 | 32 |
| deadlocks | 1 | 0 |
| Jain fairness | 0.50 | 1.00 |
| run | cut off at 603 cycles | finishes at 89 |

## What the tool says about it

```
$ veritrace perf designs/deadlock/dump.vcd --rtl designs/deadlock

node0.m
    608 cycles sampled, 599 lost (99%)
      reset              0.7%  ............................  4
      transfer           0.8%  ............................  5
      address_stall     97.5%  ###########################.  593
      response_wait      0.8%  ............................  5
      idle               0.2%  ............................  1
      = total          100.0%

DEADLOCK at c25, persistent 582 cycles

  node0.m  waits on  tb_deadlock.node0.m_awready  held by  node0.s
  node0.s  waits on  tb_deadlock.node0.s_awready  held by  node0.m
                                     `-- cycle

  Cycle of 2 agents. First blocked: WRITE#5 could not be issued at c25
  [why] why(tb_deadlock.node0.m_awready @ 255000)
```

Two things in that report are the point of the exercise. The **resources are
real wires** you can put a cursor on, not an abstraction — §8.18 warns
explicitly against a detector that only says "something looks stuck". And the
**first blocked transaction is named** even though it was never issued: there is
no row in the transaction table for `WRITE#5`, so the report says which one
*would* have been next rather than leaving the field blank.

Following the `why` on either link leaves the blocked node entirely and lands on
the peer's `out_pending` — the wire the bug is written on:

```
tb_deadlock.node0.m_awready = 0
  -> tb_deadlock.node1.s_awready = 0        (((s_free && s_allowed) && ...))
    -> tb_deadlock.node1.s_allowed = 0      (0 || !out_pending)
      -> tb_deadlock.node1.out_pending = 1  ((mstate == 2'd1) || (mstate == 2'd2))
```

## Why the nodes start at different times

A deadlock needs the two requests to cross. If both nodes started together they
would seize up on the first transaction and the dump would be dead from cycle
five — and a detector could then "find" it by noticing that nothing ever moved,
which proves nothing. The stagger gives the run real traffic first, so the
deadlock has to be found on a bus that had been working.
