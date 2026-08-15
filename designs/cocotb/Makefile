# cocotb's own Makefile, as a monitor project actually has it.
TOPLEVEL_LANG = verilog
SIM ?= icarus
VERILOG_SOURCES = $(PWD)/axil_slave.sv
TOPLEVEL = axil_slave
MODULE   = axi_mon
COCOTB_HDL_TIMEUNIT      = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

include $(shell cocotb-config --makefiles)/Makefile.sim
