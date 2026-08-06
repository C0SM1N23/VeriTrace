/**
 * TAB 9 — Performance (§11.4b).
 *
 * *Answers "why is it slow?" with attributed numbers, not impressions.* And the
 * one tab you open when you do **not** have a bug — which is why it has to be
 * readable at a glance rather than complete at a squint.
 *
 * Four decisions carry it:
 *
 * - **The stall chart is stacked to exactly 100%.** §8.17 makes that the claim,
 *   so the total is printed next to it: a reader can check the tool rather than
 *   trust it. `other` is drawn in a colour that means "the pack has a gap",
 *   because a large `other` is a finding about the *pack*, not the design.
 * - **Colour means one thing (§11.1).** Greyscale for the buckets that are
 *   normal, amber for the ones that cost throughput, magenta for `other`, red
 *   for a deadlock. Nothing is coloured for decoration.
 * - **Selecting a window in any chart narrows all of them and moves Wave.**
 *   That is §11.4b's interaction, and it is what makes this a tab rather than a
 *   report.
 * - **Everything drawn is clickable to something.** A latency outlier opens its
 *   transaction; a deadlock edge runs its `why`. A chart you cannot leave is a
 *   dead end.
 *
 * Plain SVG, no chart library: four chart types, each a handful of rects, and a
 * dependency would cost more than it saves (§4.1's economy applied to the
 * frontend).
 */

import { useEffect, useMemo, useState } from "react";
import { cycleAt, formatTime } from "../lib/time";
import type { Deadlock, InterfacePerf, StallProfile, PerfSeries, WaitEdge } from "../lib/types";
import { useWave } from "../state/store";

/** §11.1: colour is reserved for meaning. */
const BUCKET_CLASS: Record<string, string> = {
  transfer: "ok",
  reset: "muted",
  idle: "muted",
  other: "gap",
};

export function PerformanceTab() {
  const report = useWave((s) => s.perf);
  const busy = useWave((s) => s.perfBusy);
  const error = useWave((s) => s.perfError);
  const load = useWave((s) => s.loadPerformance);
  const iface = useWave((s) => s.perfIface);
  const select = useWave((s) => s.selectPerfIface);

  useEffect(() => {
    if (!report && !busy) void load();
  }, [report, busy, load]);

  if (busy && !report) return <div className="pane-note">Measuring…</div>;
  if (error && !report) return <div className="pane-note">{error}</div>;
  if (!report) return <div className="pane-note">No performance data yet.</div>;

  if (!report.interfaces.length) {
    return (
      <div className="pane-note" data-testid="perf-empty">
        No protocol interfaces were detected, so there is nothing to measure.
        <div className="pane-hint">
          Performance is measured per interface: without a pack that matches, there
          are no transactions to aggregate and no agents to look for deadlocks
          between.
        </div>
        {report.errors.map((e) => (
          <div className="pane-hint" key={e}>
            {e}
          </div>
        ))}
      </div>
    );
  }

  const current = report.interfaces.find((i) => i.iface === iface) ?? report.interfaces[0];

  return (
    <div className="perf" data-testid="performance-tab">
      <div className="perf-head">
        <select
          className="txn-iface"
          value={current.iface}
          onChange={(e) => select(e.target.value)}
          data-testid="perf-iface"
        >
          {report.interfaces.map((i) => (
            <option key={i.iface} value={i.iface}>
              {i.iface}
            </option>
          ))}
        </select>
        <WindowChip />
      </div>

      <Liveness />
      <Stalls per={current} />
      <div className="perf-row">
        <Latency per={current} />
        <Outstanding per={current} />
      </div>
      <FairnessBars />
    </div>
  );
}

/** The current time filter, and the way back out of it. */
function WindowChip() {
  const win = useWave((s) => s.perfWindow);
  const setWindow = useWave((s) => s.setPerfWindow);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  if (!win) return <span className="txn-summary dim">whole run</span>;
  return (
    <span className="txn-summary">
      <button className="chip on" onClick={() => setWindow(null)} data-testid="perf-window-clear">
        {formatTime(win.t0, timescale)}–{formatTime(win.t1, timescale)} ✕
      </button>
    </span>
  );
}

// --- §8.18 ------------------------------------------------------------------

/**
 * Deadlock, livelock and starvation, above the charts.
 *
 * Above deliberately: if the bus stopped, the reason it was slow is not the
 * question any more, and burying that under four charts would be an ordering
 * that flatters the charts rather than helping the reader.
 */
function Liveness() {
  const report = useWave((s) => s.perf);
  const live = report?.liveness;
  if (!live) return null;
  if (!live.deadlocks.length && !live.livelocks.length && !live.starvation.length) {
    return (
      <div className="perf-live clear" data-testid="perf-liveness">
        <span aria-hidden>✓</span> No deadlock, livelock or starvation found.
        {Object.entries(live.skipped).map(([k, v]) => (
          <span className="pane-hint" key={k}>
            {k}: {v}
          </span>
        ))}
      </div>
    );
  }
  return (
    <div className="perf-live" data-testid="perf-liveness">
      {live.deadlocks.map((d) => (
        <DeadlockCard key={`${d.at}-${d.agents.join()}`} d={d} />
      ))}
      {live.starvation.map((s) => (
        <div className="perf-finding warn" key={s.agent} data-testid="perf-starvation">
          <b>STARVATION</b> {s.agent} asked for {s.cycles} cycles without being served,
          while {s.served.join(", ")} progressed.
        </div>
      ))}
      {live.livelocks.map((l) => (
        <div className="perf-finding warn" key={l.signal} data-testid="perf-livelock">
          <b>LIVELOCK</b> <code>{l.signal}</code> changed {l.toggles} times between{" "}
          {l.states.length} states with nothing completing.
        </div>
      ))}
    </div>
  );
}

/** §8.18's report, as the spec writes it: the chain, then the first casualty. */
function DeadlockCard({ d }: { d: Deadlock }) {
  const runWhy = useWave((s) => s.runQueryText);
  const setTab = useWave((s) => s.setTab);
  const period = useWave((s) => s.clockPeriod);
  const origin = useWave((s) => s.clockOrigin);
  const at = period ? `c${cycleAt(d.at, period, origin)}` : String(d.at);

  const why = (e: WaitEdge) => {
    setTab(2);
    void runWhy(e.why);
  };

  return (
    <div className="perf-finding error" data-testid="perf-deadlock">
      <div className="perf-finding-title">
        <b>DEADLOCK</b> detected at {at}, persistent {d.cycles} cycles
      </div>
      <table className="perf-chain">
        <tbody>
          {d.edges.map((e) => (
            <tr key={`${e.agent}-${e.resource}`} data-testid="perf-wait-edge" data-agent={e.agent}>
              <td className="perf-agent">{e.agent}</td>
              <td className="dim">waits on</td>
              <td>
                <code>{e.resource}</code>
              </td>
              <td className="dim">held by</td>
              <td className="perf-agent">{e.holder}</td>
              <td>
                {/* §8.18: "[Why on each link]". */}
                <button className="link" onClick={() => why(e)} data-testid="perf-why-edge">
                  why
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="perf-finding-foot">
        Cycle of {d.agents.length} agents. First blocked:{" "}
        <b>{d.first_txn ?? d.first_blocked}</b>
      </div>
    </div>
  );
}

// --- §8.17 ------------------------------------------------------------------

function windowed(prof: StallProfile, win: { t0: number; t1: number } | null) {
  const series = win ? prof.series.filter((s) => s.t1 >= win.t0 && s.t0 <= win.t1) : prof.series;
  const counts: Record<string, number> = Object.fromEntries(prof.buckets.map((b) => [b, 0]));
  let total = 0;
  for (const s of series) {
    for (const b of prof.buckets) counts[b] += s.counts[b] ?? 0;
    total += s.cycles;
  }
  return { series, counts, total };
}

/**
 * The stacked area of §8.17 — the most valuable thing in the section.
 *
 * Drawn as one column per window rather than as a smoothed area: each column is
 * a real count of real cycles, and interpolating between them would draw a
 * curve through numbers nobody measured.
 */
function Stalls({ per }: { per: InterfacePerf }) {
  const win = useWave((s) => s.perfWindow);
  const setWindow = useWave((s) => s.setPerfWindow);
  const prof = per.stalls;
  const [drag, setDrag] = useState<{ from: number; to: number } | null>(null);

  const view = useMemo(() => (prof ? windowed(prof, win) : null), [prof, win]);

  if (!prof || !view) {
    return (
      <div className="perf-card" data-testid="perf-stalls">
        <div className="perf-card-title">Stall attribution</div>
        <div className="pane-note">
          {per.notes.perf ?? "no per-cycle profile for this interface"}
        </div>
      </div>
    );
  }

  const { series, counts, total } = view;
  // Recomputed for the selected window rather than reused from the server: the
  // shares have to add to 100% of *what is on screen*, or selecting a window
  // would quietly break the one property this chart promises.
  const shares = shareOf(counts, total, prof.buckets);
  const sum = Object.values(shares).reduce((a, b) => a + b, 0);

  const w = 1000;
  const h = 120;
  const cw = series.length ? w / series.length : w;

  const onDown = (i: number) => setDrag({ from: i, to: i });
  const onUp = () => {
    if (drag && series.length) {
      const [a, b] = [Math.min(drag.from, drag.to), Math.max(drag.from, drag.to)];
      setWindow(a === b ? null : { t0: series[a].t0, t1: series[b].t1 });
    }
    setDrag(null);
  };

  return (
    <div className="perf-card" data-testid="perf-stalls">
      <div className="perf-card-title">
        Stall attribution
        <span className="dim">
          {" "}
          — {total} cycles, {prof.lost} lost
        </span>
        {/* §8.17's claim, printed so it can be checked rather than trusted. */}
        <span className="perf-total" data-testid="perf-stall-total">
          {sum.toFixed(1)}%
        </span>
      </div>
      <svg
        className="perf-chart"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        data-testid="perf-stall-chart"
        onMouseUp={onUp}
        onMouseLeave={() => setDrag(null)}
      >
        {series.map((s, i) => {
          let y = 0;
          return (
            <g
              key={s.t0}
              onMouseDown={() => onDown(i)}
              onMouseEnter={() => drag && setDrag({ ...drag, to: i })}
            >
              {prof.buckets.map((b) => {
                const n = s.counts[b] ?? 0;
                if (!n || !s.cycles) return null;
                const bh = (n / s.cycles) * h;
                const top = y;
                y += bh;
                return (
                  <rect
                    key={b}
                    x={i * cw}
                    y={top}
                    width={cw + 0.5}
                    height={bh}
                    className={`bucket ${BUCKET_CLASS[b] ?? "cost"}`}
                    data-bucket={b}
                  >
                    <title>{`${b}: ${n} of ${s.cycles} cycles`}</title>
                  </rect>
                );
              })}
              {drag && i >= Math.min(drag.from, drag.to) && i <= Math.max(drag.from, drag.to) && (
                <rect x={i * cw} y={0} width={cw + 0.5} height={h} className="perf-drag" />
              )}
            </g>
          );
        })}
      </svg>
      <table className="perf-legend" data-testid="perf-legend">
        <tbody>
          {prof.buckets.map((b) => (
            <tr key={b} data-testid={`perf-bucket-${b}`}>
              <td>
                <span className={`swatch ${BUCKET_CLASS[b] ?? "cost"}`} />
                {b}
              </td>
              <td className="num">{shares[b].toFixed(1)}%</td>
              <td className="num dim">{counts[b]}</td>
              <td className="dim">{prof.reasons[b] ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {Object.entries(prof.notes).map(([k, v]) => (
        <div className="pane-hint" key={k}>
          {k} was not evaluated: {v}
        </div>
      ))}
    </div>
  );
}

/** Largest-remainder, matching the server so the two never disagree by 0.1. */
function shareOf(counts: Record<string, number>, total: number, buckets: string[]) {
  const out: Record<string, number> = {};
  if (!total) {
    for (const b of buckets) out[b] = 0;
    return out;
  }
  const exact = buckets.map((b) => ((counts[b] ?? 0) * 1000) / total);
  const floors = exact.map((v) => Math.floor(v));
  let left = 1000 - floors.reduce((a, b) => a + b, 0);
  const order = buckets
    .map((_bucket, i) => ({ i, frac: exact[i] - floors[i] }))
    .sort((a, b) => b.frac - a.frac || a.i - b.i);
  for (const { i } of order) {
    if (left-- <= 0) break;
    floors[i] += 1;
  }
  buckets.forEach((b, i) => (out[b] = floors[i] / 10));
  return out;
}

/** Latency histogram with p50/p95/p99 marked, and a clickable tail (§8.17). */
function Latency({ per }: { per: InterfacePerf }) {
  const openRef = useWave((s) => s.openTransactionRef);
  const h = per.latency;
  if (!h || !h.n) {
    return (
      <div className="perf-card" data-testid="perf-latency">
        <div className="perf-card-title">Latency</div>
        <div className="pane-note">No completed transactions on this interface.</div>
      </div>
    );
  }
  const peak = Math.max(...h.bins.map((bin) => bin.n), 1);
  return (
    <div className="perf-card" data-testid="perf-latency">
      <div className="perf-card-title">
        Latency <span className="dim">— {h.n} transactions, {h.unit}</span>
      </div>
      <div className="perf-hist">
        {h.bins.map((b) => (
          <div
            className="perf-bin"
            key={b.lo}
            style={{ height: `${(b.n / peak) * 100}%` }}
            title={`${b.lo}–${b.hi} ${h.unit}: ${b.n}`}
            data-testid="perf-latency-bin"
          />
        ))}
      </div>
      <div className="perf-percentiles" data-testid="perf-percentiles">
        <span>min {h.min}</span>
        <span>p50 {h.p50}</span>
        <span>p95 {h.p95}</span>
        <span className="warn">p99 {h.p99}</span>
        <span className="warn">max {h.max}</span>
      </div>
      <div className="perf-outliers">
        <span className="dim">slowest:</span>
        {h.outliers.slice(0, 5).map((o) => (
          <button
            key={o.ref}
            className="link"
            data-testid="perf-outlier"
            // §8.17: the tail is clickable and lands on the transaction itself.
            onClick={() => void openRef(o.ref)}
          >
            {o.ref.split(".").pop()} ({o.value})
          </button>
        ))}
      </div>
    </div>
  );
}

/** Outstanding over time. A plateau is the finding, so it is labelled. */
function Outstanding({ per }: { per: InterfacePerf }) {
  const s = per.outstanding;
  return (
    <div className="perf-card" data-testid="perf-outstanding">
      <div className="perf-card-title">
        Outstanding
        <span className="dim"> — peak {per.outstanding_peak}</span>
        {per.outstanding_plateau && (
          <span className="warn" data-testid="perf-plateau">
            {" "}
            plateau: this is the limit
          </span>
        )}
      </div>
      {s && s.points.length ? <Sparkline s={s} /> : <div className="pane-note">No data.</div>}
      <div className="perf-percentiles">
        {per.bytes_moved > 0 && <span>{per.bytes_moved} bytes</span>}
        {per.burst_efficiency !== null && (
          <span>burst efficiency {per.burst_efficiency.toFixed(2)}</span>
        )}
      </div>
    </div>
  );
}

function Sparkline({ s }: { s: PerfSeries }) {
  const max = Math.max(...s.points.map((p) => p.v), 1);
  const w = 1000;
  const h = 60;
  const dx = s.points.length > 1 ? w / (s.points.length - 1) : w;
  const d = s.points
    .map((p, i) => `${i === 0 ? "M" : "L"}${i * dx},${h - (p.v / max) * h}`)
    .join(" ");
  return (
    <svg className="perf-chart" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <path d={d} className="perf-line" />
    </svg>
  );
}

/** Bandwidth obtained vs asked for, per master, plus Jain (§8.17). */
function FairnessBars() {
  const fair = useWave((s) => s.perf?.fairness ?? null);
  if (!fair || fair.agents.length < 2) return null;
  const peak = Math.max(...fair.agents.map((a) => a.requested), 1);
  return (
    <div className="perf-card" data-testid="perf-fairness">
      <div className="perf-card-title">
        Fairness
        <span className="dim"> — Jain index </span>
        <b data-testid="perf-jain">{fair.index.toFixed(3)}</b>
        {fair.worst && (
          <span className="warn">
            {" "}
            · longest unserved: {fair.worst} ({fair.worst_cycles} cycles)
          </span>
        )}
      </div>
      <table className="perf-legend">
        <tbody>
          {fair.agents.map((a) => (
            <tr key={a.agent} data-testid="perf-agent">
              <td>{a.agent}</td>
              <td className="perf-barcell">
                <span className="perf-bar req" style={{ width: `${(a.requested / peak) * 100}%` }} />
                <span
                  className="perf-bar got"
                  style={{ width: `${(a.granted / peak) * 100}%` }}
                />
              </td>
              <td className="num dim">
                {a.granted}/{a.requested}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
