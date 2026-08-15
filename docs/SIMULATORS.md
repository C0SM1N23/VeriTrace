# Installing a simulator

VeriTrace does not simulate. It reads what a simulator wrote, so you need one —
and all four it supports are free.

If you only want to get started, install **Icarus Verilog** and stop reading:
it is the one `veritrace run` drives for you, and it needs no flags to produce a
usable trace.

| | Free | Speed | Coverage | SystemVerilog |
|---|---|---|---|---|
| **Icarus Verilog** | yes, fully | slow | no | good subset (`-g2012`) |
| **Verilator** | yes, fully | fastest by far | line + toggle | synthesisable subset |
| **ModelSim / Questa Intel FPGA Starter** | free edition | medium | not in the free edition | very good |
| **Vivado xsim** | with Vivado ML Standard | medium | with `xcrg` | very good |

---

## The one thing that matters

**A simulator that optimises the hierarchy away breaks correlation.** VeriTrace
matches trace paths against RTL declarations; if the simulator inlined a module
or hid its internals, there is nothing to match and the tool degrades to a
waveform viewer (§7.4). The flags below are not tuning, they are the difference
between 95% and 40% correlation:

```sh
verilator  -fno-inline --public-flat-rw --trace-structs --no-trace-underscore
vsim       -voptargs="+acc"
xelab      -debug typical
iverilog   # nothing needed — Icarus keeps the full hierarchy
```

`make sim-<tool>` in this repository already carries them.

---

## Icarus Verilog

The primary target. Version 11 or newer; 12.0 is what this project is developed
against.

```sh
# Debian/Ubuntu
sudo apt install iverilog

# macOS
brew install icarus-verilog
```

**Windows:** the installer from <https://bleyer.org/icarus/>. It does **not**
put itself on `PATH`, but VeriTrace also looks in `C:\iverilog\bin`, so the
default install works without touching anything. Anywhere else, add it to
`PATH` — otherwise VeriTrace reports that no simulator is installed, with the
install line for your platform.

```sh
iverilog -g2012 -o sim.vvp rtl/*.sv tb/tb_top.sv && vvp sim.vvp
```

`-g2012` is required for SystemVerilog; without it `logic`, `always_ff` and
packages are syntax errors. A testbench with no `$dumpfile`/`$dumpvars` still
produces a waveform under `veritrace run`, which compiles a generated dumper
module alongside your sources rather than editing them.

## Verilator

Use it when the trace is large or the run is in CI — it is an order of magnitude
faster than the others, and it is the only free option with real coverage.

```sh
sudo apt install verilator          # 5.x
brew install verilator
```

**Windows:** no native build — run it from WSL. VeriTrace does not drive
Verilator (only Icarus, through `veritrace run`), so simulate however you like
and hand the resulting VCD to `veritrace check`.

```sh
verilator --binary --trace --trace-structs --trace-max-array 1024 \
          --no-trace-underscore -fno-inline --public-flat-rw \
          --top-module tb_top rtl/*.sv tb/tb_top.sv -o sim && ./obj_dir/sim
```

`-fno-inline` was `--no-inline` in Verilator 4 and is rejected under that name
by Verilator 5. Coverage:

```sh
verilator --binary --coverage --coverage-line --coverage-toggle ...
veritrace coverage dump.vcd --coverage logs/coverage.dat
```

## ModelSim / Questa — Intel FPGA Starter Edition

Ships inside **Quartus Prime Lite** (free, no licence file), in its
`modelsim_ase/` or `questa_fse/` directory. The free edition is limited to
10 000 executable lines and has no code coverage, but its SystemVerilog support
is the best of the four.

```tcl
vlib work
vlog -sv rtl/*.sv tb/tb_top.sv
vsim -voptargs="+acc" work.tb_top
vcd file dump.vcd
vcd add -r /tb_top/*
run -all
```

`+acc` is not optional: without it the optimiser hides internal signals and
correlation collapses in exactly the way `-fno-inline` prevents under Verilator.

## Vivado Simulator (xsim)

Ships with **Vivado ML Standard** (free, requires an AMD account). Worth using
when the design is already a Vivado project, or when you want `veritrace probes`
to hand you an ILA configuration for the same signals.

```sh
xvlog -sv rtl/*.sv tb/tb_top.sv
xelab -debug typical tb_top -s tb_sim
xsim tb_sim -t dump.tcl        # open_vcd / log_vcd [get_objects -r /tb_top/*] / run all
```

Coverage goes through `xcrg`:

```sh
xcrg -report_format xml -dir xsim.covdb -report_dir cov_report
veritrace coverage dump.vcd --coverage cov_report/dashboard.xml
```

---

## After the simulation

Any of the four gets you to the same place:

```sh
veritrace check dump.vcd --rtl rtl/     # what is wrong
veritrace serve dump.vcd --rtl rtl/     # the interface
```

VCD is read by all four and by VeriTrace. FST is smaller and faster, and this
build reads it only if the native extension was compiled with FST support — if
`veritrace convert` refuses an `.fst`, ask your simulator for VCD instead.

## Optional extras

Only needed for the commands that name them; nothing else degrades without them.

| Tool | Needed by | Install |
|---|---|---|
| **Yosys** | `synth-diff` (§8.29) | `apt install yosys` |
| **SymbiYosys + Z3** | `formal`, `reach` (§8.27, §8.35) | `apt install z3` + [SymbiYosys](https://github.com/YosysHQ/sby), or the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) which bundles all three |
| **cocotb** | producing a log to ingest (§8.34) | `pip install cocotb` — reading one needs nothing |

VeriTrace looks for each on `PATH` first and, on Windows, falls back to the same
tool inside WSL, translating `K:\proj` to `/mnt/k/proj` as it goes. A missing
tool is reported by name with the command that needs it — never as a stack
trace.
