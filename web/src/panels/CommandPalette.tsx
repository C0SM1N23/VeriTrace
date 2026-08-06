/** Signal search palette (`⌘P` / `Ctrl+P`), §11.7. */

import { useEffect, useMemo, useRef, useState } from "react";
import { filterByPath } from "../lib/fuzzy";
import { useWave } from "../state/store";

export function CommandPalette() {
  const open = useWave((s) => s.paletteOpen);
  const setPalette = useWave((s) => s.setPalette);
  const signals = useWave((s) => s.signals);
  const addSignal = useWave((s) => s.addSignal);
  const setSelected = useWave((s) => s.setSelected);
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const hits = useMemo(() => filterByPath(signals, q, (s) => s.path, 60), [signals, q]);

  useEffect(() => {
    if (open) {
      setQ("");
      setCursor(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  if (!open) return null;

  const commit = (i: number) => {
    const sig = hits[i];
    if (!sig) return;
    addSignal(sig.handle);
    setSelected(sig.handle);
    setPalette(false);
  };

  return (
    <div className="palette-backdrop" onClick={() => setPalette(false)}>
      <div className="palette" onClick={(e) => e.stopPropagation()} data-testid="palette">
        <input
          ref={inputRef}
          value={q}
          placeholder="Find a signal…"
          onChange={(e) => {
            setQ(e.target.value);
            setCursor(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setCursor((c) => Math.min(c + 1, hits.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setCursor((c) => Math.max(c - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              commit(cursor);
            } else if (e.key === "Escape") {
              setPalette(false);
            }
          }}
          data-testid="palette-input"
        />
        <div className="palette-hits">
          {hits.map((s, i) => (
            <button
              key={s.handle}
              className={i === cursor ? "hit on" : "hit"}
              onMouseEnter={() => setCursor(i)}
              onClick={() => commit(i)}
            >
              <span className="hit-path">{s.path}</span>
              <span className="hit-meta">
                {s.width > 1 ? `[${s.msb}:${s.lsb}]` : ""} {s.kind}
              </span>
            </button>
          ))}
          {hits.length === 0 && <div className="empty">No signal matches that.</div>}
        </div>
      </div>
    </div>
  );
}
