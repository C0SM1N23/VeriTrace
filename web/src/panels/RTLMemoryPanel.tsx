/** Declared RTL arrays, sampled from the native trace at Wave's shared cursor. */
import { useEffect, useState } from "react";
import { fetchMemoryWords } from "../api/client";
import { formatTime } from "../lib/time";
import { format as formatValue } from "../lib/radix";
import type { MemoryWords, Radix, RTLMemory } from "../lib/types";
import { useWave } from "../state/store";

export function RTLMemoryPanel({ arrays, rtlError }: { arrays: RTLMemory[]; rtlError?: string }) {
  const [selected, setSelected] = useState(arrays[0]?.path ?? "");
  const [offset, setOffset] = useState(0);
  const [radix, setRadix] = useState<Radix>("hex");
  const session = useWave((s) => s.session);
  const time = useWave((s) => s.cursor ?? s.bounds.t0);
  const bounds = useWave((s) => s.bounds);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const setCursor = useWave((s) => s.setCursor);
  const jump = useWave((s) => s.jumpTo);
  const current = arrays.find((a) => a.path === selected) ?? arrays[0];
  const path = current?.path;
  const supported = current?.supported;
  const [result, setResult] = useState<MemoryWords | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let active = true;
    setResult(null);
    setError(null);
    if (session && path && supported) {
      void fetchMemoryWords(session, path, time, offset).then((words) => {
        if (active) setResult(words);
      }).catch((e: unknown) => { if (active) setError(String(e)); });
    }
    return () => { active = false; };
  }, [session, path, supported, time, offset, retry]);

  return <section className="mem-card" data-testid="rtl-memory">
    <div className="perf-card-title">RTL memories / register arrays</div>
    {rtlError && <div role="alert">{rtlError}</div>}
    {!current ? <div className="pane-hint">No RTL array declarations available. Load the matching RTL to discover SRAMs and register files; a waveform alone does not establish array bounds.</div> : <>
      <label>Memory {" "}<select aria-label="RTL memory" value={current.path} onChange={(e) => {
        setSelected(e.target.value); setOffset(0);
      }}>{arrays.map((a) => <option key={a.path} value={a.path}>{a.path}</option>)}</select></label>
      <div data-testid="rtl-memory-summary">
        {current.depth} words × {current.width} bits
        {current.supported && ` [${current.left}:${current.right}]`}
        {" · "}{current.captured} words captured · {current.source}
      </div>
      {!current.supported ? <div className="pane-hint">Word inspection currently supports one-dimensional fixed arrays. This declaration is retained, not presented as an empty memory.</div> : <>
        {current.captured < current.depth && <div className="pane-hint" data-testid="rtl-memory-capture-hint">
          Undumped words have no observed value. Re-run with <code>veritrace run … --dump-memory '{current.path}'</code> to capture every word without editing RTL.
          For Verilator, enable tracing and set <code>--trace-max-array {current.depth}</code>.
        </div>}
        <label>At trace tick {" "}<input aria-label="Memory time" type="number" min={bounds.t0} max={bounds.t1} value={time}
          onChange={(e) => { const t = e.target.valueAsNumber; if (Number.isSafeInteger(t) && t >= bounds.t0 && t <= bounds.t1) setCursor(t); }} /></label>
        {" "}<span>{formatTime(time, timescale)} · follows Wave cursor</span>{" "}
        <select aria-label="Memory radix" value={radix} onChange={(e) => setRadix(e.target.value as Radix)}>
          <option value="hex">hex</option><option value="dec">decimal</option><option value="bin">binary</option>
        </select>
        <div>
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 64))}>Previous words</button>{" "}
          <span>{offset + 1}–{Math.min(current.depth, offset + 64)} of {current.depth}</span>{" "}
          <button disabled={offset + 64 >= current.depth} onClick={() => setOffset(offset + 64)}>Next words</button>
        </div>
        {error ? <div role="alert">{error} <button onClick={() => setRetry(retry + 1)}>Retry memory</button></div>
          : !result || result.path !== path || result.time !== time || result.offset !== offset
            ? <div role="status">Reading captured words…</div>
            : <table className="perf-chain" data-testid="rtl-memory-words">
              <thead><tr><th>Word index</th><th>Observed value</th><th>Waveform</th></tr></thead>
              <tbody>{result.words.map((w) => <tr key={w.index} data-testid={`memory-word-${w.index}`}>
                <td>{w.index}</td><td>{!w.captured ? "not captured" : w.bits === null ? "no sample at this time" : formatValue(w.bits, current.width, radix)}</td>
                <td><button disabled={!w.captured} onClick={() => jump(time, [w.path], w.path)}>Show word in Wave</button></td>
              </tr>)}</tbody>
            </table>}
        <div className="pane-hint">Word indices are array indices, not byte addresses. X/Z are simulator values, never replaced with zero. These are captured contents, not SDRAM timing checks.</div>
      </>}
    </>}
  </section>;
}
