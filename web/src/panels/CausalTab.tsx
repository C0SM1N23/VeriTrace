/**
 * TAB 2 — Causal (§11.4).
 *
 * The chain as a vertical column of cards, symptom at the top, root cause at
 * the bottom. Amber appears here and nowhere else: this is the only thing in
 * the interface that colour is allowed to mean (§11.1).
 *
 * Secondary branches are collapsed and dimmed rather than hidden — nothing is
 * concealed, the likely path is simply the one already open (§11.4).
 */

import { useState } from "react";
import { formatTime } from "../lib/time";
import type { CausalNode } from "../lib/types";
import { nodeId, useWave } from "../state/store";

const REASON_TEXT: Record<string, string> = {
  primary_input: "primary input — driven from outside the design",
  constant: "constant for the whole run",
  unknown_x: "X — no defined value",
  undriven: "nothing drives this signal",
  conflict: "several drivers active at once",
  cycle: "feedback loop, already visited",
  depth_limit: "depth limit reached",
  blackbox_ip: "inside an IP with no source",
  not_traced: "not in the trace and not derivable",
  // §8.11b — an on-board capture is a window, and this is its front edge.
  capture_boundary: "already settled when the capture began",
  hold: "held: no driver was enabled",
  assigned: "assigned by an active driver",
  counterfactual: "condition that prevented the requested value",
  // §8.16 — the chain crossed into the transaction layer.
  txn_open: "a transaction that was never completed",
  txn_in_flight: "a transaction that was still running at this instant",
};

export function CausalTab() {
  const causal = useWave((s) => s.causal);
  const busy = useWave((s) => s.causalBusy);
  const error = useWave((s) => s.causalError);
  const status = useWave((s) => s.status);

  if (busy) return <div className="pane-note">Running why()…</div>;

  if (error) {
    return (
      <div className="pane-note error">
        <div>{error}</div>
        {!status?.has_rtl && (
          <div className="pane-hint">
            Start the server with <code>--rtl &lt;dir&gt;</code> to enable causal analysis.
          </div>
        )}
      </div>
    );
  }

  if (!causal) {
    return (
      <div className="pane-note">
        No causal chain yet.
        <div className="pane-hint">
          Right-click a signal in Wave and choose “Why is this value here?”, or press{" "}
          <kbd>w</kbd> with a signal selected.
        </div>
      </div>
    );
  }

  return (
    <div className="causal">
      <div className="causal-head">
        <span className="mono">{causal.query}</span>
        <span className="spacer" />
        <span className="dim">
          {causal.stats.nodes} nodes · {causal.stats.ms} ms
          {causal.stats.truncated && " · truncated"}
        </span>
      </div>
      {/* §8.16: a transaction-level question says at the top what was really
          asked, because the chain below it is about a signal. */}
      {causal.headline && (
        <div className="causal-headline" data-testid="causal-headline">
          {causal.headline}
        </div>
      )}
      {/* One scroller for all three sections of §11.4: the spine, the subtrace
          and the repro are one document, not three panes that scroll apart. */}
      <div className="causal-body">
        <div className="spine">
          <Card node={causal.root} depth={0} />
        </div>
        {/^why_not\s*\(/i.test(causal.query) ? (
          <div className="pane-hint">
            Counterfactual chains identify the observed guard/value terms that prevented the
            requested value. Replay and repro remain tied to an observed <code>why()</code> event.
          </div>
        ) : (
          <>
            <SubtraceSection />
            <ReproSection />
          </>
        )}
      </div>
    </div>
  );
}

/**
 * §11.4 section (b) — the minimal subtrace, as a compact timeline.
 *
 * Loaded on demand rather than with the chain: §8.2's minimality check is
 * quadratic in the events, and most questions are answered by reading the spine
 * above without ever needing the five-line version.
 */
function SubtraceSection() {
  const sub = useWave((s) => s.subtrace);
  const busy = useWave((s) => s.subtraceBusy);
  const error = useWave((s) => s.subtraceError);
  const load = useWave((s) => s.loadSubtrace);
  const setReplay = useWave((s) => s.setReplay);

  return (
    <section className="causal-section" data-testid="subtrace-section">
      <div className="causal-section-head">
        <h3>Minimal subtrace</h3>
        <span className="spacer" />
        {sub && (
          <span className="dim">
            {sub.considered} event{sub.considered === 1 ? "" : "s"} · {sub.dropped} redundant
          </span>
        )}
        <button className="chip" onClick={() => void load()} disabled={busy}>
          {sub ? "recompute" : "minimise"} <kbd>s</kbd>
        </button>
        <button
          className="chip primary"
          onClick={() => setReplay(true)}
          data-testid="replay-open"
          title="Walk the chain forwards, one step at a time (§11.5)"
        >
          ▶ Replay <kbd>r</kbd>
        </button>
      </div>

      {busy && <div className="pane-note">Minimising…</div>}
      {error && <div className="pane-note error">{error}</div>}
      {sub && !busy && (
        <>
          {!sub.reached_root_cause && (
            <div className="pane-hint">
              The walk did not reach a terminal that explains the value, so the last
              line is where it stopped rather than the cause.
            </div>
          )}
          <ol className="subtrace">
            {sub.steps.map((e) => (
              <li
                key={`${e.signal}@${e.time}`}
                className={e.is_root_cause ? "sub-row root" : "sub-row"}
                onClick={() => useWave.getState().gotoReplay(e.step - 1)}
              >
                <span className="sub-at mono">{e.cycle !== null ? `c${e.cycle}` : e.time}</span>
                <span className="sub-sig mono">{e.signal}</span>
                <span className="sub-val mono">{e.value}</span>
                <span className="sub-text">{e.text}</span>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

/**
 * §11.4 section (c) — the repro, and the verdict from actually running it.
 *
 * The button says what it will produce. §8.3 is explicit that a window cut must
 * not be called minimal, so the label follows the server's answer rather than
 * being decided here.
 */
function ReproSection() {
  const repro = useWave((s) => s.repro);
  const busy = useWave((s) => s.reproBusy);
  const error = useWave((s) => s.reproError);
  const build = useWave((s) => s.buildRepro);
  const [copied, setCopied] = useState(false);

  const v = repro?.validation;
  return (
    <section className="causal-section" data-testid="repro-section">
      <div className="causal-section-head">
        <h3>Repro</h3>
        <span className="spacer" />
        <button
          className="chip"
          onClick={() => void build({ validate: false })}
          disabled={busy}
          title="Write the testbench without compiling it"
        >
          generate
        </button>
        <button
          className="chip primary"
          onClick={() => void build({ validate: true })}
          disabled={busy}
          data-testid="repro-validate"
          title="Generate, compile and run it — §8.3's loop"
        >
          generate &amp; verify
        </button>
      </div>

      {busy && <div className="pane-note">Generating, compiling and running…</div>}
      {error && <div className="pane-note error">{error}</div>}

      {repro && !busy && (
        <>
          <div className="repro-head">
            <span className={`verdict ${v?.reproduced ? "ok" : v?.ran ? "no" : "unknown"}`}>
              {v?.reproduced
                ? `verified with ${v.tool}`
                : v?.ran
                  ? "did not reproduce"
                  : "not run"}
            </span>
            <span className="dim">
              {repro.mode === "minimal" ? "minimal repro" : "focused testbench"} ·{" "}
              {repro.module} · {repro.cycles} cycles · {repro.events} stimulus event
              {repro.events === 1 ? "" : "s"}
            </span>
            <span className="spacer" />
            <button
              className="chip"
              onClick={() => {
                void navigator.clipboard?.writeText(repro.code);
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              }}
            >
              {copied ? "copied" : "copy"}
            </button>
          </div>
          <div className="pane-hint">{repro.mode_reason}</div>
          {repro.tied.length > 0 && (
            <div className="pane-hint">
              tied off as don&rsquo;t-care: <span className="mono">{repro.tied.join(", ")}</span>
            </div>
          )}
          {repro.notes.map((n) => (
            <div className="pane-hint" key={n}>
              {n}
            </div>
          ))}
          {v && !v.reproduced && v.error && <div className="pane-hint error">{v.error}</div>}
          <pre className="repro-code" data-testid="repro-code">
            {repro.code}
          </pre>
        </>
      )}
    </section>
  );
}

function Card({ node, depth }: { node: CausalNode; depth: number }) {
  const activeNode = useWave((s) => s.activeNode);
  const select = useWave((s) => s.selectCausal);
  const setHovered = useWave((s) => s.setHoveredCausal);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const cursor = useWave((s) => s.cursor);
  // Secondary branches start collapsed: nothing hidden, the likely path open.
  const [open, setOpen] = useState(node.is_primary_path || depth === 0);

  const id = nodeId(node);
  const isActive = activeNode === id;
  const primary = node.is_primary_path;
  const isTxn = node.kind === "txn_link";
  const since =
    node.last_change !== null && cursor !== null
      ? `last change ${formatTime(node.last_change, timescale)}`
      : "never changed in this trace";

  return (
    <div className={`card-wrap${primary ? "" : " secondary"}`}>
      <div
        className={`card${isActive ? " active" : ""}${primary ? " primary" : ""}${
          isTxn ? " txn" : ""
        }`}
        onClick={(event) => {
          if (event.altKey && !isTxn) {
            const suggested = node.width === 1 && node.value === "0" ? "1" : "0";
            const desired =
              node.width === 1 && /^[01]$/.test(node.value)
                ? suggested
                : window.prompt(`Value expected for ${node.signal}`, suggested);
            if (desired !== null && desired.trim()) {
              void useWave
                .getState()
                .runQueryText(`why_not(${node.signal} == ${desired.trim()} @ ${node.time})`);
            }
            return;
          }
          select(node);
        }}
        title="Click to synchronize panels; Alt+click asks why a different value did not occur"
        onMouseEnter={() => !isTxn && setHovered(node.signal)}
        onMouseLeave={() => setHovered(null)}
        data-testid={isTxn ? "causal-txn-card" : "causal-card"}
        data-signal={node.signal}
        data-txn={node.txn ?? undefined}
      >
        <div className="card-top">
          <span className="dot" aria-hidden>
            ●
          </span>
          {isTxn ? (
            <span className="card-sig mono causal-txn">{node.txn}</span>
          ) : (
            <>
              <span className="card-sig mono">{node.signal}</span>
              {/* §7.3: a value the graph computed because the signal never
                  reached the dump is drawn differently from one that was
                  measured. Presenting inference as observation is the one
                  thing P2 does not allow. */}
              <span className={`card-val mono${node.derived ? " derived" : ""}`}>
                = {node.value}
              </span>
              {node.derived && (
                <span className="card-derived" title="not in the trace — evaluated from the RTL">
                  derived
                </span>
              )}
            </>
          )}
        </div>
        <div className="card-meta">
          {isTxn ? node.detail : since} · {formatTime(node.time, timescale)}
          {node.repeated && " · shown above"}
        </div>
        {node.loc && (
          <div className="card-loc mono">
            {node.loc.file}:{node.loc.line}
            {node.detail && <span className="card-detail"> {node.detail}</span>}
          </div>
        )}
        <div className="card-reason">{REASON_TEXT[node.reason] ?? node.reason}</div>
        {node.children.length > 0 && (
          <button
            className="branches"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(!open);
            }}
            data-testid="branch-toggle"
          >
            {open ? "▾" : "▸"} {node.children.length}{" "}
            {node.children.length === 1 ? "cause" : "causes"}
          </button>
        )}
      </div>

      {node.children.length > 0 && open && (
        <>
          <div className="spine-arrow" aria-hidden>
            ▼
          </div>
          <div className="spine-children">
            {node.children.map((c) => (
              <Card key={nodeId(c)} node={c} depth={depth + 1} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
