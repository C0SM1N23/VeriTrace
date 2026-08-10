# Reference simulator invocations and the VeriTrace commands that follow them.
#
# §4.0 is blunt about why this file exists: nobody reads documentation for
# simulator flags, and the flags are not optional. Without Verilator's
# -fno-inline or ModelSim's +acc the trace loses the hierarchy that correlation
# depends on, and the tool stops working for reasons that look like bugs.
#
# All four dump targets are real recipes, not printed advice. Each one checks
# that its simulator is on PATH first and says so plainly if it is not, because
# the free editions are large installs nobody has all four of.

DESIGN  ?= designs/fifo_buggy
TOP     ?= tb_fifo_buggy
SOURCES ?= $(notdir $(wildcard $(DESIGN)/*.sv))
VTX      = $(DESIGN)/dump.vtx

.PHONY: help sim-icarus sim-verilator sim-modelsim sim-xsim \
        convert serve serve-rtl correlate check stuck triage export probes \
        txn txn-why perf deadlock memory sdram track coverage dma \
        web web-build test test-web test-all bench designs clean

help:
	@echo "Simulate:  sim-icarus  sim-verilator  sim-modelsim  sim-xsim"
	@echo "Analyse:   convert  correlate  check  stuck  triage  export  probes"
	@echo "Protocol:  txn  txn-why      (§8.13-8.16)"
	@echo "Perf:      perf  deadlock    (§8.17-8.18)"
	@echo "Memory:    memory  sdram      (§8.20)"
	@echo "Data:      track  coverage  dma  (§8.19, §8.21, §8.12)"
	@echo "Serve:     serve  serve-rtl  web"
	@echo "Test:      test  test-web  test-all  bench"
	@echo ""
	@echo "DESIGN=$(DESIGN)  TOP=$(TOP)"

# `command -v` rather than a hard failure: the point is a readable message.
define require
	@command -v $(1) >/dev/null 2>&1 || { \
	  echo "$(1) is not on PATH. $(2)"; exit 1; }
endef

## --- the four simulators (§4.0) -------------------------------------------

## Icarus Verilog — the primary target. Keeps the full hierarchy with no flags.
sim-icarus:
	$(call require,iverilog,Install it from https://bleyer.org/icarus or your package manager.)
	cd $(DESIGN) && iverilog -g2012 -o sim.vvp $(SOURCES) && vvp sim.vvp | tee sim.log

## Verilator — for large traces and for CI. The flags are mandatory: without
## them it inlines modules away and correlation falls apart.
##
## NOTE: §4.0 spells the inlining flag `--no-inline`, which Verilator 5 renamed
## to `-fno-inline` and now rejects outright. The intent is unchanged.
sim-verilator:
	$(call require,verilator,Install it with apt/brew, or run it from WSL on Windows.)
	cd $(DESIGN) && verilator --binary --trace --trace-structs \
	    --trace-max-array 1024 --no-trace-underscore \
	    -fno-inline --public-flat-rw \
	    --top-module $(TOP) $(SOURCES) -o sim_verilated \
	  && ./obj_dir/sim_verilated | tee sim.log
	@echo "# Verilator 4: use --no-inline instead of -fno-inline"

## ModelSim / Questa Intel FPGA Starter Edition. +acc is not optional — without
## it the optimiser hides internal signals and correlation suffers exactly as it
## does under Verilator. It is the same trap in a different tool.
sim-modelsim:
	$(call require,vsim,Install Quartus Prime Lite; vsim ships in its modelsim_ase directory.)
	cd $(DESIGN) && printf '%s\n' \
	  'vlib work' \
	  'vlog -sv $(SOURCES)' \
	  'vsim -voptargs="+acc" work.$(TOP)' \
	  'vcd file dump.vcd' \
	  'vcd add -r /$(TOP)/*' \
	  'run -all' \
	  'quit -f' > dump_modelsim.do \
	  && vsim -c -do dump_modelsim.do | tee sim.log

## Vivado Simulator (xsim), from Vivado ML Standard.
sim-xsim:
	$(call require,xvlog,Install Vivado ML Standard; xvlog/xelab/xsim ship with it.)
	cd $(DESIGN) && xvlog -sv $(SOURCES) \
	  && xelab -debug typical $(TOP) -s $(TOP)_sim \
	  && printf '%s\n' \
	    'open_vcd dump.vcd' \
	    'log_vcd [get_objects -r /$(TOP)/*]' \
	    'run all' \
	    'flush_vcd' \
	    'close_vcd' \
	    'quit' > dump_xsim.tcl \
	  && xsim $(TOP)_sim -t dump_xsim.tcl | tee sim.log

## --- analysis -------------------------------------------------------------

## Convert the dump to a .vtx store.
convert:
	veritrace convert $(DESIGN)/dump.vcd -o $(VTX)

## Correlation report: how much of the RTL the trace actually covers.
correlate: convert
	veritrace correlate $(VTX) --rtl $(DESIGN)

## Every automatic finding — what §13.9 puts in CI.
check: convert
	veritrace check $(VTX) --rtl $(DESIGN)

## Just the frozen signals (§8.4).
stuck: convert
	veritrace stuck $(VTX) --rtl $(DESIGN)

## Root causes from the simulation log (§8.10b). Needs `sim.log` from a run.
triage: convert
	veritrace triage $(DESIGN)/sim.log --trace $(VTX) --rtl $(DESIGN)

## A ModelSim save file for the causal chain, ready to `do` (§13.3).
export: convert
	veritrace why $(VTX) --rtl $(DESIGN) "why($(TOP).dut.full)" --do -o $(DESIGN)/cone.do

## An ILA probe list from the causal cone (§8.11b).
probes:
	veritrace probes $(TOP).dut.full --rtl $(DESIGN) --format vivado

## Transactions extracted by the protocol packs (§8.13-8.14). Prints the
## interfaces it found and the correlation rate for each; add a query to filter,
## e.g. `make txn Q='txn(m0) | slowest(5)'`.
txn: convert
	veritrace txn $(VTX) $(Q) --rtl $(DESIGN)

## The two-master arbiter walk-through of §8.16: why master 0 could not issue,
## answered by naming master 1's transaction rather than by stopping at `grant`.
txn-why:
	$(MAKE) --no-print-directory convert DESIGN=designs/axi_arb
	veritrace txn designs/axi_arb/dump.vtx --rtl designs/axi_arb
	veritrace why designs/axi_arb/dump.vtx \
	    "why(txn.m0.WRITE[0].not_issued)" --rtl designs/axi_arb

## Stall attribution, latency, outstanding and fairness (§8.17), plus the
## liveness scan (§8.18). Add a query, e.g. `make perf Q='stalls(m0)'`.
perf: convert
	veritrace perf $(VTX) $(Q) --rtl $(DESIGN)

## The §8.18 walk-through: the same RTL with the deadlock injected and compiled
## out, so "none found" is as visible as "found". Needs both dumps, which
## `make designs` rebuilds.
deadlock:
	$(MAKE) --no-print-directory perf DESIGN=designs/deadlock
	@echo ""
	@echo "== the same design with the bug compiled out =="
	veritrace perf designs/deadlock/dump_ok.vcd --rtl designs/deadlock

## SDRAM command stream, bank timeline and timing checks (§8.20). Add a query,
## e.g. `make memory Q='banks(ctrl)'`.
memory: convert
	veritrace memory $(VTX) $(Q) --rtl $(DESIGN)

## The §8.20 walk-through: the same controller with four timing violations
## injected and compiled out, so "none found" is as visible as "four found".
sdram:
	$(MAKE) --no-print-directory memory DESIGN=designs/sdram
	@echo ""
	@echo "== the same controller with the violations compiled out =="
	veritrace memory designs/sdram/dump_ok.vcd

## The automatic scoreboard (§8.19): what was written, what came back, and
## which byte lanes changed in between. Add a query, e.g.
## `make track Q='track(addr=0x10)'`.
track: convert
	veritrace track $(VTX) $(Q) --rtl $(DESIGN)

## Functional coverage from transactions (§8.21) plus imported code coverage
## (§8.12). COV=<file> points at a verilator or xcrg database.
coverage: convert
	veritrace coverage $(VTX) $(Q) --rtl $(DESIGN) $(if $(COV),--coverage $(COV),)

## The §8.19 walk-through: the same DMA path with a byte-enable bug injected
## and compiled out, so "no corruption" is as visible as "sixteen mismatches".
dma:
	$(MAKE) --no-print-directory track DESIGN=designs/dma
	@echo ""
	@echo "== the same bridge with the corruption compiled out =="
	veritrace track designs/dma/dump_ok.vcd

## --- serving --------------------------------------------------------------

serve: convert
	veritrace serve $(VTX)

## With RTL: why-trace, the source view and the Checks tab.
serve-rtl: convert
	veritrace serve $(VTX) --rtl $(DESIGN)

## Frontend dev server. Needs `make serve-rtl` running in another shell.
web:
	cd web && npm run dev

web-build:
	cd web && npm run build

## --- tests ----------------------------------------------------------------

test:
	cargo test --workspace
	pytest
	cd web && npm test

## Browser acceptance tests. Needs the backend on :8765 (`make serve-rtl`).
test-web:
	cd web && npm run test:e2e

test-all: test test-web

## The tier-A performance budget of §4.2.
bench:
	cargo run --release -p vt-trace --example bench -- 50

## Re-simulate every reference design and rebuild its store.
designs:
	@for d in designs/*/; do \
	  [ -f "$$d/dump.vcd" ] || continue; \
	  echo "== $$d"; \
	  $(MAKE) --no-print-directory sim-icarus convert DESIGN=$${d%/} \
	    TOP=$$(basename $$(ls $$d/tb_*.sv) .sv); \
	done
	@# Three designs produce two dumps each from one source: §8.18, §8.20 and
	@# §8.19 all need their bug injected *and* compiled out, so that "none
	@# found" is checkable and not merely asserted.
	cd designs/deadlock && iverilog -g2012 -DNO_DEADLOCK -o sim_ok.vvp *.sv \
	    && vvp sim_ok.vvp | tee sim_ok.log
	cd designs/sdram && iverilog -g2012 -DNO_VIOLATION -o sim_ok.vvp *.sv \
	    && vvp sim_ok.vvp | tee sim_ok.log
	cd designs/dma && iverilog -g2012 -DNO_CORRUPTION -o sim_ok.vvp *.sv \
	    && vvp sim_ok.vvp | tee sim_ok.log

clean:
	rm -f $(DESIGN)/sim.vvp $(DESIGN)/dump_modelsim.do $(DESIGN)/dump_xsim.tcl
	rm -rf $(DESIGN)/dump.vtx $(DESIGN)/work $(DESIGN)/obj_dir $(DESIGN)/xsim.dir
