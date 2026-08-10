import { describe, expect, it } from "vitest";
import { shortLabels } from "./names";

describe("shortLabels", () => {
  it("uses the bare leaf while it is unique", () => {
    const got = shortLabels(["tb.dut.wr_ptr", "tb.dut.rd_ptr", "tb.clk"]);
    expect(got.get("tb.dut.wr_ptr")).toBe("wr_ptr");
    expect(got.get("tb.clk")).toBe("clk");
  });

  it("grows only as far as the collision requires", () => {
    // The case that motivated this: seven AXI interfaces, one `arready` each.
    const got = shortLabels([
      "tb_cpu_axi.dmem_inst.arready",
      "tb_cpu_axi.imem_inst.arready",
      "tb_cpu_axi.dmem_inst.rdata",
    ]);
    expect(got.get("tb_cpu_axi.dmem_inst.arready")).toBe("dmem_inst.arready");
    expect(got.get("tb_cpu_axi.imem_inst.arready")).toBe("imem_inst.arready");
    // Unaffected: `rdata` collides with nothing, so it stays short.
    expect(got.get("tb_cpu_axi.dmem_inst.rdata")).toBe("rdata");
  });

  it("keeps growing while the shorter suffix still collides", () => {
    const got = shortLabels(["top.a.slv.rdata", "top.b.slv.rdata"]);
    expect(got.get("top.a.slv.rdata")).toBe("a.slv.rdata");
    expect(got.get("top.b.slv.rdata")).toBe("b.slv.rdata");
  });

  it("elides in the middle once a label outgrows the column", () => {
    const got = shortLabels(["top.x.y.z.sig", "top.q.y.z.sig"]);
    // Keeps the block it is in and the leaf, drops the middle.
    expect(got.get("top.x.y.z.sig")).toBe("x.….z.sig");
    expect(got.get("top.q.y.z.sig")).toBe("q.….z.sig");
  });

  it("gives the same label to the same path listed twice", () => {
    // Two rows of one signal are two rows of one signal; a difference here
    // would be a lie about the design.
    const got = shortLabels(["tb.dut.q", "tb.dut.q"]);
    expect(got.size).toBe(1);
    expect(got.get("tb.dut.q")).toBe("q");
  });

  it("handles a path with no scope at all", () => {
    const got = shortLabels(["clk"]);
    expect(got.get("clk")).toBe("clk");
  });
});
