/**
 * Causal Replay — §11.5.
 *
 * A **mode**, not a tab: it takes the whole screen, because its job is to hold
 * your attention on one step at a time. Entered from Causal with `▶ Replay` or
 * `r`, left with Escape.
 *
 * The story runs *forwards* — root cause first, symptom last — which is the
 * opposite of how the chain was built. §11.5 gives two reasons and both are
 * practical: walking it in chronological order is how you check that the chain
 * is actually causal, and it is how you explain the bug to someone else.
 *
 * One step is one event of the **minimal subtrace** (§8.2), never a node of the
 * full tree. Two hundred nodes is not a story. The sentence on each step comes
 * from the server's template table (§11.5), so nothing here writes prose.
 */

import { useEffect, useRef } from "react";
import { formatTime } from "../lib/time";
import type { SubtraceEvent } from "../lib/types";
import { useWave } from "../state/store";

/** §11.5: auto-play advances every two seconds. */
const PLAY_MS = 2000;
/** Lines of source shown around the step's line. */
const CONTEXT = 2;

export function ReplayMode() {
  const open = useWave((s) => s.replay);
  const sub = useWave((s) => s.subtrace);
  const busy = useWave((s) => s.subtraceBusy);
  const error = useWave((s) => s.subtraceError);
  const index = useWave((s) => s.replayStep);
  const playing = useWave((s) => s.replayPlaying);

  // Keys are bound here rather than in the global handler: while replay owns
  // the screen, the arrows mean "previous/next step", not "previous/next
  // transition", and two handlers fighting over them would be a bug.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      const s = useWave.getState();
      if (e.key === "Escape") s.setReplay(false);
      else if (e.key === "ArrowRight") s.stepReplay(1);
      else if (e.key === "ArrowLeft") s.stepReplay(-1);
      else if (e.key === "Home") s.gotoReplay(0);
      else if (e.key === "End") s.gotoReplay((s.subtrace?.steps.length ?? 1) - 1);
      else if (e.key === " ") s.toggleReplayPlay();
      else return;
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open]);

  useEffect(() => {
    if (!open || !playing) return;
    const t = setInterval(() => {
      const s = useWave.getState();
      const last = (s.subtrace?.steps.length ?? 1) - 1;
      if (s.replayStep >= last) s.toggleReplayPlay();
      else s.stepReplay(1);
    }, PLAY_MS);
    return () => clearInterval(t);
  }, [open, playing]);

  if (!open) return null;

  const steps = sub?.steps ?? [];
  const step = steps[Math.min(index, Math.max(0, steps.length - 1))];

  return (
    <div className="replay" data-testid="replay">
      <div className="replay-bar">
        <span className="replay-count">
          {steps.length ? `Step ${index + 1} of ${steps.length}` : "Replay"}
        </span>
        <span className="spacer" />
        <Controls count={steps.length} index={index} playing={playing} />
        <button
          className="chip"
          onClick={() => void useWave.getState().exportReport()}
          title="§12 — a standalone HTML report of this chain"
          data-testid="replay-export"
        >
          export as HTML
        </button>
        <button
          className="chip"
          onClick={() => useWave.getState().setReplay(false)}
          data-testid="replay-close"
        >
          close
        </button>
      </div>

      {busy && <div className="pane-note">Minimising the chain…</div>}
      {error && <div className="pane-note error">{error}</div>}
      {!busy && !error && !steps.length && (
        <div className="pane-note">
          Nothing to replay.
          <div className="pane-hint">The chain reduced to no events at all.</div>
        </div>
      )}

      {step && (
        <div className="replay-body">
          <Step step={step} />
          <Track steps={steps} index={index} />
        </div>
      )}
    </div>
  );
}

function Controls({
  count,
  index,
  playing,
}: {
  count: number;
  index: number;
  playing: boolean;
}) {
  const at = (n: number) => () => useWave.getState().gotoReplay(n);
  const by = (n: number) => () => useWave.getState().stepReplay(n);
  return (
    <div className="replay-controls">
      <button className="chip" onClick={at(0)} disabled={index === 0} title="First (Home)">
        ⏮
      </button>
      <button className="chip" onClick={by(-1)} disabled={index === 0} title="Previous (←)">
        ◀
      </button>
      <button
        className="chip"
        onClick={() => useWave.getState().toggleReplayPlay()}
        title="Play / pause (Space)"
        data-testid="replay-play"
      >
        {playing ? "⏸" : "▶"}
      </button>
      <button
        className="chip"
        onClick={by(1)}
        disabled={index >= count - 1}
        title="Next (→)"
        data-testid="replay-next"
      >
        ▶|
      </button>
      <button
        className="chip"
        onClick={at(count - 1)}
        disabled={index >= count - 1}
        title="Last (End)"
      >
        ⏭
      </button>
    </div>
  );
}

function Step({ step }: { step: SubtraceEvent }) {
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const source = useWave((s) => s.source);
  const at = step.cycle !== null ? `c${step.cycle}` : formatTime(step.time, timescale);

  return (
    <div className="replay-step">
      <div className="replay-when">
        {at} — <span className="mono">{step.signal}</span>
        {step.kind === "transition" && step.prev !== null ? (
          <span className="mono dim">
            {" "}
            {step.prev} → {step.value}
          </span>
        ) : (
          <span className="mono dim"> = {step.value}</span>
        )}
        {step.is_root_cause && <span className="replay-tag">root cause</span>}
        {step.is_symptom && <span className="replay-tag">symptom</span>}
      </div>

      <p className="replay-text">{step.text}</p>

      {step.loc && (
        <>
          <div className="replay-loc mono">
            {step.loc.file}:{step.loc.line}
          </div>
          <Snippet file={step.loc.file} line={step.loc.line} text={source?.text ?? ""} open={source?.file === step.loc.file} />
        </>
      )}
    </div>
  );
}

/**
 * The five lines around the step, highlighted.
 *
 * Reads the source the shared selection already loaded (§11.6) instead of
 * fetching its own copy — the Source tab and this pane must never disagree
 * about what is on line 88.
 */
function Snippet({
  file,
  line,
  text,
  open,
}: {
  file: string;
  line: number;
  text: string;
  open: boolean | undefined;
}) {
  if (!open || !text) return <div className="replay-snippet dim">{file}</div>;
  const lines = text.split("\n");
  const lo = Math.max(1, line - CONTEXT);
  const hi = Math.min(lines.length, line + CONTEXT);
  const rows = [];
  for (let n = lo; n <= hi; n++) {
    rows.push(
      <div key={n} className={n === line ? "replay-line hit" : "replay-line"}>
        <span className="replay-ln">{n}</span>
        <span className="mono">{lines[n - 1]}</span>
      </div>,
    );
  }
  return <div className="replay-snippet">{rows}</div>;
}

/** The dot track of §11.5, with the cycle of each step underneath. */
function Track({ steps, index }: { steps: SubtraceEvent[]; index: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>(".on")?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [index]);
  return (
    <div className="replay-track" ref={ref}>
      {steps.map((s, i) => (
        <button
          key={`${s.signal}@${s.time}`}
          className={`replay-dot${i === index ? " on" : ""}${i < index ? " done" : ""}`}
          onClick={() => useWave.getState().gotoReplay(i)}
          title={s.text}
        >
          <span className="replay-dot-mark" aria-hidden>
            ●
          </span>
          <span className="replay-dot-t">{s.cycle !== null ? `c${s.cycle}` : s.time}</span>
        </button>
      ))}
    </div>
  );
}
