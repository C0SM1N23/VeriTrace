/**
 * TAB 7 — Coverage, code and functional, unified (§11.4, §8.21, §8.12).
 *
 * *Aveam doua sisteme paralele care raspundeau la aceeasi intrebare — „ce n-am
 * testat". Un tab, doua sectiuni.* So this is one page: the functional matrix
 * derived from transactions, and the code coverage imported from Verilator or
 * Vivado, with the same loop closing both — *find the hole, learn what closes
 * it, write the test, run, back to Wave*.
 *
 * Four decisions carry it:
 *
 * - **An empty cell is drawn, not omitted.** The whole feature is the cell that
 *   was never reached; a matrix built only from what happened cannot show one.
 * - **Colour is reserved for causality (§11.1)**, so coverage does not get the
 *   accent. Reached cells are foreground, unreached are an outlined void, and
 *   the one accent on the page is the hole currently expanded.
 * - **A skipped point reads differently from a point at 0%.** One is a gap in
 *   the test, the other a gap in the pack, and merging them would make the
 *   percentage a lie (P1).
 * - **Clicking an uncovered line opens Source there**, with §8.12's derived
 *   conditions beside it — the "copy the conditions" affordance §11.4 asks for
 *   is the block of text itself, selectable as one unit.
 */

import { useEffect, useMemo, useState } from "react";
import type {
  CoverageReport,
  CoverPoint,
  FunctionalCoverage,
  Hole,
  TestPlan,
} from "../lib/types";
import { useWave } from "../state/store";

export function CoverageTab() {
  const report = useWave((s) => s.coverage);
  const busy = useWave((s) => s.coverageBusy);
  const error = useWave((s) => s.coverageError);
  const load = useWave((s) => s.loadCoverage);
  const iface = useWave((s) => s.covIface);
  const select = useWave((s) => s.selectCovIface);

  useEffect(() => {
    if (!report && !busy && !error) void load();
  }, [report, busy, error, load]);

  if (busy && !report) return <div className="pane-note">Measuring coverage…</div>;
  if (error && !report) return <div className="pane-note" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>;
  if (!report) return <div className="pane-note">No coverage yet.</div>;

  const current =
    report.functional.find((f) => f.iface === iface) ?? report.functional[0] ?? null;

  return (
    <div className="cov" data-testid="coverage-tab">
      <div className="cov-sections">
        <section className="cov-section" data-testid="cov-functional">
          <header className="cov-head">
            <h2>Functional</h2>
            <span className="cov-sub">from transactions — no covergroup written (§8.21)</span>
            {report.functional.length > 1 && (
              <select
                className="txn-iface"
                value={current?.iface ?? ""}
                onChange={(e) => select(e.target.value)}
                data-testid="cov-iface"
              >
                {report.functional.map((f) => (
                  <option key={f.iface} value={f.iface}>
                    {f.iface} — {f.pack}
                  </option>
                ))}
              </select>
            )}
          </header>
          {current ? (
            <Functional cov={current} />
          ) : (
            <div className="pane-note" data-testid="cov-no-functional">
              {/* `skipped.functional` is not shown: §8.21 skips this section for
                  exactly one reason, and the CLI's phrasing of it is the sentence
                  above. Printing both read like the app saying it twice. */}
              No protocol interface was extracted, so there is nothing to derive
              functional coverage from.
            </div>
          )}
        </section>

        <section className="cov-section" data-testid="cov-code">
          <header className="cov-head">
            <h2>Code</h2>
            <span className="cov-sub">
              {report.code
                ? `imported from ${report.code.source} (§8.12)`
                : "imported from Verilator or Vivado (§8.12)"}
            </span>
          </header>
          <Code report={report} />
        </section>
      </div>
      <HoleList report={report} />
      {report.plan && <Plan plan={report.plan} />}
    </div>
  );
}

/**
 * §8.37 — the verification plan, cross-referenced.
 *
 * The column that matters is **evidence**, not status: `status` is what someone
 * wrote in the file and `evidence` is what this run can show. Where they differ
 * the row says so, because a plan that only repeats its own claims is a document,
 * not a measurement.
 */
function Plan({ plan }: { plan: TestPlan }) {
  return (
    <section className="cov-section cov-plan" data-testid="cov-plan">
      <header className="cov-head">
        <h2>Verification plan</h2>
        <span className="cov-sub">
          {/* Either separator: the path is produced by the server, which may be
              on Windows. */}
          {plan.path?.split(/[\\/]/).pop() ?? "testplan.toml"} — coverage of intent (§8.37)
        </span>
        {plan.score !== null && (
          <span className="cov-score" data-testid="cov-plan-score">
            {Math.round(plan.score)}%
          </span>
        )}
      </header>
      <table className="cov-plan-table">
        <tbody>
          {plan.items.map((item) => (
            <tr key={item.id} data-testid={`plan-${item.id}`}>
              <td className={`plan-state plan-${item.evidence}`}>{item.evidence}</td>
              <td className="plan-id">{item.id}</td>
              <td className="plan-desc">
                {item.desc}
                {item.status === "covered" && item.evidence !== "covered" && (
                  <div className="plan-drift">
                    the plan says covered; this run does not show it
                  </div>
                )}
                {item.note && <div className="plan-note">{item.note}</div>}
                {item.links.map((l) => (
                  <div className="plan-link" key={l.kind + l.ref}>
                    <span className={`plan-state plan-${l.state}`}>{l.state}</span>
                    <code>
                      {l.kind}:{l.ref}
                    </code>
                    <span className="plan-detail">{l.detail}</span>
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {plan.errors.map((e) => (
        <div className="pane-hint" key={e}>
          ! {e}
        </div>
      ))}
    </section>
  );
}

/** One interface's §8.21 points. */
function Functional({ cov }: { cov: FunctionalCoverage }) {
  return (
    <div className="cov-fn">
      <div className="cov-summary" data-testid="cov-summary">
        <Score value={cov.score} />
        <span className="cov-meta">
          {cov.n_transactions} transaction{cov.n_transactions === 1 ? "" : "s"} · {cov.pack}
        </span>
        {cov.automatic && (
          // A weaker claim than declared bins, so it says so rather than
          // letting the percentage imply more than it means.
          <span className="cov-note">
            automatic bins — this pack declares no <code>[[cover]]</code>
          </span>
        )}
      </div>
      {cov.points.map((p) => (
        <Point key={p.name} point={p} />
      ))}
    </div>
  );
}

function Point({ point }: { point: CoverPoint }) {
  if (point.skipped) {
    return (
      <div className="cov-point" data-testid={`cov-point-${point.name}`}>
        <div className="cov-point-head">
          <span className="cov-point-name">{point.name}</span>
          <span className="cov-point-skipped">not measured — {point.skipped}</span>
        </div>
      </div>
    );
  }
  return (
    <div className="cov-point" data-testid={`cov-point-${point.name}`}>
      <div className="cov-point-head">
        <span className="cov-point-name">{point.name}</span>
        <span className="cov-point-shape">{point.shape}</span>
        <span className="cov-point-count">
          {point.covered}/{point.total}
        </span>
        {point.msg && <span className="cov-point-msg">{point.msg}</span>}
      </div>
      {point.labels.length === 2 ? <Matrix point={point} /> : <Bins point={point} />}
    </div>
  );
}

function hitsOf(point: CoverPoint): Map<string, number> {
  const out = new Map<string, number>();
  for (const c of point.cells) out.set(c.key.join("\0"), c.hits);
  return out;
}

/** A one-axis point: every bin, hit or not. */
function Bins({ point }: { point: CoverPoint }) {
  const hits = useMemo(() => hitsOf(point), [point]);
  const labels = point.labels[0] ?? [];
  return (
    <div className="cov-bins">
      {labels.map((label) => {
        const n = hits.get(label) ?? 0;
        return (
          <div
            key={label}
            className={`cov-bin${n ? " on" : ""}`}
            title={n ? `${label}: ${n}` : `${label}: never`}
            data-testid={`cov-bin-${point.name}-${label}`}
            data-hits={n}
          >
            <span className="cov-bin-label">{label}</span>
            <span className="cov-bin-hits">{n || "—"}</span>
          </div>
        );
      })}
    </div>
  );
}

/** A cross or a sequence: rows x columns, empty cells visible. */
function Matrix({ point }: { point: CoverPoint }) {
  const hits = useMemo(() => hitsOf(point), [point]);
  const [rows, cols] = point.labels;
  return (
    <div className="cov-matrix-wrap">
      <table className="cov-matrix" data-testid={`cov-matrix-${point.name}`}>
        <thead>
          <tr>
            <th className="cov-axis">{point.axes[0]}</th>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r}>
              <th className="cov-axis">{r}</th>
              {cols.map((c) => {
                const n = hits.get(`${r}\0${c}`) ?? 0;
                return (
                  <td
                    key={c}
                    className={n ? "on" : ""}
                    data-hits={n}
                    data-testid={`cov-cell-${point.name}-${r}-${c}`}
                    title={n ? `${r} -> ${c}: ${n}` : `${r} -> ${c}: never`}
                  >
                    {n || ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The §8.12 half: imported totals, per-file heatmap, and the derived holes. */
function Code({ report }: { report: CoverageReport }) {
  const code = report.code;

  if (!code) {
    return (
      <div className="pane-note" data-testid="cov-no-code">
        {/* Two different situations, and only one of them is normal: no database
            configured at all, or one that was found and would not parse. The
            second is a problem to fix, so it leads. */}
        {report.code_error ? (
          <>
            The coverage database could not be read.
            <div className="pane-hint">{report.code_error}</div>
          </>
        ) : (
          <>
            No coverage database was found.
            <div className="pane-hint">
              Run <code>verilator --coverage --coverage-line</code> and point at{" "}
              <code>logs/coverage.dat</code>, or <code>xcrg -report_format xml</code> for
              Vivado. Both are read; neither is required.
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="cov-code">
      <div className="cov-summary">
        <Score value={code.score} />
        <span className="cov-meta">
          {code.covered}/{code.total} points · {code.source}
        </span>
      </div>
      {code.error && <div className="pane-hint">{code.error}</div>}

      <div className="cov-files">
        {code.files.map((f) => (
          <div className="cov-file" key={f.file} data-testid={`cov-file-${f.file}`}>
            <span className="cov-file-name">{f.file}</span>
            <span className="cov-file-bar">
              <span
                className="cov-file-fill"
                style={{ width: `${Math.round((f.score ?? 0) * 100)}%` }}
              />
            </span>
            <span className="cov-file-count">
              {f.covered}/{f.total}
            </span>
          </div>
        ))}
      </div>

    </div>
  );
}

/** FSM-derived holes remain useful when no code coverage database exists. */
function HoleList({ report }: { report: CoverageReport }) {
  const openFinding = useWave((s) => s.openFinding);
  const expanded = useWave((s) => s.covHole);
  const selectHole = useWave((s) => s.selectHole);
  if (!report.holes.length) return null;
  return (
        <div className="cov-holes" data-testid="cov-holes">
          <h3>
            Uncovered — and what would close it
            <span className="cov-sub">derived from the graph, not suggested (§8.12)</span>
          </h3>
          {report.holes.map((h) => {
            const key = `${h.file}:${h.line}:${h.label}`;
            return (
              <HoleRow
                key={key}
                hole={h}
                open={expanded === key}
                onToggle={() => selectHole(expanded === key ? null : key)}
                onOpenSource={() => {
                  selectHole(key);
                  useWave.getState().setFsmOpen(false);
                  openFinding({ why: null, loc: { file: h.file, line: h.line } });
                }}
              />
            );
          })}
          {report.skipped.holes && <div className="pane-hint">{report.skipped.holes}</div>}
        </div>
  );
}

function HoleRow({
  hole,
  open,
  onToggle,
  onOpenSource,
}: {
  hole: Hole;
  open: boolean;
  onToggle: () => void;
  onOpenSource: () => void;
}) {
  const [copyMessage, setCopyMessage] = useState("");
  const copyConditions = async () => {
    try {
      await navigator.clipboard.writeText(
        [`${hole.file}:${hole.line} — ${hole.label}`, ...hole.conditions.map((c) => c.text)].join("\n"),
      );
      setCopyMessage("Conditions copied.");
    } catch (error) {
      setCopyMessage(`Could not copy: ${error instanceof Error ? error.message : String(error)}`);
    }
  };
  return (
    <div className={`cov-hole${open ? " on" : ""}`} data-testid={`cov-hole-${hole.line}`}>
      <div className="cov-hole-head">
        <button className="link" onClick={onToggle} data-testid={`cov-hole-toggle-${hole.line}`}>
          {open ? "▾" : "▸"} {hole.file}:{hole.line}
        </button>
        <span className="cov-hole-kind">{hole.kind}</span>
        {hole.text && <code className="cov-hole-src">{hole.text}</code>}
        <button className="link" onClick={onOpenSource}>
          open in Source
        </button>
      </div>
      {open && (
        <div className="cov-hole-body">
          {hole.note && <div className="cov-hole-note">{hole.note}</div>}
          {hole.conditions.length > 0 && (
            <div className="cov-conds">
              <button className="chip" onClick={() => void copyConditions()} data-testid="cov-copy-conditions">
                Copy conditions
              </button>
              {copyMessage && <span role="status">{copyMessage}</span>}
              <div className="cov-conds-lead">All of these must hold at once:</div>
              {hole.conditions.map((c) => (
                <div className="cov-cond" key={c.text}>
                  <code>{c.text}</code>
                  <span className={`cov-cond-state${c.ever === false ? " bad" : ""}`}>
                    {c.held === null
                      ? "not evaluated against this trace"
                      : c.held === 0
                        ? "NEVER observed"
                        : `${c.held}/${c.sampled} cycles`}
                  </span>
                  {c.produced_by.length > 0 && (
                    <ul className="cov-cond-src">
                      {c.produced_by.map((p) => (
                        <li key={p}>
                          <code>{p}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Score({ value }: { value: number | null }) {
  return (
    <span className="cov-score" data-testid="cov-score">
      {value === null ? "—" : `${Math.round(value * 100)}%`}
    </span>
  );
}
