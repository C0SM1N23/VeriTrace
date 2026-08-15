# VeriTrace

**A simulation failed. VeriTrace tells you why.**

```
$ veritrace triage sim.log
19 failure(s) in log -> 1 root cause(s)

[1] 19 failure(s)  -  tb_fifo_buggy.dut.rd_rst_n = 0 [constant] at c27   fifo_buggy.sv:30
      log:4  ERROR: assertion "p_fifo_drains" failed at time 275ns
      log:6  ERROR: assertion "p_fifo_drains" failed at time 285ns
      ... and 17 more
      why(tb_fifo_buggy.dut.rd_rst_n @ 275000)
```

Nineteen failing assertions, one tied-low reset, and a file and line to go fix.
No waveform opened, no signal named by hand.

VeriTrace is an RTL verification hub built entirely on free tooling — Icarus,
Verilator, ModelSim Starter, Vivado xsim. It parses SystemVerilog into a causal
signal-dependency graph, correlates that graph with a simulation trace, and
answers *"why does this signal have this value"* with a deterministic,
reproducible chain that ends at a line of source.

---

## Install

No wheel is published yet, so install from a checkout. You need Python
3.12 – 3.14, a [Rust toolchain](https://rustup.rs) for the trace engine, and
[Node](https://nodejs.org) for the interface.

```sh
git clone https://github.com/C0SM1N23/VeriTrace && cd VeriTrace
python -m venv .venv               # or: uv venv --seed --python 3.12 .venv
source .venv/bin/activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -e . && (cd web && npm install && npm run build)
veritrace --version
```

The Rust extension links against a real interpreter, so the virtualenv has to be
**activated** before `pip` runs — otherwise the build targets whatever Python is
on `PATH`. `python -c "import sys; print(sys.executable)"` should print a path
inside `.venv`; if it does not, the activation did not take.

**A simulator is a separate install** — VeriTrace reads waveforms, it does not
produce them. Any of the four free ones works;
[docs/SIMULATORS.md](docs/SIMULATORS.md) has the install line and the mandatory
flags for each. Start with Icarus (`apt install iverilog`, `brew install
icarus-verilog`, or the Windows installer from
[bleyer.org](https://bleyer.org/icarus/)) — it is the one `veritrace run` drives
for you, and it needs no flags.

You do not need one for the next section: the reference dumps are committed.

<details>
<summary>Why the Python range is pinned</summary>

It is set by PyO3, which refuses to compile against a CPython newer than it
knows about. `requires-python` in `pyproject.toml` and the `pyo3` version in
`crates/vt-py/Cargo.toml` are kept in step, so pip declines an unsupported
interpreter with a readable message instead of starting a source build that ends
in a wall of Rust output.
</details>

## Your first why-trace

Straight from the checkout, no simulator involved — `designs/fifo_buggy` ships
with the dump it produced:

```
$ veritrace why designs/fifo_buggy/dump.vcd --rtl designs/fifo_buggy \
      "why(tb_fifo_buggy.dut.full @ 455000)"

converting designs/fifo_buggy/dump.vcd -> designs/fifo_buggy/dump.vcd.vtx
tb_fifo_buggy.dut.full = 1   [assigned] at c45   fifo_buggy.sv:33
     ((wr_ptr[32'd4:32'd4] != rd_ptr[32'd4:32'd4]) && (wr_ptr[32'd3:32'd0] == rd_ptr[32'd3:32'd0]))
  -> tb_fifo_buggy.dut.rd_ptr = 00000   [assigned] at c45   fifo_buggy.sv:48
       5'd0   [guard: !rd_rst_n]
    -> tb_fifo_buggy.dut.rd_rst_n = 0   [constant] at c45   fifo_buggy.sv:30
  -> tb_fifo_buggy.dut.wr_ptr = 10000   [hold] at c45   fifo_buggy.sv:26
       held: no driver was enabled
    -> tb_fifo_buggy.dut.rst_n = 1   [assigned] at c45   tb_fifo_buggy.sv:17
         rst_n
      -> tb_fifo_buggy.rst_n = 1   [primary_input] at c45   tb_fifo_buggy.sv:10
    -> tb_fifo_buggy.dut.full = 1   [cycle] at c45
    -> tb_fifo_buggy.dut.wr_en = 0   [assigned] at c45   tb_fifo_buggy.sv:18
         wr_en
      -> tb_fifo_buggy.wr_en = 0   [primary_input] at c45   tb_fifo_buggy.sv:11

9 nodes in 10.4 ms
```

The FIFO reports full and never drains. Four levels down, `rd_rst_n` is a
constant 0 at `fifo_buggy.sv:30` — the read side is held in reset, so `rd_ptr`
never advances. Every line carries a file and a line number, and no signal was
named by hand except the one that looked wrong.

The same question in the interface, with the wave, the source and the tree
moving together:

```sh
veritrace serve designs/fifo_buggy/dump.vcd --rtl designs/fifo_buggy
```

Then press <kbd>w</kbd> on a signal, or type the query into the bar at the top.
On your own design, `veritrace run rtl/` does the simulation first and writes a
`.veritrace.toml` so nothing needs arguments after that.

## The first sixty seconds

If you have RTL and no waveform yet — which is where every project starts — one
command does the whole thing:

```sh
$ veritrace run rtl/
  7 source file(s), top module 'tb_cpu'
  simulated with Icarus Verilog in 1.4 s
  waveform: .veritrace/dump.vcd (512 signals)
  correlation: 487/512 signals (95.1%)

  2 stuck · 1 x sources · 4 lint
    ! top.arb.lock_r  frozen at 1 since c12   arb.sv:88

  Wrote .veritrace.toml - later commands need no arguments.
  veritrace serve .veritrace/dump.vtx --rtl rtl/
```

Drop your files in a folder and point at it. It works out the top module,
simulates, converts, and reports — and **a testbench with no `$dumpfile` still
produces a waveform**, because a generated module is compiled alongside it
rather than your sources being edited. Everything lands in `.veritrace/`.

`--fail-on stuck,x,cdc` makes the same command a CI gate, and `--serve` opens
the interface when it finishes. Only Icarus is driven from here; the other three
simulators keep their `make sim-<tool>` recipes, where their mandatory flags
already live.

<details>
<summary>If a waveform already exists</summary>

```sh
$ cd ~/projects/my_cpu
$ veritrace init
  Found 14 RTL file(s)
  top module: detected 'tb_cpu' - use this? [Y/n]
  clock signal: detected 'clk' - use this? [Y/n]
  reset signal: detected 'rst_n' - use this? [Y/n]
  Wrote .veritrace.toml

  Trace found: sim/dump.vcd
    converted to sim/dump.vcd.vtx - 512 signals
    correlation: 487/512 signals (95.1%)
    2 stuck - 1 x sources - 4 lint
      ! top.arb.lock_r  frozen at 1 since c12   arb.sv:88
      ! top.cpu.regfile  register with no reset, never written   regfile.sv:21
      ... 4 more in the Checks tab

$ veritrace serve
```

`init` guesses everything it can from the RTL — the module nobody instantiates
is the top, the signal in the first `@(posedge X)` is the clock — and writes a
complete `.veritrace.toml` so you never type the same six arguments twice.
Then it runs the checks on whatever dump it finds, because the first impression
should be *"it already knows something I didn't"*, not an empty config file.
</details>

## Where the trace comes from

This matters more than anything else in the tool: **the hierarchy in the trace
has to match the hierarchy in the RTL**, or correlation fails and every answer
after it is built on sand. `make sim-<tool>` has the flags right for all four.

| Simulator | Keeps hierarchy? | What it needs |
|---|---|---|
| **Icarus Verilog** | Yes, completely | nothing — the primary target |
| **Verilator** | **No, by default** | `-fno-inline --public-flat-rw --trace-max-array 1024` |
| **ModelSim** (Quartus Lite) | Yes, with `+acc` | `vsim -voptargs="+acc"` |
| **Vivado xsim** | Yes | `xelab -debug typical` |

Verilator inlines modules and deletes intermediate wires; ModelSim's optimiser
hides internal signals. Both are the same trap in different clothing, and both
are silent. `veritrace correlate` is how you find out:

```sh
$ veritrace correlate dump.vcd --rtl rtl/
93/96 signals correlated (96.9%)
  by method: exact=91, elements=2, not_traced=3
  2 not dumped but reconstructible from RTL:
    ~ tb_lanes.dut.g_lane[2].u_fifo.mem
```

The rate is a first-class number and what it could not place is named, rather
than quietly matched on a suffix. `--fail-under 90` makes it a build gate, and
`--json` gives a script the list rather than the sentence.

## What it does

### Find the signal

Every other command wants a hierarchical path, and nobody remembers
`tb_axi_arb.dut.m0.u_fifo.wr_ptr`:

```sh
$ veritrace signals dump.vcd wr_ptr
2 of 4183 signal(s) matching 'wr_ptr'
  tb_axi_arb.dut.m0.u_fifo.wr_ptr                          [4:0]  reg   612 event(s)
  tb_axi_arb.dut.m1.u_fifo.wr_ptr                          [4:0]  reg   588 event(s)
```

`why` and `cone` take a partial name directly when it can only mean one signal,
and list the candidates when it cannot — the same rule the correlator follows:

```sh
$ veritrace why dump.vcd --rtl rtl/ "why(dut.full @ c45)"
  dut.full -> tb_fifo_buggy.dut.full
```

### Ask why

```sh
$ veritrace why dump.vcd --rtl rtl/ "why(tb.dut.full == 1 @ c45)"
tb_fifo_buggy.dut.full = 1   [assigned] at c45   fifo_buggy.sv:33
     ((wr_ptr[4] != rd_ptr[4]) && (wr_ptr[3:0] == rd_ptr[3:0]))
  -> tb_fifo_buggy.dut.rd_ptr = 00000   [assigned] at c45   fifo_buggy.sv:48
       5'd0   [guard: !rd_rst_n]
    -> tb_fifo_buggy.dut.rd_rst_n = 0   [constant] at c45   fifo_buggy.sv:30
```

The graph comes from **pyslang's elaborated AST** — generate blocks expanded,
parameters resolved, instance arrays unrolled — so `top.g_lane[2].u_fifo.wr_ptr`
already reads the way the trace spells it. Each driver carries the guard it
fires under, accumulated down the AST with consumed `else` branches negated:

```
wr_ptr   guard !rst_n                      -> 5'd0
wr_ptr   guard rst_n && wr_en && !full     -> wr_ptr + 1
```

Three rules keep the answers honest:

- **Registers are explained at their clock edge**, with inputs read *before* it.
  Sampling at the edge gives "state is WAIT because next_state is WAIT" when
  next_state has already moved on.
- **"Constant for the whole run" only ends the chain when the RTL says the
  signal cannot change.** A register that merely never fired is the symptom, and
  stopping there would make the root cause depend on which simulator produced
  the dump.
- **Ordering is a heuristic; the tree is complete.** Causes are ranked by oldest
  last transition — what has not moved is the suspect — but every branch is
  present and expandable, so the result is deterministic.

### Find what is wrong before you ask

Opening a session runs every check with no prompting, because this is what turns
a debugger into something used daily:

```
▾ STUCK (7)
    tb.dut.lock_r        frozen at 1 since c4    (324 cycles)          [why]
▾ X SOURCES (1)
    tb.dut.uninit        register with no reset, never written         [why]
      └─ reaches tb.acc, tb.dut.acc, tb.dut.u_dut.acc
▾ LINT (10)
    tb.dut.status        parity (clk) crosses into status (slow_clk)   [why]
      └─ sampled in the risky window 4x in this trace, at c8, c25, c42, c76
      └─ Structural check: not formal CDC sign-off.
▾ PARAMETERS (1)
    tb.dut.u_dut.DEPTH   DEPTH = 4 left at its default
```

The lint is deliberately narrow. Generic lint is done better by Verilator and
slang, so VeriTrace **aggregates them** rather than competing, and implements
natively only the class that needs *both* the graph and the trace — the
"works in simulation, fails on the board" bugs:

| | |
|---|---|
| Inferred latch, incomplete case, multiple drivers | from slang's dataflow analysis |
| Blocking in `always_ff`, non-blocking in `always_comb`, incomplete sensitivity list | from the graph |
| Unsynchronised CDC, async reset with no synchronous deassert | from the graph, **with the cycles where the trace shows it actually sampled in the risky window** |
| Dependence on an initial value, X-optimism | from the graph **and** the trace |

That last column is the point. A static tool can tell you a crossing exists; it
cannot tell you it was sampled at c8, c25, c42 and c76 in the run you just did.

Every row has an exact source location and a working `[why]`. Suppressing one
requires a reason, and the suppression lives in the session file so it survives
re-running the simulation.

**Honesty about CDC**: the check is *structural* — it finds a crossing with no
two-flop synchroniser. It is not formal CDC sign-off: no reconvergence analysis,
no handshake-protocol proof, no glitch analysis. It catches the common real bug
(a signal passed straight between domains) and does not replace a dedicated tool
on a production design. That paragraph is attached to every CDC finding, not
buried here.

### Narrow 4000 signals to 8

```sh
$ veritrace cone dump.vcd --rtl rtl/ top.ctrl.ready --depth 4 --active-only
fanin cone of top.ctrl.ready, depth 4: 11 signal(s)
  38 dropped as inactive in the window
```

Backward BFS answers "what could have produced this", forward answers "if I
change this, what breaks". Intersecting with the signals that actually toggled
in the visible window removes the resets, straps and clocks that are in every
cone and never the answer.

### Feed the viewer you already use

You are not going to abandon GTKWave, so VeriTrace feeds it instead of competing
with it. **Any** analysis can be written as a save file:

```sh
veritrace why   ... --gtkw   > cone.gtkw    # GTKWave
veritrace cone  ... --do     > cone.do      # ModelSim
veritrace stuck ... --wcfg   > cone.wcfg    # Vivado xsim
veritrace why   ... --surfer > cone.surf    # Surfer
```

```tcl
# cone.do, generated — run it directly in ModelSim
add wave -group "Causal chain" -radix hexadecimal /tb/dut/ctrl/ready
add wave -group "Causal chain" /tb/dut/arb/lock_r
wave cursor add -time 1247ns -name "root cause"
```

Someone who never opens the VeriTrace UI still gets most of the value from one
command. This has been verified in real ModelSim (Quartus Prime Lite 20.1), not
just against a format description: the file loads with zero errors and the
signals carry the values VeriTrace reported.

### Work in transactions, not in `awvalid`

Nobody debugging an interconnect thinks in `awvalid`. They think "write 128 took
22 cycles". A **protocol pack** is a TOML file that says how to lift signals into
transactions, and the extraction engine that reads it knows nothing about any
protocol:

```sh
$ veritrace txn dump.vtx --rtl rtl/
3 interface(s) - 40 transactions

m0    [AXI4-Lite]   tb_axi_arb.m0
    clock tb_axi_arb.m0.aclk   reset tb_axi_arb.m0.aresetn   236 cycles sampled
    10 WRITE   (30/30 channel events correlated, 100%)
    table: dump.vtx/txn/m0.parquet
```

Interfaces are detected automatically — a pack lists the signals it needs and the
prefixes to try, so nothing has to be configured per project. `axi4`, `axi4lite`
and `handshake` ship with the tool; a new protocol is one file and about thirty
minutes.

Then query them:

```sh
veritrace txn dump.vtx "txn(m0, type=WRITE, addr=0x4000:0x5000)"
veritrace txn dump.vtx "txn(m0) | slowest(10)"
veritrace txn dump.vtx "txn(m0) | open()"     # never completed
```

Each interface's table is written to `dump.vtx/txn/<iface>.parquet`, which is
both the cache and the export: a colleague can plot latency without this tool,
without an API and without asking you.

```python
import polars as pl
pl.read_parquet("dump.vtx/txn/m0.parquet")
```

### Cross from a signal into the transaction holding it up

This is the part nothing else does. When a causal chain reaches a signal that
belongs to a known interface, it checks whether a transaction is in flight there
and carries on at that level:

```sh
$ veritrace why dump.vtx "why(txn.m0.WRITE[0].not_issued)" --rtl rtl/
WRITE#0 on m0 was issued at c116; this is why it was not issued earlier, in c0-c116

tb_axi_arb.m0.awvalid = 0   [assigned] at c57
  ...
    -> tb_axi_arb.arb.grant = 01        (lock ? owner : pick)
      -> tb_axi_arb.m1.awvalid = 0
        -> [txn] m1.WRITE[4]
             m1: WRITE#4 still in flight here (addr=0x0)
          -> tb_axi_arb.s_bvalid = 1
            -> tb_axi_arb.slv.bvalid   [guard: ... && (wcnt >= 4'd8)]
              -> [txn] slv.WRITE[4]
```

Three levels of abstraction in one answer: master 0's signal, master 1's
transaction, and the slave transaction that one is waiting on. A signal-level
answer stops at `grant != 0` — true, and useless.

Protocol rules from the pack are checked while extracting, and violations arrive
in the Checks tab alongside the latches and the stuck signals:

```sh
veritrace check dump.vcd --rtl rtl/ --fail-on protocol
```

### Configure the capture before the bug

```sh
veritrace probes top.ctrl.ready --depth 3 --format vivado  > ila_probes.tcl
veritrace probes top.ctrl.ready --depth 3 --format quartus > stp_probes.tcl
```

The signals in the causal cone are exactly the ones worth probing, so this is a
direct translation. It needs only the graph — no trace, no capture — which makes
the tool useful *before* the bug, when the ILA is being configured and the
choice of probes actually costs a board iteration to get wrong.

## The interface

```sh
veritrace serve dump.vcd --rtl rtl/
```

Opens on **Checks** when there is something to report and on **Wave** when there
is not, because an empty tab is exactly the blank canvas to avoid.

The query bar above every tab takes VTQ, and not only `why()`:

```
why(top.ctrl.ready == 0 @ c1247)     cone(ctrl.ready, depth=3, active=true)
find(wr_ptr)                          fanout(ctrl.ready, depth=2)
stuck(min_duration=c50)               edges(ctrl.ready, c100:c500)
hold(ctrl.ready, c100:c500)           xtrace()   fsm(state)   lint(top.dma)
```

Each one calls the same module the terminal command does, so the two cannot
answer differently. Partial names resolve the way the correlator resolves them —
unique suffix, and the candidates listed rather than a guess when there are
several.

### Wave

![The Wave tab](docs/img/wave-tab.png)

Traces are drawn on an **OffscreenCanvas inside a Web Worker** — never the DOM,
which dies at a few thousand signals. The main thread stays out of the render
loop: pan and zoom go from the wheel event straight to the worker, so React
never re-renders during a scroll. Measured at **0.67 ms average, 0.80 ms p95**
per frame over 40 rows, against the 16.7 ms that 60 fps allows.

Monochrome by design — colour is reserved for causality. The only exceptions are
the logic-analyser conventions: X in red, Z in violet. A value containing X or Z
is never rendered as a number.

Scroll pans, `⌃scroll` zooms, `⇧scroll` moves vertically; `⌘P` finds a signal,
`w` asks why about the selection, `?` lists every shortcut. Layout, groups,
radix, zoom and cursors save themselves and come back exactly as you left them.

### Causal

![The Causal tab](docs/img/causal-tab.png)

The chain as a column of cards, symptom at the top, root cause at the bottom.
Right-click a signal in Wave → *Why is this value here?*, or click `[why]` on
any finding. Secondary branches are collapsed and dimmed rather than hidden.

### Source

![The Source tab](docs/img/source-tab.png)

CodeMirror 6 with an amber gutter on the lines of the current chain, and a value
inlay beside every line showing what its signals hold **at the cursor time**.
Move the cursor in Wave and the numbers follow. Click an identifier to add it to
Wave; alt-click to ask why.

Editing RTL after a run is caught by a sha256 recorded with the session — a
banner, never a block.

### Transactions

A Gantt band per transaction over **Wave's own time axis** — the same view, so a
band and the handshake below it stay aligned at every zoom level. Colour means
what it means everywhere else: amber only for a transaction in the current
causal chain, magenta only for one that broke a protocol rule.

Clicking a band filters Wave to that interface's signals and jumps to its
window. Below it, every field and metric the pack defines, sortable. A
transaction that never completed gets a `why` that asks the right question.

A design with two or more interfaces opens on this tab rather than on Checks.

### Performance

The tab for the two daily questions: **why is it slow** and **why did it stop**,
answered with attributed numbers rather than impressions.

Every clock cycle gets exactly one label from a cascade the protocol pack
defines — `reset`, `transfer`, then the pack's own reasons in priority order,
then `other`. The shares add to **exactly 100%**, and the total is printed next
to the chart so it can be checked rather than trusted. `other` is a bucket of
its own on purpose: it means the *pack's* cascade has a gap, which is a
different fact from a bus that was idle, and merging the two would hide the only
signal that says the attribution is incomplete.

```
node0.m
    608 cycles sampled, 599 lost (99%)
      reset              0.7%  ............................  4
      transfer           0.8%  ............................  5
      address_stall     97.5%  ###########################.  593
      response_wait      0.8%  ............................  5
      idle               0.2%  ............................  1
      = total          100.0%
```

Beside it: a latency histogram with p50/p95/p99 and a clickable tail, outstanding
over time with a plateau called out when the series has found a ceiling, and a
fairness bar per master with Jain's index.

Deadlock, livelock and starvation are scans over the same transaction table, so
they run when the session opens — like the stuck detector — and appear in Checks
as ordinary findings. A wait-for cycle is reported the way §8.18 writes it, with
**real wires** rather than abstractions, and a `why` on every link:

```
DEADLOCK at c25, persistent 582 cycles

  node0.m  waits on  tb_deadlock.node0.m_awready  held by  node0.s
  node0.s  waits on  tb_deadlock.node0.s_awready  held by  node0.m
                                     `-- cycle

  Cycle of 2 agents. First blocked: WRITE#5 could not be issued at c25
```

Naming who holds a wire is a question about the design, so it needs RTL. Without
it the blockages are still listed and the report says why they have no holder,
rather than guessing one.

### Memory

Everything about an SDRAM controller in the vocabulary it is actually debugged
in — banks, rows, commands and timing — decoded from the command bus alone. No
simulator support, no testbench code, and no SDRAM model needed: the controller
driving the bus is the whole device under test.

A memory pack replaces `[[channel]]`/`[[transaction]]` with a decode table, and
the shipped one is §8.20's own:

```toml
[[command]]
name   = "ACTIVATE"
encode = { cs_n = 0, ras_n = 0, cas_n = 1, we_n = 1 }
args   = { bank = "ba", row = "a" }
```

The **timing checker** is the reason this exists — a violated `tRCD` corrupts
data intermittently and takes weeks to find by hand. All twelve constraints of
§8.20 are checked against a per-chip file in `packs/timing/`, and the report
names both commands and the exact cycle rather than saying something looks
wrong:

```
ctrl   [mt48lc16m16a2]   4 banks   13 commands
    ! tRCD violated 1 time(s)
        c9   bank 0  ACTIVATE@c8 -> READ@c9  (1 cycles, min 2)
    ok  tRAS, tRC, tREFI, tRRD, tRTP, tWR, tWTR: conformant
```

The constraints that *held* are listed too: "conformant" is a fact worth
stating, and an absent line is not one. CL/CWL say why they were not checked
rather than being reported as passing — measuring data latency needs a DQ
signal a pin-level pack does not declare.

Beside it: a **bank timeline**, one row per bank on Wave's own time axis, with
the open row labelled — and it shows what the controller *did*, so a segment
cut short by a too-early ACTIVATE stays cut short. A searchable **command
stream**, row **hit/miss/conflict** rates, and an **address-map inspector**
that decomposes an address into (bank, row, column) using the ranges the pack
states.

A design with a memory interface opens on this tab (§11.4b).

### Check the data arrived

*I wrote `0xDEADBEEF` to `0x4000` through the DMA. What reached memory?*

Every write transaction on every interface updates a byte-granular `addr →
value` model, and every read is compared against it. It is a scoreboard with no
testbench written, and it runs when the session opens:

```sh
$ veritrace track dump.vcd
dma.s_axi   [AXI4-Lite]   8 write(s), 8 read(s), 32 byte(s) compared
mem         [AXI4-Lite]   8 write(s), 8 read(s), 16 byte(s) compared
    path dma.s_axi -> mem: 8 write(s) matched by address and order

! 16 data mismatch(es)
    c8   dma.s_axi -> mem  0x0  byte(s) 0-1
        byte(s) 0-1 were enabled upstream and not downstream
    c58  dma.s_axi  0x0  byte(s) 0-1
        read back 0xdead0000 where 0xdeadbeef was written
```

Two answers, and the second is the one that saves the week: *which bytes*, and
*where in the path they changed*. The value is never what is followed — the same
word appears a thousand times in a real trace — so what is followed is the
**(address, sequence)** pair, and two interfaces are compared only when their
write address sequences are identical. That rule is what keeps two unrelated
masters that happen to share an address from being reported as corruption.

```sh
veritrace track dump.vcd "track(addr=0x10)"
veritrace track dump.vcd "track(from=src, to=sink)"   # a stream: order, not address
```

For a stream with no address, the check is the other one §8.19 names: the n-th
word in must be the n-th word out. Reordering, duplication, loss and words that
were never sent are told apart rather than merged.

Mismatches arrive in Checks as ordinary findings, at ERROR without qualification.
A latch might not matter; a word that came back different always does.

### See what you did not test

One tab, two sections, because they answer one question. **Functional coverage
is computed from the extracted transactions** — no covergroup, no simulator
feature, nothing to write:

```sh
$ veritrace coverage dump.vcd --rtl rtl/
cpu   [AXI4-Lite]   24 transactions   47%
    bresp                  field     2/4
        never: EXOKAY
        never: DECERR
    kind                   sequence  2/4
        never: READ x READ
        never: WRITE x WRITE
    partial_write          corner    0/1
        never: hit   a write that did not enable every byte lane
```

The bins come from the pack's declared domain, so a value that never occurred is
an **empty box rather than a missing row** — which is the whole point. A `[[cover]]`
entry is a field, a cross, a consecutive-pair matrix or a corner, and a corner is
an expression, so the engine still knows no protocol:

```toml
[[cover]]
corner = "burst_crosses_4k"
when   = "(addr % 4096) + (awlen + 1) * (1 << awsize) > 4096"
```

**Code coverage is imported**, from Verilator or from Vivado — both are valid
sources and neither is required:

```sh
verilator --binary --coverage --coverage-line rtl/*.sv tb.sv && ./obj_dir/Vtb
veritrace coverage dump.vcd --coverage logs/coverage.dat --rtl rtl/
```

And an uncovered point becomes the conditions that would close it, derived from
the graph rather than suggested:

```
fifo_buggy.sv:49 [branch]
    else if (rd_en && !empty) rd_ptr <= rd_ptr + 1'b1;
    rd_rst_n            NEVER observed
        <- rd_rst_n <= 0
    rd_en               24/46 cycles
    !empty              44/46 cycles
```

Three conjuncts, two of them routinely true, one never — and the assignment that
makes it never. That is the injected bug, reached from a coverage hole.

### Ask how complete it all is

`veritrace scorecard` puts every measurement in one page. It computes nothing of
its own: it reads what the other commands already emit, so a CI pipeline can
assemble one out of four separate jobs. This is `designs/axi_lite`, for real:

```
$ veritrace scorecard designs/axi_lite/dump.vcd --rtl designs/axi_lite --top tb_axi_lite \
      --mutation mut.json --formal formal.json --synth synth.json --plan designs/axi_lite/testplan.toml

VERITRACE SCORECARD — tb_axi_lite @ 87b5b25

   -  line coverage                       —   target >=90%
        no coverage database was imported
  XX  functional coverage               47%   target >=80%
        7/15 bins, 8 hole(s) open
  XX  mutation score                     0%   target >=70%
        0/11 mutants killed, seed 7; 1 did not build and are not scored
   !  formal                            4/7
        held to bounded depth 20, never proved unconditionally; 3 not checkable as written
  XX  protocol violations                 2   target 0
        over 24 transaction(s) in this run
  OK  synth-diff                  identical
        21 top-level port(s) compared, RTL vs yosys (WSL (Ubuntu))
  XX  deadlock/stuck                      8   target 0
        open stuck and deadlock findings, out of 23 finding(s) in this run

6 of 7 categories measured in this run. No line here says a design is verified —
that is not a claim this tool makes.
```

Read the rows that are *not* green, because they are the honest ones. **Mutation
score 0%**: the AXI testbench drives the bus and checks nothing, so not one of
the eleven mutants was caught — the design may be fine and the testbench is not
evidence either way. **Formal 4/7** says `held to bounded depth 20`, never
"proved": a bounded model check that finds no counterexample in twenty steps has
found none *in twenty steps*. And **line coverage** reads `—`, not 0%, because
no database was imported — a number nobody measured is not a low score.

A `testplan.toml` (§8.37) cross-references the same run against what you meant
to verify, and says when the document has drifted:

```
VERIFICATION PLAN — testplan.toml   17% of items covered

  missing   AXI-01     A write is answered with a response code the master reads
              missing  fcov:bresp   2/4 bins
              the plan says covered; this run does not show it
  covered   AXI-02     The write address holds until the slave accepts it
              hit      formal:AXI_AWSTABLE   HELD (bounded, depth=20)
```

The measurement always outranks the file. An item marked `covered` that this run
cannot show is reported as missing, with that last line naming the disagreement.

### Hand the whole screen to a colleague

§13.8's rule for every team feature: **files and git, nothing else.** No server,
no account, no cloud.

```sh
veritrace note "grant drops a cycle early" --signal top.u_dma.state --at c120 \
    -f notes/dma_deadlock.vtnotes
veritrace share dump.vcd -o notes/dma_deadlock.vtsession
```

Annotations are one line each in `notes/*.vtnotes` — reviewable in the pull
request that fixes the bug, and findable with `grep`:

```
top.u_dma.state @ 12500 :: never leaves ARB — grant drops a cycle early
rtl/dma.sv:42 :: this `if` should test busy too
```

A `.vtsession` carries the layout, the cursors, the annotations and the question
you were asking. It does **not** carry the dump — that is referenced by hash,
because your colleague already has the gigabytes and what they lack is the
certainty that it is the same run:

```sh
$ veritrace restore dma_deadlock.vtsession dump.vcd
layout    -> dump.vcd.vtx.session.json
notes     -> notes/dma_deadlock.vtnotes
query        why(top.u_dma.state @ 12500)

veritrace serve dump.vcd
```

Opening it re-asks the question against *their* dump rather than replaying your
answer, so what they see is computed from the data in front of them. If the hash
does not match, the layout is still applied — it is the useful half either way —
and the mismatch is stated, because bookmarks from another run point at times
that do not exist in theirs. The file is plain sorted JSON with no timestamp in
it, so committing it next to the bug it explains produces no churn.

## The `.vtx` store

A directory of Parquet plus one binary index, so the data is usable without this
tool:

```python
import pyarrow.parquet as pq
pq.read_table("dump.vtx/events/")     # (signal_id, time, delta, value)
```

`index.bin` is the only proprietary piece; Parquet gives scans, not `value_at`
in O(log n). Two rules the store is built around:

- `value_at(sig, t)` returns the **last** write at `t` — the settled value,
  never an intermediate delta-cycle glitch. Glitches stay reachable through
  `deltas_at` / `value_at_delta` rather than being discarded.
- `value_before(sig, t)` gives the value strictly before `t`. For
  `always_ff @(posedge clk) q <= d`, `d` may change at the same timestamp as the
  edge, so sampling it *at* the edge yields "q is 1 because d is 0".

```python
from veritrace import TraceStore
s = TraceStore("dump.vtx")
h = s.handle("tb.dut.wr_ptr")
s.value_at(h, 25000), s.value_before(h, 25000), s.deltas_at(h, 25000)
```

## Command reference

```sh
veritrace run   rtl/ [--top X]      # simulate + convert + check, in one command
veritrace init                      # detect everything, write .veritrace.toml
veritrace serve [trace] --rtl src/  # the interface
veritrace triage sim.log            # log -> root causes
veritrace why   trace "why(sig @ cN)"       [--json|--do|--gtkw|--wcfg|--surfer]
veritrace why   trace "top.ctrl.ready == 0 @ c1247"    # the wrapper is optional
veritrace why   trace "..." --json | jq '.chain[0].loc'   # the spine, flat
veritrace why   trace "why(txn.iface.WRITE[n].not_issued)"
veritrace cone  trace sig --depth N --direction fanin|fanout|both [--active-only]
veritrace stuck trace --cycles N
veritrace txn   trace ["txn(iface, type=WRITE) | slowest(10)"]
veritrace perf  trace ["stalls(iface)" | "deadlock()" | "latency(iface, by=master)"]
veritrace memory trace ["cmds(iface)" | "banks(iface)" | "timing(iface, chip=…)"]
veritrace track trace ["track(addr=0x10)" | "track(data=0x…)" | "scoreboard(iface)"]
veritrace coverage trace ["fcov(iface)" | "uncovered()"] [--coverage logs/coverage.dat]
veritrace repro trace "why(sig)" [-o tb.sv] [--mode minimal|focused] [--no-validate]
veritrace export trace --why "why(sig)" -o bug.html   # standalone report, no network
veritrace diff  good.vcd bad.vcd [--align cycle|handshake|retire] [--ignore "*_cnt"]
veritrace fsm   [trace] [signal] [--svg d.svg]        # state machines; no trace needed
veritrace gen-sva trace --iface top.dut.m_axi --target verilator|portable -o chk.sv
veritrace packs [--trace dump.vcd]   # protocol packs loaded, and what each matched
veritrace import-capture ila.csv --format vivado-ila|signaltap --scope top.dut
veritrace plugins                    # what analysis plugins this project has

veritrace mutate --rtl rtl/ --top tb [--run "make sim"] [--sample N --seed S]
veritrace synth-diff --rtl rtl/ --tb tb.sv --top tb   # RTL vs netlist, same testbench
veritrace formal --rtl rtl/ --pack axi4lite --depth 20    # HELD (bounded, depth=N)
veritrace reach  --rtl rtl/ --top dut --uncovered cov.json   # dead code, or a missing test
veritrace stimgen --rtl rtl/ --top dut --cover-holes fcov.json --target sv|cocotb
veritrace timing report.rpt [trace] --rtl rtl/   # Vivado's paths, over your run
veritrace saif  trace -o activity.saif [--gating]
veritrace wavedrom trace --signals "a,b,c" --range c120:c150 [--svg --light]

veritrace ingest --cocotb-log sim.log --trace dump.fst   # transactions a monitor recorded
veritrace ingest --uvm-tr-db uvm_tr.dat --trace dump.fst
veritrace record trace --rtl rtl/ --seed N --simulator icarus --simulator-version 12.0
veritrace history ["SELECT commit_sha, p99_latency FROM txn_metrics JOIN runs USING (run_id)"]
veritrace reproduce --run-id N --rtl rtl/       # the exact command, or why it is not identical
veritrace scorecard trace --rtl rtl/ [--mutation m.json --formal f.json --plan testplan.toml]

veritrace note "..." [--signal S --at cN | --loc file.sv:42] [-f notes/x.vtnotes]
veritrace share trace -o notes/bug.vtsession   # layout + notes + query, dump by hash
veritrace restore notes/bug.vtsession trace    # ...on the other machine

veritrace check trace --fail-on stuck,x,cdc,protocol,deadlock,memory,integrity  # the CI gate
veritrace probes sig --format vivado|quartus
veritrace correlate trace --rtl src/
veritrace convert dump.vcd -o dump.vtx
```

Every command accepts a raw `.vcd`/`.fst` and converts it on the way in, and
falls back to `.veritrace.toml` for the trace, the RTL and the top module.

## In CI

```yaml
- run: veritrace check dump.vcd --rtl rtl/ --fail-on stuck,x,cdc,protocol
- run: veritrace triage sim.log --trace dump.vcd --rtl rtl/
```

A failing build then carries the cause in its log rather than a dump nobody will
open. This repository does it to itself: `.github/workflows/ci.yml` runs `check`
and `triage` against the designs with deliberately injected bugs and fails if
the tool stops finding them.

## Reference designs

| | |
|---|---|
| `designs/fifo_async` | a clean synchronous FIFO — the baseline, 100% correlated |
| `designs/fifo_buggy` | the same FIFO with `rd_rst_n` tied low, plus the log it produces |
| `designs/lanes` | a for-generate and an instance array — the correlation stress case, and the one memory deeper than it is wide |
| `designs/cpu_top` | a two-stage RV32I subset with a register file read by a dynamic index — §5.6's own case, plus three injected bugs ([README](designs/cpu_top/README.md)) |
| `designs/handshake` | a `ready` computed from `valid`: the one bug in this repo that **no waveform can show** ([README](designs/handshake/README.md)) |
| `designs/multidriver` | two continuous assignments on one net: §8.1's `CONFLICT`, and an X that clears inside reset and comes back ([README](designs/multidriver/README.md)) |
| `designs/checks` | **eleven deliberate flaws**, one per check ([README](designs/checks/README.md)) |
| `designs/axi_lite` | a correct AXI4-Lite master and slave — the transaction fixture ([README](designs/axi_lite/README.md)) |
| `designs/axi_arb` | two masters, an arbiter and real starvation ([README](designs/axi_arb/README.md)) |
| `designs/deadlock` | two nodes waiting on each other — the bug is one `define` away from being gone ([README](designs/deadlock/README.md)) |
| `designs/sdram` | an SDR SDRAM command bus with four injected timing violations, one per category ([README](designs/sdram/README.md)) |
| `designs/dma` | a DMA path whose byte-enable mask drops two lanes — also one `define` from being clean ([README](designs/dma/README.md)) |
| `designs/fsm` | **one deliberate flaw per §8.8 check**, plus a clean control machine — and a testbench that deliberately never reaches most of them ([README](designs/fsm/README.md)) |
| `designs/mutation` | a **correct** FIFO with a thorough-looking testbench that never fills it — the design under test is the testbench ([README](designs/mutation/README.md)) |
| `designs/synth_mismatch` | an incomplete sensitivity list: the RTL and the netlist genuinely disagree, and no RTL run can show it ([README](designs/synth_mismatch/README.md)) |
| `designs/formal` | an AXI rule a solver breaks in four steps, and a branch proved unreachable ([README](designs/formal/README.md)) |
| `designs/axi_lite/timing_summary.rpt` | a Vivado timing report with one violated path the run never switches — §8.30's false priority |
| `designs/axi_lite/testplan.toml` | §8.37's coverage of intent, with one item the plan claims and the run does not show |
| `designs/cocotb` | a real cocotb monitor — its log is what §8.34 ingests, with no protocol pack and no signal scan |

## Status

Usable as a causal debugger, as a daily checker, and as a transaction-,
performance- and memory-level debugger. VCD parsing, the `.vtx` store,
value-at-time queries, a FastAPI server with downsampled streaming, the RTL
graph and correlation layer, why-trace, stuck / X-prop / cone / lint / parameter
checks, log triage, viewer and probe export, protocol packs with automatic
interface detection and rule checking, transaction-level why-trace, stall
attribution and the liveness scan, SDRAM command decode with the twelve timing
constraints, the automatic data-integrity scoreboard, functional coverage
from transactions with code coverage imported from Verilator or Vivado, causal
subtrace minimisation with a generated testbench that is compiled and run to
prove it reproduces, the standalone HTML bug report, first-divergence diff
between two runs, FSM extraction with five static checks that need no trace at
all, protocol checkers generated as SVA or as plain Verilog, and analysis
plugins — and the Wave, Causal, Source, Checks, Diff, Coverage, Transactions,
Performance and Memory tabs, plus Causal Replay and FSM modes.

Fifteen protocol packs ship in the repo: AXI4, AXI4-Lite, AXI4-Stream, AHB-Lite,
APB, Avalon-MM, Avalon-ST, Wishbone B4, SDR SDRAM, DDR3, SPI, I2C, UART, generic
handshake and RISC-V retire.

Measured on a 50 MB / 3.6 M-event synthetic dump over 5000 signals, against the
tier-A budget:

| Operation | Measured | Budget |
|---|---|---|
| VCD → `.vtx` conversion | 5.0 s | < 6 s |
| `value_at` (warm) | 0.5 µs | < 5 µs |
| Session open | 10 ms | < 1 s |
| `why()` on the reference design | 3–9 ms | < 150 ms |
| All checks on a 330-cycle design | 12 ms | — |
| Extracting 3 interfaces, 236 cycles | 25 ms | — |
| Wave frame, 40 rows | 0.67 ms avg | < 16.7 ms |

### FST support

Off by default, and **partly broken on Windows** — measured, not assumed.

`fstapi` compiles GTKWave's libfst from C and needs zlib plus libclang. On Linux
that is `zlib1g-dev` and `libclang-dev`; on Windows it additionally wants vcpkg
(`zlib`, `pthreads` and `mman`, triplet `x64-windows-static-md`) and a
`LIBCLANG_PATH`. VCD — the primary target — needs none of it.

```sh
cargo build --features vt-trace/fst
maturin develop --features fst        # to reach it from Python
```

**What actually happens on Windows/MSVC:** the value data reads correctly, and
the *hierarchy* does not. `fstReaderRecreateHierFile` duplicates the file
descriptor and hands it to `gzdopen` after "flushing" an input stream —
undefined behaviour that glibc tolerates and the MSVC CRT does not; the scratch
file libfst writes beside the dump comes out zero bytes long. So every signal
name is lost while all 302 events of the fixture arrive intact.

The reader **refuses** rather than converting anonymous events into a store that
looks fine:

```
$ veritrace convert dump.fst
Error: the FST hierarchy could not be read: the file declares 29 variable(s)
and libfst returned 0. The value data is intact, so this is a limitation of the
bundled libfst on this platform, not a corrupt dump — convert the same run to
VCD instead.
```

`crates/vt-trace/tests/fst.rs` holds both halves of the contract: where the
hierarchy reads, the FST store must match the VCD store signal for signal and
event for event (§14.2's round-trip); where it does not, the failure must name
the hierarchy. `designs/fifo_async/dump.fst` is the fixture — 1.2 KB, the same
run as `dump.vcd`, produced with `vvp sim.vvp -fst`.

**Icarus keeps the `$dumpfile` name whatever the format**, so `-fst` writes FST
content into a file called `dump.vcd`. Rename it, or the extension lies.

One more thing libfst does on Windows: it unpacks the hierarchy into a scratch
file beside your dump and then unlinks it while the handle is still open, which
Windows refuses — so a zero-byte `dump.fst.hier_<pid>_<addr>` is left behind on
every read. `.gitignore` covers them; nothing in the tool depends on them.

### A note on `$dumpvars`

Dumping unpacked-array elements needs `$dumpvars(1, dut.mem[0])`, which the LRM
does not allow and only Icarus accepts. The reference testbenches guard it with
`` `ifdef __ICARUS__ ``. VeriTrace also defines `VERITRACE` during elaboration,
so `` `ifndef VERITRACE `` hides anything else from the graph without affecting
simulation.

## Documentation

| | |
|---|---|
| [docs/SIMULATORS.md](docs/SIMULATORS.md) | installing each of the four, and the flags that are not optional |
| [docs/PACKS.md](docs/PACKS.md) | teaching VeriTrace a bus it does not know — one TOML file, no code |
| [docs/PLUGINS.md](docs/PLUGINS.md) | an analysis of your own that touches neither the core nor the UI |
| [docs/INSTALARE.md](docs/INSTALARE.md) | the install and first-run guide, in Romanian |
| `designs/*/README.md` | what bug each reference design carries, and which check is supposed to find it |

## License

**MIT** — see [LICENSE](LICENSE). Permissive for a concrete reason, not an
ideological one: VeriTrace reads your RTL and your waveforms as *data*. It does
not link into your design, and nothing it computes reaches a netlist, so its
license cannot travel into your code by any route. Nobody should need legal
review to run a debugger.

**Everything VeriTrace generates is yours** —
[LICENSE-GENERATED](LICENSE-GENERATED). Checkers from `gen-sva`, testbenches
from `stimgen` and every other source file it emits are released under
`CC0-1.0 OR MIT`, and each one carries that line in its own header. Keep them,
edit them, ship them, relicense them, attribute nothing. The tool's MIT terms
apply to the tool, never to its output.

## Development

```sh
make test        # cargo test + pytest + vitest
make test-web    # Playwright, needs `make serve-rtl` running
make bench       # the tier-A budget
make designs     # re-simulate every reference design
```

**1024 tests**: 74 Rust, 803 Python, 45 Vitest, 102 Playwright — 76 Rust with `--features fst` — with the
acceptance criterion of each stage tested rather than asserted.

Two of those suites are the ones worth knowing about. `tests/test_golden.py`
reads an `expected.toml` beside each reference design and checks that `why()`
lands on the root cause written there (§14.1) — adding a design means adding
data, not code. `tests/test_properties.py` generates random 4-state values and
checks the expression evaluator against a deliberately naive reference (§14.2),
which is where the cases nobody thinks of come from.
