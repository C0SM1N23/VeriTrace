import { useEffect } from "react";
import { WaveCanvas } from "./canvas/WaveCanvas";
import { CausalTab } from "./panels/CausalTab";
import { ChecksTab } from "./panels/ChecksTab";
import { CommandPalette } from "./panels/CommandPalette";
import { CoverageTab } from "./panels/CoverageTab";
import { DiffTab } from "./panels/DiffTab";
import { FsmMode } from "./panels/FsmMode";
import { FIXED_TABS, HelpOverlay, QueryBar, StatusBar, TabStrip, TopBar } from "./panels/Chrome";
import { PluginTab } from "./panels/PluginTab";
import type { PluginTable } from "./lib/types";

/** Shared, so the selector below returns a stable reference — see Chrome.tsx. */
const NO_TABLES: PluginTable[] = [];
import { SignalPanel } from "./panels/SignalPanel";
import { SourceTab } from "./panels/SourceTab";
import { MemoryTab } from "./panels/MemoryTab";
import { PerformanceTab } from "./panels/PerformanceTab";
import { ReplayMode } from "./panels/ReplayMode";
import { TransactionsTab } from "./panels/TransactionsTab";
import { WaveMenu } from "./panels/WaveMenu";
import { flushPersist, useWave } from "./state/store";

export default function App() {
  const ready = useWave((s) => s.ready);
  const error = useWave((s) => s.error);
  const load = useWave((s) => s.load);
  const tab = useWave((s) => s.activeTab);
  const replay = useWave((s) => s.replay);
  const fsmOpen = useWave((s) => s.fsmOpen);
  const rtlChanged = useWave((s) => s.status?.rtl_changed ?? false);
  const pluginTables = useWave((s) => s.checks?.plugin_tables ?? NO_TABLES);

  useEffect(() => {
    void load();
  }, [load]);

  // A layout change must not be lost because the tab closed inside the save
  // debounce window (P5).
  useEffect(() => {
    const onHide = () => void flushPersist();
    window.addEventListener("pagehide", onHide);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") onHide();
    });
    return () => window.removeEventListener("pagehide", onHide);
  }, []);

  useKeyboard();

  if (error) {
    return (
      <div className="fatal">
        <div className="fatal-title">
          <span aria-hidden>✕</span> Cannot reach the trace.
        </div>
        <div className="fatal-body">{error}</div>
        <div className="fatal-next">
          Start the backend with <code>veritrace serve dump.vtx</code>, then reload.
        </div>
      </div>
    );
  }

  return (
    <div className={replay ? "app replaying" : "app"}>
      <TopBar />
      {rtlChanged && (
        // §5.7: the RTL moved on after the run. Warn, never block.
        <div className="banner" data-testid="rtl-banner">
          <span aria-hidden>⚠</span> RTL has changed since this trace was made. Causal analysis
          may be wrong.
          <span className="banner-hint">Re-run the simulation to be sure.</span>
        </div>
      )}
      <QueryBar />
      <TabStrip />
      <div className="main">
        <SignalPanel />
        {/* The wave canvas stays mounted across tabs: re-creating the worker
            and refetching on every tab switch would make the app feel cheap. */}
        <div className="panes">
          {/* Replay covers everything but a strip of this canvas: §11.5 wants a
              waveform windowed on the current step, and the real one — already
              mounted, already fed by the worker — is the only one that can be
              trusted to agree with the rest of the app. */}
          <div className={tab === 1 || replay ? "pane on" : "pane"}>
            <WaveCanvas />
          </div>
          {tab === 2 && (
            <div className="pane on">
              <CausalTab />
            </div>
          )}
          {tab === 3 && (
            <div className="pane on">
              {/* §11.4: FSM took the Source pane rather than a tab of its own —
                  "FSM e o vedere asupra structurii codului, nu un domeniu
                  separat". Leaving the mode puts the code back. */}
              {fsmOpen ? <FsmMode /> : <SourceTab />}
            </div>
          )}
          {tab === 5 && (
            <div className="pane on">
              <ChecksTab />
            </div>
          )}
          {tab === 6 && (
            <div className="pane on">
              <DiffTab />
            </div>
          )}
          {tab === 7 && (
            <div className="pane on">
              <CoverageTab />
            </div>
          )}
          {tab === 8 && (
            <div className="pane on">
              <TransactionsTab />
            </div>
          )}
          {tab === 9 && (
            <div className="pane on">
              <PerformanceTab />
            </div>
          )}
          {tab === 10 && (
            <div className="pane on">
              <MemoryTab />
            </div>
          )}
          {tab > FIXED_TABS && pluginTables[tab - FIXED_TABS - 1] && (
            <div className="pane on">
              <PluginTab table={pluginTables[tab - FIXED_TABS - 1]} />
            </div>
          )}
          <ReplayMode />
        </div>
      </div>
      <StatusBar />
      <CommandPalette />
      <HelpOverlay />
      <WaveMenu />
      {!ready && <div className="loading">Opening trace…</div>}
    </div>
  );
}

/** §11.7. Shortcuts for features that do not exist yet are simply not bound. */
function useKeyboard() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      const s = useWave.getState();
      const mod = e.metaKey || e.ctrlKey;

      if (mod && e.key.toLowerCase() === "p") {
        e.preventDefault();
        s.setPalette(true);
        return;
      }
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        window.dispatchEvent(new Event("vt:focus-query"));
        return;
      }
      if (e.key === "Escape") {
        s.setPalette(false);
        s.setHelp(false);
        return;
      }
      if (typing) return;

      if (e.key >= "1" && e.key <= "9" && !mod) {
        s.setTab(Number(e.key));
        return;
      }
      // `0` is the tenth tab, the way a ten-slot toolbar is always numbered.
      if (e.key === "0" && !mod) {
        s.setTab(10);
        return;
      }
      if (e.key === "?") {
        s.setHelp(!s.helpOpen);
        return;
      }
      if (mod && e.key.toLowerCase() === "m") {
        // §11.4: FSM is a mode of the Source tab, entered and left with the
        // same chord.
        e.preventDefault();
        s.setFsmOpen(!s.fsmOpen);
        return;
      }
      if (e.key === "s" && !mod) {
        // §11.7: the minimal subtrace of the chain on screen.
        if (s.causal) void s.loadSubtrace();
        return;
      }
      if (e.key === "r" && !mod) {
        // §11.7: replay. Only meaningful with a chain to replay.
        if (s.causal) s.setReplay(true);
        return;
      }
      if (e.key === "w" && !mod) {
        // §11.7: why on the current selection.
        const sig = s.selected !== null ? s.signalsByHandle.get(s.selected) : undefined;
        if (sig) void s.runWhy(sig.path, s.cursor ?? s.view.t1);
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const dir = e.key === "ArrowRight" ? 1 : -1;
        if (e.shiftKey && s.clockPeriod) {
          const base = s.cursor ?? s.view.t0;
          s.setCursor(Math.round(base + dir * s.clockPeriod));
        } else {
          void s.stepEdge(dir);
        }
        return;
      }
      if (e.key === "f" && !mod) {
        s.zoomAll();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
