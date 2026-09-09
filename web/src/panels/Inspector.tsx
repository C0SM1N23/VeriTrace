/**
 * §11.3's third column: what is selected, in detail.
 *
 * The layout in §11.3 is HIERARCHY | main panel | INSPECTOR, and this was the
 * one of the three that was never built — `--inspector-w: 300px` sat in
 * tokens.css referenced by nothing. Three places in the spec send information
 * here and had nowhere to send it:
 *
 * * §5.5, problem 3 — a signal in a second clock domain has to be shown with
 *   *its own* cycle number, marked, because `c1247` means nothing without
 *   saying which clock counted it;
 * * §11.4, TAB 8 — clicking a transaction shows all its fields and metrics;
 * * §11.4, TAB 7 — clicking an uncovered point shows the derived conditions.
 *
 * It reads the shared selection of §11.6 and nothing else, so it never has to
 * be told to refresh: whatever any panel selected is what this describes.
 */

import { useEffect, useState } from "react";
import { fetchValues } from "../api/client";
import { format } from "../lib/radix";
import { formatTime } from "../lib/time";
import { enumLabelsFor, useWave } from "../state/store";

export function Inspector() {
  const open = useWave((s) => s.inspectorOpen);
  if (!open) return null;
  return (
    <aside className="inspector" data-testid="inspector">
      <Selection />
    </aside>
  );
}

function Selection() {
  const tab = useWave((s) => s.activeTab);
  const txnSelected = useWave((s) => s.txnSelected);
  const covHole = useWave((s) => s.covHole);
  const coverage = useWave((s) => s.coverage);
  const sourceLoc = useWave((s) => s.sourceLoc);

  // The tab decides what "the selection" means, because that is what the user
  // just clicked. Falling back to the signal keeps the column useful rather
  // than empty when a tab has nothing of its own selected.
  if (tab === 8 && txnSelected) return <TxnDetail />;
  if (tab === 7 && covHole) return <HoleDetail />;
  if (tab === 3 && covHole && coverage?.holes.some((h) =>
    `${h.file}:${h.line}:${h.label}` === covHole &&
    h.file === sourceLoc?.file && h.line === sourceLoc?.line,
  )) return <HoleDetail />;
  return <SignalDetail />;
}

/** A row of the definition list every section is built from. */
function Field({ label, value, title }: { label: string; value: React.ReactNode; title?: string }) {
  return (
    <div className="insp-row" title={title}>
      <span className="insp-key">{label}</span>
      <span className="insp-val mono">{value}</span>
    </div>
  );
}

function SignalDetail() {
  const handle = useWave((s) => s.selected);
  const byHandle = useWave((s) => s.signalsByHandle);
  const cursor = useWave((s) => s.cursor);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const period = useWave((s) => s.clockPeriod);
  const origin = useWave((s) => s.clockOrigin);
  const runWhy = useWave((s) => s.runWhy);
  const runCone = useWave((s) => s.runCone);
  const session = useWave((s) => s.session);
  const radix = useWave((s) => s.radix);
  const machines = useWave((s) => s.machines);
  const sig = handle !== null ? byHandle.get(handle) : undefined;

  // Inspector values are facts, not pixels.  Ask the store for the exact
  // settled value instead of reading a downsampled canvas bucket.
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setValue(null);
    if (handle === null || cursor === null || !session) return () => { live = false; };
    void fetchValues(
      session,
      [handle],
      cursor,
      sig?.derived ? { [handle]: sig.path } : {},
    )
      .then((values) => live && setValue(values.get(handle) ?? null))
      .catch(() => live && setValue(null));
    return () => {
      live = false;
    };
  }, [handle, cursor, session, sig?.derived, sig?.path]);

  if (!sig) {
    return (
      <div className="insp-empty">
        Nothing selected.
        <div className="pane-hint">Click a signal in Wave, or a row in any list.</div>
      </div>
    );
  }

  const cycle = period && cursor !== null ? Math.round((cursor - origin) / period) : null;

  return (
    <>
      <header className="insp-head">
        <div className="insp-title mono" title={sig.path}>
          {sig.name}
        </div>
        <div className="insp-sub mono">{sig.scope}</div>
      </header>

      <section>
        <Field
          label="value"
          value={
            value === null
              ? "—"
              : format(value, sig.width, radix[sig.path] ?? "hex", enumLabelsFor(machines, sig.path))
          }
          title="Exact settled value at the cursor"
        />
        <Field
          label="at"
          value={cursor === null ? "—" : formatTime(cursor, timescale)}
        />
        {cycle !== null && <Field label="cycle" value={`c${cycle}`} />}
        <OtherDomain path={sig.path} />
      </section>

      <section>
        <Field label="width" value={sig.width === 1 ? "1 bit" : `${sig.width} bits`} />
        <Field label="kind" value={sig.kind} />
        <Field label="events" value={sig.n_events.toLocaleString()} />
      </section>

      <div className="insp-actions">
        <button
          className="chip"
          onClick={() => void runWhy(sig.path, cursor ?? 0)}
          title="Why is this value here? (w)"
          data-testid="insp-why"
        >
          why
        </button>
        <button className="chip" onClick={() => void runCone("fanin")} title="Fan-in (c)">
          cone
        </button>
        <button className="chip" onClick={() => void runCone("fanout")} title="Fan-out (f)">
          fanout
        </button>
      </div>
    </>
  );
}

/**
 * §5.5, problem 3: a signal clocked by something other than the primary clock
 * is reported in *its own* cycles, named.
 *
 * *"Semnalele din alt domeniu se afiseaza cu ciclul lor propriu in Inspector,
 * marcat explicit: `axi: c891 (aclk)`."* Without the clock's name a cycle
 * number is an invitation to compare two counts that do not mean the same
 * thing, which is the mistake §5.5 exists to prevent.
 */
function OtherDomain({ path }: { path: string }) {
  const domains = useWave((s) => s.status?.clock_domains ?? null);
  const cursor = useWave((s) => s.cursor);
  if (!domains || cursor === null) return null;

  // Per signal, not per scope: two clocks routinely drive registers in one
  // module, and a scope prefix cannot tell them apart.
  const own = domains.find((d) => !d.primary && d.signals.includes(path));
  if (!own || !own.period) return null;
  const cycle = Math.round((cursor - own.origin) / own.period);
  return (
    <Field
      label="domain"
      value={`c${cycle} (${own.name})`}
      title={`Counted on ${own.path}, not the primary clock`}
    />
  );
}

/** §11.4, TAB 8: every field and metric of the selected transaction. */
function TxnDetail() {
  const ref = useWave((s) => s.txnSelected);
  const rows = useWave((s) => s.txnRows);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const t = rows.find((x) => x.ref === ref);
  if (!t) return <div className="insp-empty">That transaction is not in the current list.</div>;

  return (
    <>
      <header className="insp-head">
        <div className="insp-title mono">{t.ref}</div>
        <div className="insp-sub mono">
          {t.kind} · {t.status}
        </div>
      </header>
      <section>
        <Field label="start" value={formatTime(t.start_time, timescale)} />
        <Field
          label="end"
          value={t.end_time === null ? "never closed" : formatTime(t.end_time, timescale)}
        />
      </section>
      {Object.keys(t.fields).length > 0 && (
        <section>
          <div className="insp-legend">FIELDS</div>
          {Object.entries(t.fields).map(([k, v]) => (
            <Field key={k} label={k} value={String(v)} />
          ))}
        </section>
      )}
      {Object.keys(t.metrics).length > 0 && (
        <section>
          <div className="insp-legend">METRICS</div>
          {Object.entries(t.metrics).map(([k, v]) => (
            <Field key={k} label={k} value={v === null ? "—" : String(v)} />
          ))}
        </section>
      )}
      {t.violations.length > 0 && (
        <section>
          <div className="insp-legend">PROTOCOL</div>
          {t.violations.map((v) => (
            <div key={v.rule} className="insp-violation" data-testid="insp-violation">
              <span className="mono">{v.rule}</span>
              <span className="dim">{v.msg}</span>
            </div>
          ))}
        </section>
      )}
    </>
  );
}

/** §11.4, TAB 7: the derived conditions that would close the selected hole. */
function HoleDetail() {
  const key = useWave((s) => s.covHole);
  const coverage = useWave((s) => s.coverage);
  // Keyed the way the Coverage tab keys it, so the two agree about which row
  // is open without either owning the other's identity scheme.
  const hole = (coverage?.holes ?? []).find((h) => `${h.file}:${h.line}:${h.label}` === key);
  if (!hole) return <div className="insp-empty">That point is not in the current report.</div>;

  return (
    <>
      <header className="insp-head">
        <div className="insp-title mono">{hole.label}</div>
        <div className="insp-sub mono">
          {hole.file}:{hole.line}
        </div>
      </header>
      <section>
        <div className="insp-legend">TO REACH IT, ALL OF</div>
        {hole.conditions.length === 0 ? (
          <div className="pane-hint">
            No assignment at that line, so there is nothing to derive from (§8.12).
          </div>
        ) : (
          hole.conditions.map((c) => (
            <div key={c.text} className="insp-cond" data-testid="insp-condition">
              <span className="mono">{c.text}</span>
              <span className={c.ever === false ? "insp-never" : "dim"}>
                {c.ever === false
                  ? "never observed"
                  : c.held === null
                    ? "not measured"
                    : `held in ${Math.round((100 * c.held) / Math.max(c.sampled, 1))}% of cycles`}
              </span>
            </div>
          ))
        )}
      </section>
    </>
  );
}
