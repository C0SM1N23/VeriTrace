/**
 * Formatting of 4-state values for display.
 *
 * Values arrive from the server as canonical VCD digit strings (MSB first,
 * redundant leading digits already dropped), e.g. "1010", "x", "z1".
 *
 * The rule throughout: a value containing X or Z is never converted to a
 * number. Rendering `xxxx` as `0` would be a lie, and this tool does not guess
 * (P1). Such values are shown in their bit form instead.
 */

import type { Radix } from "./types";

const RADIX_LABEL: Record<Radix, string> = {
  hex: "hex",
  dec: "dec",
  bin: "bin",
  ascii: "asc",
};

export const RADICES: Radix[] = ["hex", "dec", "bin", "ascii"];

export function radixLabel(r: Radix): string {
  return RADIX_LABEL[r];
}

export function isTwoState(bits: string): boolean {
  for (const c of bits) {
    if (c !== "0" && c !== "1") return false;
  }
  return bits.length > 0;
}

export function hasX(bits: string): boolean {
  return bits.includes("x") || bits.includes("X");
}

export function hasZ(bits: string): boolean {
  return bits.includes("z") || bits.includes("Z");
}

/**
 * Expand a canonical VCD value to `width` bits.
 *
 * VCD left-extends with 0 when the leading digit is 0/1, and with the leading
 * digit itself when it is x or z — so `x` on an 8-bit signal is eight X bits,
 * not one.
 */
export function expand(bits: string, width: number): string {
  if (bits.length >= width) return bits.slice(bits.length - width);
  const lead = bits[0];
  const fill = lead === "x" || lead === "X" || lead === "z" || lead === "Z" ? lead : "0";
  return fill.repeat(width - bits.length) + bits;
}

/** Format for display in the given radix. */
export function format(bits: string, width: number, radix: Radix): string {
  if (!bits) return "";
  const full = expand(bits, Math.max(width, bits.length));

  // Unknown bits have no numeric meaning; show them as-is rather than inventing
  // a number.
  if (!isTwoState(full)) {
    return radix === "bin" ? full : collapseUnknown(full);
  }

  switch (radix) {
    case "bin":
      return full;
    case "dec":
      return BigInt("0b" + full).toString(10);
    case "hex": {
      const hex = BigInt("0b" + full).toString(16);
      const digits = Math.ceil(full.length / 4);
      return hex.padStart(digits, "0");
    }
    case "ascii":
      return toAscii(full);
  }
}

/**
 * Shorten an unknown value for narrow columns: a value that is entirely X (or
 * entirely Z) shows as a single character, mixed values keep their bits.
 */
function collapseUnknown(full: string): string {
  const first = full[0].toLowerCase();
  if ((first === "x" || first === "z") && full.toLowerCase() === first.repeat(full.length)) {
    return first;
  }
  return full;
}

function toAscii(full: string): string {
  let out = "";
  // Walk byte-aligned from the LSB so a 12-bit value yields two characters.
  const padded = full.padStart(Math.ceil(full.length / 8) * 8, "0");
  for (let i = 0; i < padded.length; i += 8) {
    const code = parseInt(padded.slice(i, i + 8), 2);
    out += code >= 0x20 && code < 0x7f ? String.fromCharCode(code) : ".";
  }
  return out;
}

/** Colour token for a 1-bit value. */
export function bitColor(bits: string): "sig-0" | "sig-1" | "sig-x" | "sig-z" {
  const c = bits[bits.length - 1];
  if (c === "1") return "sig-1";
  if (c === "0") return "sig-0";
  if (c === "z" || c === "Z") return "sig-z";
  return "sig-x";
}

/** Fit text into `maxPx`, eliding the middle so both ends stay readable. */
export function elide(text: string, maxPx: number, charW: number): string {
  const fits = Math.floor(maxPx / charW);
  if (fits >= text.length) return text;
  if (fits < 2) return "";
  if (fits < 4) return text.slice(0, fits);
  const head = Math.ceil((fits - 1) / 2);
  const tail = fits - 1 - head;
  return text.slice(0, head) + "…" + (tail > 0 ? text.slice(text.length - tail) : "");
}
