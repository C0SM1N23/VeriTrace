/** Top bar, tab strip, status bar and the shortcut overlay (§11.3, §11.7). */

import { useEffect, useRef, useState } from "react";
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
        onClick={() => setRowH(rowH === 20 ? 28 : 20)}
        title="Row density"
        data-testid="density-toggle"
      >
        {rowH === 20 ? "compact" : "tall"}
      </button>
      <span className="hint">? for shortcuts</span>
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

/**
 * Query bar — always visible, because it is the spine of the tool (§11.3).
 *
 * `why(...)` is the only form that exists so far; anything else is refused by
 * the server with a message saying so, rather than failing quietly.
 */
export function QueryBar() {
  const ref = useRef<HTMLInputElement>(null);
  const hasRtl = useWave((s) => s.status?.has_rtl ?? false);

  useEffect(() => {
    const onFocus = () => ref.current?.focus();
    window.addEventListener("vt:focus-query", onFocus);
    return () => window.removeEventListener("vt:focus-query", onFocus);
  }, []);

  return (
    <div className="query-bar">
      <span className="prompt">&gt;</span>
      <input
        ref={ref}
        placeholder={
          hasRtl
            ? "why(top.ctrl.ready == 0 @ c1247)"
            : "why() needs RTL — start the server with --rtl"
        }
        disabled={!hasRtl}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return;
          const text = (e.target as HTMLInputElement).value.trim();
          if (!text) return;
          // Hand the keyboard back to the application. The answer arrives with
          // `s` and `r` offered on it (§11.4b, §11.5), and with the caret still
          // here those go into the query instead of running — the shortcut is
          // printed next to the button that ignores it. ⌘K comes back.
          (e.target as HTMLInputElement).blur();
          void useWave.getState().runQueryText(text);
        }}
        data-testid="query-bar"
      />
    </div>
  );
}
