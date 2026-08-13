/**
 * TAB 5 — Diff (§11.4, §8.7).
 *
 * Regression triage: two runs, and the one question worth asking first — *from
 * which cycle do we part company?*
 *
 * The layout follows §11.4: the two-trace selector and the alignment strategy at
 * the top, the first divergence as one large card, and the two causal chains
 * side by side underneath with the first differing node in magenta. Magenta
 * appears here and nowhere else in the application (§11.2), which is why a
 * reader can tell at a glance which line is the answer.
 *
 * The cycle numbers on screen are positions on the *shared* axis: the server
 * normalised both timescales and matched anchor events before comparing, so
 * "c1200" is one moment in both runs rather than two (§8.7).
 */

import { useEffect, useMemo } from "react";
import { shortLabels } from "../lib/names";
import type { CausalNode, DiffReport, Divergence, OpenSession } from "../lib/types";
import { useWave } from "../state/store";

const STRATEGIES: [string, string][] = [
  ["cycle", "clock edges — cycle numbering"],
  ["handshake", "completed handshakes — for differing latencies"],
  ["retire", "retired instructions — for CPUs"],
  ["manual", "anchors you placed"],
];

export function DiffTab() {
  const report = useWave((s) => s.diff);
  const busy = useWave((s) => s.diffBusy);
  const error = useWave((s) => s.diffError);
  const sessions = useWave((s) => s.diffSessions);
  const other = useWave((s) => s.diffOther);
  const strategy = useWave((s) => s.diffStrategy);
  const ignored = useWave((s) => s.diffIgnore);
  const index = useWave((s) => s.diffIndex);
  const session = useWave((s) => s.session);

  useEffect(() => {
    void useWave.getState().loadDiffSessions();
  }, []);

  // `n` / `p` walk the divergences — §11.7 binds them to "next/previous
  // finding", and in this tab a divergence is the finding.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (e.key === "n") useWave.getState().stepDivergence(1);
      else if (e.key === "p") useWave.getState().stepDivergence(-1);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const current = report?.divergences[index];
  // Every design dumps to `dump.vtx`, so the basename is not a name. Grow each
  // path until it distinguishes — the same rule the signal list uses.
  const others = useMemo(
    () => sessions.filter((s) => s.session_id !== session),
    [sessions, session],
  );
  const labels = useMemo(() => traceLabels(others), [others]);

  return (
    <div className="diff">
      <div className="diff-head">
        <span className="dim">compare</span>
        <span className="mono strong">{report?.a.name ?? "this run"}</span>
        <span className="dim">with</span>
        <select
          className="diff-select"
          value={other}
          onChange={(e) => useWave.getState().setDiffOther(e.target.value)}
          data-testid="diff-other"
        >
          <option value="">choose a trace…</option>
          {others.map((s) => (
            <option key={s.session_id} value={s.trace}>
              {labels.get(s.trace) ?? s.name}
            </option>
          ))}
        </select>
        <select
          className="diff-select"
          value={strategy}
          onChange={(e) => useWave.getState().setDiffStrategy(e.target.value)}
          data-testid="diff-strategy"
          title="§8.7: align on anchor events, never on absolute time"
        >
          {STRATEGIES.map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
        <button
          className="chip primary"
          onClick={() => void useWave.getState().runDiff()}
          disabled={!other || busy}
          data-testid="diff-run"
        >
          compare
        </button>
      </div>

      {sessions.length < 2 && !report && (
        <div className="pane-note">
          Only one trace is open.
          <div className="pane-hint">
            Open the other run in the same server —{" "}
            <code>veritrace serve a.vtx b.vtx --rtl src/</code> — or point this at its
            path.
          </div>
        </div>
      )}

      {busy && <div className="pane-note">Aligning and comparing…</div>}
      {error && <div className="pane-note error">{error}</div>}

      {report && !busy && (
        <div className="diff-body">
          <Alignment report={report} />
          {current ? (
            <FirstDivergence d={current} index={index} total={report.divergences.length} />
          ) : (
            <div className="pane-note">
              The two runs agree on every signal they share.
              <div className="pane-hint">
                {report.compared} signal(s) compared over {report.alignment.matched} matched
                anchor(s).
              </div>
            </div>
          )}

          {report.txn_divergences.length > 0 && (
            <section className="diff-section">
              <h3>First diverging transaction</h3>
              {report.txn_divergences.map((d) => (
                <div className="diff-txn" key={d.ref}>
                  <span className="mono strong">{d.ref}</span>
                  <span className="dim">c{d.at}</span>
                  <span className="mono">{d.value_a}</span>
                  <span className="dim">vs</span>
                  <span className="mono">{d.value_b}</span>
                  {d.detail && <span className="pane-hint">{d.detail}</span>}
                </div>
              ))}
            </section>
          )}

          <Chains report={report} />

          <section className="diff-section">
            <h3>Diverging signals</h3>
            <ol className="diff-list">
              {report.divergences.map((d, i) => (
                <li
                  key={d.signal}
                  className={i === index ? "diff-row on" : "diff-row"}
                  onClick={() => useWave.getState().gotoDivergence(i)}
                >
                  <span className="diff-at mono">c{d.at}</span>
                  <span className="diff-sig mono">{d.signal}</span>
                  <span className="diff-va mono">{d.value_a}</span>
                  <span className="diff-vb mono">{d.value_b}</span>
                  <button
                    className="chip"
                    onClick={(e) => {
                      e.stopPropagation();
                      void useWave.getState().ignoreSignal(d.signal);
                    }}
                    title="Leave this signal out and compare again"
                  >
                    ignore
                  </button>
                </li>
              ))}
            </ol>
            {ignored.length > 0 && (
              <div className="pane-hint">
                ignoring: <span className="mono">{ignored.join(", ")}</span>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

/** `trace path -> the shortest tail that names it apart from the others open. */
function traceLabels(sessions: OpenSession[]): Map<string, string> {
  const posix = new Map(sessions.map((s) => [s.trace, s.trace.replace(/\\/g, "/")]));
  const short = shortLabels([...posix.values()], "/");
  return new Map(
    sessions.map((s) => [s.trace, short.get(posix.get(s.trace) ?? "") ?? s.name]),
  );
}

/** What the two runs were put on one axis by — §8.7's first two steps, stated. */
function Alignment(props: { report: DiffReport }) {
  const a = props.report.alignment;
  const same = a.timescale_a === a.timescale_b;
  return (
    <div className="diff-align">
      <span>
        aligned on <b>{a.strategy}</b> · {a.matched} anchor(s) matched ({a.anchors_a} vs{" "}
        {a.anchors_b})
      </span>
      <span className="dim">
        {same
          ? `both runs at ${a.timescale_a} fs per tick`
          : `${a.timescale_a} fs vs ${a.timescale_b} fs per tick — normalised before comparing`}
      </span>
      {a.note && <span className="dim">{a.note}</span>}
      <span className="dim">
        {props.report.compared} compared · {props.report.ignored.length} ignored ·{" "}
        {props.report.only_a.length + props.report.only_b.length} in one run only
      </span>
    </div>
  );
}

function FirstDivergence({
  d,
  index,
  total,
}: {
  d: Divergence;
  index: number;
  total: number;
}) {
  return (
    <div className="diff-card" data-testid="diff-first">
      <div className="diff-card-head">
        <span className="diff-card-title">
          {index === 0 ? "First divergence" : `Divergence ${index + 1}`} at c{d.at}
        </span>
        <span className="spacer" />
        <span className="dim">
          {index + 1} of {total}
        </span>
        <button className="chip" onClick={() => useWave.getState().stepDivergence(-1)}>
          ◀ <kbd>p</kbd>
        </button>
        <button className="chip" onClick={() => useWave.getState().stepDivergence(1)}>
          <kbd>n</kbd> ▶
        </button>
      </div>
      <div className="diff-card-sig mono">{d.signal}</div>
      <div className="diff-card-values">
        <span className="mono diff-va">{d.value_a}</span>
        <span className="dim">vs</span>
        <span className="mono diff-vb">{d.value_b}</span>
      </div>
      {d.detail && <div className="pane-hint">{d.detail}</div>}
    </div>
  );
}

/** §11.4: the two chains side by side, first differing node in magenta. */
function Chains({ report }: { report: DiffReport }) {
  if (report.why_error) {
    return (
      <section className="diff-section">
        <h3>Why, on both sides</h3>
        <div className="pane-hint">{report.why_error}</div>
      </section>
    );
  }
  if (!report.why_a || !report.why_b) return null;
  return (
    <section className="diff-section">
      <h3>Why, on both sides</h3>
      <div className="diff-chains">
        <Chain root={report.why_a} at={report.first_differing} label={report.a.name} />
        <Chain root={report.why_b} at={report.first_differing} label={report.b.name} />
      </div>
      {report.first_differing === null && (
        <div className="pane-hint">
          The two chains are identical — whatever caused this is upstream of both.
        </div>
      )}
    </section>
  );
}

function Chain({
  root,
  at,
  label,
}: {
  root: CausalNode;
  at: number | null;
  label: string;
}) {
  const rows: CausalNode[] = [];
  const seen = new Set<CausalNode>();
  let node: CausalNode | undefined = root;
  while (node && !seen.has(node)) {
    seen.add(node);
    rows.push(node);
    node = node.children.find((c) => c.is_primary_path && !seen.has(c));
  }
  return (
    <div className="diff-chain">
      <div className="diff-chain-head mono">{label}</div>
      {rows.map((n, i) => (
        <div
          key={`${n.signal}@${n.time}-${i}`}
          className={i === at ? "diff-node parted" : "diff-node"}
          onClick={() => useWave.getState().selectCausal(n)}
        >
          <span className="mono">{n.signal}</span>
          <span className="mono dim"> = {n.value}</span>
          <div className="diff-node-reason">{n.reason.replace(/_/g, " ")}</div>
        </div>
      ))}
    </div>
  );
}
