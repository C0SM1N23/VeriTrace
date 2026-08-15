"""A minimal cocotb monitor for `designs/axi_lite` — the fixture for §8.34.

It exists to produce a *real* log, not a hand-written one: §8.34's claim is that
a team's existing monitor needs no protocol pack, only a regex over the lines it
already writes, and the only way to check that is to write the monitor, run it,
and ingest what it actually printed.

Deliberately naive. It logs one line per phase with `self.log.info`, which is
what a monitor written in an afternoon does — and that is the point.

    pip install cocotb
    cd designs/cocotb && make            # writes sim.log and dump.vcd
    veritrace ingest --cocotb-log sim.log --trace dump.vcd
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

CLOCK_NS = 10


async def reset(dut):
    dut.aresetn.value = 0
    dut.awvalid.value = 0
    dut.wvalid.value = 0
    dut.arvalid.value = 0
    dut.bready.value = 1
    dut.rready.value = 1
    for _ in range(4):
        await RisingEdge(dut.aclk)
    dut.aresetn.value = 1
    await RisingEdge(dut.aclk)


async def write(dut, addr, data, strb=0xF):
    """One AXI4-Lite write, logged at its start and at its response.

    Two lines rather than one so the ingested transaction has a start *and* an
    end — which is what gives it a latency, without the monitor computing one.
    """
    dut.awaddr.value = addr
    dut.awprot.value = 0
    dut.wdata.value = data
    dut.wstrb.value = strb
    dut.awvalid.value = 1
    dut.wvalid.value = 1
    dut._log.info(f"axi_mon WRITE_BEGIN addr={hex(addr)} data={hex(data)} strb={hex(strb)}")

    pending = {"aw", "w"}
    while pending:
        await RisingEdge(dut.aclk)
        if "aw" in pending and dut.awvalid.value and dut.awready.value:
            dut.awvalid.value = 0
            pending.discard("aw")
        if "w" in pending and dut.wvalid.value and dut.wready.value:
            dut.wvalid.value = 0
            pending.discard("w")

    while True:
        await RisingEdge(dut.aclk)
        if dut.bvalid.value:
            break
    dut._log.info(f"axi_mon WRITE_END addr={hex(addr)} resp={int(dut.bresp.value)}")


async def read(dut, addr):
    dut.araddr.value = addr
    dut.arprot.value = 0
    dut.arvalid.value = 1
    dut._log.info(f"axi_mon READ_BEGIN addr={hex(addr)}")

    while True:
        await RisingEdge(dut.aclk)
        if dut.arvalid.value and dut.arready.value:
            dut.arvalid.value = 0
            break
    while True:
        await RisingEdge(dut.aclk)
        if dut.rvalid.value:
            break
    dut._log.info(
        f"axi_mon READ_END addr={hex(addr)} data={hex(int(dut.rdata.value))} "
        f"resp={int(dut.rresp.value)}"
    )


@cocotb.test()
async def traffic(dut):
    """Eight writes and eight read-backs, so the log has both kinds."""
    cocotb.start_soon(Clock(dut.aclk, CLOCK_NS, unit="ns").start())
    await reset(dut)

    for i in range(8):
        await write(dut, (i % 2) * 4, 0xA0 + i)
        await read(dut, (i % 2) * 4)

    for _ in range(8):
        await RisingEdge(dut.aclk)
    dut._log.info("axi_mon DONE")
