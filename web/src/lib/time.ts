/**
 * Viewport maths and time formatting.
 *
 * All pure so the panning/zoom behaviour is unit-testable without a browser.
 */

export interface View {
  t0: number;
  t1: number;
}

export const MIN_SPAN = 1;

export function clampView(v: View, bounds: View): View {
  const fullSpan = Math.max(bounds.t1 - bounds.t0, MIN_SPAN);
  let span = Math.max(Math.min(v.t1 - v.t0, fullSpan), MIN_SPAN);
  let t0 = v.t0;
  // Keep the window inside the trace rather than letting it drift into empty
  // space on either side.
  if (t0 < bounds.t0) t0 = bounds.t0;
  if (t0 + span > bounds.t1) t0 = bounds.t1 - span;
  if (t0 < bounds.t0) {
    t0 = bounds.t0;
    span = Math.min(span, fullSpan);
  }
  return { t0, t1: t0 + span };
}

export function panBy(v: View, dtPixels: number, widthPx: number, bounds: View): View {
  const span = v.t1 - v.t0;
  const dt = (dtPixels / Math.max(widthPx, 1)) * span;
  return clampView({ t0: v.t0 + dt, t1: v.t1 + dt }, bounds);
}

/** Zoom around a fixed pixel, so the value under the pointer stays put. */
export function zoomAt(v: View, factor: number, pixelX: number, widthPx: number, bounds: View): View {
  const span = v.t1 - v.t0;
  const frac = Math.min(Math.max(pixelX / Math.max(widthPx, 1), 0), 1);
  const anchor = v.t0 + span * frac;
  const newSpan = Math.max(span * factor, MIN_SPAN);
  return clampView({ t0: anchor - newSpan * frac, t1: anchor - newSpan * frac + newSpan }, bounds);
}

export function timeToX(t: number, v: View, widthPx: number): number {
  const span = Math.max(v.t1 - v.t0, MIN_SPAN);
  return ((t - v.t0) / span) * widthPx;
}

export function xToTime(x: number, v: View, widthPx: number): number {
  const span = Math.max(v.t1 - v.t0, MIN_SPAN);
  return v.t0 + (x / Math.max(widthPx, 1)) * span;
}

const UNIT_EXP: Record<string, number> = { s: 0, ms: -3, us: -6, ns: -9, ps: -12, fs: -15 };
const ORDER = ["fs", "ps", "ns", "us", "ms", "s"];

/** Parse a timescale string such as `1ps` or `10ns`. */
export function parseTimescale(ts: string): { num: number; unit: string } {
  const m = /^(\d*)\s*([a-z]+)$/i.exec(ts.trim());
  if (!m) return { num: 1, unit: "ns" };
  return { num: m[1] ? parseInt(m[1], 10) : 1, unit: m[2].toLowerCase() };
}

/**
 * Render a raw tick count as an engineering-notation time.
 *
 * Ticks are in timescale units, so `#25000` at `1ps` is 25 ns.
 */
export function formatTime(ticks: number, timescale: string, digits = 3): string {
  const { num, unit } = parseTimescale(timescale);
  const baseExp = UNIT_EXP[unit] ?? -9;
  let value = ticks * num;
  let idx = ORDER.indexOf(unit) >= 0 ? ORDER.indexOf(unit) : 2;

  while (Math.abs(value) >= 1000 && idx < ORDER.length - 1) {
    value /= 1000;
    idx += 1;
  }
  const shown = Number.isInteger(value) ? value.toString() : value.toFixed(digits).replace(/\.?0+$/, "");
  void baseExp;
  return `${shown}${ORDER[idx]}`;
}

/** Choose a round tick spacing of at least `minPx` pixels. */
export function tickStep(view: View, widthPx: number, minPx = 80): number {
  const span = Math.max(view.t1 - view.t0, MIN_SPAN);
  const target = (span * minPx) / Math.max(widthPx, 1);
  const pow = Math.pow(10, Math.floor(Math.log10(Math.max(target, 1))));
  for (const mult of [1, 2, 5, 10]) {
    if (pow * mult >= target) return pow * mult;
  }
  return pow * 10;
}

/** Cycle number for a timestamp, given the clock period and first edge. */
export function cycleAt(t: number, period: number, origin: number): number {
  if (period <= 0) return 0;
  return Math.floor((t - origin) / period);
}
