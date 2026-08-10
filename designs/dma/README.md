# designs/dma — §8.19's worked example, as a design

A DMA path with a byte-enable bug injected, and the same RTL one `define` away
from being clean. It is the fixture for the automatic scoreboard (§8.19) and for
TAB 7's functional coverage (§8.21).

## The path

```
tb_dma  --s_axi_*-->  dma_bridge  --m_axi_*-->  axil_mem
```

Two AXI4-Lite interfaces, not one. That matters: a bridge that merely renamed
the nets would show up in the trace as a single bus under two names, and there
would be no *path* for a corruption to happen **in**. `dma_bridge` is
store-and-forward, one transaction in flight, so the two ports are genuinely
separate event streams a few cycles apart.

The testbench writes eight words at `0x00`–`0x1C`, the first of them
`0xDEADBEEF` — §8.19's own value — and then reads the same eight addresses back.
Nothing in the testbench checks anything. The point is that the tool finds the
bug from the trace alone, which is what "a free scoreboard" means.

## The bug

One line of `dma_bridge.sv`:

```systemverilog
localparam logic [3:0] STRB_MASK = 4'b1100;   // 4'b1111 with -DNO_CORRUPTION
...
wstrb_q <= s_axi_wstrb & STRB_MASK;
```

The mask is applied to the **byte enables** and not to the data, so the word
still looks correct on the CPU side of the flop and only the lanes that reach
memory are short. `axil_mem` is deliberately correct about `wstrb` — it writes
exactly the lanes it is told to — because a memory that ignored the byte enables
would paper over the bug and leave the scoreboard nothing to find.

## What VeriTrace should report

```sh
veritrace track dump.vcd
```

Sixteen mismatches, eight of each kind, every one naming bytes `0-1`:

* **path** — the same write seen on both interfaces, with lanes 0 and 1 enabled
  upstream and not downstream. This is *where in the path* it happened.
* **scoreboard** — the read-back at `0x00` returning `0xDEAD0000` where
  `0xDEADBEEF` was written. This is *what* is wrong.

The two are complementary and both are needed: the scoreboard alone says the
data is wrong, the path comparison alone says the bus changed it. Together they
name a byte lane and a hop.

## Both halves

```sh
iverilog -g2012 -o sim.vvp *.sv                     && vvp sim.vvp     # dump.vcd
iverilog -g2012 -DNO_CORRUPTION -o sim_ok.vvp *.sv  && vvp sim_ok.vvp  # dump_ok.vcd
```

`dump_ok.vcd` must report **no** mismatches — while still saying that eight
writes were compared across the path. "Found nothing" and "checked nothing" are
different results, and only one of them is good news.

## Why it is also the coverage fixture

The functional matrix over this design has real holes: every write drives
`wstrb = 4'b1111` on the CPU side, so `partial_write` is never hit, and every
address is word-aligned, so `unaligned` is never hit. Both are facts about
`tb_dma.sv` that can be checked by reading it — which is what makes the coverage
assertions in `tests/test_coverage.py` and `web/tests/coverage.spec.ts` worth
anything.
