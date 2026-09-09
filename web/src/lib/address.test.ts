import { expect, it } from "vitest";
import { addressPattern, decodeAddress } from "./address";

const ranges: Record<string, [number, number]> = { bank: [11, 10], row: [24, 12], col: [9, 0] };

it("preserves low bits of addresses larger than the Number exact-integer range", () => {
  expect(decodeAddress("0x20000000000001", ranges)?.find(([name]) => name === "col")?.[1]).toBe(1n);
  expect(decodeAddress("9007199254740993", ranges)?.find(([name]) => name === "col")?.[1]).toBe(1n);
  const byteAddressed = { ...ranges, col: [9, 1] as [number, number] };
  expect(decodeAddress("0x40000000000002", byteAddressed)?.find(([name]) => name === "col")?.[1]).toBe(1n);
  expect(decodeAddress("0x20000000000001", byteAddressed)?.find(([name]) => name === "col")?.[1]).toBe(0n);
});

it("rejects partial hex, fractions, signs and invalid bit ranges", () => {
  for (const input of ["0x1oops", "1.5", "-1", "Infinity", "1e3", "", "0x"])
    expect(decodeAddress(input, ranges)).toBeNull();
  expect(decodeAddress("0", { bank: [1, 3] })).toBeNull();
});

it("counts row reuse and replacement independently for each bank", () => {
  expect(addressPattern("0 4 0x1000 0", ranges)).toEqual({ misses: 1, hits: 1, conflicts: 2 });
  expect(addressPattern("0 0x400 4 0x404", ranges)).toEqual({ misses: 2, hits: 2, conflicts: 0 });
  expect(() => addressPattern("0 0x1badZ", ranges)).toThrow("position 2");
});
