/** Top bar, tab strip, status bar and the shortcut overlay (§11.3, §11.7). */

import { useEffect, useRef } from "react";
import { formatTime } from "../lib/time";
import { useWave } from "../state/store";

/** The tab strip of §11.4. Tabs that do not exist yet are visible but inert. */
const TABS = [
  "Wave", "Causal", "Source", "FSM", "Checks", "Diff", "Coverage",
  "Transactions", "Performance", "Memory",
];
/** 1-based indices of the tabs that are built. FSM (4), Diff (6) and
 *  Coverage (7) are still design only. */
const BUILT = new Set([1, 2, 3, 5, 8, 9, 10]);
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
      <span className="crumb">
        {status.correlation_rate === null ? "no RTL" : `${status.correlation_rate}% correlated`}
      </span>
      <Dot />
      <span className="crumb">
        t: {formatTime(status.t0, status.timescale)}–{formatTime(status.t1, status.timescale)}
      </span>
    </div>
  );
}

function Dot() {
  return <span className="dot">·</span>;
}

export function TabStrip() {
  const active = useWave((s) => s.activeTab);
  const setTab = useWave((s) => s.setTab);
  const nFindings = useWave((s) => s.status?.n_findings ?? 0);
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
            title={enabled ? t : `${t} — not built yet`}
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
    </div>
  );
}

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
        <div className="help-note">FSM, Diff and Coverage are designed but not built yet.</div>
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
          if (text) void useWave.getState().runQueryText(text);
        }}
        data-testid="query-bar"
      />
    </div>
  );
}
