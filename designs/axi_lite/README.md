# designs/axi_lite — the reference bus for the transaction engine

One AXI4-Lite master against a two-register slave. Unlike `designs/checks` and
`designs/fifo_buggy`, this design is **deliberately correct**: it is the fixture
the §8.14 extraction engine is measured against, so anything the tool reports
about it can be checked by hand against the source.

```
tb_axi_lite
├── cpu   axil_master  N_TXN=12, BAD_AT=9
└── regs  axil_slave   WAIT_STATES=2      registers at 0x00 and 0x04
```

## What it produces

12 writes and 12 read-backs, alternating between the two registers.

| | |
|---|---|
| Transactions | 12 WRITE, 12 READ |
| Channel events | 60 (12 x AW,W,B + 12 x AR,R) |
| Correlation | 100% |
| `latency` | 3 cycles — issue to response, with 2 wait states in between |
| `addr_latency` | 2 cycles — the wait states, measured on their own |
| `stall_cycles` | 4 — two on AW, two on W |

## The one deliberate flaw

Transaction 9 is aimed at `0x20`, which is not mapped. The slave answers
`SLVERR`, and the AXI4-Lite pack's response rules catch it:

```
$ veritrace check designs/axi_lite/dump.vcd --rtl designs/axi_lite
PROTOCOL (2)
  ...  cpu - cpu.WRITE[9]: the slave answered a write with something other than OKAY
  ...  cpu - cpu.READ[9]:  the slave answered a read with something other than OKAY
```

That is the only thing wrong with it. Everything else holds, including all four
temporal rules — which matters as much as the violation does: a checker that
fires on a correct design teaches people to ignore it.

## Rebuild

```
make sim-icarus convert DESIGN=designs/axi_lite TOP=tb_axi_lite
veritrace txn designs/axi_lite/dump.vtx --rtl designs/axi_lite
```

The extracted table is written to `dump.vtx/txn/cpu.parquet` and is readable
without this tool at all (§6.3):

```python
import polars as pl
pl.read_parquet("designs/axi_lite/dump.vtx/txn/cpu.parquet")
```
