// A dumper compiled alongside the design, so the cocotb run produces a VCD
// VeriTrace can open. The same device §13.4b uses: nothing in the design or the
// monitor changes, this is just an extra root module.
module vt_dump;
  initial begin
    $dumpfile("K:/VeriTrace/designs/cocotb/dump.vcd");
    $dumpvars(0, axil_slave);
  end
endmodule
