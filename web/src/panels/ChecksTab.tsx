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
import type { ChecksReport, Finding, FindingGroup, ParamNode } from "../lib/types";
import { useWave } from "../state/store";

/** Section order and headings, exactly as §11.4 lists them. */
const SECTIONS: { key: FindingGroup; label: string }[] = [
  { key: "stuck", label: "STUCK" },
  { key: "x_sources", label: "X SOURCES" },
  { key: "protocol", label: "PROTOCOL" },
  { key: "lint", label: "LINT" },
  { key: "parameters", label: "PARAMETERS" },
];

export function ChecksTab() {
  const checks = useWave((s) => s.checks);
  const busy = useWave((s) => s.checksBusy);
  const hasRtl = useWave((s) => s.status?.has_rtl ?? false);
  const load = useWave((s) => s.loadChecks);
  const [filter, setFilter] = useState("");

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

  const total = checks.findings.length;
  const shown = checks.findings.filter(match);

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

      {total === 0 && (
        <div className="pane-note">
          <span aria-hidden>✓</span> Nothing found automatically.
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
              <Row key={f.id} finding={f} />
            ))}
          </Section>
        );
      })}

      {checks.parameters && <ParamTree root={checks.parameters} />}
      <Skipped report={checks} />
      <Suppressed report={checks} />
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

function Row({ finding }: { finding: Finding }) {
  const openFinding = useWave((s) => s.openFinding);
  const suppress = useWave((s) => s.suppress);
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <div
      className={`check-row${finding.severity === "info" ? "" : ` ${finding.severity}`}`}
      data-testid="check-row"
      data-check={finding.check}
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
function Skipped({ report }: { report: ChecksReport }) {
  const entries = Object.entries(report.skipped);
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
