import { describe, expect, it } from "vitest";
import { bitColor, elide, expand, format, hasX, hasZ, isTwoState } from "./radix";

describe("expand", () => {
  it("left-extends with zero when the leading digit is 0 or 1", () => {
    expect(expand("1", 8)).toBe("00000001");
    expect(expand("101", 8)).toBe("00000101");
  });

  it("left-extends x and z with themselves, matching VCD", () => {
    // `bx` on an 8-bit signal is eight X bits, not one X and seven zeros.
    expect(expand("x", 8)).toBe("xxxxxxxx");
    expect(expand("z", 4)).toBe("zzzz");
  });

  it("leaves values that are already wide enough", () => {
    expect(expand("10101010", 8)).toBe("10101010");
    expect(expand("110", 2)).toBe("10");
  });
});

describe("format", () => {
  it("renders the four radices", () => {
    expect(format("10100000", 8, "bin")).toBe("10100000");
    expect(format("10100000", 8, "hex")).toBe("a0");
    expect(format("10100000", 8, "dec")).toBe("160");
    expect(format("1000001", 8, "ascii")).toBe("A");
  });

  it("pads hex to the signal width", () => {
    expect(format("1", 8, "hex")).toBe("01");
    expect(format("1", 16, "hex")).toBe("0001");
  });

  it("handles values wider than 64 bits", () => {
    const bits = "1".repeat(100);
    expect(format(bits, 100, "hex")).toBe("f".repeat(25));
    expect(format(bits, 100, "dec")).toBe((2n ** 100n - 1n).toString());
  });

  it("never converts an unknown value to a number", () => {
    // Showing `xxxx` as 0 would be a lie; the bits are shown instead.
    expect(format("x", 8, "hex")).toBe("x");
    expect(format("z", 4, "dec")).toBe("z");
    expect(format("x", 8, "bin")).toBe("xxxxxxxx");
    expect(format("01xz", 4, "hex")).toBe("01xz");
  });

  it("collapses a wholly-unknown value to one character", () => {
    expect(format("xxxx", 4, "hex")).toBe("x");
    expect(format("zzzz", 4, "hex")).toBe("z");
  });

  it("renders unprintable ascii as a dot", () => {
    expect(format("00000000", 8, "ascii")).toBe(".");
    expect(format("0100100001101001", 16, "ascii")).toBe("Hi");
  });
});

describe("state predicates", () => {
  it("distinguishes x from z", () => {
    expect(hasX("01x0")).toBe(true);
    expect(hasX("01z0")).toBe(false);
    expect(hasZ("01z0")).toBe(true);
    expect(isTwoState("0101")).toBe(true);
    expect(isTwoState("010x")).toBe(false);
    expect(isTwoState("")).toBe(false);
  });

  it("maps a bit to its palette token", () => {
    expect(bitColor("1")).toBe("sig-1");
    expect(bitColor("0")).toBe("sig-0");
    expect(bitColor("x")).toBe("sig-x");
    expect(bitColor("z")).toBe("sig-z");
    // Multi-bit values colour by their LSB when drawn as a scalar.
    expect(bitColor("0001")).toBe("sig-1");
  });
});

describe("elide", () => {
  it("keeps text that fits", () => {
    expect(elide("abcd", 100, 7)).toBe("abcd");
  });

  it("elides the middle so both ends stay readable", () => {
    const out = elide("deadbeefcafe", 8 * 7, 7);
    expect(out.length).toBeLessThanOrEqual(8);
    expect(out).toContain("…");
    expect(out.startsWith("d")).toBe(true);
  });

  it("returns nothing when there is no room at all", () => {
    expect(elide("abcd", 5, 7)).toBe("");
  });
});
