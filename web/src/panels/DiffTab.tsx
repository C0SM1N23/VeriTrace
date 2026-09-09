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

import { useEffect, useId, useMemo } from "react";
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
  const anchor = useWave((s) => s.diffAnchor);
  const marksA = useWave((s) => s.diffMarksA);
  const marksB = useWave((s) => s.diffMarksB);
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
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
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

      {strategy === "manual" && <div className="diff-head">
        <label>Anchors A (trace ticks) <input aria-label="Anchors A" value={marksA}
          onChange={(e) => useWave.getState().setDiffAnchors({ diffMarksA: e.target.value })} /></label>
        <label>Anchors B (trace ticks) <input aria-label="Anchors B" value={marksB}
          onChange={(e) => useWave.getState().setDiffAnchors({ diffMarksB: e.target.value })} /></label>
        <span className="pane-hint">Paired in order. Separate ticks by commas; use each trace's own time unit.</span>
      </div>}
      {strategy === "retire" && <label>Retire signal <input aria-label="Retire signal" value={anchor}
        placeholder="auto-detect, or full signal path"
        onChange={(e) => useWave.getState().setDiffAnchors({ diffAnchor: e.target.value })} /></label>}

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

      {/* The other tabs arrive with their answer already computed; this one
          cannot, because it does not know which run to compare against until
          asked. Saying so beats an empty pane that looks like a tab that failed
          to load. */}
      {sessions.length >= 2 && !report && !busy && !error && (
        <div className="pane-note">
          Pick the run to compare against, then <b>compare</b>.
          <div className="pane-hint">
            Both runs are normalised to femtoseconds and aligned on the anchor chosen
            above — clock edges, completed handshakes or retired instructions — so
            latency that differs between runs does not read as a divergence (§8.7).
          </div>
        </div>
      )}

      {busy && <div className="pane-note">Aligning and comparing…</div>}
      {error && <div className="pane-note error" role="alert">{error}</div>}

      {report && !busy && (
        <div className="diff-body">
          <Alignment report={report} />
          <button className="chip" onClick={() => exportReport(report)}>Export regression JSON</button>
          {current ? (
            <FirstDivergence d={current} index={index} total={report.divergences.length} strategy={strategy} />
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
                  <span className="dim">{position(d.at, strategy)}</span>
                  <span className="mono">{d.value_a}</span>
                  <span className="dim">vs</span>
                  <span className="mono">{d.value_b}</span>
                  {d.detail && <span className="pane-hint">{d.detail}</span>}
                </div>
              ))}
            </section>
          )}

          <Chains report={report} />
          <WaveOverlay report={report} />

          <section className="diff-section">
            <h3>Diverging signals</h3>
            <ol className="diff-list">
              {report.divergences.map((d, i) => (
                <li
                  key={d.signal}
                  className={i === index ? "diff-row on" : "diff-row"}
                  onClick={() => useWave.getState().gotoDivergence(i)}
                >
                  <span className="diff-at mono">{position(d.at, strategy)}</span>
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
  strategy,
}: {
  d: Divergence;
  index: number;
  total: number;
  strategy: string;
}) {
  return (
    <div className="diff-card" data-testid="diff-first">
      <div className="diff-card-head">
        <span className="diff-card-title">
          {index === 0 ? "First divergence" : `Divergence ${index + 1}`} at {position(d.at, strategy)}
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
        <Chain root={report.why_a} at={report.first_differing} label={report.a.name} session={report.a.session_id} />
        <Chain root={report.why_b} at={report.first_differing} label={report.b.name} session={report.b.session_id} />
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
  session,
}: {
  root: CausalNode;
  at: number | null;
  label: string;
  session: string;
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
          title="Open this signal and time in its own run"
          role="button"
          tabIndex={0}
          onClick={() => openNode(n, session)}
          onKeyDown={(e) => { if (e.key === "Enter") openNode(n, session); }}
        >
          <span className="mono">{n.signal}</span>
          <span className="mono dim"> = {n.value}</span>
          <div className="diff-node-reason">{n.reason.replace(/_/g, " ")}</div>
        </div>
      ))}
    </div>
  );
}

function position(at: number, strategy: string) {
  return `${strategy === "cycle" ? "c" : "anchor "}${Number(at.toFixed(6))}`;
}

function openNode(node: CausalNode, session: string) {
  if (session === useWave.getState().session) {
    useWave.getState().selectCausal(node);
    useWave.getState().setTab(3);
  } else {
    // Never interpret B's timestamp, value or source location in A's backend.
    const url = new URL(location.href);
    url.search = new URLSearchParams({ session, signal: node.signal, time: String(node.time), tab: "3" }).toString();
    if (node.loc) {
      url.searchParams.set("file", node.loc.file);
      url.searchParams.set("line", String(node.loc.line));
    }
    window.open(url.href, "_blank", "noopener");
  }
}

function exportReport(report: DiffReport) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "veritrace-regression.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Both native event streams projected by the backend onto the same anchor axis. */
function WaveOverlay({ report }: { report: DiffReport }) {
  const hatch = useId().replace(/:/g, "");
  const wave = report.wave;
  if (!wave) return null;
  const x = (at: number) => 16 + 768 * (at - wave.start) / (wave.end - wave.start || 1);
  const digital = wave.segments.every((s) => s.a.length === 1 && s.b.length === 1);
  const y = (value: string) => value === "1" ? 24 : value === "0" ? 68 : 46;
  const path = (side: "a" | "b") => wave.segments.map((s, i) =>
    `${i ? "L" : "M"}${x(s.start)},${y(s[side])}H${x(s.end)}`).join(" ");
  return <section className="diff-section" data-testid="diff-wave" data-signal={wave.signal}>
    <h3>Aligned wave — <span className="mono">{wave.signal}</span></h3>
    <div className="pane-hint"><span className="diff-va">A: {report.a.name} (solid)</span> · {" "}
      <span className="diff-vb">B: {report.b.name} (dashed)</span> · hatched = different · click to inspect A</div>
    <svg viewBox="0 0 800 112" role="img" aria-label={`Aligned wave for ${wave.signal}`} className="diff-wave-svg">
      <defs><pattern id={hatch} width="8" height="8" patternUnits="userSpaceOnUse">
        <path d="M-2,2L2,-2M0,8L8,0M6,10L10,6" stroke="var(--diff)" opacity="0.4" />
      </pattern></defs>
      {wave.segments.map((s, i) => <g key={i} data-different={s.different} data-time-a={s.time_a}
        onClick={() => {
          const state = useWave.getState();
          const sig = state.signals.find((v) => v.path === wave.signal || v.path.endsWith(`.${wave.signal}`));
          state.jumpTo(s.time_a, sig ? [sig.path] : []);
        }}>
        <rect x={x(s.start)} y="12" width={Math.max(0, x(s.end) - x(s.start))} height="76"
          fill={s.different ? `url(#${hatch})` : "transparent"} />
        <title>{`A @${s.time_a}: ${s.a}; B @${s.time_b}: ${s.b}`}</title>
        {!digital && <>
          <path d={`M${x(s.start)},22H${x(s.end)}M${x(s.start)},44H${x(s.end)}`} stroke="var(--sig-1)" />
          <path d={`M${x(s.start)},52H${x(s.end)}M${x(s.start)},74H${x(s.end)}`} stroke="var(--diff)" strokeDasharray="5 3" />
          {x(s.end) - x(s.start) > 35 && <>
            <text x={x(s.start) + 3} y="38" style={{ fill: "var(--sig-1)" }}>{s.a}</text>
            <text x={x(s.start) + 3} y="68" style={{ fill: "var(--diff)" }}>{s.b}</text>
          </>}
        </>}
      </g>)}
      {digital && <g fill="none" strokeWidth="2" pointerEvents="none">
        <path d={path("a")} stroke="var(--sig-1)" />
        <path d={path("b")} stroke="var(--diff)" strokeDasharray="5 3" />
      </g>}
      <text x="16" y="106">{position(wave.start, report.alignment.strategy)}</text>
      <text x="784" y="106" textAnchor="end">{position(wave.end, report.alignment.strategy)}</text>
    </svg>
    {!wave.segments.length && <div className="pane-hint">Only a single observed instant overlaps; there is no interval to draw.</div>}
  </section>;
}
