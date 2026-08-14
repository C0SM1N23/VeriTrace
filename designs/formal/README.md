# `designs/formal` — one property to break, one branch that cannot fire

`axil_wobble.sv` is an AXI4-Lite master with two deliberate faults, and neither
of them can be found by running it.

| Fault | Found by |
|---|---|
| `awaddr`/`wdata` come straight from a free-running counter, so they move while `awvalid` is high | §8.27, bounded model check |
| `if (retry > 3)` where `retry` is two bits wide | §8.35, cover reachability |

## §8.27 — the property, and the counterexample

```sh
veritrace formal --rtl designs/formal --pack axi4lite --depth 20
```

```
AXI4-Lite on axil_wobble — bmc, depth 20, smtbmc z3

  HELD (bounded, depth=4)  AXI_AWSTABLE
  HELD (bounded, depth=4)  AXI_ARSTABLE
  HELD (bounded, depth=4)  AXI_NODROP
  FAILED at step 4  AXI_WDATA_STABLE
      the write data changed before it was accepted

--- AXI_WDATA_STABLE: the counterexample, explained ---
  why(vt_formal_top.u_dut.wdata) at the step the assertion broke:
vt_formal_top.u_dut.wdata = ...0011   [assigned] at c5   axil_wobble.sv:56
  -> vt_formal_top.u_dut.tick = ...0011   [assigned] at c5   axil_wobble.sv:75
       (tick + 32'd1)   [guard: aresetn]
```

Three things in that output are the whole point of §8.27.

**No result says `PROVED`.** It says `HELD (bounded, depth=N)`, because bounded
model checking has nothing to say about cycle N+1. The model has no unqualified
member to reach for.

**The three that held say `depth=4`, not `depth=20`.** The search stopped where
it broke `AXI_WDATA_STABLE`, so nothing was examined past step 4. Reporting them
at the depth that was *asked for* rather than the depth that was *reached* would
be the same overstatement, one level in.

**The counterexample is a waveform, so the causal engine works on it unchanged.**
It is converted, elaborated against the generated harness, and why-traced with no
step in between — which is the loop §8.27 describes: property → counterexample →
root cause, without opening another tool.

## §8.35 — reachable, or dead

```sh
veritrace coverage dump.vcd --rtl designs/formal --json > cov.json
veritrace reach --rtl designs/formal --top axil_wobble --uncovered cov.json --depth 30
```

```
axil_wobble.sv:83  if "(retry > 3)"
  -> PROVED UNREACHABLE (depth=30) — dead code, not a missing test

axil_wobble.sv:81  if "(bvalid && bresp != 2'b00)"
  -> REACHABLE (formal), counterexample at depth 1
```

`retry` is `logic [1:0]`, so it cannot exceed 3 and the branch below it cannot
execute. No test will ever close that coverage hole. Telling that apart from a
hole worth writing a test for is the difference between "81% and I do not know
what to do with the rest" and "81%, and three of the rest cannot happen".

## Where the cover statements go

Into a **copy of the design**, inside the module. They cannot go in the wrapper:
a hole's condition is written in the module's own scope (`retry > 3`), and Yosys
does not resolve `u_dut.retry` from outside — it declares an implicit wire with
no driver, and every cover then comes back unreachable. That is the worst failure
available here, because the wrong answer is indistinguishable from the right one.

## Requirements

Yosys and SymbiYosys. On Windows they run through WSL, the same way Verilator
does; VeriTrace finds them either way (`python/veritrace/tools.py`).

```sh
wsl -d Ubuntu -u root -- apt-get install -y yosys z3
wsl -d Ubuntu -u root -- bash -c "git clone https://github.com/YosysHQ/sby && make -C sby install"
```
