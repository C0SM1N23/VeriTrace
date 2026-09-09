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

# The same PATH the rest of this file assumes: with the venv on it, `python`
# and `veritrace` both resolve. Overridable for a different interpreter.
PY        ?= python
VERITRACE ?= veritrace

DESIGN  ?= designs/fifo_buggy
TOP     ?= tb_fifo_buggy
## The default is intentionally only a convenience.  Package/interface order
## matters in SystemVerilog, so a real project can (and should) override this
## with its elaboration order: `make sim-icarus SOURCES='pkg.sv rtl.sv tb.sv'`.
## Discovery happens after `cd $(DESIGN)`, in the shell, so DESIGN may contain
## spaces.  Quote an individual source in SOURCES when its basename has spaces.
SOURCES ?=
DEFINES ?=
INCDIRS ?=
VCD     ?= dump.vcd
FST     ?= dump.fst
TRACE   ?=
TRACE_MARKER ?= .veritrace-trace
## Some deliberately-buggy reference benches print assertion failures with
## `$display` and still return zero from vvp.  That is a failed simulation by
## default.  The aggregate fixture builder opts in only where the failure is
## itself the fixture being captured.
ALLOW_SIM_ERRORS ?= 0
SIM_ERROR_RE ?= (^|[[:space:]])(%|\*\*[[:space:]]+)?(error|fatal)(:|[[:space:]])
VTX      = $(DESIGN)/dump.vtx

CPP_DEFINE_FLAGS = $(addprefix -D,$(DEFINES))
CPP_INCLUDE_FLAGS = $(addprefix -I,$(INCDIRS))
VLOG_DEFINE_FLAGS = $(addprefix +define+,$(DEFINES))
VLOG_INCLUDE_FLAGS = $(addprefix +incdir+,$(INCDIRS))
XVLOG_DEFINE_FLAGS = $(foreach d,$(DEFINES),-d $(d))
XVLOG_INCLUDE_FLAGS = $(foreach d,$(INCDIRS),-i $(d))

# Run after changing into DESIGN.  Shell globbing preserves spaces in default
# filenames; an explicit SOURCES list remains the authoritative source order.
define select_sources
if [ -n "$(strip $(SOURCES))" ]; then \
	  set -- $(SOURCES); \
	else \
	  set --; \
	  for src in ./*.sv ./*.v; do \
	    [ -f "$$src" ] && set -- "$$@" "$$src"; \
	  done; \
	fi; \
	[ "$$#" -gt 0 ] || { echo "no Verilog/SystemVerilog sources found in $(DESIGN)" >&2; exit 2; }
endef

# Simulator processes do not agree on assertion exit codes.  In particular,
# vvp returns zero after `$error`/an assertion-like ERROR line.  Refuse that
# fake-success path unless the caller explicitly says the failing trace is a
# deliberate fixture.
define reject_sim_errors
if grep -Eiq '$(SIM_ERROR_RE)' sim.log; then \
	  if [ "$(ALLOW_SIM_ERRORS)" = "1" ]; then \
	    echo "simulation log contains ERROR/FATAL output (explicitly allowed)" >&2; \
	  else \
	    echo "simulation returned zero but sim.log contains ERROR/FATAL output" >&2; \
	    exit 1; \
	  fi; \
	fi
endef

define validate_vcd
[ -s "$(VCD)" ] || { echo "simulation succeeded but did not produce $(VCD)" >&2; exit 1; }; \
	grep -q '\$$enddefinitions' "$(VCD)" || { \
	  echo "simulation produced $(VCD), but it is not a complete VCD header" >&2; exit 1; \
	}
endef

define validate_fst
[ -s "$(FST)" ] || { echo "simulation succeeded but did not produce $(FST)" >&2; exit 1; }; \
	magic=$$(od -An -tx1 -N8 "$(FST)" | tr -d '[:space:]'); \
	[ "$$magic" = "0000000000000001" ] || { \
	  echo "simulation produced $(FST), but its header is not FST" >&2; exit 1; \
	}
endef

.PHONY: help run sim-icarus sim-verilator sim-modelsim sim-xsim \
        convert serve serve-rtl correlate check stuck triage export probes \
        txn txn-why perf deadlock memory sdram track coverage dma \
        web web-build test test-web test-all bench bench-stress designs clean

help:
	@echo "Start:     run          (folder of .sv -> findings, one command)"
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
	@cd "$(DESIGN)" && rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)" sim.log
	$(call require,iverilog,Install it from https://bleyer.org/icarus or your package manager.)
	$(call require,vvp,The Icarus runtime is installed together with iverilog.)
	@set -e; cd "$(DESIGN)"; \
	  ok=0; trap 'if [ "$$ok" -ne 1 ]; then rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)"; fi' 0 1 2 15; \
	  $(select_sources); \
	  rm -f sim.vvp; \
	  iverilog -g2012 $(CPP_DEFINE_FLAGS) $(CPP_INCLUDE_FLAGS) \
	    -s "$(TOP)" -o sim.vvp "$$@"; \
	  status=0; vvp sim.vvp >sim.log 2>&1 || status=$$?; \
	  cat sim.log; \
	  [ $$status -eq 0 ] || { echo "vvp failed with exit code $$status" >&2; exit $$status; }; \
	  $(reject_sim_errors); \
	  $(validate_vcd); \
	  printf '%s\n' "$(VCD)" > "$(TRACE_MARKER)"; \
	  ok=1; trap - 0 1 2 15

## Verilator — for large traces and for CI. The flags are mandatory: without
## them it inlines modules away and correlation falls apart.
##
## NOTE: §4.0 spells the inlining flag `--no-inline`, which Verilator 5 renamed
## to `-fno-inline` and now rejects outright. The intent is unchanged.
sim-verilator:
	@cd "$(DESIGN)" && rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)" sim.log
	$(call require,verilator,Install it with apt/brew, or run it from WSL on Windows.)
	@set -e; cd "$(DESIGN)"; \
	  ok=0; trap 'if [ "$$ok" -ne 1 ]; then rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)"; fi' 0 1 2 15; \
	  $(select_sources); \
	  verilator --binary --trace-fst --trace-structs \
	    --trace-max-array 1024 --no-trace-underscore \
	    -fno-inline --public-flat-rw \
	    $(CPP_DEFINE_FLAGS) $(CPP_INCLUDE_FLAGS) \
	    --top-module "$(TOP)" "$$@" -o sim_verilated; \
	  status=0; ./obj_dir/sim_verilated >sim.log 2>&1 || status=$$?; \
	  cat sim.log; \
	  [ $$status -eq 0 ] || { echo "Verilated simulation failed with exit code $$status" >&2; exit $$status; }; \
	  $(reject_sim_errors); \
	  if [ -s "$(FST)" ]; then :; \
	  elif [ -s "$(VCD)" ]; then \
	    mv "$(VCD)" "$(FST)"; \
	  else \
	    echo "simulation succeeded but did not produce $(FST) (or $(VCD) to normalize)" >&2; exit 1; \
	  fi; \
	  $(validate_fst); \
	  printf '%s\n' "$(FST)" > "$(TRACE_MARKER)"; \
	  ok=1; trap - 0 1 2 15
	@echo "# Verilator 4: use --no-inline instead of -fno-inline"

## ModelSim / Questa Intel FPGA Starter Edition. +acc is not optional — without
## it the optimiser hides internal signals and correlation suffers exactly as it
## does under Verilator. It is the same trap in a different tool.
sim-modelsim:
	@cd "$(DESIGN)" && rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)" sim.log
	$(call require,vsim,Install Quartus Prime Lite; vsim ships in its modelsim_ase directory.)
	$(call require,vlib,ModelSim/Questa ships vlib together with vsim.)
	$(call require,vlog,ModelSim/Questa ships vlog together with vsim.)
	@set -e; cd "$(DESIGN)"; \
	  ok=0; trap 'if [ "$$ok" -ne 1 ]; then rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)"; fi' 0 1 2 15; \
	  $(select_sources); \
	  rm -f dump_modelsim.do; \
	  [ -d work ] || vlib work; \
	  vlog -sv $(VLOG_DEFINE_FLAGS) $(VLOG_INCLUDE_FLAGS) "$$@"; \
	  printf '%s\n' \
	  'onerror {quit -f -code 1}' \
	  'vsim -voptargs="+acc" work.$(TOP)' \
	  'vcd file $(VCD)' \
	  'vcd add -r /$(TOP)/*' \
	  'run -all' \
	  'vcd flush' \
	  'quit -f -code 0' > dump_modelsim.do; \
	  status=0; vsim -c -do dump_modelsim.do >sim.log 2>&1 || status=$$?; \
	  cat sim.log; \
	  [ $$status -eq 0 ] || { echo "ModelSim failed with exit code $$status" >&2; exit $$status; }; \
	  $(reject_sim_errors); \
	  $(validate_vcd); \
	  printf '%s\n' "$(VCD)" > "$(TRACE_MARKER)"; \
	  ok=1; trap - 0 1 2 15

## Vivado Simulator (xsim), from Vivado ML Standard.
sim-xsim:
	@cd "$(DESIGN)" && rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)" sim.log
	$(call require,xvlog,Install Vivado ML Standard; xvlog/xelab/xsim ship with it.)
	$(call require,xelab,Install Vivado ML Standard; xelab must be on PATH with xvlog.)
	$(call require,xsim,Install Vivado ML Standard; xsim must be on PATH with xvlog.)
	@set -e; cd "$(DESIGN)"; \
	  ok=0; trap 'if [ "$$ok" -ne 1 ]; then rm -f -- "$(VCD)" "$(FST)" "$(TRACE_MARKER)"; fi' 0 1 2 15; \
	  $(select_sources); \
	  rm -f dump_xsim.tcl; \
	  xvlog -sv $(XVLOG_DEFINE_FLAGS) $(XVLOG_INCLUDE_FLAGS) "$$@"; \
	  xelab -debug typical "$(TOP)" -s "$(TOP)_sim"; \
	  printf '%s\n' \
	    'open_vcd $(VCD)' \
	    'log_vcd [get_objects -r /$(TOP)/*]' \
	    'run all' \
	    'flush_vcd' \
	    'close_vcd' \
	    'quit' > dump_xsim.tcl \
	  ; status=0; xsim "$(TOP)_sim" -t dump_xsim.tcl >sim.log 2>&1 || status=$$?; \
	  cat sim.log; \
	  [ $$status -eq 0 ] || { echo "xsim failed with exit code $$status" >&2; exit $$status; }; \
	  $(reject_sim_errors); \
	  $(validate_vcd); \
	  printf '%s\n' "$(VCD)" > "$(TRACE_MARKER)"; \
	  ok=1; trap - 0 1 2 15

## --- analysis -------------------------------------------------------------

## §13.4b: from a folder of SystemVerilog to a verdict, in one command. This is
## the target to reach for with somebody else's code, before there is any dump
## or any config — it simulates, converts and checks in one pass.
run:
	$(VERITRACE) run "$(DESIGN)" $(if $(TOP),--top "$(TOP)",)

## Convert the one simulator artifact to a .vtx store.  Icarus/ModelSim/xsim
## produce VCD; the Verilator target normalizes its FST payload to `dump.fst`.
## If both exist, refusing to guess prevents a stale trace from being analysed.
convert:
	@set -e; trace="$(TRACE)"; \
	  if [ -z "$$trace" ]; then \
	    if [ -s "$(DESIGN)/$(TRACE_MARKER)" ]; then \
	      selected=$$(sed -n '1p' "$(DESIGN)/$(TRACE_MARKER)"); \
	      case "$$selected" in "$(VCD)"|"$(FST)") ;; \
	        *) echo "invalid trace marker: $$selected" >&2; exit 2;; esac; \
	      trace="$(DESIGN)/$$selected"; \
	      [ -s "$$trace" ] || { echo "selected trace is missing or empty: $$trace" >&2; exit 1; }; \
	    else \
	      have_vcd=0; have_fst=0; \
	      [ -s "$(DESIGN)/$(VCD)" ] && have_vcd=1; \
	      [ -s "$(DESIGN)/$(FST)" ] && have_fst=1; \
	      if [ $$have_vcd -eq 1 ] && [ $$have_fst -eq 1 ]; then \
	        echo "both $(DESIGN)/$(VCD) and $(DESIGN)/$(FST) exist; set TRACE explicitly" >&2; exit 2; \
	      elif [ $$have_fst -eq 1 ]; then trace="$(DESIGN)/$(FST)"; \
	      elif [ $$have_vcd -eq 1 ]; then trace="$(DESIGN)/$(VCD)"; \
	      else echo "no non-empty trace found in $(DESIGN)" >&2; exit 1; fi; \
	    fi; \
	  fi; \
	  $(VERITRACE) convert "$$trace" -o "$(VTX)"

## Correlation report: how much of the RTL the trace actually covers.
correlate: convert
	$(VERITRACE) correlate "$(VTX)" --rtl "$(DESIGN)"

## Every automatic finding — what §13.9 puts in CI.
check: convert
	$(VERITRACE) check "$(VTX)" --rtl "$(DESIGN)"

## Just the frozen signals (§8.4).
stuck: convert
	$(VERITRACE) stuck "$(VTX)" --rtl "$(DESIGN)"

## Root causes from the simulation log (§8.10b). Needs `sim.log` from a run.
triage: convert
	$(VERITRACE) triage "$(DESIGN)/sim.log" --trace "$(VTX)" --rtl "$(DESIGN)"

## A ModelSim save file for the causal chain, ready to `do` (§13.3).
export: convert
	$(VERITRACE) why "$(VTX)" --rtl "$(DESIGN)" "why($(TOP).dut.full)" --do -o "$(DESIGN)/cone.do"

## An ILA probe list from the causal cone (§8.11b).
probes:
	$(VERITRACE) probes "$(TOP).dut.full" --rtl "$(DESIGN)" --format vivado

## Transactions extracted by the protocol packs (§8.13-8.14). Prints the
## interfaces it found and the correlation rate for each; add a query to filter,
## e.g. `make txn Q='txn(m0) | slowest(5)'`.
txn: convert
	$(VERITRACE) txn "$(VTX)" $(Q) --rtl "$(DESIGN)"

## The two-master arbiter walk-through of §8.16: why master 0 could not issue,
## answered by naming master 1's transaction rather than by stopping at `grant`.
txn-why:
	$(MAKE) --no-print-directory convert DESIGN=designs/axi_arb
	$(VERITRACE) txn designs/axi_arb/dump.vtx --rtl designs/axi_arb
	$(VERITRACE) why designs/axi_arb/dump.vtx \
	    "why(txn.m0.WRITE[0].not_issued)" --rtl designs/axi_arb

## Stall attribution, latency, outstanding and fairness (§8.17), plus the
## liveness scan (§8.18). Add a query, e.g. `make perf Q='stalls(m0)'`.
perf: convert
	$(VERITRACE) perf "$(VTX)" $(Q) --rtl "$(DESIGN)"

## The §8.18 walk-through: the same RTL with the deadlock injected and compiled
## out, so "none found" is as visible as "found". Needs both dumps, which
## `make designs` rebuilds.
deadlock:
	$(MAKE) --no-print-directory perf DESIGN=designs/deadlock
	@echo ""
	@echo "== the same design with the bug compiled out =="
	$(VERITRACE) perf designs/deadlock/dump_ok.vcd --rtl designs/deadlock

## SDRAM command stream, bank timeline and timing checks (§8.20). Add a query,
## e.g. `make memory Q='banks(ctrl)'`.
memory: convert
	$(VERITRACE) memory "$(VTX)" $(Q) --rtl "$(DESIGN)"

## The §8.20 walk-through: the same controller with four timing violations
## injected and compiled out, so "none found" is as visible as "four found".
sdram:
	$(MAKE) --no-print-directory memory DESIGN=designs/sdram
	@echo ""
	@echo "== the same controller with the violations compiled out =="
	$(VERITRACE) memory designs/sdram/dump_ok.vcd

## The automatic scoreboard (§8.19): what was written, what came back, and
## which byte lanes changed in between. Add a query, e.g.
## `make track Q='track(addr=0x10)'`.
track: convert
	$(VERITRACE) track "$(VTX)" $(Q) --rtl "$(DESIGN)"

## Functional coverage from transactions (§8.21) plus imported code coverage
## (§8.12). COV=<file> points at a verilator or xcrg database.
coverage: convert
	$(VERITRACE) coverage "$(VTX)" $(Q) --rtl "$(DESIGN)" $(if $(COV),--coverage "$(COV)",)

## The §8.19 walk-through: the same DMA path with a byte-enable bug injected
## and compiled out, so "no corruption" is as visible as "sixteen mismatches".
dma:
	$(MAKE) --no-print-directory track DESIGN=designs/dma
	@echo ""
	@echo "== the same bridge with the corruption compiled out =="
	$(VERITRACE) track designs/dma/dump_ok.vcd

## --- serving --------------------------------------------------------------

serve: convert
	$(VERITRACE) serve "$(VTX)"

## With RTL: why-trace, the source view and the Checks tab.
serve-rtl: convert
	$(VERITRACE) serve "$(VTX)" --rtl "$(DESIGN)"

## Frontend dev server. Needs `make serve-rtl` running in another shell.
web:
	cd web && npm run dev

web-build:
	cd web && npm run build

## --- tests ----------------------------------------------------------------

test:
	cargo test --workspace
	$(PY) -m pytest
	cd web && npm test

## Browser acceptance tests. Needs the backend on :8765 (`make serve-rtl`).
test-web:
	cd web && npm run test:e2e

test-all: test test-web

## The tier-A performance budget of §4.2, at the size the spec names.
bench:
	cargo run --release -p vt-trace --example bench -- 100 5000
	$(PY) bench/pybench.py

## Tier B, "stress": 1 GB over 50k signals. Validation, not a daily gate — it
## needs ~2 GB of scratch space and about a minute.
bench-stress:
	cargo run --release -p vt-trace --example bench -- 1000 50000
	$(PY) bench/pybench.py --tier b

## Re-simulate every reference design and rebuild its store.
designs:
	@set -e; for spec in designs/*/expected.toml; do \
	  d=$$(dirname "$$spec"); \
	  top=$$(sed -n 's/^top[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$$spec" | head -n 1); \
	  [ -n "$$top" ] || { echo "$$spec has no top" >&2; exit 1; }; \
	  echo "== $$d ($$top)"; \
	  allow=0; [ "$$(basename "$$d")" = fifo_buggy ] && allow=1; \
	  $(MAKE) --no-print-directory sim-icarus convert DESIGN="$$d" TOP="$$top" \
	    ALLOW_SIM_ERRORS="$$allow"; \
	done
	@# Three designs produce two dumps each from one source: §8.18, §8.20 and
	@# §8.19 all need their bug injected *and* compiled out, so that "none
	@# found" is checkable and not merely asserted.
	$(MAKE) --no-print-directory sim-icarus DESIGN=designs/deadlock TOP=tb_deadlock \
	    DEFINES=NO_DEADLOCK VCD=dump_ok.vcd FST=dump_ok.fst
	$(MAKE) --no-print-directory sim-icarus DESIGN=designs/sdram TOP=tb_sdram \
	    DEFINES=NO_VIOLATION VCD=dump_ok.vcd FST=dump_ok.fst
	$(MAKE) --no-print-directory sim-icarus DESIGN=designs/dma TOP=tb_dma \
	    DEFINES=NO_CORRUPTION VCD=dump_ok.vcd FST=dump_ok.fst

clean:
	rm -f "$(DESIGN)/sim.vvp" "$(DESIGN)/sim.log" \
	    "$(DESIGN)/$(VCD)" "$(DESIGN)/$(FST)" "$(DESIGN)/$(TRACE_MARKER)" \
	    "$(DESIGN)/dump_modelsim.do" "$(DESIGN)/dump_xsim.tcl"
	rm -rf "$(DESIGN)/dump.vtx" "$(DESIGN)/work" "$(DESIGN)/obj_dir" "$(DESIGN)/xsim.dir"
