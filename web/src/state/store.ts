/**
 * Application state.
 *
 * Performance note that shapes the whole design: `view` changes on every frame
 * of a pan. Components must therefore *not* select it — the canvas and the
 * status bar subscribe imperatively (`useWave.subscribe`) and write to the
 * worker or the DOM directly. Selecting `view` in a React component would put a
 * full render on the critical path of every wheel event and cost the 60 fps
 * budget.
 */

import { create } from "zustand";
import {
  fetchChecks,
  fetchLayout,
  fetchRoot,
  fetchSignals,
  fetchSource,
  fetchStatus,
  fetchTransactions,
  putLayout,
  runQuery,
  fetchCommands,
  fetchCoverage,
  fetchMemory,
  fetchPerformance,
  runTxnQuery,
  suppressFinding,
  unsuppressFinding,
} from "../api/client";
import type {
  CausalNode,
  ChecksReport,
  Layout,
  Radix,
  Row,
  SessionStatus,
  SignalMeta,
  SourceFile,
  Transaction,
  CmdEvent,
  CoverageReport,
  MemoryReport,
  PerfReport,
  TxnReport,
  WhyResult,
} from "../lib/types";
import { clampView, panBy, zoomAt, type View } from "../lib/time";

const PERSIST_DEBOUNCE_MS = 400;
/** Rows seeded when a trace is opened for the first time. */
const SEED_ROWS = 40;

/** Tab names the server may return for §11.4b, mapped to strip positions. */
const TAB_BY_NAME: Record<string, number> = {
  wave: 1,
  causal: 2,
  source: 3,
  fsm: 4,
  checks: 5,
  diff: 6,
  coverage: 7,
  transactions: 8,
  performance: 9,
  memory: 10,
};

export interface WaveState {
  session: string | null;
  status: SessionStatus | null;
  signals: SignalMeta[];
  signalsByHandle: Map<number, SignalMeta>;

  rows: Row[];
  radix: Record<string, Radix>;
  view: View;
  bounds: View;
  scrollY: number;
  rowH: number;
  cursor: number | null;
  markers: number[];
  rulerMode: "time" | "cycle";
  clockPeriod: number | null;
  clockOrigin: number;

  selected: number | null;
  /** §11.6: the one selection every panel reads and writes. */
  sourceLoc: { file: string; line: number } | null;
  causal: WhyResult | null;
  causalError: string | null;
  causalBusy: boolean;
  activeNode: string | null;
  source: SourceFile | null;
  sourceBusy: boolean;
  checks: ChecksReport | null;
  checksBusy: boolean;
  // §8.13-8.14, TAB 8.
  txn: TxnReport | null;
  txnBusy: boolean;
  /** Which interface the Gantt is showing. */
  txnIface: string | null;
  txnRows: Transaction[];
  txnSelected: string | null;
  txnError: string | null;
  // §8.17-8.18, TAB 9.
  perf: PerfReport | null;
  perfBusy: boolean;
  perfError: string | null;
  /** Which interface the Performance charts are showing. */
  perfIface: string | null;
  /**
   * Time window selected by dragging in any chart. §11.4b: selecting one
   * narrows every other chart to it and sends Wave there, so the four charts
   * and the waveform are one view of one interval rather than four pictures.
   */
  perfWindow: { t0: number; t1: number } | null;
  // §8.20, TAB 10.
  memory: MemoryReport | null;
  memoryBusy: boolean;
  memoryError: string | null;
  /** Which memory interface the bank timeline and command list are showing. */
  memIface: string | null;
  /** Decoded command stream for `memIface`, fetched on demand (it is large). */
  memCommands: CmdEvent[];
  memFilter: string;
  /**
   * The row list a click-through replaced, kept so the move can be undone.
   *
   * §11.4b's click-throughs focus Wave on one transaction or one violation,
   * which means replacing the list you assembled. Without this that was a
   * one-way door: no undo, and the replacement was persisted, so a reload
   * did not bring your work back either. Everything else in the app is
   * additive or reversible; this was the exception.
   *
   * Held as a *swap* rather than an undo stack: going back stashes what you
   * were looking at, so neither list can be lost in either direction, and
   * there is no history to reason about.
   */
  stashedRows: Row[] | null;
  /** What the swap will bring back — shown on the button. */
  stashedLabel: string;
  /** What the current list is, when a click-through chose it. */
  focusLabel: string;
  // §8.21 + §8.12, TAB 7.
  coverage: CoverageReport | null;
  coverageBusy: boolean;
  coverageError: string | null;
  /** Which interface's functional matrix is shown. */
  covIface: string | null;
  /** Which uncovered point is expanded into its derived conditions. */
  covHole: string | null;
  filter: string;
  paletteOpen: boolean;
  helpOpen: boolean;
  activeTab: number;
  ready: boolean;
  error: string | null;

  load: () => Promise<void>;
  setView: (v: View) => void;
  pan: (dxPx: number, widthPx: number) => void;
  zoom: (factor: number, pixelX: number, widthPx: number) => void;
  zoomAll: () => void;
  setScrollY: (y: number) => void;
  setCursor: (t: number | null) => void;
  addMarker: (t: number) => void;
  clearMarkers: () => void;
  setRadix: (path: string, r: Radix) => void;
  cycleRadix: (path: string) => void;
  moveRow: (from: number, to: number) => void;
  removeRow: (index: number) => void;
  addSignal: (handle: number) => void;
  addGroup: (name: string) => void;
  toggleGroup: (id: string) => void;
  renameGroup: (id: string, name: string) => void;
  setRowH: (h: number) => void;
  toggleRuler: () => void;
  setSelected: (handle: number | null) => void;
  setFilter: (q: string) => void;
  setPalette: (open: boolean) => void;
  setHelp: (open: boolean) => void;
  setTab: (n: number) => void;
  stepEdge: (dir: 1 | -1) => Promise<void>;
  runWhy: (signalPath: string, t: number) => Promise<void>;
  runQueryText: (vtq: string) => Promise<void>;
  openSource: (file: string, line?: number) => Promise<void>;
  selectCausal: (node: CausalNode) => void;
  clearCausal: () => void;
  loadChecks: () => Promise<void>;
  loadTransactions: () => Promise<void>;
  selectIface: (name: string) => Promise<void>;
  runTxn: (vtq: string) => Promise<void>;
  openTransaction: (t: Transaction) => void;
  loadPerformance: () => Promise<void>;
  selectPerfIface: (name: string) => void;
  setPerfWindow: (w: { t0: number; t1: number } | null) => void;
  openTransactionRef: (ref: string) => Promise<void>;
  loadMemory: () => Promise<void>;
  selectMemIface: (name: string) => Promise<void>;
  setMemFilter: (q: string) => void;
  /** Focus Wave on a set of signals, remembering what it replaced. */
  focusRows: (rows: Row[], label: string) => void;
  /** Swap the current list with the one a click-through replaced. */
  swapRows: () => void;
  loadCoverage: () => Promise<void>;
  selectCovIface: (name: string) => void;
  selectHole: (key: string | null) => void;
  jumpTo: (t: number, signals?: string[], label?: string) => void;
  suppress: (id: string, reason: string) => Promise<void>;
  unsuppress: (id: string) => Promise<void>;
  openFinding: (f: { why: string | null; loc: { file: string; line: number } | null }) => void;
}

let persistTimer: ReturnType<typeof setTimeout> | null = null;
/** Set by tests to observe that a save actually reached the server. */
let persistCount = 0;

export function layoutFrom(s: WaveState): Layout {
  return {
    signals: s.rows,
    groups: [],
    radix: s.radix,
    bookmarks: [],
    cursors: s.cursor === null ? s.markers : [s.cursor, ...s.markers],
    zoom: { t0: Math.round(s.view.t0), t1: Math.round(s.view.t1) },
    // Extra keys: the server stores the layout as an open document, so view
    // preferences ride along with it. "Exactly as you left it" includes the
    // ruler mode and row density, not just which signals were on screen.
    rulerMode: s.rulerMode,
    rowH: s.rowH,
  };
}

function schedulePersist(get: () => WaveState): void {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    const s = get();
    if (!s.session || !s.ready) return;
    void putLayout(s.session, layoutFrom(s))
      .then(() => {
        persistCount += 1;
        (window as unknown as { __vtSaves?: number }).__vtSaves = persistCount;
      })
      .catch((e) => console.warn("layout save failed", e));
  }, PERSIST_DEBOUNCE_MS);
}

/** Flush any pending layout save immediately (used on page hide). */
export async function flushPersist(): Promise<void> {
  if (persistTimer) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  const s = useWave.getState();
  if (!s.session || !s.ready) return;
  await putLayout(s.session, layoutFrom(s)).catch(() => {});
}

function seedRows(signals: SignalMeta[]): Row[] {
  return signals
    .slice(0, SEED_ROWS)
    .map((s) => ({ kind: "signal", handle: s.handle, path: s.path }) as Row);
}

/** Guess the primary clock: the narrowest, busiest 1-bit signal named like one. */
function guessClock(signals: SignalMeta[]): SignalMeta | null {
  const ones = signals.filter((s) => s.width === 1 && s.n_events > 2);
  const named = ones.filter((s) => /(^|[._])c(lk|lock)\b|clk/i.test(s.name));
  const pool = named.length > 0 ? named : ones;
  if (pool.length === 0) return null;
  return pool.reduce((a, b) => (b.n_events > a.n_events ? b : a));
}

export const useWave = create<WaveState>((set, get) => ({
  session: null,
  status: null,
  signals: [],
  signalsByHandle: new Map(),
  rows: [],
  radix: {},
  view: { t0: 0, t1: 1 },
  bounds: { t0: 0, t1: 1 },
  scrollY: 0,
  rowH: 20,
  cursor: null,
  markers: [],
  rulerMode: "time",
  clockPeriod: null,
  clockOrigin: 0,
  selected: null,
  sourceLoc: null,
  causal: null,
  causalError: null,
  causalBusy: false,
  activeNode: null,
  source: null,
  sourceBusy: false,
  checks: null,
  checksBusy: false,
  txn: null,
  txnBusy: false,
  txnIface: null,
  txnRows: [],
  txnSelected: null,
  txnError: null,
  perf: null,
  perfBusy: false,
  perfError: null,
  perfIface: null,
  perfWindow: null,
  memory: null,
  memoryBusy: false,
  memoryError: null,
  memIface: null,
  memCommands: [],
  memFilter: "",
  stashedRows: null,
  stashedLabel: "",
  focusLabel: "",
  coverage: null,
  coverageBusy: false,
  coverageError: null,
  covIface: null,
  covHole: null,
  filter: "",
  paletteOpen: false,
  helpOpen: false,
  activeTab: 1,
  ready: false,
  error: null,

  load: async () => {
    try {
      const root = await fetchRoot();
      // `?session=<id>` opens a trace other than the one the server was
      // started on. One server can hold several sessions (§10.1), and this is
      // what makes the second one reachable — for a diff, a second dump, or a
      // link a colleague pasted (§13.8).
      const requested = new URLSearchParams(location.search).get("session");
      const session = requested || root.default_session;
      if (!session) throw new Error("server has no trace open");
      const [status, signals, layout] = await Promise.all([
        fetchStatus(session),
        fetchSignals(session),
        fetchLayout(session),
      ]);

      const bounds = { t0: status.t0, t1: Math.max(status.t1, status.t0 + 1) };
      const saved = Array.isArray(layout.signals) ? (layout.signals as Row[]) : [];
      const known = new Set(signals.map((s) => s.handle));
      // Drop rows whose signal no longer exists (the dump may have changed).
      const rows = saved.filter((r) => r.kind === "group" || known.has(r.handle));

      const clock = guessClock(signals);
      let clockPeriod: number | null = null;
      let clockOrigin = 0;
      if (clock && status.t1 > status.t0 && clock.n_events > 1) {
        // Two edges per cycle.
        clockPeriod = ((status.t1 - status.t0) / clock.n_events) * 2;
        clockOrigin = status.t0;
      }

      set({
        session,
        status,
        signals,
        signalsByHandle: new Map(signals.map((s) => [s.handle, s])),
        rows: rows.length > 0 ? rows : seedRows(signals),
        radix: layout.radix ?? {},
        bounds,
        view: layout.zoom ? clampView(layout.zoom, bounds) : bounds,
        cursor: layout.cursors?.length ? layout.cursors[0] : null,
        markers: layout.cursors?.length ? layout.cursors.slice(1) : [],
        rulerMode: layout.rulerMode === "cycle" ? "cycle" : "time",
        rowH: layout.rowH === 28 ? 28 : 20,
        clockPeriod,
        clockOrigin,
        // §11.4b: the server decides which tab suits this design. §13.4 wants
        // the first impression to be "the tool already knows something", so a
        // session with findings opens on Checks rather than an empty canvas.
        activeTab: TAB_BY_NAME[status.default_tab] ?? 1,
        ready: true,
        error: null,
      });
      if (status.default_tab === "checks") void get().loadChecks();
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e), ready: false });
    }
  },

  setView: (v) => {
    set({ view: clampView(v, get().bounds) });
    schedulePersist(get);
  },
  pan: (dxPx, widthPx) => {
    set({ view: panBy(get().view, dxPx, widthPx, get().bounds) });
    schedulePersist(get);
  },
  zoom: (factor, pixelX, widthPx) => {
    set({ view: zoomAt(get().view, factor, pixelX, widthPx, get().bounds) });
    schedulePersist(get);
  },
  zoomAll: () => {
    set({ view: { ...get().bounds } });
    schedulePersist(get);
  },
  setScrollY: (y) => set({ scrollY: Math.max(0, y) }),

  setCursor: (t) => {
    set({ cursor: t });
    schedulePersist(get);
  },
  addMarker: (t) => {
    set({ markers: [...get().markers, t].slice(-4) });
    schedulePersist(get);
  },
  clearMarkers: () => {
    set({ markers: [] });
    schedulePersist(get);
  },

  setRadix: (path, r) => {
    set({ radix: { ...get().radix, [path]: r } });
    schedulePersist(get);
  },
  cycleRadix: (path) => {
    const order: Radix[] = ["hex", "dec", "bin", "ascii"];
    const cur = get().radix[path] ?? "hex";
    const next = order[(order.indexOf(cur) + 1) % order.length];
    get().setRadix(path, next);
  },

  moveRow: (from, to) => {
    const rows = [...get().rows];
    if (from < 0 || from >= rows.length) return;
    const [item] = rows.splice(from, 1);
    rows.splice(Math.max(0, Math.min(to, rows.length)), 0, item);
    set({ rows });
    schedulePersist(get);
  },
  removeRow: (index) => {
    const rows = [...get().rows];
    rows.splice(index, 1);
    set({ rows });
    schedulePersist(get);
  },
  addSignal: (handle) => {
    const sig = get().signalsByHandle.get(handle);
    if (!sig) return;
    set({ rows: [...get().rows, { kind: "signal", handle, path: sig.path }] });
    schedulePersist(get);
  },
  addGroup: (name) => {
    const id = `g${Date.now().toString(36)}`;
    set({ rows: [...get().rows, { kind: "group", id, name, collapsed: false }] });
    schedulePersist(get);
  },
  toggleGroup: (id) => {
    set({
      rows: get().rows.map((r) =>
        r.kind === "group" && r.id === id ? { ...r, collapsed: !r.collapsed } : r,
      ),
    });
    schedulePersist(get);
  },
  renameGroup: (id, name) => {
    set({
      rows: get().rows.map((r) => (r.kind === "group" && r.id === id ? { ...r, name } : r)),
    });
    schedulePersist(get);
  },

  setRowH: (h) => {
    set({ rowH: h });
    schedulePersist(get);
  },
  toggleRuler: () => {
    set({ rulerMode: get().rulerMode === "time" ? "cycle" : "time" });
    schedulePersist(get);
  },
  setSelected: (handle) => set({ selected: handle }),
  setFilter: (q) => set({ filter: q }),
  setPalette: (open) => set({ paletteOpen: open }),
  setHelp: (open) => set({ helpOpen: open }),
  setTab: (n) => set({ activeTab: n }),

  stepEdge: async (dir) => {
    const s = get();
    if (s.selected === null || !s.session) return;
    const t = s.cursor ?? s.view.t0;
    const url =
      `/session/${s.session}/signals?q=` +
      encodeURIComponent(s.signalsByHandle.get(s.selected)?.path ?? "");
    void url;
    // Edge stepping is served by the wave data already in the worker; the store
    // only needs to move the cursor, which WaveCanvas resolves against its
    // cached transitions.
    const detail = { handle: s.selected, from: t, dir };
    window.dispatchEvent(new CustomEvent("vt:step-edge", { detail }));
  },

  /**
   * Run `why()` and switch to Causal. This is the main route through the
   * application (§11.4), so it also parks the cursor on the asked-about time.
   */
  runWhy: async (signalPath, t) => {
    const s = get();
    if (!s.session) return;
    set({ causalBusy: true, causalError: null, activeTab: 2, cursor: Math.round(t) });
    try {
      const res = await runQuery(s.session, `why(${signalPath} @ ${Math.round(t)})`);
      set({ causal: res, causalBusy: false, activeNode: nodeId(res.root) });
      const loc = res.root.loc;
      if (loc) void get().openSource(loc.file, loc.line);
    } catch (e) {
      set({
        causal: null,
        causalBusy: false,
        causalError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openSource: async (file, line) => {
    const s = get();
    if (!s.session) return;
    if (s.source?.file === file) {
      if (line) set({ sourceLoc: { file, line } });
      return;
    }
    set({ sourceBusy: true });
    try {
      const src = await fetchSource(s.session, file);
      set({ source: src, sourceBusy: false, sourceLoc: line ? { file, line } : null });
    } catch {
      set({ source: null, sourceBusy: false });
    }
  },

  /**
   * §11.6: one click, every panel moves. Cursor, wave selection and source
   * location update together, with no animation and no manual sync.
   */
  selectCausal: (node) => {
    const s = get();
    const handle = s.signals.find((x) => x.path === node.signal)?.handle ?? null;
    set({
      activeNode: nodeId(node),
      cursor: node.time,
      selected: handle,
      sourceLoc: node.loc ? { file: node.loc.file, line: node.loc.line } : s.sourceLoc,
    });
    // Bring the moment into view if it drifted off screen.
    if (node.time < s.view.t0 || node.time > s.view.t1) {
      const span = s.view.t1 - s.view.t0;
      get().setView({ t0: node.time - span / 2, t1: node.time + span / 2 });
    }
    if (node.loc) void get().openSource(node.loc.file, node.loc.line);
  },

  /** Run a query typed into the query bar verbatim. */
  runQueryText: async (text) => {
    const s = get();
    if (!s.session) return;
    set({ causalBusy: true, causalError: null, activeTab: 2 });
    try {
      const res = await runQuery(s.session, text);
      set({ causal: res, causalBusy: false, activeNode: nodeId(res.root), cursor: res.time });
      if (res.root.loc) void get().openSource(res.root.loc.file, res.root.loc.line);
    } catch (e) {
      set({
        causal: null,
        causalBusy: false,
        causalError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  clearCausal: () => set({ causal: null, causalError: null, activeNode: null }),

  // --- TAB 6, Checks (§11.4) ------------------------------------------

  loadTransactions: async () => {
    const s = get();
    if (!s.session || s.txnBusy) return;
    set({ txnBusy: true, txnError: null });
    try {
      const report = await fetchTransactions(s.session);
      set({ txn: report, txnBusy: false });
      const first = report.interfaces[0]?.interface.name ?? null;
      if (first && !get().txnIface) await get().selectIface(first);
    } catch (e) {
      set({ txnBusy: false, txnError: e instanceof Error ? e.message : String(e) });
    }
  },

  selectIface: async (name) => {
    set({ txnIface: name, txnSelected: null });
    await get().runTxn(`txn(${name})`);
  },

  runTxn: async (vtq) => {
    const s = get();
    if (!s.session) return;
    set({ txnBusy: true, txnError: null });
    try {
      const r = await runTxnQuery(s.session, vtq);
      set({ txnRows: r.transactions, txnBusy: false });
    } catch (e) {
      set({ txnBusy: false, txnError: e instanceof Error ? e.message : String(e) });
    }
  },

  /**
   * §11.4b: clicking a transaction filters Wave to the interface's signals and
   * jumps to its window. The two views share one time axis, which is the whole
   * point of the tab — a band on the Gantt and the handshake underneath it have
   * to line up.
   */
  openTransaction: (t) => {
    const s = get();
    const iface = s.txn?.interfaces.find((i) => i.interface.name === t.iface);
    set({ txnSelected: t.ref });
    if (iface) {
      const paths = new Set(Object.values(iface.interface.signals));
      const rows: Row[] = s.signals
        .filter((sig) => paths.has(sig.path))
        .map((sig) => ({ kind: "signal", handle: sig.handle, path: sig.path }) as Row);
      get().focusRows(rows, t.ref);
    }
    const end = t.end_time ?? s.bounds.t1;
    // A little air either side, so the transaction is not flush with the edge.
    const pad = Math.max(1, Math.round((end - t.start_time) * 0.25));
    set({ view: clampView({ t0: t.start_time - pad, t1: end + pad }, s.bounds) });
    set({ cursor: t.start_time });
    schedulePersist(get);
  },

  // --- TAB 9, Performance (§8.17-8.18, §11.4b) ------------------------

  loadPerformance: async () => {
    const s = get();
    if (!s.session || s.perfBusy) return;
    set({ perfBusy: true, perfError: null });
    try {
      const report = await fetchPerformance(s.session);
      set({
        perf: report,
        perfBusy: false,
        perfIface: get().perfIface ?? report.interfaces[0]?.iface ?? null,
      });
    } catch (e) {
      set({ perfBusy: false, perfError: e instanceof Error ? e.message : String(e) });
    }
  },

  selectPerfIface: (name) => set({ perfIface: name }),

  /**
   * Open a transaction named only by reference — §8.17's clickable latency
   * outliers, which know `m0.WRITE[7]` and nothing else.
   *
   * Deliberately not `runQueryText`: that is the `why(...)` path and lands on
   * Causal. An outlier is a request to *look at the transaction*, so it goes to
   * TAB 8 and selects it there, which is what §8.17 means by "jump straight to
   * it".
   */
  openTransactionRef: async (ref) => {
    const iface = ref.slice(0, ref.lastIndexOf("."));
    set({ activeTab: 8 });
    await get().selectIface(iface);
    const t = get().txnRows.find((x) => x.ref === ref);
    if (t) get().openTransaction(t);
    set({ activeTab: 8 });
  },

  /**
   * §11.4b: a window selected in one chart restricts the others and sends Wave
   * to it. Clearing it (selecting nothing) restores the full run rather than
   * leaving the tab silently filtered.
   */
  setPerfWindow: (w) => {
    const s = get();
    set({ perfWindow: w });
    if (w) {
      set({ view: clampView({ t0: w.t0, t1: w.t1 }, s.bounds), cursor: w.t0 });
      schedulePersist(get);
    }
  },

  // --- TAB 10, Memory (§8.20, §11.4b) ---------------------------------

  loadMemory: async () => {
    const s = get();
    if (!s.session || s.memoryBusy) return;
    set({ memoryBusy: true, memoryError: null });
    try {
      const report = await fetchMemory(s.session);
      set({ memory: report, memoryBusy: false });
      const first = report.interfaces[0]?.iface ?? null;
      if (first && !get().memIface) await get().selectMemIface(first);
    } catch (e) {
      set({ memoryBusy: false, memoryError: e instanceof Error ? e.message : String(e) });
    }
  },

  selectMemIface: async (name) => {
    const s = get();
    set({ memIface: name, memCommands: [] });
    if (!s.session) return;
    try {
      const got = await fetchCommands(s.session, name);
      set({ memCommands: got.commands });
    } catch (e) {
      set({ memoryError: e instanceof Error ? e.message : String(e) });
    }
  },

  setMemFilter: (q) => set({ memFilter: q }),

  focusRows: (rows, label) => {
    const s = get();
    // Nothing to show, or already showing it: leave the list and the stash
    // alone rather than offering to undo a move that never happened.
    if (!rows.length) return;
    const same =
      rows.length === s.rows.length &&
      rows.every((r, i) => JSON.stringify(r) === JSON.stringify(s.rows[i]));
    if (same) return;
    set({
      rows,
      stashedRows: s.rows,
      stashedLabel: s.focusLabel || "your signal list",
      focusLabel: label,
    });
  },

  swapRows: () => {
    const s = get();
    if (!s.stashedRows) return;
    set({
      rows: s.stashedRows,
      stashedRows: s.rows,
      stashedLabel: s.focusLabel,
      focusLabel: s.stashedLabel,
    });
    schedulePersist(get);
  },

  // --- TAB 7, Coverage (§8.21, §8.12) ---------------------------------

  loadCoverage: async () => {
    const s = get();
    if (!s.session || s.coverageBusy) return;
    set({ coverageBusy: true, coverageError: null });
    try {
      const report = await fetchCoverage(s.session);
      set({
        coverage: report,
        coverageBusy: false,
        covIface: get().covIface ?? report.functional[0]?.iface ?? null,
      });
    } catch (e) {
      set({ coverageBusy: false, coverageError: e instanceof Error ? e.message : String(e) });
    }
  },

  selectCovIface: (name) => set({ covIface: name }),

  selectHole: (key) => set({ covHole: key }),

  /**
   * Centre Wave on an instant, optionally loading the signals that explain it.
   *
   * §8.20's two click-throughs both land here — a timing violation and a bank
   * segment — because both mean the same thing to the rest of the app: *put
   * the cursor there and show me these wires*.
   */
  jumpTo: (t, signals, label) => {
    const s = get();
    if (signals && signals.length) {
      const wanted = new Set(signals);
      const rows: Row[] = s.signals
        .filter((sig) => wanted.has(sig.path))
        .map((sig) => ({ kind: "signal", handle: sig.handle, path: sig.path }) as Row);
      get().focusRows(rows, label || "this moment");
    }
    const span = Math.max(1, Math.round((s.bounds.t1 - s.bounds.t0) / 40));
    set({
      view: clampView({ t0: t - span, t1: t + span }, s.bounds),
      cursor: t,
      activeTab: 1,
    });
    schedulePersist(get);
  },

  loadChecks: async () => {
    const s = get();
    if (!s.session) return;
    set({ checksBusy: true });
    try {
      set({ checks: await fetchChecks(s.session), checksBusy: false });
    } catch (e) {
      console.warn("checks failed", e);
      set({ checksBusy: false });
    }
  },

  suppress: async (id, reason) => {
    const s = get();
    if (!s.session) return;
    await suppressFinding(s.session, id, reason);
    await get().loadChecks();
  },

  unsuppress: async (id) => {
    const s = get();
    if (!s.session) return;
    await unsuppressFinding(s.session, id);
    await get().loadChecks();
  },

  /**
   * §11.4: `[why]` on a finding runs its query and lands in Causal, with the
   * source already open on the line the finding points at. The query travels
   * with the finding, so a check decides what question it is an answer to.
   */
  openFinding: (f) => {
    if (f.loc) void get().openSource(f.loc.file, f.loc.line);
    if (f.why) {
      void get().runQueryText(f.why);
    } else if (f.loc) {
      // Nothing to explain causally — a parameter has no trace value — so show
      // where it is rather than running a query that would only error (P7).
      set({ activeTab: 3 });
    }
  },
}));

/** Stable identity for a causal node: one signal at one time. */
export function nodeId(n: CausalNode): string {
  return `${n.signal}@${n.time}`;
}

// Exposed for the end-to-end tests and for poking at state from the console.
// Read-only in spirit: the app never reads it back.
if (typeof window !== "undefined") {
  (window as unknown as { __vtStore?: typeof useWave }).__vtStore = useWave;
}

/** Visible rows, with the members of collapsed groups removed. */
export function visibleRows(rows: Row[]): Row[] {
  const out: Row[] = [];
  let hidden = false;
  for (const r of rows) {
    if (r.kind === "group") {
      hidden = r.collapsed;
      out.push(r);
    } else if (!hidden) {
      out.push(r);
    }
  }
  return out;
}
