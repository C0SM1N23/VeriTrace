# designs/axi_arb — two masters, one arbiter, and §8.16

The design behind the third acceptance criterion of Prompt 9: **transaction-level
why-trace has to cross the arbiter**. Master 0 spends the first half of the run
unable to issue anything, and the reason is not in master 0 at all.

```
tb_axi_arb
├── m0   arb_master  N_TXN=10, write-only      gated by grant == 0
├── m1   arb_master  N_TXN=10, write-only      always allowed to ask
├── arb  arbiter     fixed priority to m1, locked until the write response
└── slv  arb_slave   WAIT_STATES=8
```

`arbiter.sv` gives master 1 the bus whenever it has work, and holds it until the
write response comes back. Master 0 therefore issues nothing until master 1 has
finished all ten of its transactions — starvation, not merely contention.

## Three interfaces

Because the arbiter muxes the bus, the two masters and the slave are three
distinct sets of wires and so three interfaces. That also makes this the design
that exercises §11.4b's rule: two or more interfaces means the session opens on
the **Transactions** tab.

```
$ veritrace txn designs/axi_arb/dump.vtx --rtl designs/axi_arb
3 interface(s) - 40 transactions

m0    [AXI4-Lite]  10 WRITE  (30/30 channel events correlated, 100%)
m1    [AXI4-Lite]  10 WRITE  (30/30 channel events correlated, 100%)
slv   [AXI4-Lite]  20 WRITE  (60/60 channel events correlated, 100%)
```

## The question this design exists to answer

```
$ veritrace why designs/axi_arb/dump.vtx "why(txn.m0.WRITE[0].not_issued)" \
      --rtl designs/axi_arb

WRITE#0 on m0 was issued at c116; this is why it was not issued earlier, in c0-c116

tb_axi_arb.m0.awvalid = 0   [assigned] at c57
  ...
    -> tb_axi_arb.sel1 = 1              (grant == 2'd1)
      -> tb_axi_arb.arb.grant = 01      (lock ? owner : pick)
        ...
          -> tb_axi_arb.m1.awvalid = 0
            -> [txn] m1.WRITE[4]   at c49
                 m1: WRITE#4 still in flight here (awaddr=0x0)
              -> tb_axi_arb.s_bvalid = 1
                -> tb_axi_arb.slv.bvalid   [guard: ... && (wcnt >= 4'd8)]
                  -> [txn] slv.WRITE[4]
```

Three levels of abstraction in one answer, which is what §8.16 asks for: master
0's signal, master 1's *transaction*, and the slave transaction that one is
waiting on. A signal-level answer stops at `grant != 0` — true, and useless.

## Rebuild

```
make sim-icarus convert DESIGN=designs/axi_arb TOP=tb_axi_arb
make txn-why
```
