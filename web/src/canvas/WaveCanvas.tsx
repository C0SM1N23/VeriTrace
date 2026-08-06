/**
 * Owns the OffscreenCanvas, the render worker and the wave socket.
 *
 * This component renders one `<canvas>` and then never re-renders during
 * interaction: pan, zoom and cursor moves go straight from the event handler to
 * the worker through an imperative store subscription. That is what keeps a
 * continuous scroll at 60 fps — React is not in the loop.
 *
 * Data is fetched for a window wider than the viewport, so panning redraws from
 * cache and only crossing the margin triggers a new request.
 */

import { useEffect, useRef } from "react";
import jetbrainsMonoUrl from "@fontsource/jetbrains-mono/files/jetbrains-mono-latin-400-normal.woff2?url";
import { WaveSocket, type WaveRequest } from "../api/client";
import type { ExactPoint, MinMaxPoint, RenderRow, WaveChunk } from "../lib/types";
import { xToTime } from "../lib/time";
import { useWave, visibleRows, type WaveState } from "../state/store";
import { setChunks } from "../state/values";

/** Fetch this many viewports either side of the visible window. */
const MARGIN = 1.0;
const REFETCH_DEBOUNCE_MS = 90;

function readColors(): Record<string, string> {
  const s = getComputedStyle(document.documentElement);
  const v = (n: string) => s.getPropertyValue(n).trim();
  return {
    bgDeep: v("--bg-deep"),
    bgPanel: v("--bg-panel"),
    bgRaised: v("--bg-raised"),
    line: v("--line"),
    textDim: v("--text-dim"),
    text: v("--text"),
    textBright: v("--text-bright"),
    sig0: v("--sig-0"),
    sig1: v("--sig-1"),
    sigX: v("--sig-x"),
    sigZ: v("--sig-z"),
    sigBus: v("--sig-bus"),
  };
}

function renderRows(s: WaveState): RenderRow[] {
  return visibleRows(s.rows).map((r) => {
    if (r.kind === "group") {
      return { kind: "group", handle: -1, label: r.name, width: 0, radix: "hex" } as RenderRow;
    }
    const sig = s.signalsByHandle.get(r.handle);
    return {
      kind: "signal",
      handle: r.handle,
      label: sig?.name ?? r.path,
      width: sig?.width ?? 1,
      radix: s.radix[r.path] ?? "hex",
    } as RenderRow;
  });
}

export function WaveCanvas() {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const workerRef = useRef<Worker | null>(null);
  /** Set once the canvas has been handed to the worker; never reset. */
  const transferredRef = useRef(false);
  const socketRef = useRef<WaveSocket | null>(null);
  const sizeRef = useRef({ w: 0, h: 0 });
  /** Window currently held in the worker, so we know when to refetch. */
  const cachedRef = useRef<{ t0: number; t1: number; px: number; handles: string } | null>(null);
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunksRef = useRef(new Map<number, WaveChunk>());

  const session = useWave((s) => s.session);
  const ready = useWave((s) => s.ready);

  // --- worker lifecycle ---------------------------------------------------
  //
  // `transferControlToOffscreen` can only ever be called once for a given
  // canvas element. StrictMode runs effects twice in development, so the guard
  // has to be a ref that survives the remount — and the cleanup must not tear
  // the worker down, or the second mount would face a canvas it can no longer
  // transfer. This component lives for the lifetime of the app, so holding the
  // worker is the right trade.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || transferredRef.current) return;
    transferredRef.current = true;

    const worker = new Worker(new URL("./waveWorker.ts", import.meta.url), { type: "module" });
    workerRef.current = worker;
    const offscreen = canvas.transferControlToOffscreen();
    worker.postMessage({ type: "init", canvas: offscreen, dpr: window.devicePixelRatio || 1 }, [
      offscreen,
    ]);
    worker.postMessage({ type: "theme", colors: readColors() });
    worker.postMessage({ type: "font", family: "JetBrains Mono", url: jetbrainsMonoUrl });

    // Frame timings are exposed for the performance test to sample.
    const w = window as unknown as { __vtFrames?: number[] };
    w.__vtFrames = [];
    worker.onmessage = (ev: MessageEvent) => {
      if (ev.data?.type === "frame") {
        w.__vtFrames!.push(ev.data.ms);
        if (w.__vtFrames!.length > 2000) w.__vtFrames!.shift();
      }
    };

    // Deliberately no teardown here: see the note above.
  }, []);

  // --- size ---------------------------------------------------------------
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const ro = new ResizeObserver(() => {
      const rect = host.getBoundingClientRect();
      sizeRef.current = { w: rect.width, h: rect.height };
      workerRef.current?.postMessage({
        type: "resize",
        width: rect.width,
        height: rect.height,
        dpr: window.devicePixelRatio || 1,
      });
      pushView();
      scheduleFetch();
    });
    ro.observe(host);
    return () => ro.disconnect();
  }, []);

  // --- push view to worker on every store change (no React render) --------
  function pushView(): void {
    const s = useWave.getState();
    workerRef.current?.postMessage({
      type: "view",
      t0: s.view.t0,
      t1: s.view.t1,
      scrollY: s.scrollY,
      rowH: s.rowH,
      rows: renderRows(s),
      cursor: s.cursor,
      markers: s.markers,
      rulerMode: s.rulerMode,
      clockPeriod: s.clockPeriod,
      clockOrigin: s.clockOrigin,
      timescale: s.status?.timescale ?? "1ns",
    });
  }

  function neededRequest(): WaveRequest | null {
    const s = useWave.getState();
    const { w } = sizeRef.current;
    if (!s.ready || w <= 0) return null;
    const handles = visibleRows(s.rows)
      .filter((r) => r.kind === "signal")
      .map((r) => (r as { handle: number }).handle);
    if (handles.length === 0) return null;

    const span = s.view.t1 - s.view.t0;
    const t0 = Math.max(s.bounds.t0, s.view.t0 - span * MARGIN);
    const t1 = Math.min(s.bounds.t1 + 1, s.view.t1 + span * MARGIN);
    // Resolution matched to the widened window so panning stays sharp.
    const px = Math.min(Math.round(w * (1 + 2 * MARGIN)), 8192);
    return { signals: handles, t0, t1, pxWidth: px };
  }

  function needsRefetch(req: WaveRequest): boolean {
    const cached = cachedRef.current;
    if (!cached) return true;
    if (cached.handles !== req.signals.join(",")) return true;
    const s = useWave.getState();
    // Refetch once the viewport leaves the cached window, or the zoom level
    // drifts far enough that the cached resolution is wrong.
    if (s.view.t0 < cached.t0 || s.view.t1 > cached.t1) return true;
    const cachedSpanPerPx = (cached.t1 - cached.t0) / cached.px;
    const wantSpanPerPx = (s.view.t1 - s.view.t0) / Math.max(sizeRef.current.w, 1);
    return wantSpanPerPx < cachedSpanPerPx * 0.5 || wantSpanPerPx > cachedSpanPerPx * 4;
  }

  function scheduleFetch(): void {
    if (refetchTimer.current) clearTimeout(refetchTimer.current);
    refetchTimer.current = setTimeout(() => {
      const req = neededRequest();
      if (!req || !socketRef.current) return;
      if (!needsRefetch(req)) return;
      socketRef.current.request(req);
    }, REFETCH_DEBOUNCE_MS);
  }

  // --- socket -------------------------------------------------------------
  useEffect(() => {
    if (!session || !ready) return;
    const sock = new WaveSocket(session, (chunks, req) => {
      for (const c of chunks) chunksRef.current.set(c.h, c);
      // The Source tab reads values from here for its inlays.
      setChunks(chunks);
      cachedRef.current = {
        t0: req.t0,
        t1: req.t1,
        px: req.pxWidth,
        handles: req.signals.join(","),
      };
      workerRef.current?.postMessage({ type: "data", chunks });
      (window as unknown as { __vtDataReady?: boolean }).__vtDataReady = true;
    });
    socketRef.current = sock;
    void sock.connect().then(() => scheduleFetch());
    return () => {
      sock.close();
      socketRef.current = null;
    };
  }, [session, ready]);

  // --- subscribe imperatively --------------------------------------------
  useEffect(() => {
    pushView();
    scheduleFetch();
    const unsub = useWave.subscribe(() => {
      pushView();
      scheduleFetch();
    });
    return unsub;
  }, []);

  // --- interactions -------------------------------------------------------
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const s = useWave.getState();
      const rect = host.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        // Ctrl+scroll = zoom around the pointer.
        const factor = Math.exp(e.deltaY * 0.002);
        s.zoom(factor, e.clientX - rect.left, rect.width);
      } else if (e.shiftKey) {
        s.setScrollY(s.scrollY + e.deltaY);
      } else {
        // Plain scroll = horizontal pan, the primary gesture here.
        const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        s.pan(dx, rect.width);
      }
    };

    const onPointerDown = (e: PointerEvent) => {
      const rect = host.getBoundingClientRect();
      const s = useWave.getState();
      const t = xToTime(e.clientX - rect.left, s.view, rect.width);
      if (e.shiftKey) s.addMarker(Math.round(t));
      else s.setCursor(Math.round(t));

      // Row under the pointer becomes the selection.
      const rows = visibleRows(s.rows);
      const idx = Math.floor((e.clientY - rect.top + s.scrollY - 26) / s.rowH);
      const row = rows[idx];
      if (row && row.kind === "signal") s.setSelected(row.handle);
    };

    // Right-click opens the causal menu on the signal under the pointer —
    // the main route through the application (§11.4).
    const onContext = (e: MouseEvent) => {
      const rect = host.getBoundingClientRect();
      const s = useWave.getState();
      const rows = visibleRows(s.rows);
      const idx = Math.floor((e.clientY - rect.top + s.scrollY - 26) / s.rowH);
      const row = rows[idx];
      if (!row || row.kind !== "signal") return;
      e.preventDefault();
      const t = Math.round(xToTime(e.clientX - rect.left, s.view, rect.width));
      s.setSelected(row.handle);
      s.setCursor(t);
      window.dispatchEvent(
        new CustomEvent("vt:wave-menu", {
          detail: { x: e.clientX, y: e.clientY, path: row.path, handle: row.handle, time: t },
        }),
      );
    };

    host.addEventListener("wheel", onWheel, { passive: false });
    host.addEventListener("pointerdown", onPointerDown);
    host.addEventListener("contextmenu", onContext);
    return () => {
      host.removeEventListener("wheel", onWheel);
      host.removeEventListener("pointerdown", onPointerDown);
      host.removeEventListener("contextmenu", onContext);
    };
  }, []);

  // Arrow keys step to the previous/next transition of the selected signal,
  // resolved against the data already cached here.
  useEffect(() => {
    const onStep = (ev: Event) => {
      const { handle, from, dir } = (ev as CustomEvent).detail as {
        handle: number;
        from: number;
        dir: 1 | -1;
      };
      const chunk = chunksRef.current.get(handle);
      if (!chunk) return;
      const times =
        chunk.mode === "exact"
          ? (chunk.transitions as ExactPoint[]).map((p) => p[0])
          : (chunk.transitions as MinMaxPoint[]).map((p) => p[0]);
      const next =
        dir > 0 ? times.find((t) => t > from) : [...times].reverse().find((t) => t < from);
      if (next !== undefined) {
        const s = useWave.getState();
        s.setCursor(next);
        // Keep the cursor on screen.
        if (next < s.view.t0 || next > s.view.t1) {
          const span = s.view.t1 - s.view.t0;
          s.setView({ t0: next - span / 2, t1: next + span / 2 });
        }
      }
    };
    window.addEventListener("vt:step-edge", onStep);
    return () => window.removeEventListener("vt:step-edge", onStep);
  }, []);

  return (
    <div className="wave-canvas" ref={hostRef} data-testid="wave-canvas">
      <canvas ref={canvasRef} />
    </div>
  );
}
