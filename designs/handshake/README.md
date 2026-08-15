# handshake — a ready that depends on valid

§8.9's one structural row, and the only reference design here whose bug **the
trace cannot show**.

```systemverilog
// correct: ready comes out of a flop
always_ff @(posedge clk) s_axi_awready <= ~busy;

// the bug: ready is a function of this cycle's valid
assign s_axi_wready = s_axi_wvalid & ~busy;
```

## Why simulation does not find it

The testbench drives a master that raises `valid` without waiting for `ready`.
Every transfer completes, the run is clean, and nothing anywhere in the waveform
is wrong. The combinational path is still there. Put this slave opposite a
master that waits for `ready` before asserting `valid` — which the AXI
specification explicitly permits — and both sides wait forever.

That is why the check reads the RTL and says so in the finding:

> Structural: found in the RTL, not observed in this run. A trace where it never
> deadlocked does not clear it.

## Expected

```sh
veritrace check designs/handshake/dump.vcd --rtl designs/handshake
#  PROTOCOL (1)
#    tb_handshake.dut.s_axi_wready   dut.s_axi.W: s_axi_wready depends
#        combinationally on s_axi_wvalid   handshake.sv:23
#        path: s_axi_wready <- s_axi_wvalid
```

The other three channels are quiet, and they are the control: `awready` is
registered, `arready` is `~rvalid`. Only the one that reads this cycle's `valid`
is reported.

Building with `-DFIX_HANDSHAKE` — or elaborating with that define — silences it,
because the check is about the source and not about the run.

`expected.toml` is what `tests/test_golden.py` checks on every run.
