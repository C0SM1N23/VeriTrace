/** Top bar, tab strip, status bar and the shortcut overlay (§11.3, §11.7). */

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchCorrelation } from "../api/client";
import { formatTime } from "../lib/time";
import type { CorrelationReport, PluginTable } from "../lib/types";
import { useWave } from "../state/store";

/**
 * One empty array, shared.
 *
 * `useWave((s) => s.checks?.plugin_tables ?? [])` looks harmless and is not: the
 * `??` builds a *new* array on every render, so the store's snapshot never
 * compares equal, React re-renders, and the component loops until it throws
 * "Maximum update depth exceeded" — which takes the whole application down, not
 * just the tab strip.
 */
const NO_TABLES: PluginTable[] = [];

/** The tab strip of §11.4. Tabs that do not exist yet are visible but inert. */
const TABS = [
  "Wave", "Causal", "Source", "FSM", "Checks", "Diff", "Coverage",
  "Transactions", "Performance", "Memory",
];
/**
 * 1-based indices of the tabs that are built.
 *
 * FSM keeps its place in the strip but is not one: §11.4 consolidated it into
 * the Source tab — *"FSM e o vedere asupra structurii codului, nu un domeniu
 * separat... 10 tab-uri era prea mult"* — so the slot says where it went rather
 * than pretending the feature is missing.
 */
const BUILT = new Set([1, 2, 3, 5, 6, 7, 8, 9, 10]);
const MOVED: Record<number, string> = { 4: "FSM — a mode of the Source tab (⌘M)" };
/** Checks is tab 5; its badge shows how much it already knows (§13.4). */
export const CHECKS_TAB = 5;

export function TopBar() {
  const status = useWave((s) => s.status);
  const rows = useWave((s) => s.rows);
  if (!status) return <div className="top-bar" />;
  return (
    <div className="top-bar">
      <span className="crumb strong">dump.vtx</span>
      <Dot />
      <span className="crumb">{status.n_signals} sig</span>
      <Dot />
      <span className="crumb">{rows.length} shown</span>
      <Dot />
      <Correlation rate={status.correlation_rate} />
      <Dot />
      <span className="crumb">
        t: {formatTime(status.t0, status.timescale)}–{formatTime(status.t1, status.timescale)}
      </span>
    </div>
  );
}

/**
 * §7.2's rate, and the names behind it.
 *
 * The percentage has always been here; what it could not do was answer the
 * question it provokes. §7.2 makes the rate a first-class metric and prescribes
 * a remedy — the simulator dump flags of §4.0 — that is chosen by looking at
 * *which* signals are missing, so the list is one click from the number rather
 * than only in `veritrace correlate`.
 */
function Correlation({ rate }: { rate: number | null }) {
  const session = useWave((s) => s.session);
  const [open, setOpen] = useState<CorrelationReport | null>(null);

  if (rate === null) return <span className="crumb">no RTL</span>;
  return (
    <>
      <button
        className="crumb crumb-button"
        title="Which RTL signals are not in this dump (§7.2)"
        onClick={() => {
          if (open) return setOpen(null);
          if (session) void fetchCorrelation(session).then(setOpen).catch(() => setOpen(null));
        }}
        data-testid="correlation-crumb"
      >
        {rate}% correlated
      </button>
      {open && (
        <div className="palette-backdrop" onClick={() => setOpen(null)}>
          <div
            className="correlation"
            onClick={(e) => e.stopPropagation()}
            data-testid="correlation-panel"
          >
            <div className="strong">{open.summary}</div>
            <div className="dim">
              by method:{" "}
              {Object.entries(open.by_method)
                .map(([k, v]) => `${k}=${v}`)
                .join(", ")}
            </div>
            {open.percent < 90 && (
              <div className="pane-hint" data-testid="correlation-warning">
                Below §7.2's 90%. This is nearly always a missing simulator dump
                flag — Verilator needs --no-inline and --trace-structs (§4.0).
              </div>
            )}
            {open.n_reconstructible > 0 && (
              <>
                {/* §7.3: absent but derivable is not a correlation failure, and
                    showing it in the same list as the losses would read as one. */}
                <h3>
                  NOT DUMPED, RECONSTRUCTIBLE FROM RTL ({open.n_reconstructible})
                </h3>
                <ul data-testid="correlation-reconstructible">
                  {open.reconstructible.map((p) => (
                    <li key={p}>~ {p}</li>
                  ))}
                </ul>
              </>
            )}
            {open.n_unmatched > 0 ? (
              <>
                <h3>NOT CORRELATED ({open.n_unmatched})</h3>
                <ul data-testid="correlation-unmatched">
                  {open.unmatched.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </>
            ) : (
              <h3 data-testid="correlation-complete">EVERY RTL SIGNAL WAS FOUND</h3>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function Dot() {
  return <span className="dot">·</span>;
}

export function TabStrip() {
  const active = useWave((s) => s.activeTab);
  const setTab = useWave((s) => s.setTab);
  const nFindings = useWave((s) => s.status?.n_findings ?? 0);
  // §13.7: a plugin's table becomes a tab, and nobody edits this file to add
  // one. The strip grows past the ten §11.4 names by however many came back.
  const tables = useWave((s) => s.checks?.plugin_tables ?? NO_TABLES);
  return (
    <div className="tab-strip" role="tablist">
      {TABS.map((t, i) => {
        const n = i + 1;
        const enabled = BUILT.has(n);
        return (
          <button
            key={t}
            role="tab"
            aria-selected={active === n}
            className={`tab${active === n ? " on" : ""}${enabled ? "" : " disabled"}`}
            onClick={() => enabled && setTab(n)}
            title={enabled ? t : (MOVED[n] ?? `${t} — not built yet`)}
            data-testid={`tab-${n}`}
          >
            <span className="tab-n">{n}</span>
            {t}
            {n === CHECKS_TAB && nFindings > 0 && (
              <span className="tab-badge" data-testid="checks-badge">
                {nFindings}
              </span>
            )}
          </button>
        );
      })}
      {tables.map((table, i) => {
        const n = TABS.length + i + 1;
        return (
          <button
            key={table.title}
            role="tab"
            aria-selected={active === n}
            className={`tab${active === n ? " on" : ""}`}
            onClick={() => setTab(n)}
            title={`${table.title} — from a plugin (§13.7)`}
            data-testid={`tab-${n}`}
          >
            <span className="tab-n">+</span>
            {table.title}
          </button>
        );
      })}
    </div>
  );
}

/** How many fixed tabs there are, so plugin tabs can be numbered after them. */
export const FIXED_TABS = TABS.length;

/**
 * Status bar. Subscribes imperatively and writes through refs: selecting `view`
 * would re-render this on every frame of a pan.
 */
export function StatusBar() {
  const rangeRef = useRef<HTMLSpanElement>(null);
  const cursorRef = useRef<HTMLSpanElement>(null);
  const rulerMode = useWave((s) => s.rulerMode);
  const toggleRuler = useWave((s) => s.toggleRuler);
  const rowH = useWave((s) => s.rowH);
  const setRowH = useWave((s) => s.setRowH);
  const zoomAll = useWave((s) => s.zoomAll);
  const treeOpen = useWave((s) => s.treeOpen);
  const toggleTree = useWave((s) => s.toggleTree);
  const note = useWave((s) => s.note);
  const inspectorOpen = useWave((s) => s.inspectorOpen);
  const toggleInspector = useWave((s) => s.toggleInspector);
  const nBookmarks = useWave((s) => s.bookmarks.length);
  const [marksOpen, setMarksOpen] = useState(false);

  useEffect(() => {
    const write = () => {
      const s = useWave.getState();
      const ts = s.status?.timescale ?? "1ns";
      if (rangeRef.current) {
        rangeRef.current.textContent = `${formatTime(s.view.t0, ts)} – ${formatTime(s.view.t1, ts)}`;
      }
      if (cursorRef.current) {
        cursorRef.current.textContent =
          s.cursor === null ? "cursor: —" : `cursor: ${formatTime(s.cursor, ts)}`;
      }
    };
    write();
    return useWave.subscribe(write);
  }, []);

  return (
    <div className="status-bar">
      <span ref={rangeRef} className="mono" data-testid="status-range" />
      <Dot />
      <span ref={cursorRef} className="mono" data-testid="status-cursor" />
      {note && (
        <>
          <Dot />
          <span className="status-note" data-testid="status-note">
            {note}
          </span>
        </>
      )}
      <span className="spacer" />
      <button className="chip" onClick={zoomAll} title="Zoom to fit">
        fit
      </button>
      <button
        className="chip"
        onClick={toggleRuler}
        title="Toggle absolute time / clock cycles"
        data-testid="ruler-toggle"
      >
        {rulerMode === "time" ? "time" : "cycles"}
      </button>
      <button
        className="chip"
        onClick={toggleTree}
        title="Show or hide the design tree (⌘\)"
        data-testid="tree-toggle"
      >
        {treeOpen ? "tree" : "no tree"}
      </button>
      <button
        className="chip"
        onClick={toggleInspector}
        title="Show or hide the inspector (§11.3)"
        data-testid="inspector-toggle"
      >
        {inspectorOpen ? "inspector" : "no inspector"}
      </button>
      <button
        className="chip"
        onClick={() => setRowH(rowH === 20 ? 28 : 20)}
        title="Row density"
        data-testid="density-toggle"
      >
        {rowH === 20 ? "compact" : "tall"}
      </button>
      <button
        className="chip"
        onClick={() => setMarksOpen(true)}
        title="Bookmarks on this trace (⌘B to add)"
        data-testid="bookmarks-toggle"
      >
        {nBookmarks ? `${nBookmarks} mark${nBookmarks === 1 ? "" : "s"}` : "marks"}
      </button>
      <span className="hint">? for shortcuts</span>
      {marksOpen && <Bookmarks onClose={() => setMarksOpen(false)} />}
    </div>
  );
}

/**
 * §11.4's bookmarks: a note on a (signal, time), kept in the layout (P5).
 *
 * The shortcut was in the help overlay from the start with nothing behind it,
 * and the layout wrote an empty list over whatever was there. `⌘B` marks the
 * spot; the note is typed here, because a modal prompt in the middle of a
 * debugging session is the wrong shape for something you want to be cheap.
 */
function Bookmarks({ onClose }: { onClose: () => void }) {
  const marks = useWave((s) => s.bookmarks);
  const setLabel = useWave((s) => s.setBookmarkLabel);
  const remove = useWave((s) => s.removeBookmark);
  const jumpTo = useWave((s) => s.jumpTo);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");

  return (
    <div className="palette-backdrop" onClick={onClose}>
      <div className="marks" onClick={(e) => e.stopPropagation()} data-testid="bookmarks-panel">
        <div className="marks-head">
          <span className="strong">BOOKMARKS</span>
          <span className="spacer" />
          <span className="dim">⌘B marks the cursor</span>
        </div>
        {marks.length === 0 ? (
          <div className="pane-note">
            No bookmarks yet.
            <div className="pane-hint">
              Select a signal, put the cursor where it matters, and press <kbd>⌘B</kbd>.
            </div>
          </div>
        ) : (
          <ul className="marks-list">
            {marks.map((b, i) => (
              <li key={`${b.t}:${b.signal ?? ""}`}>
                <button
                  className="chip mono"
                  onClick={() => {
                    jumpTo(b.t, b.signal ? [b.signal] : undefined, "bookmark");
                    onClose();
                  }}
                  title="Go to this mark"
                >
                  {formatTime(b.t, timescale)}
                </button>
                <span className="mono dim marks-sig">{b.signal ?? "—"}</span>
                <input
                  value={b.label}
                  placeholder="what matters here?"
                  onChange={(e) => setLabel(i, e.target.value)}
                  data-testid="bookmark-label"
                />
                <button className="chip" onClick={() => remove(i)} title="Remove">
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

const SHORTCUTS: [string, string][] = [
  ["⌘K", "focus query bar"],
  ["⌘P", "find a signal"],
  ["w", "why on the selected signal"],
  ["s", "minimal subtrace of the current chain"],
  ["r", "replay the chain, step by step"],
  ["⌘M", "state machines, in the Source tab"],
  ["right-click", "why, on the signal under the pointer"],
  ["c", "fan-in cone of the selection (§8.6)"],
  ["f", "fan-out of the selection"],
  ["n / p", "next / previous finding, in Checks and Diff"],
  ["z", "zoom to fit"],
  ["1–9, 0", "switch tab"],
  ["← →", "previous/next transition"],
  ["⇧← ⇧→", "previous/next clock cycle"],
  ["⌘B", "bookmark with annotation"],
  ["⌘\\", "toggle side panels"],
  ["click", "move cursor"],
  ["⇧click", "drop a measurement marker"],
  ["scroll", "pan · ⌃scroll zoom · ⇧scroll vertical"],
  ["?", "this overlay"],
];

export function HelpOverlay() {
  const open = useWave((s) => s.helpOpen);
  const setHelp = useWave((s) => s.setHelp);
  if (!open) return null;
  return (
    <div className="palette-backdrop" onClick={() => setHelp(false)}>
      <div className="help" onClick={(e) => e.stopPropagation()} data-testid="help-overlay">
        <div className="help-title">Keyboard</div>
        {SHORTCUTS.map(([k, v]) => (
          <div className="help-row" key={k}>
            <kbd>{k}</kbd>
            <span>{v}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** The §10.1 verbs, for the completion list. `why` first: it is the main road. */
const VERBS = [
  "why", "cone", "fanout", "find", "stuck", "edges", "hold",
  "changed", "xtrace", "fsm", "lint", "handshake", "uncovered",
];

/**
 * Query bar — always visible, because it is the spine of the tool (§11.3).
 *
 * **It is a controlled input, and that is the feature.** §9.4: *"fiecare
 * actiune din UI scrie query-ul echivalent in bara… asa se invata limbajul
 * fara tutorial"*. Right-clicking a signal, pressing `c`, clicking `[why]` on
 * a finding — each writes what it is equivalent to here, so the language is
 * read off the tool instead of out of a manual. While this was uncontrolled it
 * showed nothing anybody had done.
 *
 * `↑`/`↓` walk the session's history, and typing offers the verbs and the
 * signal names that match — the other two halves of §9.4.
 */
export function QueryBar() {
  const ref = useRef<HTMLInputElement>(null);
  const hasRtl = useWave((s) => s.status?.has_rtl ?? false);
  const text = useWave((s) => s.queryText);
  const setQueryText = useWave((s) => s.setQueryText);
  const history = useWave((s) => s.queryHistory);
  const signals = useWave((s) => s.signals);
  // -1 means "not walking history"; 0 is the most recent query.
  const [histAt, setHistAt] = useState(-1);
  const [open, setOpen] = useState(false);
  // -1 means the list is only *offered*. Enter runs the query that was typed
  // unless the arrow keys moved into the list — completing a query somebody
  // has finished writing would make the bar fight whoever is using it.
  const [pick, setPick] = useState(-1);

  useEffect(() => {
    const onFocus = () => ref.current?.focus();
    window.addEventListener("vt:focus-query", onFocus);
    return () => window.removeEventListener("vt:focus-query", onFocus);
  }, []);

  // Complete the verb while it is being typed, and the signal name once inside
  // the parentheses — the two things that are long enough to be worth it.
  const suggestions = useMemo(() => {
    if (!open || !text.trim()) return [];
    const paren = text.indexOf("(");
    if (paren < 0) {
      const head = text.trim().toLowerCase();
      return VERBS.filter((v) => v.startsWith(head) && v !== head).map((v) => `${v}(`);
    }
    const inner = text.slice(paren + 1).replace(/[)\s].*$/, "");
    if (inner.length < 2) return [];
    const low = inner.toLowerCase();
    return signals
      .filter((s) => s.path.toLowerCase().includes(low))
      .slice(0, 8)
      .map((s) => `${text.slice(0, paren + 1)}${s.path}`);
  }, [open, text, signals]);

  const accept = (value: string) => {
    setQueryText(value);
    setOpen(false);
    ref.current?.focus();
  };

  const submit = (value: string) => {
    const q = value.trim();
    if (!q) return;
    setOpen(false);
    setHistAt(-1);
    // Hand the keyboard back to the application. The answer arrives with `s`
    // and `r` offered on it (§11.4b, §11.5), and with the caret still here
    // those go into the query instead of running. ⌘K comes back.
    ref.current?.blur();
    void useWave.getState().runQueryText(q);
  };

  return (
    <div className="query-bar">
      <span className="prompt">&gt;</span>
      <input
        ref={ref}
        value={text}
        placeholder={
          hasRtl
            ? "why(top.ctrl.ready == 0 @ c1247)"
            : "why() needs RTL — start the server with --rtl"
        }
        disabled={!hasRtl}
        onChange={(e) => {
          setQueryText(e.target.value);
          setHistAt(-1);
          setOpen(true);
          setPick(-1);
        }}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            if (open && pick >= 0 && suggestions[pick]) {
              e.preventDefault();
              accept(suggestions[pick]);
              return;
            }
            submit(text);
            return;
          }
          if (e.key === "Escape") {
            setOpen(false);
            return;
          }
          if (e.key === "Tab" && suggestions.length) {
            // Tab is the key that means "complete it", so it takes the first
            // suggestion when none has been picked.
            e.preventDefault();
            accept(suggestions[Math.max(pick, 0)]);
            return;
          }
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            const down = e.key === "ArrowDown";
            if (open && suggestions.length) {
              e.preventDefault();
              // Down from "nothing picked" enters the list at the top; up from
              // the top leaves it again, back to what was typed.
              const next = down ? pick + 1 : pick - 1;
              setPick(next >= suggestions.length ? -1 : next < -1 ? suggestions.length - 1 : next);
              return;
            }
            // §9.4's history. Up walks back; down walks forward and off the
            // end into an empty bar, which is where a new question is typed.
            if (!history.length) return;
            e.preventDefault();
            const next = Math.min(history.length - 1, Math.max(-1, histAt + (down ? -1 : 1)));
            setHistAt(next);
            setQueryText(next < 0 ? "" : history[next]);
            // Recalled text would otherwise open the completion list, and the
            // next arrow key would walk that instead of the history.
            setOpen(false);
            setPick(-1);
          }
        }}
        data-testid="query-bar"
      />
      {suggestions.length > 0 && (
        <ul className="query-suggest" data-testid="query-suggest">
          {suggestions.map((sug, i) => (
            <li key={sug}>
              <button
                className={i === pick ? "on" : ""}
                data-testid="query-suggestion"
                // `mousedown`, not `click`: blur fires first and would close
                // the list out from under the pointer.
                onMouseDown={(e) => {
                  e.preventDefault();
                  accept(sug);
                }}
              >
                {sug}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
