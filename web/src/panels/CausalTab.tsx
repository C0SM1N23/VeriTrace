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
  hold: "held: no driver was enabled",
  assigned: "assigned by an active driver",
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
      <div className="spine">
        <Card node={causal.root} depth={0} />
      </div>
    </div>
  );
}

function Card({ node, depth }: { node: CausalNode; depth: number }) {
  const activeNode = useWave((s) => s.activeNode);
  const select = useWave((s) => s.selectCausal);
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
        onClick={() => select(node)}
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
              <span className="card-val mono">= {node.value}</span>
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
