# designs/sdram — a command bus with four timing bugs in it

The design behind Prompt 11's acceptance criteria: **four injected SDRAM timing
violations, one from each category**, and the same RTL compiled clean so that
"none found" is as checkable as "four found".

```
tb_sdram
└── ctrl   sdram_ctrl #(.VIOLATE(1))    drives a JEDEC SDR command bus
```

There is no SDRAM model on the other end, and there does not need to be: §8.20
analyses the **command bus**, so the controller driving it is the whole device
under test. Every number below is decoded from `cs_n/ras_n/cas_n/we_n`, `ba`
and `a` — nothing else is dumped, and nothing else is needed.

## The bug, in one parameter

```systemverilog
localparam int BUG = VIOLATE ? 1 : 0;

localparam int S_RD0  = S_ACT0 + T_RCD - BUG;   // (1) tRCD
localparam int S_ACT1 = S_PRE0 + T_RP  - BUG;   // (2) tRP
localparam int S_ACT2 = S_REF  + T_RFC - BUG;   // (3) tRFC
localparam int S_FAW3 = S_FAW0 + T_FAW - BUG;   // (4) tFAW
```

Four commands, each landing exactly one cycle early. At 100 MHz one step is
10 ns, so the MT48LC16M16A2 minima come out in whole cycles and the injected
gap is unambiguous.

Every *other* gap in the sequence was worked out by hand to sit clear of every
constraint — which is why the run produces these four violations and no
collateral ones, and why the test can assert an exact set rather than "at
least four". `ROW_OPEN = 6` exists for precisely that reason: at 5 it would
still satisfy tRAS, but the injected tRP bug would then drag the second
ACTIVATE inside tRC and turn one bug into two.

## Two dumps from one source

```sh
cd designs/sdram
iverilog -g2012 -o sim.vvp *.sv                   && vvp sim.vvp     # dump.vcd
iverilog -g2012 -DNO_VIOLATION -o sim_ok.vvp *.sv && vvp sim_ok.vvp  # dump_ok.vcd
```

| | `dump.vcd` | `dump_ok.vcd` |
|---|---|---|
| commands decoded | 13 | 13 |
| timing violations | **4** | **0** |
| tRCD / tRP / tRFC / tFAW | 1 each | conformant |

## What the tool says

```
$ veritrace memory designs/sdram/dump.vcd

ctrl   [mt48lc16m16a2]   4 banks   13 commands
    ACTIVATE=7  PRECHARGE=3  READ=1  REFRESH=1  WRITE=1
    ! tFAW violated 1 time(s)
        c44  ACTIVATE@c37 -> ACTIVATE@c44  (7 cycles, min 8)
    ! tRCD violated 1 time(s)
        c9   bank 0  ACTIVATE@c8 -> READ@c9  (1 cycles, min 2)
    ! tRFC violated 1 time(s)
        c29  REFRESH@c23 -> ACTIVATE@c29  (6 cycles, min 7)
    ! tRP violated 1 time(s)
        c15  bank 0  PRECHARGE@c14 -> ACTIVATE@c15  (1 cycles, min 2)
    ok  tRAS, tRC, tREFI, tRRD, tRTP, tWR, tWTR: conformant
```

Two things there are the point of the exercise. Each violation names **both
commands it was measured between and the exact cycle** — §8.20 warns against a
report that only says something looks wrong. And the constraints that held are
*listed*, because "conformant" has to be a stated fact rather than the absence
of a line.

## What the bank timeline shows

`veritrace memory dump.vcd "banks(ctrl)"` gives one row per bank, contiguous
and gap-free across the whole run. On this design bank 0 is the interesting
one: the tRP violation means the second ACTIVATE arrives **while the bank is
still precharging**, so that `precharging` segment is cut short at the
ACTIVATE rather than drawn overlapping it. The timeline shows what the
controller did; the checker says it was illegal.

## Why tFAW is 75 ns here

SDR SDRAM datasheets specify no tFAW at all — a single bank's activation
current is low enough that the four-bank window is a DDR-era constraint. The
value in `packs/timing/mt48lc16m16a2.toml` is therefore a controller-level
power policy rather than a datasheet number, and it is set at **5 × tRRD**,
the proportion the DDR generations that *do* specify tFAW land on.

That ratio also happens to be what makes the two constraints independently
testable at a 10 ns clock: at 60 ns, four ACTIVATEs at the tRRD minimum span
exactly 60 ns, so no stimulus could violate tFAW without violating tRRD at the
same time, and the injected bug would fire two constraints instead of one.
