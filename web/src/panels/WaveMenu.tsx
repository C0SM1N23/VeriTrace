/**
 * Right-click menu over the waveform.
 *
 * "Why is this value here?" is the main route through the application (§11.4),
 * so it is the first item and it goes straight to Causal.
 */

import { useEffect, useState } from "react";
import { useWave } from "../state/store";

export interface MenuAt {
  x: number;
  y: number;
  path: string;
  handle: number;
  time: number;
}

export function WaveMenu() {
  const [at, setAt] = useState<MenuAt | null>(null);
  const runWhy = useWave((s) => s.runWhy);
  const hasRtl = useWave((s) => s.status?.has_rtl ?? false);

  useEffect(() => {
    const open = (e: Event) => setAt((e as CustomEvent<MenuAt>).detail);
    const close = () => setAt(null);
    window.addEventListener("vt:wave-menu", open);
    window.addEventListener("click", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("vt:wave-menu", open);
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", close);
    };
  }, []);

  if (!at) return null;

  return (
    <div
      className="ctx"
      style={{ left: at.x, top: at.y }}
      onClick={(e) => e.stopPropagation()}
      data-testid="wave-menu"
    >
      <div className="ctx-title mono">{at.path}</div>
      <button
        className="ctx-item"
        disabled={!hasRtl}
        onClick={() => {
          setAt(null);
          void runWhy(at.path, at.time);
        }}
        data-testid="ctx-why"
      >
        Why is this value here?
        {!hasRtl && <span className="ctx-note">needs --rtl</span>}
      </button>
      <button
        className="ctx-item"
        onClick={() => {
          useWave.getState().setCursor(at.time);
          setAt(null);
        }}
      >
        Move cursor here
      </button>
      <button
        className="ctx-item"
        onClick={() => {
          useWave.getState().addMarker(at.time);
          setAt(null);
        }}
      >
        Drop a measurement marker
      </button>
    </div>
  );
}
