/**
 * TAB 8 — Transactions (§11.4b).
 *
 * *Someone working on an interconnect does not think in `awvalid`. They think
 * "write 128 took 22 cycles". Without this layer the tool is not useful to
 * them.* So the unit here is the transaction, and the signals are one click
 * away rather than the other way round.
 *
 * Three things carry the design:
 *
 * - **The Gantt shares Wave's time axis.** Not approximately: the same `view`
 *   from the same store, so a band and the handshake beneath it line up at
 *   every zoom level. §11.4b calls this essential, and it is the reason the
 *   bands are positioned from `view` rather than from their own scale.
 * - **Monochrome, with two exceptions.** §11.1 reserves colour for meaning:
 *   amber only when a transaction is in the current causal chain, magenta only
 *   when it broke a protocol rule. Everything else is greyscale.
 * - **Clicking a transaction reprograms Wave**, filtering to that interface's
 *   signals and jumping to its window — the interaction §11.4b specifies, and
 *   the one that makes the two views a single tool instead of two.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { cycleAt, formatTime } from "../lib/time";
import type { InterfaceReport, Transaction } from "../lib/types";
import { useWave } from "../state/store";

/** Rows in the Gantt before it starts scrolling internally. */
const BAND_H = 18;

export function TransactionsTab() {
  const report = useWave((s) => s.txn);
  const busy = useWave((s) => s.txnBusy);
  const error = useWave((s) => s.txnError);
  const load = useWave((s) => s.loadTransactions);
  const iface = useWave((s) => s.txnIface);
  const select = useWave((s) => s.selectIface);

  useEffect(() => {
    if (!report && !busy) void load();
  }, [report, busy, load]);

  if (busy && !report) return <div className="pane-note">Extracting transactions…</div>;
  if (error && !report) return <div className="pane-note">{error}</div>;
  if (!report) return <div className="pane-note">No transactions yet.</div>;

  if (!report.interfaces.length) {
    return (
      <div className="pane-note" data-testid="txn-empty">
        No protocol interfaces were detected.
        <div className="pane-hint">
          A pack matches a scope only when every signal it requires is in the dump.
          Check that the interface is dumped, or write a pack for it — one file,
          about thirty minutes.
        </div>
        {report.errors.map((e) => (
          <div className="pane-hint" key={e}>
            {e}
          </div>
        ))}
      </div>
    );
  }

  const current =
    report.interfaces.find((i) => i.interface.name === iface) ?? report.interfaces[0];

  return (
    <div className="txn" data-testid="transactions-tab">
      <div className="txn-head">
        <select
          className="txn-iface"
          value={current.interface.name}
          onChange={(e) => void select(e.target.value)}
          data-testid="txn-iface"
        >
          {report.interfaces.map((i) => (
            <option key={i.interface.name} value={i.interface.name}>
              {i.interface.name} — {i.interface.pack} ({i.n_transactions})
            </option>
          ))}
        </select>
        <Summary report={current} />
      </div>
      <Gantt />
      <FieldTable />
    </div>
  );
}

function Summary({ report }: { report: InterfaceReport }) {
  const i = report.interface;
  return (
    <span className="txn-summary" data-testid="txn-summary">
      <span className="dim">{i.scope}</span>
      <Dot />
      <span data-testid="txn-count">{report.n_transactions} transactions</span>
      <Dot />
      {/* §8.14's acceptance criterion: the correlation rate is always on screen,
          never implied by the absence of a complaint. */}
      <span data-testid="txn-correlation">
        {report.correlation === null
          ? "no channel activity"
          : `${report.correlation}% of ${report.n_events} events correlated`}
      </span>
      {report.n_open > 0 && (
        <>
          <Dot />
          <span className="warn">{report.n_open} never completed</span>
        </>
      )}
      {report.violations.length > 0 && (
        <>
          <Dot />
          <span className="warn">{report.violations.length} rule violations</span>
        </>
      )}
    </span>
  );
}

const Dot = () => <span className="dot">·</span>;

/**
 * The Gantt: one band per transaction, on Wave's time axis.
 *
 * Subscribed imperatively for the same reason the canvas is (see the store's
 * header): `view` changes on every frame of a pan, and putting a React render
 * on that path would cost the frame budget.
 */
function Gantt() {
  const rows = useWave((s) => s.txnRows);
  const selected = useWave((s) => s.txnSelected);
  const open = useWave((s) => s.openTransaction);
  const causal = useWave((s) => s.causal);
  const ref = useRef<HTMLDivElement>(null);

  /** Refs of transactions named anywhere in the current causal chain (§11.4b). */
  const inChain = useMemo(() => {
    const out = new Set<string>();
    const walk = (n: { txn: string | null; children: unknown[] }) => {
      if (n.txn) out.add(n.txn);
      for (const c of n.children as typeof n[]) walk(c);
    };
    if (causal) walk(causal.root as never);
    return out;
  }, [causal]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const paint = () => {
      const { view } = useWave.getState();
      const span = Math.max(1, view.t1 - view.t0);
      for (const child of Array.from(el.children) as HTMLElement[]) {
        const t0 = Number(child.dataset.t0);
        const t1 = Number(child.dataset.t1);
        // Zoomed in, most transactions are far outside the window. Leaving them
        // in the layout at left:-8000% costs a reflow per band and leaves
        // buttons that look present and cannot be reached.
        if (t1 < view.t0 || t0 > view.t1) {
          child.style.display = "none";
          continue;
        }
        child.style.display = "";
        child.style.left = `${((t0 - view.t0) / span) * 100}%`;
        child.style.width = `${Math.max(0.4, ((t1 - t0) / span) * 100)}%`;
      }
    };
    paint();
    return useWave.subscribe(paint);
  }, [rows]);

  if (!rows.length) return <div className="pane-note">No transactions on this interface.</div>;

  return (
    <div className="txn-gantt" data-testid="txn-gantt" style={{ height: rows.length * BAND_H }}>
      <div className="txn-bands" ref={ref}>
        {rows.map((t, i) => {
          const end = t.end_time ?? t.start_time;
          const cls = [
            "txn-band",
            t.status === "open" ? "open" : "",
            t.violations.length ? "violated" : "",
            inChain.has(t.ref) ? "causal" : "",
            selected === t.ref ? "on" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <button
              key={t.ref}
              className={cls}
              style={{ top: i * BAND_H }}
              data-t0={t.start_time}
              data-t1={end}
              data-testid={`txn-band-${t.ref}`}
              title={`${t.kind}#${t.index} ${t.status}`}
              onClick={() => open(t)}
            >
              <span className="txn-band-label">
                {t.kind}#{t.index}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** Every field and metric, sortable — the lower half of §11.4b's TAB 8. */
function FieldTable() {
  const rows = useWave((s) => s.txnRows);
  const selected = useWave((s) => s.txnSelected);
  const open = useWave((s) => s.openTransaction);
  const runWhy = useWave((s) => s.runQueryText);
  const setTab = useWave((s) => s.setTab);
  const period = useWave((s) => s.clockPeriod);
  const origin = useWave((s) => s.clockOrigin);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: "", desc: false });

  const columns = useMemo(() => keysOf(rows), [rows]);
  const sorted = useMemo(() => {
    if (!sort.key) return rows;
    const val = (t: Transaction) => cellValue(t, sort.key);
    return [...rows].sort((a, b) => {
      const x = val(a);
      const y = val(b);
      if (x === y) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (x < y ? -1 : 1) * (sort.desc ? -1 : 1);
    });
  }, [rows, sort]);

  // Cycles when a clock is known, raw time otherwise — §5.5's rule that a cycle
  // number is meaningless without one, applied here too.
  const at = (t: number | null) =>
    t === null ? "—" : period ? `c${cycleAt(t, period, origin)}` : formatTime(t, timescale);

  if (!rows.length) return null;

  return (
    <div className="txn-table-wrap">
      <table className="txn-table" data-testid="txn-table">
        <thead>
          <tr>
            {["", "start", "end", ...columns].map((c) => (
              <th
                key={c || "ref"}
                onClick={() => c && setSort({ key: c, desc: sort.key === c && !sort.desc })}
                className={c ? "sortable" : ""}
                data-testid={`txn-th-${c || "ref"}`}
              >
                {c || "transaction"}
                {/* The arrow is its own element so the header's text stays the
                    column name — sorting must not rename the thing it sorts. */}
                {sort.key === c && c && <span className="sort-arrow">{sort.desc ? "▾" : "▴"}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((t) => (
            <tr
              key={t.ref}
              className={selected === t.ref ? "on" : ""}
              data-testid={`txn-row-${t.ref}`}
              onClick={() => open(t)}
            >
              <td className="txn-ref">
                {t.kind}#{t.index}
                {t.status === "open" && (
                  <button
                    className="link"
                    title="why was this never completed? (§8.16)"
                    data-testid={`txn-why-${t.ref}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      setTab(2);
                      void runWhy(`why(txn.${t.ref}.not_completed)`);
                    }}
                  >
                    why
                  </button>
                )}
              </td>
              <td>{at(t.start_time)}</td>
              <td>{t.end_time === null ? "OPEN" : at(t.end_time)}</td>
              {columns.map((c) => (
                <td key={c}>{format(cellValue(t, c), c)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Union of field and metric names, in first-seen order. */
function keysOf(rows: Transaction[]): string[] {
  const out: string[] = [];
  for (const t of rows) {
    for (const k of [...Object.keys(t.fields), ...Object.keys(t.metrics)]) {
      if (!out.includes(k)) out.push(k);
    }
  }
  return out;
}

function cellValue(t: Transaction, key: string): number | string | null {
  if (key === "start") return t.start_time;
  if (key === "end") return t.end_time;
  return t.fields[key] ?? t.metrics[key] ?? null;
}

function format(v: number | string | null, key: string): string {
  if (v === null) return "—";
  if (typeof v === "number" && key.toLowerCase().includes("addr")) {
    return `0x${v.toString(16)}`;
  }
  return String(v);
}
