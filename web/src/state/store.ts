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
  // Aliased: the store exposes an action of the same name, and a function that
  // shadows its own caller is a five-minute puzzle for no reason.
  buildRepro as requestRepro,
  downloadReport,
  fetchChecks,
  fetchCommands,
  fetchCoverage,
  fetchLayout,
  fetchMachines,
  fetchMemory,
  fetchPerformance,
  fetchRoot,
  fetchSessions,
  fetchSignals,
  fetchSource,
  fetchStatus,
  fetchSubtrace,
  fetchTransactions,
  putLayout,
  runQuery,
  // Same reason as `requestRepro`: the store action owns the name.
  runDiff as runDiffRequest,
  runTxnQuery,
  suppressFinding,
  unsuppressFinding,
} from "../api/client";
import type {
  Bookmark,
  CausalNode,
  ChecksReport,
  CmdEvent,
  CoverageReport,
  DiffReport,
  Layout,
  Machine,
  MemoryReport,
  OpenSession,
  PerfReport,
  Radix,
  Repro,
  Row,
  SessionStatus,
  SignalMeta,
  SourceFile,
  SubtraceResult,
  Transaction,
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
  // §8.2, §8.3, §11.5 — the minimised chain, the testbench, and the replay.
  subtrace: SubtraceResult | null;
  subtraceBusy: boolean;
  subtraceError: string | null;
  /** §11.5 is a mode, not a tab: it takes the screen or it is not there. */
  replay: boolean;
  /** 0-based index into `subtrace.steps`. */
  replayStep: number;
  replayPlaying: boolean;
  repro: Repro | null;
  reproBusy: boolean;
  reproError: string | null;
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
  /**
   * What the query bar shows — §9.4.
   *
   * *"Fiecare actiune din UI scrie query-ul echivalent in bara. Asa se invata
   * limbajul fara tutorial."* Every route that runs a query sets this, so
   * right-clicking a signal spells out `why(...)` and pressing `c` spells out
   * `cone(...)`. The bar is the one place the language is visible, and it was
   * an uncontrolled input that never showed anything anyone had done.
   */
  queryText: string;
  /** Queries this session has run, most recent first — §9.4's history. */
  queryHistory: string[];
  /** §11.4's `⌘B` marks, persisted with the layout (P5). */
  bookmarks: Bookmark[];
  /**
   * One line of transient feedback, shown in the status bar.
   *
   * §11.8's rule applies: it says what happened and what to do next, never
   * "something went wrong". Cleared by the next action that succeeds.
   */
  note: string;
  // §8.8 — a mode of Source, not a tab.
  machines: Machine[];
  fsmBusy: boolean;
  fsmError: string | null;
  /** True while the diagram has the Source tab. */
  fsmOpen: boolean;
  /** Which machine is on screen. */
  fsmSignal: string | null;
  /** Cycle the playback scrubber sits on, or `null` to follow the main cursor. */
  fsmScrub: number | null;
  // §8.7, TAB 5.
  diff: DiffReport | null;
  diffBusy: boolean;
  diffError: string | null;
  /** Traces this server has open, for the second-trace selector. */
  diffSessions: OpenSession[];
  /** Path of the run being compared against. */
  diffOther: string;
  diffStrategy: string;
  /** §11.4: "ignore this signal" — globs excluded from the comparison. */
  diffIgnore: string[];
  /** Which divergence `n`/`p` are on. */
  diffIndex: number;
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
  /** §11.3's design tree column. ⌘\ collapses it. */
  treeOpen: boolean;
  /** §11.3's third column: details of whatever is selected. Collapsible too. */
  inspectorOpen: boolean;
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
  /** Add many signals at once, optionally under a group header (§11.3). */
  addSignals: (handles: number[], group?: string) => number;
  addGroup: (name: string) => void;
  toggleGroup: (id: string) => void;
  renameGroup: (id: string, name: string) => void;
  setRowH: (h: number) => void;
  toggleRuler: () => void;
  setSelected: (handle: number | null) => void;
  setFilter: (q: string) => void;
  setPalette: (open: boolean) => void;
  setHelp: (open: boolean) => void;
  toggleTree: () => void;
  toggleInspector: () => void;
  setTab: (n: number) => void;
  stepEdge: (dir: 1 | -1) => Promise<void>;
  runWhy: (signalPath: string, t: number) => Promise<void>;
  runQueryText: (vtq: string) => Promise<void>;
  /** Re-ask the question stored in the layout, without moving the user. */
  restoreQuery: (vtq: string) => Promise<void>;
  openSource: (file: string, line?: number) => Promise<void>;
  selectCausal: (node: CausalNode) => void;
  clearCausal: () => void;
  loadSubtrace: () => Promise<void>;
  setReplay: (on: boolean) => void;
  stepReplay: (delta: number) => void;
  gotoReplay: (index: number) => void;
  toggleReplayPlay: () => void;
  buildRepro: (opts?: { mode?: string; validate?: boolean }) => Promise<void>;
  exportReport: () => Promise<void>;
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
  /** What the query bar shows, without running it (§9.4). */
  setQueryText: (text: string) => void;
  /** Put a query at the head of the session's history, de-duplicated (§9.4). */
  rememberQuery: (text: string) => void;
  /** §11.4: mark the current (signal, time). The note is typed afterwards. */
  addBookmark: () => void;
  setBookmarkLabel: (index: number, label: string) => void;
  removeBookmark: (index: number) => void;
  /**
   * §8.6 — the fan-in or fan-out of the current selection, filtered into Wave.
   *
   * *"Din 4000 de semnale ramai cu 8."* The analysis and its endpoint existed
   * from the start; nothing in the UI called them, so the feature §8.6 calls
   * the best value for effort in the project could only be reached by typing
   * the query by hand.
   */
  runCone: (direction: "fanin" | "fanout") => Promise<void>;
  /** Swap the current list with the one a click-through replaced. */
  swapRows: () => void;
  loadMachines: () => Promise<void>;
  setFsmOpen: (open: boolean) => void;
  selectMachine: (signal: string) => void;
  setFsmScrub: (cycle: number | null) => void;
  loadDiffSessions: () => Promise<void>;
  setDiffOther: (trace: string) => void;
  setDiffStrategy: (s: string) => void;
  runDiff: () => Promise<void>;
  ignoreSignal: (path: string) => Promise<void>;
  stepDivergence: (delta: number) => void;
  gotoDivergence: (index: number) => void;
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
    bookmarks: s.bookmarks,
    cursors: s.cursor === null ? s.markers : [s.cursor, ...s.markers],
    zoom: { t0: Math.round(s.view.t0), t1: Math.round(s.view.t1) },
    // Extra keys: the server stores the layout as an open document, so view
    // preferences ride along with it. "Exactly as you left it" includes the
    // ruler mode and row density, not just which signals were on screen.
    rulerMode: s.rulerMode,
    rowH: s.rowH,
    // The question, not the answer: the tree is derivable from it, and this is
    // the one field §13.8's `.vtsession` is really about.
    query: s.causal?.query ?? "",
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

/**
 * State to clear when a new question is asked.
 *
 * The subtrace, the testbench and the replay position are all *answers to the
 * previous question*. Leaving them on screen while a new chain loads is how a
 * user ends up reading a repro for a bug they stopped looking at.
 */
function askingAgain(): Partial<WaveState> {
  return {
    causalError: null,
    subtrace: null,
    subtraceError: null,
    repro: null,
    reproError: null,
    replay: false,
    replayStep: 0,
    replayPlaying: false,
  };
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
  subtrace: null,
  subtraceBusy: false,
  subtraceError: null,
  replay: false,
  replayStep: 0,
  replayPlaying: false,
  repro: null,
  reproBusy: false,
  reproError: null,
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
  queryText: "",
  queryHistory: [],
  bookmarks: [],
  note: "",
  machines: [],
  fsmBusy: false,
  fsmError: null,
  fsmOpen: false,
  fsmSignal: null,
  fsmScrub: null,
  diff: null,
  diffBusy: false,
  diffError: null,
  diffSessions: [],
  diffOther: "",
  diffStrategy: "cycle",
  diffIgnore: [],
  diffIndex: 0,
  coverage: null,
  coverageBusy: false,
  coverageError: null,
  covIface: null,
  covHole: null,
  filter: "",
  paletteOpen: false,
  helpOpen: false,
  treeOpen: true,
  inspectorOpen: true,
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
        // P5: bookmarks come back with everything else. Filtered to the marks
        // this trace can still place — a shared session (§13.8) may carry marks
        // for signals that are not in your dump, and a mark on nothing is worse
        // than no mark.
        bookmarks: (Array.isArray(layout.bookmarks) ? layout.bookmarks : []).filter(
          (b): b is Bookmark =>
            !!b &&
            typeof b.t === "number" &&
            (b.signal === null || signals.some((s) => s.path === b.signal)),
        ),
        clockPeriod,
        clockOrigin,
        // §11.4b: the server decides which tab suits this design. §13.4 wants
        // the first impression to be "the tool already knows something", so a
        // session with findings opens on Checks rather than an empty canvas.
        activeTab: TAB_BY_NAME[status.default_tab] ?? 1,
        ready: true,
        error: null,
      });
      // Always, not only on the Checks tab: §13.7's plugin tables arrive with
      // the checks, and the tab strip cannot show them before they are here.
      // The report is computed when the session opens, so this is one read.
      void get().loadChecks();
      // P5 and §13.8: the question is part of the screen, so it is asked again.
      const restored = typeof layout.query === "string" ? layout.query.trim() : "";
      if (restored && status.has_rtl) void get().restoreQuery(restored);
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
  /**
   * The bulk half of §11.3's tree, and the reason it exists.
   *
   * A viewer where a design is assembled one signal at a time is not one
   * anybody uses next to `add wave -r /*`. Already-shown signals are skipped
   * rather than duplicated — clicking a scope twice should be idempotent, not
   * a way to get two of everything — and the count of what was actually added
   * is returned so the caller can say so.
   */
  addSignals: (handles, group) => {
    const state = get();
    const shown = new Set(
      state.rows.flatMap((r) => (r.kind === "signal" ? [r.handle] : [])),
    );
    const fresh: Row[] = [];
    for (const handle of handles) {
      if (shown.has(handle)) continue;
      const sig = state.signalsByHandle.get(handle);
      if (!sig) continue;
      shown.add(handle);
      fresh.push({ kind: "signal", handle, path: sig.path });
    }
    if (fresh.length === 0) return 0;
    const header: Row[] = group
      ? [{ kind: "group", id: `g${Date.now().toString(36)}`, name: group, collapsed: false }]
      : [];
    set({ rows: [...state.rows, ...header, ...fresh] });
    schedulePersist(get);
    return fresh.length;
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
  toggleTree: () => set({ treeOpen: !get().treeOpen }),
  toggleInspector: () => set({ inspectorOpen: !get().inspectorOpen }),

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
    // §9.4: the bar shows the query this action is equivalent to, which is how
    // the language is learned without a tutorial.
    const text = `why(${signalPath} @ ${Math.round(t)})`;
    set({
      ...askingAgain(),
      causalBusy: true,
      activeTab: 2,
      cursor: Math.round(t),
      queryText: text,
      note: "",
    });
    try {
      const res = await runQuery(s.session, text);
      set({ causal: res, causalBusy: false, activeNode: nodeId(res.root) });
      get().rememberQuery(text);
      schedulePersist(get);
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

    // §9.2 lists `subtrace(...)` and `repro(...)` beside `why(...)`, and they
    // are the same question asked one step further: minimise the chain, or
    // build the testbench from it. Both already have a button and an endpoint,
    // so the verbs route to them rather than to a second implementation.
    const step = /^\s*(subtrace|repro)\s*\((.*)\)\s*$/i.exec(text);
    if (step) {
      set({ queryText: text, note: "" });
      await get().runQueryText(`why(${step[2]})`);
      if (!get().causal) return; // the why failed and already said why
      get().rememberQuery(text);
      if (step[1].toLowerCase() === "subtrace") await get().loadSubtrace();
      else await get().buildRepro({ validate: false });
      return;
    }

    set({ ...askingAgain(), causalBusy: true, activeTab: 2, queryText: text, note: "" });
    try {
      const res = await runQuery(s.session, text);
      set({ causal: res, causalBusy: false, activeNode: nodeId(res.root), cursor: res.time });
      get().rememberQuery(text);
      schedulePersist(get);
      if (res.root.loc) void get().openSource(res.root.loc.file, res.root.loc.line);
    } catch (e) {
      set({
        causal: null,
        causalBusy: false,
        causalError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  /**
   * Re-ask the saved question when a session opens (P5, §13.8).
   *
   * Re-running beats storing the tree: the answer has to come from *this* dump,
   * and a restored session that painted a colleague's tree over your data would
   * be the one lie this tool cannot afford.
   *
   * It deliberately does not move the user. §11.4b gives the server the last
   * word on which tab a session opens on, so a restored session is a question
   * with its answer already waiting, not a hijacked landing page — and a query
   * that no longer resolves (the dump moved on) stays quiet, because nobody in
   * this session asked it.
   */
  restoreQuery: async (text) => {
    const s = get();
    if (!s.session) return;
    try {
      const res = await runQuery(s.session, text);
      set({ causal: res, activeNode: nodeId(res.root) });
    } catch {
      /* silent by design — see above */
    }
  },

  clearCausal: () => {
    set({
      causal: null,
      causalError: null,
      activeNode: null,
      subtrace: null,
      repro: null,
      replay: false,
    });
    // Clearing has to be persisted too, or the question comes back on reload.
    schedulePersist(get);
  },

  // --- §8.2, §8.3, §11.5 — subtrace, repro, replay ---------------------

  loadSubtrace: async () => {
    const s = get();
    if (!s.session || !s.causal || s.subtraceBusy) return;
    set({ subtraceBusy: true, subtraceError: null });
    try {
      const got = await fetchSubtrace(s.session, s.causal.query);
      set({ subtrace: got, subtraceBusy: false, replayStep: 0 });
    } catch (e) {
      set({ subtraceBusy: false, subtraceError: e instanceof Error ? e.message : String(e) });
    }
  },

  /**
   * §11.5: entering replay parks the cursor on the first step, which is the
   * root cause — the story runs forwards even though the chain was built
   * backwards.
   */
  setReplay: (on) => {
    set({ replay: on, replayPlaying: false });
    if (on) {
      if (!get().subtrace) void get().loadSubtrace();
      else get().gotoReplay(0);
    }
  },

  gotoReplay: (index) => {
    const s = get();
    const steps = s.subtrace?.steps ?? [];
    if (!steps.length) return;
    const i = Math.max(0, Math.min(index, steps.length - 1));
    const step = steps[i];
    set({ replayStep: i });
    // §11.5: the waveform auto-windows around the event, ±8 cycles.
    const span = (s.clockPeriod ?? Math.max(1, (s.bounds.t1 - s.bounds.t0) / 200)) * 8;
    set({
      cursor: step.time,
      view: clampView({ t0: step.time - span, t1: step.time + span }, s.bounds),
      sourceLoc: step.loc ? { file: step.loc.file, line: step.loc.line } : s.sourceLoc,
      selected: s.signals.find((x) => x.path === step.signal)?.handle ?? s.selected,
    });
    if (step.loc) void get().openSource(step.loc.file, step.loc.line);
  },

  stepReplay: (delta) => get().gotoReplay(get().replayStep + delta),

  toggleReplayPlay: () => set({ replayPlaying: !get().replayPlaying }),

  buildRepro: async (opts = {}) => {
    const s = get();
    if (!s.session || !s.causal || s.reproBusy) return;
    set({ reproBusy: true, reproError: null });
    try {
      const got = await requestRepro(s.session, s.causal.query, opts);
      set({ repro: got, reproBusy: false });
    } catch (e) {
      set({ reproBusy: false, reproError: e instanceof Error ? e.message : String(e) });
    }
  },

  exportReport: async () => {
    const s = get();
    if (!s.session || !s.causal) return;
    try {
      await downloadReport(s.session, s.causal.query);
    } catch (e) {
      set({ reproError: e instanceof Error ? e.message : String(e) });
    }
  },

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

  setQueryText: (text) => set({ queryText: text }),

  rememberQuery: (text) => {
    const q = text.trim();
    if (!q) return;
    const s = get();
    // Most recent first, and asking the same thing twice does not fill the
    // history with one question. Capped: this is a session's recall, not a log.
    set({ queryHistory: [q, ...s.queryHistory.filter((x) => x !== q)].slice(0, 50) });
  },

  addBookmark: () => {
    const s = get();
    const t = s.cursor ?? Math.round((s.view.t0 + s.view.t1) / 2);
    const sig = s.selected !== null ? (s.signalsByHandle.get(s.selected)?.path ?? null) : null;
    // Marking the same instant twice is a slip, not a second mark.
    if (s.bookmarks.some((b) => b.t === t && b.signal === sig)) {
      set({ note: "Already bookmarked here." });
      return;
    }
    const mark: Bookmark = { t, signal: sig, label: "" };
    set({
      bookmarks: [...s.bookmarks, mark].sort((a, b) => a.t - b.t),
      note: `Bookmarked ${sig ?? "this instant"} — add a note in the list.`,
    });
    schedulePersist(get);
  },

  setBookmarkLabel: (index, label) => {
    const s = get();
    if (!s.bookmarks[index]) return;
    set({ bookmarks: s.bookmarks.map((b, i) => (i === index ? { ...b, label } : b)) });
    schedulePersist(get);
  },

  removeBookmark: (index) => {
    const s = get();
    set({ bookmarks: s.bookmarks.filter((_, i) => i !== index) });
    schedulePersist(get);
  },

  runCone: async (direction) => {
    const s = get();
    if (!s.session) return;
    const sig = s.selected !== null ? s.signalsByHandle.get(s.selected) : undefined;
    if (!sig) {
      set({ note: "Select a signal first — a cone starts somewhere." });
      return;
    }
    const verb = direction === "fanout" ? "fanout" : "cone";
    const text = `${verb}(${sig.path}, depth=4)`;
    set({ queryText: text });
    try {
      const res = (await runQuery(s.session, text)) as unknown as {
        nodes?: { path: string }[];
      };
      const paths = new Set((res.nodes ?? []).map((n) => n.path));
      const rows: Row[] = s.signals
        .filter((x) => paths.has(x.path))
        .map((x) => ({ kind: "signal", handle: x.handle, path: x.path }) as Row);
      if (!rows.length) {
        set({ note: `${verb}(${sig.name}) reached nothing in this trace` });
        return;
      }
      get().focusRows(rows, `${verb} of ${sig.name}`);
      get().rememberQuery(text);
      set({ activeTab: 1, note: "" });
    } catch (e) {
      set({ note: e instanceof Error ? e.message : String(e) });
    }
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

  // --- FSM mode (§8.8) — a mode of Source, not a tab -------------------

  loadMachines: async () => {
    const s = get();
    if (!s.session || s.fsmBusy || s.machines.length) return;
    set({ fsmBusy: true, fsmError: null });
    try {
      const machines = await fetchMachines(s.session);
      set({
        machines,
        fsmBusy: false,
        // Prefer the machine whose file is already open: the mode is entered
        // from the source, so it should open on what is being read.
        fsmSignal:
          get().fsmSignal ??
          machines.find((m) => m.loc?.file === get().source?.file)?.signal ??
          machines[0]?.signal ??
          null,
      });
    } catch (e) {
      set({ fsmBusy: false, fsmError: e instanceof Error ? e.message : String(e) });
    }
  },

  setFsmOpen: (open) => {
    set({ fsmOpen: open, activeTab: open ? 3 : get().activeTab });
    if (open) void get().loadMachines();
  },

  selectMachine: (signal) => {
    set({ fsmSignal: signal });
    // §11.6: choosing a machine opens its declaration, like any other selection.
    const m = get().machines.find((x) => x.signal === signal);
    if (m?.loc) void get().openSource(m.loc.file, m.loc.line);
  },

  setFsmScrub: (cycle) => {
    const s = get();
    set({ fsmScrub: cycle });
    if (cycle !== null && s.clockPeriod) {
      // §8.8 step 6: scrubbing moves the shared cursor, so Wave and Source
      // follow the state the diagram is lighting up.
      set({ cursor: Math.round(s.clockOrigin + cycle * s.clockPeriod) });
    }
  },

  // --- TAB 5, Diff (§8.7) ---------------------------------------------

  loadDiffSessions: async () => {
    try {
      const open = await fetchSessions();
      const s = get();
      set({
        diffSessions: open,
        // Preselect the other trace the server already has open, when there is
        // exactly one — the common case is `serve a.vtx` plus one more.
        diffOther:
          s.diffOther || open.find((x) => x.session_id !== s.session)?.trace || "",
      });
    } catch (e) {
      set({ diffError: e instanceof Error ? e.message : String(e) });
    }
  },

  setDiffOther: (trace) => set({ diffOther: trace, diff: null, diffIndex: 0 }),
  setDiffStrategy: (strategy) => set({ diffStrategy: strategy, diff: null, diffIndex: 0 }),

  runDiff: async () => {
    const s = get();
    if (!s.session || !s.diffOther || s.diffBusy) return;
    set({ diffBusy: true, diffError: null });
    try {
      const got = await runDiffRequest(s.session, s.diffOther, {
        strategy: s.diffStrategy,
        ignore: s.diffIgnore,
      });
      set({ diff: got, diffBusy: false, diffIndex: 0 });
      if (got.divergences.length) get().gotoDivergence(0);
    } catch (e) {
      set({ diffBusy: false, diffError: e instanceof Error ? e.message : String(e) });
    }
  },

  /** §11.4: drop a signal that legitimately differs, and compare again. */
  ignoreSignal: async (path) => {
    if (get().diffIgnore.includes(path)) return;
    set({ diffIgnore: [...get().diffIgnore, path] });
    await get().runDiff();
  },

  gotoDivergence: (index) => {
    const s = get();
    const list = s.diff?.divergences ?? [];
    if (!list.length) return;
    const i = Math.max(0, Math.min(index, list.length - 1));
    const d = list[i];
    set({ diffIndex: i });
    // §11.6: choosing a divergence moves every other panel to it, in *this*
    // trace's own time — the shared axis is for comparing, not for navigating.
    const span = (s.clockPeriod ?? Math.max(1, (s.bounds.t1 - s.bounds.t0) / 200)) * 8;
    set({
      cursor: d.time_a,
      view: clampView({ t0: d.time_a - span, t1: d.time_a + span }, s.bounds),
    });
    const sig = s.signals.find((x) => x.path.endsWith(d.signal));
    if (sig) set({ selected: sig.handle });
  },

  stepDivergence: (delta) => get().gotoDivergence(get().diffIndex + delta),

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
