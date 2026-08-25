/**
 * TAB 6 — Checks (§11.4).
 *
 * The first tab you open, and the one the session lands on when there is
 * anything to say (§11.4b, §13.4). It answers "what is wrong?" before a
 * question has been formulated, which §1.4 calls the engine of daily adoption.
 *
 * Grouped by severity within the sections §11.4 names, every row carrying a
 * working `[why]` and a suppression that demands a reason. Monochrome like the
 * rest of the interface (§11.1): severity is carried by a text label and the
 * ordering, not by colour, which stays reserved for causality.
 */

import { useEffect, useState } from "react";
import { fetchStuck } from "../api/client";
import type { ChecksReport, Finding, FindingGroup, ParamNode, StuckResult } from "../lib/types";
import { useWave } from "../state/store";

/**
 * Section order and headings, exactly as §11.4 lists them — and it has to be
 * every group the server can emit. A group missing from this list is not shown
 * anywhere, so a finding the backend produced would vanish between the API and
 * the screen, which is worse than one that was never produced (P1). The type
 * annotation is what keeps that from happening quietly: adding a `FindingGroup`
 * without a section here is a compile error.
 */
const LABELS: Record<FindingGroup, string> = {
  stuck: "STUCK",
  x_sources: "X SOURCES",
  protocol: "PROTOCOL",
  liveness: "LIVENESS",
  memory: "MEMORY",
  integrity: "DATA INTEGRITY",
  fsm: "FSM",
  lint: "LINT",
  plugin: "PLUGINS",
  parameters: "PARAMETERS",
};

const SECTIONS = (Object.entries(LABELS) as [FindingGroup, string][]).map(
  ([key, label]) => ({ key, label }),
);

export function ChecksTab() {
  const checks = useWave((s) => s.checks);
  const busy = useWave((s) => s.checksBusy);
  const hasRtl = useWave((s) => s.status?.has_rtl ?? false);
  const load = useWave((s) => s.loadChecks);
  const [filter, setFilter] = useState("");
  // §8.4's threshold, tried without restarting the server. Held here rather
  // than in the store because it is a question about this view, not session
  // state: nothing else in the application depends on the window it is asking
  // about, and a reload should go back to what the config says.
  const [stuck, setStuck] = useState<StuckResult | null>(null);
  // §11.7 gives `n`/`p` to Checks as well as Diff: walking the findings is how
  // this tab is read when there are thirty of them. Only Diff had it.
  const [at, setAt] = useState(-1);

  useEffect(() => {
    if (!checks && !busy) void load();
  }, [checks, busy, load]);

  if (busy && !checks) return <div className="pane-note">Running checks…</div>;
  if (!checks) return <div className="pane-note">No checks yet.</div>;

  const q = filter.trim().toLowerCase();
  const match = (f: Finding) =>
    !q ||
    f.signal?.toLowerCase().includes(q) ||
    f.check.includes(q) ||
    f.title.toLowerCase().includes(q);

  // An override replaces the STUCK group and nothing else, so the rest of the
  // report stays the one the session computed.
  const findings = stuck
    ? [...checks.findings.filter((f) => f.group !== "stuck"), ...stuck.findings]
    : checks.findings;
  const total = findings.length;
  const shown = findings.filter(match);
  const skipped = stuck
    ? Object.fromEntries(Object.entries(checks.skipped).filter(([k]) => k !== "stuck"))
    : checks.skipped;
  const nSkipped = Object.keys(skipped).length;
  return (
    <ChecksBody
      checks={checks}
      hasRtl={hasRtl}
      shown={shown}
      total={total}
      skipped={skipped}
      nSkipped={nSkipped}
      stuck={stuck}
      setStuck={setStuck}
      filter={filter}
      setFilter={setFilter}
      at={at}
      setAt={setAt}
    />
  );
}

/**
 * The list itself, split out so the `n`/`p` walker of §11.7 can live beside it.
 *
 * §11.7 gives those keys to Checks as well as Diff — a report of thirty rows is
 * read by stepping through it — and only Diff had them. Stepping moves the
 * shared selection (§11.6), so Wave and Source follow the finding the way they
 * follow everything else.
 */
function ChecksBody({
  checks,
  hasRtl,
  shown,
  total,
  skipped,
  nSkipped,
  stuck,
  setStuck,
  filter,
  setFilter,
  at,
  setAt,
}: {
  checks: ChecksReport;
  hasRtl: boolean;
  shown: Finding[];
  total: number;
  skipped: Record<string, string>;
  nSkipped: number;
  stuck: StuckResult | null;
  setStuck: (r: StuckResult | null) => void;
  filter: string;
  setFilter: (q: string) => void;
  at: number;
  setAt: (i: number) => void;
}) {
  // Sections render grouped, so the walk order is the order on screen.
  const ordered = SECTIONS.flatMap(({ key }) => shown.filter((f) => f.group === key));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "n" && e.key !== "p") return;
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) {
        return;
      }
      if (!ordered.length) return;
      e.preventDefault();
      const next =
        e.key === "n"
          ? (at + 1) % ordered.length
          : (at - 1 + ordered.length) % ordered.length;
      setAt(next);
      const f = ordered[next];
      const s = useWave.getState();
      if (f.time !== null) s.setCursor(f.time);
      const sig = f.signal ? s.signals.find((x) => x.path === f.signal) : undefined;
      if (sig) s.setSelected(sig.handle);
      if (f.loc) void s.openSource(f.loc.file, f.loc.line);
      document
        .querySelector(`[data-finding-index="${next}"]`)
        ?.scrollIntoView({ block: "nearest" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ordered, at, setAt]);

  return (
    <div className="checks" data-testid="checks-tab">
      <div className="checks-head">
        <input
          className="checks-filter"
          placeholder="filter by signal, scope or check…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          data-testid="checks-filter"
        />
        <span className="spacer" />
        <span className="dim" data-testid="checks-count">
          {total} finding{total === 1 ? "" : "s"} · {checks.ms} ms
        </span>
      </div>

      {/*
        P7, and the reason it is above the verdict rather than below it: a "✓
        nothing found" printed over a list of checks that never ran reads as a
        clean bill of health for a scan that mostly did not happen.
      */}
      <Skipped skipped={skipped} />
      <StuckWindow report={stuck} skipped={checks.skipped.stuck} onChange={setStuck} />

      {total === 0 && (
        <div className="pane-note">
          <span aria-hidden>✓</span> Nothing found
          {nSkipped > 0 ? " by the checks that ran" : " automatically"}.
          <div className="pane-hint">
            {hasRtl
              ? "No frozen signals, no X sources, no sim/synth mismatches."
              : "Only the stuck detector runs without RTL. Start the server with --rtl for the rest."}
          </div>
        </div>
      )}

      {SECTIONS.map(({ key, label }) => {
        const rows = shown.filter((f) => f.group === key);
        if (rows.length === 0) return null;
        return (
          <Section key={key} label={label} count={rows.length}>
            {rows.map((f) => (
              <Row key={f.id} finding={f} index={ordered.indexOf(f)} current={at} />
            ))}
          </Section>
        );
      })}

      {checks.parameters && <ParamTree root={checks.parameters} />}
      <Suppressed report={checks} />
    </div>
  );
}

/**
 * §8.4's threshold, adjustable from here.
 *
 * The default is 100 cycles and a testbench is routinely shorter than that, so
 * on most runs the check cannot fire at all — and until now the only way to
 * find that out, or to try a narrower window, was to edit `.veritrace.toml`
 * and restart. A knob whose right value is discovered by turning it belongs
 * where the answer is read.
 */
function StuckWindow({
  report,
  skipped,
  onChange,
}: {
  report: StuckResult | null;
  skipped: string | undefined;
  onChange: (r: StuckResult | null) => void;
}) {
  const session = useWave((s) => s.session);
  const nCycles = useWave((s) => s.status?.n_cycles ?? 0);
  const [text, setText] = useState("");
  const [error, setError] = useState("");

  // Nothing to offer without a clock: cycles have no meaning, and the skip
  // already says so in the list above.
  if (!session || nCycles === 0) return null;

  const apply = async (value: string) => {
    const n = Number.parseInt(value, 10);
    if (!Number.isFinite(n) || n < 1) {
      onChange(null);
      setError("");
      return;
    }
    try {
      onChange(await fetchStuck(session, n));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="checks-stuck" data-testid="stuck-window">
      <span className="dim">STUCK window</span>
      <input
        className="checks-cycles mono"
        type="number"
        min={1}
        placeholder={report ? String(report.cycles) : "cycles"}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && void apply(text)}
        data-testid="stuck-cycles"
      />
      <button className="chip" onClick={() => void apply(text)} data-testid="stuck-apply">
        try it
      </button>
      {report && (
        <button
          className="chip"
          onClick={() => {
            setText("");
            onChange(null);
          }}
          data-testid="stuck-reset"
        >
          back to the configured window
        </button>
      )}
      <span className="spacer" />
      <span className="dim mono" data-testid="stuck-note">
        {error ||
          report?.unreachable ||
          skipped ||
          (report
            ? `${report.findings.length} frozen over ${report.cycles} of ${nCycles} cycles`
            : `the run is ${nCycles} cycles`)}
      </span>
    </div>
  );
}

function Section({
  label,
  count,
  children,
}: {
  label: string;
  count: number;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="checks-section">
      <button className="checks-section-head" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} {label} <span className="dim">({count})</span>
      </button>
      {open && <div className="checks-rows">{children}</div>}
    </div>
  );
}

function Row({
  finding,
  index,
  current,
}: {
  finding: Finding;
  index: number;
  current: number;
}) {
  const openFinding = useWave((s) => s.openFinding);
  const suppress = useWave((s) => s.suppress);
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <div
      className={`check-row${finding.severity === "info" ? "" : ` ${finding.severity}`}${
        index === current ? " walked" : ""
      }`}
      data-testid="check-row"
      data-check={finding.check}
      data-finding-index={index}
    >
      <div className="check-main">
        <span className={`sev sev-${finding.severity}`}>{finding.severity}</span>
        {finding.signal && <span className="check-sig mono">{finding.signal}</span>}
        <span className="check-title">{finding.title}</span>
        <span className="spacer" />
        {finding.loc && (
          <button
            className="check-loc mono"
            onClick={() => openFinding({ why: null, loc: finding.loc })}
            title="Open in Source"
          >
            {finding.loc.file}:{finding.loc.line}
          </button>
        )}
        <button
          className="chip why"
          disabled={!finding.why}
          title={finding.why ?? "nothing to explain: this has no value in the trace"}
          onClick={() => openFinding(finding)}
          data-testid="check-why"
        >
          why
        </button>
        <button
          className="chip"
          onClick={() => setAsking(!asking)}
          data-testid="check-suppress"
        >
          suppress
        </button>
      </div>

      {finding.detail && <div className="check-detail">{finding.detail}</div>}
      {finding.notes.map((n, i) => (
        <div className="check-note" key={i}>
          └─ {n}
        </div>
      ))}

      {asking && (
        // §11.4: the reason is mandatory, or the list becomes a graveyard.
        <form
          className="check-suppress"
          onSubmit={(e) => {
            e.preventDefault();
            if (!reason.trim()) return;
            void suppress(finding.id, reason.trim());
          }}
        >
          <input
            autoFocus
            placeholder="why is this not a problem? (required)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="suppress-reason"
          />
          <button className="chip" type="submit" disabled={!reason.trim()}>
            hide it
          </button>
          <button className="chip" type="button" onClick={() => setAsking(false)}>
            cancel
          </button>
        </form>
      )}
    </div>
  );
}

/** §8.11c. Defaults an ancestor contradicts are marked; the rest is reference. */
function ParamTree({ root }: { root: ParamNode }) {
  const [open, setOpen] = useState(false);
  const shadowed = countShadowed(root);
  return (
    <div className="checks-section">
      <button className="checks-section-head" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} PARAMETER TREE{" "}
        <span className="dim">
          ({shadowed > 0 ? `${shadowed} on a contradicted default` : "all propagated"})
        </span>
      </button>
      {open && (
        <div className="param-tree mono" data-testid="param-tree">
          <ParamLevel node={root} depth={0} />
        </div>
      )}
    </div>
  );
}

function ParamLevel({ node, depth }: { node: ParamNode; depth: number }) {
  const pad = { paddingLeft: `${depth * 1.2}em` };
  return (
    <>
      <div className="param-inst" style={pad}>
        {node.path.split(".").pop()} <span className="dim">({node.module})</span>
      </div>
      {node.params.map((p) => (
        <div className="param-row" key={p.name} style={pad}>
          <span className="param-name">├─ {p.name}</span>
          <span className="param-value">= {p.value}</span>
          {p.shadowed ? (
            <span className="param-warn">⚠ default — an enclosing scope sets it differently</span>
          ) : (
            <span className="dim">{p.overridden ? "✓ overridden here" : ""}</span>
          )}
        </div>
      ))}
      {node.children.map((c) => (
        <ParamLevel key={c.path} node={c} depth={depth + 1} />
      ))}
    </>
  );
}

function countShadowed(node: ParamNode): number {
  return (
    node.params.filter((p) => p.shadowed).length +
    node.children.reduce((n, c) => n + countShadowed(c), 0)
  );
}

/** P7: a check that could not run says so, rather than looking like a pass. */
function Skipped({ skipped }: { skipped: Record<string, string> }) {
  const entries = Object.entries(skipped);
  if (entries.length === 0) return null;
  return (
    <div className="checks-skipped" data-testid="checks-skipped">
      {entries.map(([name, why]) => (
        <div key={name}>
          <span className="dim">not run:</span> {name} — {why}
        </div>
      ))}
    </div>
  );
}

function Suppressed({ report }: { report: ChecksReport }) {
  const unsuppress = useWave((s) => s.unsuppress);
  const entries = Object.entries(report.suppressed);
  const [open, setOpen] = useState(false);
  if (entries.length === 0) return null;
  return (
    <div className="checks-section">
      <button className="checks-section-head" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} SUPPRESSED <span className="dim">({entries.length})</span>
      </button>
      {open && (
        <div className="checks-rows" data-testid="suppressed-list">
          {entries.map(([id, reason]) => (
            <div className="check-row" key={id}>
              <div className="check-main">
                <span className="check-title">{reason}</span>
                <span className="spacer" />
                <button className="chip" onClick={() => void unsuppress(id)}>
                  restore
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
