/// <reference lib="webworker" />
/**
 * Waveform renderer, running off the main thread on an OffscreenCanvas.
 *
 * §11.4 makes this mandatory: rendering must never block the main thread or
 * scrolling feels sticky and the whole tool feels cheap. Nothing here touches
 * the DOM, React or the store — it receives a view description plus wave data
 * and draws.
 *
 * The draw loop batches by colour into a handful of `Path2D`s and strokes each
 * once, because the cost that matters at 40+ rows is canvas state changes, not
 * the arithmetic.
 */

import type { ExactPoint, FromWorker, MinMaxPoint, RenderRow, ToWorker, WaveChunk } from "../lib/types";
import { FLAG_X, FLAG_Z } from "../lib/types";
import { cycleAt, formatTime, tickStep, timeToX } from "../lib/time";
import { elide, format } from "../lib/radix";

const RULER_H = 26;
/** Don't try to write a bus value into a sliver. */
const MIN_TEXT_PX = 26;
const CHAR_W = 7.2; // JetBrains Mono at 12px

interface Colors {
  bgDeep: string;
  bgPanel: string;
  bgRaised: string;
  line: string;
  textDim: string;
  text: string;
  textBright: string;
  sig0: string;
  sig1: string;
  sigX: string;
  sigZ: string;
  sigBus: string;
}

let canvas: OffscreenCanvas | null = null;
let ctx: OffscreenCanvasRenderingContext2D | null = null;
let dpr = 1;
let width = 0;
let height = 0;
let fontsReady = false;

let colors: Colors = {
  bgDeep: "#0E1116",
  bgPanel: "#151A21",
  bgRaised: "#1C232C",
  line: "#262F3A",
  textDim: "#6B7785",
  text: "#A8B4C2",
  textBright: "#E4EAF1",
  sig0: "#4A5A6B",
  sig1: "#7FD1A8",
  sigX: "#E5484D",
  sigZ: "#8B7AB8",
  sigBus: "#A8B4C2",
};

const data = new Map<number, WaveChunk>();

interface ViewState {
  t0: number;
  t1: number;
  scrollY: number;
  rowH: number;
  rows: RenderRow[];
  cursor: number | null;
  markers: number[];
  rulerMode: "time" | "cycle";
  clockPeriod: number | null;
  clockOrigin: number;
  timescale: string;
}

let view: ViewState = {
  t0: 0,
  t1: 1,
  scrollY: 0,
  rowH: 20,
  rows: [],
  cursor: null,
  markers: [],
  rulerMode: "time",
  clockPeriod: null,
  clockOrigin: 0,
  timescale: "1ns",
};

let dirty = false;
let rafHandle = 0;
let statsOn = false;
let frames = 0;
let totalMs = 0;
let maxMs = 0;

function post(msg: FromWorker): void {
  (self as unknown as Worker).postMessage(msg);
}

function requestDraw(): void {
  dirty = true;
  if (rafHandle) return;
  rafHandle = requestAnimationFrame(() => {
    rafHandle = 0;
    if (!dirty) return;
    dirty = false;
    const t = performance.now();
    draw();
    const ms = performance.now() - t;
    if (statsOn) {
      frames += 1;
      totalMs += ms;
      if (ms > maxMs) maxMs = ms;
    }
    post({ type: "frame", ms });
  });
}

self.onmessage = (ev: MessageEvent<ToWorker>) => {
  const msg = ev.data;
  switch (msg.type) {
    case "init":
      canvas = msg.canvas;
      dpr = msg.dpr;
      ctx = canvas.getContext("2d", { alpha: false });
      post({ type: "ready" });
      requestDraw();
      break;
    case "resize":
      width = msg.width;
      height = msg.height;
      dpr = msg.dpr;
      if (canvas) {
        canvas.width = Math.max(1, Math.round(width * dpr));
        canvas.height = Math.max(1, Math.round(height * dpr));
      }
      requestDraw();
      break;
    case "theme":
      colors = { ...colors, ...(msg.colors as unknown as Colors) };
      requestDraw();
      break;
    case "data":
      for (const c of msg.chunks) data.set(c.h, c);
      requestDraw();
      break;
    case "clear":
      data.clear();
      requestDraw();
      break;
    case "view":
      view = { ...msg };
      requestDraw();
      break;
    case "stats":
      statsOn = msg.enabled;
      if (msg.enabled) {
        frames = 0;
        totalMs = 0;
        maxMs = 0;
      } else {
        post({ type: "stats", frames, avgMs: frames ? totalMs / frames : 0, maxMs });
      }
      break;
  }
};

/** Load the data font into the worker so canvas text matches the chrome. */
export async function loadFont(family: string, url: string): Promise<void> {
  const anyself = self as unknown as { fonts?: FontFaceSet };
  if (!anyself.fonts) return;
  try {
    const face = new FontFace(family, `url(${url})`);
    await face.load();
    anyself.fonts.add(face);
    fontsReady = true;
    requestDraw();
  } catch {
    // Falls back to the generic monospace stack; not worth failing over.
  }
}

self.addEventListener("message", (ev: MessageEvent) => {
  const m = ev.data as { type?: string; family?: string; url?: string };
  if (m?.type === "font" && m.family && m.url) void loadFont(m.family, m.url);
});

function dataFont(px: number, weight = 400): string {
  return `${weight} ${px}px ${fontsReady ? '"JetBrains Mono", ' : ""}ui-monospace, monospace`;
}

function draw(): void {
  if (!ctx || !canvas) return;
  const c = ctx;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);

  c.fillStyle = colors.bgDeep;
  c.fillRect(0, 0, width, height);

  const v = { t0: view.t0, t1: view.t1 };
  drawGrid(c, v);
  drawRows(c, v);
  drawRuler(c, v);
  drawCursors(c, v);
}

function drawGrid(c: OffscreenCanvasRenderingContext2D, v: { t0: number; t1: number }): void {
  const step = tickStep(v, width);
  c.strokeStyle = colors.line;
  c.lineWidth = 1;
  c.beginPath();
  const first = Math.ceil(v.t0 / step) * step;
  for (let t = first; t <= v.t1; t += step) {
    const x = Math.round(timeToX(t, v, width)) + 0.5;
    c.moveTo(x, RULER_H);
    c.lineTo(x, height);
  }
  c.stroke();
}

function drawRuler(c: OffscreenCanvasRenderingContext2D, v: { t0: number; t1: number }): void {
  c.fillStyle = colors.bgPanel;
  c.fillRect(0, 0, width, RULER_H);
  c.strokeStyle = colors.line;
  c.beginPath();
  c.moveTo(0, RULER_H + 0.5);
  c.lineTo(width, RULER_H + 0.5);
  c.stroke();

  const step = tickStep(v, width);
  c.fillStyle = colors.textDim;
  c.font = dataFont(11);
  c.textBaseline = "middle";
  c.textAlign = "left";

  const first = Math.ceil(v.t0 / step) * step;
  for (let t = first; t <= v.t1; t += step) {
    const x = Math.round(timeToX(t, v, width));
    c.strokeStyle = colors.line;
    c.beginPath();
    c.moveTo(x + 0.5, RULER_H - 6);
    c.lineTo(x + 0.5, RULER_H);
    c.stroke();
    const label =
      view.rulerMode === "cycle" && view.clockPeriod
        ? `c${cycleAt(t, view.clockPeriod, view.clockOrigin)}`
        : formatTime(t, view.timescale);
    c.fillText(label, x + 4, RULER_H / 2);
  }
}

function rowY(i: number): number {
  return RULER_H + i * view.rowH - view.scrollY;
}

function drawRows(c: OffscreenCanvasRenderingContext2D, v: { t0: number; t1: number }): void {
  const rows = view.rows;
  const rowH = view.rowH;

  // Only touch rows that intersect the viewport.
  const firstVisible = Math.max(0, Math.floor((view.scrollY - RULER_H) / rowH));
  const lastVisible = Math.min(rows.length - 1, Math.ceil((view.scrollY + height) / rowH));

  // Alternating row bands, batched into one fill.
  c.fillStyle = colors.bgPanel;
  for (let i = firstVisible; i <= lastVisible; i++) {
    if (i % 2 === 1) continue;
    const y = rowY(i);
    if (y + rowH < RULER_H || y > height) continue;
    c.fillRect(0, y, width, rowH);
  }

  const pathHigh = new Path2D();
  const pathLow = new Path2D();
  const pathBus = new Path2D();
  const xRects: [number, number, number, number][] = [];
  const zRects: [number, number, number, number][] = [];
  const busText: [string, number, number, number][] = [];

  for (let i = firstVisible; i <= lastVisible; i++) {
    const row = rows[i];
    if (!row) continue;
    const y = rowY(i);
    if (y + rowH < RULER_H || y > height) continue;

    if (row.kind === "group") {
      c.fillStyle = colors.bgRaised;
      c.fillRect(0, y, width, rowH);
      continue;
    }
    const chunk = data.get(row.handle);
    if (!chunk) continue;

    if (row.width === 1) {
      drawScalarRow(chunk, y, rowH, v, pathHigh, pathLow, xRects, zRects);
    } else {
      drawBusRow(chunk, row, y, rowH, v, pathBus, xRects, zRects, busText);
    }
  }

  // Unknown states first, so the traces sit on top of their bands.
  if (xRects.length) {
    c.fillStyle = colors.sigX;
    c.globalAlpha = 0.28;
    for (const [x, y, w, h] of xRects) c.fillRect(x, y, w, h);
    c.globalAlpha = 1;
    c.strokeStyle = colors.sigX;
    c.lineWidth = 1;
    c.beginPath();
    for (const [x, y, w, h] of xRects) {
      c.moveTo(x, y + h / 2);
      c.lineTo(x + w, y + h / 2);
    }
    c.stroke();
  }
  if (zRects.length) {
    c.strokeStyle = colors.sigZ;
    c.lineWidth = 1;
    c.beginPath();
    for (const [x, y, w, h] of zRects) {
      c.moveTo(x, y + h / 2);
      c.lineTo(x + w, y + h / 2);
    }
    c.stroke();
  }

  c.lineWidth = 1.25;
  c.strokeStyle = colors.sig0;
  c.stroke(pathLow);
  c.strokeStyle = colors.sig1;
  c.stroke(pathHigh);
  c.lineWidth = 1;
  c.strokeStyle = colors.sigBus;
  c.stroke(pathBus);

  if (busText.length) {
    c.font = dataFont(11);
    c.fillStyle = colors.textBright;
    c.textAlign = "center";
    c.textBaseline = "middle";
    for (const [text, x, y, w] of busText) {
      const fitted = elide(text, w - 8, CHAR_W);
      if (fitted) c.fillText(fitted, x, y);
    }
  }
}

/** Iterate a chunk as [tStart, tEnd, bits, busy] segments clipped to the view. */
function* segments(
  chunk: WaveChunk,
  v: { t0: number; t1: number },
): Generator<[number, number, string, boolean, number]> {
  const tr = chunk.transitions;
  if (chunk.mode === "exact") {
    const pts = tr as ExactPoint[];
    let prevT = v.t0;
    let prevV = chunk.initial;
    for (const [t, bits] of pts) {
      if (prevV !== null && t > prevT) yield [prevT, t, prevV, false, 0];
      prevT = t;
      prevV = bits;
    }
    if (prevV !== null) yield [prevT, v.t1, prevV, false, 0];
  } else {
    const pts = tr as MinMaxPoint[];
    let prevT = v.t0;
    let prevV = chunk.initial;
    for (const [t, min, max, n, flags] of pts) {
      if (prevV !== null && t > prevT) yield [prevT, t, prevV, false, 0];
      // The bucket itself: a pixel column that saw `n` changes.
      const next = t + Math.max((v.t1 - v.t0) / Math.max(chunk.px_width, 1), 1);
      yield [t, next, min === max ? min : `${min} ${max}`, n > 1 || min !== max, flags];
      prevT = next;
      prevV = max;
    }
    if (prevV !== null && prevT < v.t1) yield [prevT, v.t1, prevV, false, 0];
  }
}

function drawScalarRow(
  chunk: WaveChunk,
  y: number,
  rowH: number,
  v: { t0: number; t1: number },
  pathHigh: Path2D,
  pathLow: Path2D,
  xRects: [number, number, number, number][],
  zRects: [number, number, number, number][],
): void {
  const yHigh = Math.round(y + 3) + 0.5;
  const yLow = Math.round(y + rowH - 4) + 0.5;
  let lastX: number | null = null;
  let lastY: number | null = null;

  for (const [ta, tb, bits, busy, flags] of segments(chunk, v)) {
    const xa = Math.max(timeToX(ta, v, width), -2);
    const xb = Math.min(timeToX(tb, v, width), width + 2);
    if (xb < 0 || xa > width) continue;

    const level = bits[bits.length - 1];
    if (busy || (flags & (FLAG_X | FLAG_Z)) !== 0) {
      // A pixel that toggled, or held an unknown: draw the full-height band.
      const w = Math.max(xb - xa, 1);
      if (flags & FLAG_X) xRects.push([xa, y + 2, w, rowH - 5]);
      else if (flags & FLAG_Z) zRects.push([xa, y + 2, w, rowH - 5]);
      else {
        pathHigh.moveTo(xa + 0.5, yHigh);
        pathHigh.lineTo(xa + 0.5, yLow);
        pathHigh.moveTo(xa + 0.5, yHigh);
        pathHigh.lineTo(xb, yHigh);
        pathLow.moveTo(xa + 0.5, yLow);
        pathLow.lineTo(xb, yLow);
      }
      lastX = xb;
      lastY = null;
      continue;
    }

    if (level === "x" || level === "X") {
      xRects.push([xa, y + 2, Math.max(xb - xa, 1), rowH - 5]);
      lastY = null;
      lastX = xb;
      continue;
    }
    if (level === "z" || level === "Z") {
      zRects.push([xa, y + 2, Math.max(xb - xa, 1), rowH - 5]);
      lastY = null;
      lastX = xb;
      continue;
    }

    const yy = level === "1" ? yHigh : yLow;
    const path = level === "1" ? pathHigh : pathLow;
    // Vertical edge from the previous level.
    if (lastY !== null && lastX !== null && lastY !== yy) {
      path.moveTo(lastX + 0.5, lastY);
      path.lineTo(lastX + 0.5, yy);
    }
    path.moveTo(Math.max(xa, 0), yy);
    path.lineTo(xb, yy);
    lastX = xb;
    lastY = yy;
  }
}

function drawBusRow(
  chunk: WaveChunk,
  row: RenderRow,
  y: number,
  rowH: number,
  v: { t0: number; t1: number },
  pathBus: Path2D,
  xRects: [number, number, number, number][],
  zRects: [number, number, number, number][],
  busText: [string, number, number, number][],
): void {
  const yTop = Math.round(y + 3) + 0.5;
  const yBot = Math.round(y + rowH - 4) + 0.5;
  const yMid = (yTop + yBot) / 2;

  for (const [ta, tb, bits, busy, flags] of segments(chunk, v)) {
    const xa = Math.max(timeToX(ta, v, width), -4);
    const xb = Math.min(timeToX(tb, v, width), width + 4);
    const w = xb - xa;
    if (xb < 0 || xa > width || w <= 0) continue;

    if (flags & FLAG_X) {
      xRects.push([xa, y + 2, Math.max(w, 1), rowH - 5]);
      continue;
    }
    if (flags & FLAG_Z) {
      zRects.push([xa, y + 2, Math.max(w, 1), rowH - 5]);
      continue;
    }
    if (bits === "x" || bits === "X") {
      xRects.push([xa, y + 2, Math.max(w, 1), rowH - 5]);
      continue;
    }
    if (bits === "z" || bits === "Z") {
      zRects.push([xa, y + 2, Math.max(w, 1), rowH - 5]);
      continue;
    }

    // Hexagon: bevelled ends, so consecutive values read as distinct cells.
    const bevel = Math.min(3, w / 2);
    pathBus.moveTo(xa, yMid);
    pathBus.lineTo(xa + bevel, yTop);
    pathBus.lineTo(xb - bevel, yTop);
    pathBus.lineTo(xb, yMid);
    pathBus.lineTo(xb - bevel, yBot);
    pathBus.lineTo(xa + bevel, yBot);
    pathBus.closePath();

    if (!busy && w >= MIN_TEXT_PX) {
      const text = format(bits, row.width, row.radix);
      busText.push([text, (xa + xb) / 2, yMid, w]);
    }
  }
}

function drawCursors(c: OffscreenCanvasRenderingContext2D, v: { t0: number; t1: number }): void {
  // Measurement markers first, dimmer than the main cursor.
  c.lineWidth = 1;
  for (const m of view.markers) {
    const x = Math.round(timeToX(m, v, width)) + 0.5;
    if (x < 0 || x > width) continue;
    c.strokeStyle = colors.textDim;
    c.setLineDash([3, 3]);
    c.beginPath();
    c.moveTo(x, RULER_H);
    c.lineTo(x, height);
    c.stroke();
    c.setLineDash([]);
  }

  if (view.cursor === null) return;
  const x = Math.round(timeToX(view.cursor, v, width)) + 0.5;
  if (x < -1 || x > width + 1) return;
  // The main cursor stays monochrome: amber means causality (§11.1) and must
  // not be spent on chrome.
  c.strokeStyle = colors.textBright;
  c.beginPath();
  c.moveTo(x, RULER_H - 4);
  c.lineTo(x, height);
  c.stroke();

  // Delta readout against the nearest marker.
  if (view.markers.length > 0) {
    const nearest = view.markers.reduce((a, b) =>
      Math.abs(b - view.cursor!) < Math.abs(a - view.cursor!) ? b : a,
    );
    const dt = Math.abs(view.cursor - nearest);
    const label = `Δ ${formatTime(dt, view.timescale)}`;
    c.font = dataFont(11);
    c.textAlign = "left";
    c.textBaseline = "top";
    const tw = c.measureText(label).width + 8;
    c.fillStyle = colors.bgRaised;
    c.fillRect(x + 4, RULER_H + 2, tw, 15);
    c.fillStyle = colors.textBright;
    c.fillText(label, x + 8, RULER_H + 5);
  }
}
