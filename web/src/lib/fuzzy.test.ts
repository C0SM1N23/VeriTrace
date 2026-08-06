import { describe, expect, it } from "vitest";
import { filterByPath, score } from "./fuzzy";

const PATHS = [
  "tb.clk",
  "tb.dut.clk",
  "tb.dut.wr_ptr",
  "tb.dut.rd_ptr",
  "tb.dut.mem[0]",
  "tb.data",
  "tb.dut.state",
];

describe("score", () => {
  it("matches substrings and ranks leaf hits first", () => {
    expect(score("clk", "tb.clk")).not.toBeNull();
    expect(score("clk", "tb.dut.clk")).not.toBeNull();
    // A hit in the leaf name beats one buried in the hierarchy.
    expect(score("dut", "tb.dut.clk")!).toBeGreaterThan(score("clk", "tb.dut.clk")!);
  });

  it("matches out-of-order characters as a subsequence", () => {
    expect(score("dwp", "tb.dut.wr_ptr")).not.toBeNull();
    expect(score("mem0", "tb.dut.mem[0]")).not.toBeNull();
  });

  it("rejects when a character is missing", () => {
    expect(score("zzz", "tb.dut.clk")).toBeNull();
    expect(score("clkx", "tb.clk")).toBeNull();
  });

  it("treats an empty query as a match", () => {
    expect(score("", "anything")).toBe(0);
  });

  it("is case-insensitive", () => {
    expect(score("CLK", "tb.clk")).toEqual(score("clk", "tb.clk"));
  });
});

describe("filterByPath", () => {
  const items = PATHS.map((p) => ({ path: p }));
  const key = (x: { path: string }) => x.path;

  it("returns everything for an empty query", () => {
    expect(filterByPath(items, "", key)).toHaveLength(PATHS.length);
  });

  it("ranks the closest match first", () => {
    expect(filterByPath(items, "wr_ptr", key)[0].path).toBe("tb.dut.wr_ptr");
    expect(filterByPath(items, "state", key)[0].path).toBe("tb.dut.state");
  });

  it("is deterministic", () => {
    const a = filterByPath(items, "t", key).map(key);
    for (let i = 0; i < 5; i++) {
      expect(filterByPath(items, "t", key).map(key)).toEqual(a);
    }
  });

  it("honours the limit", () => {
    expect(filterByPath(items, "", key, 3)).toHaveLength(3);
  });
});
