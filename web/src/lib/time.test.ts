import { describe, expect, it } from "vitest";
import { clampView, cycleAt, formatTime, panBy, tickStep, timeToX, xToTime, zoomAt } from "./time";

const BOUNDS = { t0: 0, t1: 1000 };

describe("clampView", () => {
  it("keeps the window inside the trace", () => {
    expect(clampView({ t0: -50, t1: 50 }, BOUNDS)).toEqual({ t0: 0, t1: 100 });
    expect(clampView({ t0: 980, t1: 1080 }, BOUNDS)).toEqual({ t0: 900, t1: 1000 });
  });

  it("never lets the span exceed the trace or collapse to zero", () => {
    expect(clampView({ t0: -500, t1: 5000 }, BOUNDS)).toEqual({ t0: 0, t1: 1000 });
    const tiny = clampView({ t0: 500, t1: 500 }, BOUNDS);
    expect(tiny.t1 - tiny.t0).toBeGreaterThan(0);
  });
});

describe("panBy", () => {
  it("moves by a fraction of the span", () => {
    const v = panBy({ t0: 100, t1: 200 }, 100, 1000, BOUNDS);
    expect(v.t0).toBeCloseTo(110);
    expect(v.t1).toBeCloseTo(210);
  });

  it("stops at the edges instead of drifting into empty space", () => {
    expect(panBy({ t0: 0, t1: 100 }, -10_000, 1000, BOUNDS).t0).toBe(0);
    expect(panBy({ t0: 900, t1: 1000 }, 10_000, 1000, BOUNDS).t1).toBe(1000);
  });

  it("preserves the span", () => {
    const v = panBy({ t0: 400, t1: 500 }, 37, 800, BOUNDS);
    expect(v.t1 - v.t0).toBeCloseTo(100);
  });
});

describe("zoomAt", () => {
  it("keeps the time under the pointer fixed", () => {
    const v = { t0: 0, t1: 1000 };
    const px = 250;
    const width = 1000;
    const before = xToTime(px, v, width);
    const after = zoomAt(v, 0.5, px, width, BOUNDS);
    expect(xToTime(px, after, width)).toBeCloseTo(before, 6);
  });

  it("zooming in shrinks the span, out grows it", () => {
    const v = { t0: 200, t1: 400 };
    expect(zoomAt(v, 0.5, 100, 200, BOUNDS).t1 - zoomAt(v, 0.5, 100, 200, BOUNDS).t0).toBeCloseTo(100);
    const out = zoomAt(v, 2, 100, 200, BOUNDS);
    expect(out.t1 - out.t0).toBeCloseTo(400);
  });

  it("cannot zoom out past the trace", () => {
    const out = zoomAt({ t0: 0, t1: 1000 }, 10, 500, 1000, BOUNDS);
    expect(out).toEqual({ t0: 0, t1: 1000 });
  });
});

describe("timeToX / xToTime", () => {
  it("round-trip", () => {
    const v = { t0: 137, t1: 9421 };
    for (const x of [0, 13, 500, 999]) {
      expect(timeToX(xToTime(x, v, 1000), v, 1000)).toBeCloseTo(x, 6);
    }
  });
});

describe("formatTime", () => {
  it("scales raw ticks by the timescale", () => {
    // 25000 ticks at 1ps is 25 ns.
    expect(formatTime(25_000, "1ps")).toBe("25ns");
    expect(formatTime(1_000_000, "1ps")).toBe("1us");
    expect(formatTime(500, "1ns")).toBe("500ns");
    // Zero stays in the trace's own unit rather than being rescaled.
    expect(formatTime(0, "1ps")).toBe("0ps");
  });

  it("honours a multiplier in the timescale", () => {
    expect(formatTime(1, "10ns")).toBe("10ns");
  });
});

describe("tickStep", () => {
  it("picks a round step of at least the minimum pixel spacing", () => {
    const step = tickStep({ t0: 0, t1: 1000 }, 1000, 100);
    expect([100, 200, 500]).toContain(step);
    expect((1000 / step) * 1).toBeLessThanOrEqual(10);
  });

  it("grows with the span", () => {
    const a = tickStep({ t0: 0, t1: 1000 }, 1000);
    const b = tickStep({ t0: 0, t1: 1_000_000 }, 1000);
    expect(b).toBeGreaterThan(a);
  });
});

describe("cycleAt", () => {
  it("counts clock periods from the origin", () => {
    expect(cycleAt(0, 10, 0)).toBe(0);
    expect(cycleAt(25, 10, 0)).toBe(2);
    expect(cycleAt(105, 10, 5)).toBe(10);
  });

  it("is safe when there is no clock", () => {
    expect(cycleAt(100, 0, 0)).toBe(0);
  });
});
