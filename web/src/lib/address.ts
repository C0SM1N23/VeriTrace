/** Exact address arithmetic. Number silently discards low bits above 2**53. */
export type AddressRanges = Record<string, [number, number]>;
export type AddressField = [name: string, value: bigint, hi: number, lo: number];

export function decodeAddress(text: string, ranges: AddressRanges): AddressField[] | null {
  const value = text.trim();
  if (value.length > 4096 || !/^(?:0x[0-9a-f]+|[0-9]+)$/i.test(value)) return null;
  const n = BigInt(value);
  const out: AddressField[] = [];
  for (const name of ["bank", "row", "col"]) {
    const range = ranges[name];
    if (!range) continue;
    const [hi, lo] = range;
    if (!Number.isSafeInteger(hi) || !Number.isSafeInteger(lo) || lo < 0 || hi < lo || hi > 65535) return null;
    const field = (n >> BigInt(lo)) & ((1n << BigInt(hi - lo + 1)) - 1n);
    out.push([name, field, hi, lo]);
  }
  return out.length ? out : null;
}

/** Hypothetical open-page accesses, not inferred traffic from the simulator. */
export function addressPattern(text: string, ranges: AddressRanges): { hits: number; misses: number; conflicts: number } {
  if (!ranges.bank || !ranges.row) throw new Error("A bank and row mapping are required.");
  const addresses = text.trim().split(/[\s,;]+/).filter(Boolean);
  if (!addresses.length) throw new Error("Enter addresses separated by commas or whitespace.");
  if (addresses.length > 4096) throw new Error("At most 4096 addresses per inspection.");
  const openRows = new Map<bigint, bigint>();
  const result = { hits: 0, misses: 0, conflicts: 0 };
  for (const [index, value] of addresses.entries()) {
    const decoded = decodeAddress(value, ranges);
    if (!decoded) throw new Error(`Invalid address or mapping at position ${index + 1}: ${value}`);
    const fields = new Map(decoded.map(([name, n]) => [name, n]));
    const bank = fields.get("bank")!, row = fields.get("row")!;
    const previous = openRows.get(bank);
    if (previous === undefined) result.misses++;
    else if (previous === row) result.hits++;
    else result.conflicts++;
    openRows.set(bank, row);
  }
  return result;
}
