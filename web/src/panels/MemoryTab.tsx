/**
 * TAB 10 — Memory (§11.4b).
 *
 * *SDRAM/DDR are un vocabular propriu (bancuri, randuri, comenzi, constrangeri
 * de timing) care nu se exprima bine nici ca semnale, nici ca tranzactii
 * generice. Merita tab propriu.* Everything here is in that vocabulary: banks
 * and rows, not `cs_n` and `ras_n`.
 *
 * Four decisions carry it:
 *
 * - **The bank timeline is one row per bank on Wave's own time axis**, so a
 *   segment and the command that opened it line up at every zoom level — the
 *   same rule TAB 8's Gantt follows, and for the same reason.
 * - **Colour means state, and only state** (§11.1): a bank is either doing
 *   something or it is not. `active` carries the accent because an open row is
 *   the thing you are looking for; `idle` is background.
 * - **Every violation is clickable and lands on the cycle**, with the command
 *   wires already loaded — §8.20's own interaction, and the reason the report
 *   carries both commands rather than just a complaint.
 * - **What was checked and passed is shown**, not just what failed. "tRAS,
 *   tRC: conformant" is a fact the tab states; an absent line is not.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { formatTime } from "../lib/time";
import { addressPattern, decodeAddress } from "../lib/address";
import type { CmdEvent, MemoryInterface, MemoryReport, TimingViolation } from "../lib/types";
import { useWave } from "../state/store";

/** Height of one bank row in the timeline. */
const BANK_H = 22;

export function MemoryTab() {
  const report = useWave((s) => s.memory);
  const busy = useWave((s) => s.memoryBusy);
  const error = useWave((s) => s.memoryError);
  const load = useWave((s) => s.loadMemory);
  const iface = useWave((s) => s.memIface);
  const select = useWave((s) => s.selectMemIface);

  useEffect(() => {
    if (!report && !busy && !error) void load();
  }, [report, busy, error, load]);

  if (busy && !report) return <div className="pane-note">Decoding the command bus…</div>;
  if (error && !report) return <div className="pane-note" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>;
  if (!report) return <div className="pane-note">No memory analysis yet.</div>;

  if (!report.interfaces.length) {
    return (
      <div className="pane-note" data-testid="mem-empty">
        No memory interfaces were detected.
        <div className="pane-hint">
          A memory pack matches a scope only when every signal it requires is in the
          dump — for SDR SDRAM that is <code>cs_n</code>, <code>ras_n</code>,{" "}
          <code>cas_n</code>, <code>we_n</code>, <code>ba</code> and <code>a</code>.
          Check that the command bus is dumped, or write a pack for your device.
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
    <div className="mem" data-testid="memory-tab">
      {error && <div className="pane-note" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>}
      {report.errors.concat(report.timing_errors).map((e) => <div className="pane-note" role="alert" key={e}>{e}</div>)}
      <div className="mem-head">
        <select
          className="txn-iface"
          value={current.iface}
          onChange={(e) => void select(e.target.value)}
          data-testid="mem-iface"
        >
          {report.interfaces.map((i) => (
            <option key={i.iface} value={i.iface}>
              {i.iface} — {i.chip} ({i.n_banks} banks)
            </option>
          ))}
        </select>
        <span className="txn-summary" data-testid="mem-summary">
          <span data-testid="mem-count">{i18nCommands(current.n_commands)}</span>
          {current.clock_path && <span title="Cycle labels count the edges of this interface clock"> · {current.clock_path}</span>}
          <Dot />
          <span className={current.violations.length ? "warn" : "ok"} data-testid="mem-violation-count">
            {current.violations.length === 0
              ? "no timing violations"
              : `${current.violations.length} timing violation${
                  current.violations.length === 1 ? "" : "s"
                }`}
          </span>
        </span>
      </div>

      <ChipSelector key={current.iface} per={current} report={report} />
      <Violations per={current} />
      <BankTimeline per={current} />
      <div className="mem-row">
        <CommandStream per={current} />
        <div className="mem-col">
          <RowHits per={current} />
          <RefreshCompliance per={current} />
          <AddressMap per={current} />
        </div>
      </div>
    </div>
  );
}

const Dot = () => <span className="dot">·</span>;

function ChipSelector({ per, report }: { per: MemoryInterface; report: MemoryReport }) {
  const apply = useWave((s) => s.applyMemoryTiming);
  const busy = useWave((s) => s.memoryBusy);
  const saved = report.selection[per.iface];
  const [custom, setCustom] = useState(saved?.toml !== undefined);
  const [text, setText] = useState(saved?.toml ?? "");
  const [readError, setReadError] = useState<string | null>(null);
  const template = () => {
    const chip = report.chips.find((c) => c.slug === per.chip) ?? report.chips[0];
    return chip ? Object.entries(chip).filter(([k]) => k !== "slug")
      .map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join("\n") : "";
  };
  return <div className="mem-card" data-testid="mem-chip-settings">
    <label>Timing chip {" "}<select aria-label="Timing chip" disabled={busy}
      value={custom ? "__custom__" : per.chip}
      onChange={(e) => {
        const chip = e.target.value;
        setCustom(chip === "__custom__");
        if (chip === "__custom__") { if (!text) setText(template()); }
        else void apply(per.iface, { chip });
      }}>
      {!report.chips.some((c) => c.slug === per.chip) && <option value={per.chip}>{per.chip} (saved)</option>}
      {report.chips.map((c) => <option key={c.slug} value={c.slug}>{c.name}</option>)}
      <option value="__custom__">Custom timing…</option>
    </select></label>
    {busy && <span role="status"> Rechecking timing…</span>}
    {custom && <div>
      <div className="pane-hint">Nanoseconds for t*; whole clock cycles for CL/CWL. Saved with this session, without modifying chip files.</div>
      <textarea aria-label="Custom timing TOML" rows={8} style={{ width: "100%" }} value={text}
        onChange={(e) => setText(e.target.value)} />
      <label>Load timing file <input type="file" accept=".toml" aria-label="Load timing file"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void file.text().then((body) => { setText(body); setReadError(null); })
            .catch((err: unknown) => setReadError(String(err)));
        }} /></label>{" "}
      <button disabled={busy || !text.trim()} onClick={() => void apply(per.iface, { toml: text })}>Apply timing</button>
      {readError && <div role="alert">{readError}</div>}
    </div>}
  </div>;
}

function RefreshCompliance({ per }: { per: MemoryInterface }) {
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const jump = useWave((s) => s.jumpTo);
  const intervals = per.refresh_intervals;
  const limit = per.refresh_limit;
  const max = intervals.reduce((bound, i) => Math.max(bound, i.elapsed), Math.max(1, limit ?? 0));
  const first = intervals[0]?.t0 ?? 0;
  const span = Math.max(1, (intervals[intervals.length - 1]?.t1 ?? first) - first);
  const x = (t: number) => 10 + ((t - first) / span) * 580;
  const y = (duration: number) => 100 - (duration / max) * 80;
  return (
    <div className="mem-card" data-testid="mem-refresh">
      <div className="perf-card-title">Refresh compliance</div>
      <div className="dim">
        {limit === null ? "No usable tREFI limit." : `tREFI maximum: ${formatTime(limit, timescale)}`}
      </div>
      {intervals.length ? (
        <svg viewBox="0 0 600 120" style={{ width: "100%" }} data-testid="mem-refresh-chart"
             aria-label="Consecutive refresh intervals against the tREFI maximum">
          {intervals.map((interval) => {
            const violated = limit !== null && interval.elapsed > limit;
            const show = () => jump(interval.t1, Object.values(per.signals), "REFRESH interval");
            return <rect key={interval.t1} x={x(interval.t0)} y={y(interval.elapsed)}
                         width={Math.max(1, x(interval.t1) - x(interval.t0) - 1)}
                         height={100 - y(interval.elapsed)} fill="currentColor"
                         className={violated ? "warn" : "dim"} opacity={0.6}
                         role="button" tabIndex={0} data-testid="mem-refresh-interval"
                         aria-label={`${formatTime(interval.elapsed, timescale)}${violated ? ", exceeds tREFI" : ""}`}
                         onClick={show} onKeyDown={(e) => { if (e.key === "Enter") show(); }}>
              <title>{formatTime(interval.t0, timescale)}–{formatTime(interval.t1, timescale)}: {formatTime(interval.elapsed, timescale)}</title>
            </rect>;
          })}
          {limit !== null && <line x1={10} x2={590} y1={y(limit)} y2={y(limit)}
                                  stroke="currentColor" strokeDasharray="5 3" data-testid="mem-refresh-limit" />}
        </svg>
      ) : <div className="pane-hint">No consecutive refresh pair observed; compliance was not measured.</div>}
      <div className="pane-hint">Observed intervals only; the trace boundaries do not establish a preceding or following refresh.</div>
    </div>
  );
}

function i18nCommands(n: number): string {
  return `${n} command${n === 1 ? "" : "s"}`;
}

// --- timing violations -------------------------------------------------------

/**
 * §8.20's report, in its own words: what failed, by how much, and between
 * which two commands. Above the timeline because a controller that violates
 * tRCD is corrupting data, and that outranks everything else on the tab.
 */
function Violations({ per }: { per: MemoryInterface }) {
  const jumpTo = useWave((s) => s.jumpTo);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");

  const at = (c: CmdEvent) => c.cycle !== null ? `c${c.cycle}` : formatTime(c.time, timescale);

  const byConstraint = useMemo(() => {
    const out = new Map<string, TimingViolation[]>();
    for (const v of per.violations) {
      const list = out.get(v.constraint) ?? [];
      list.push(v);
      out.set(v.constraint, list);
    }
    return [...out.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [per.violations]);

  // Constraints that were checked and held. §8.20 prints these, because
  // "conformant" is a fact and an absent line is not.
  const clean = Object.entries(per.checked)
    .filter(([, n]) => n === 0)
    .map(([name]) => name)
    .sort();

  const wires = Object.values(per.signals);

  return (
    <div className="mem-card" data-testid="mem-violations">
      <div className="perf-card-title">Timing constraints</div>

      {byConstraint.length === 0 && (
        <div className="mem-clear" data-testid="mem-no-violations">
          {Object.keys(per.checked).length
            ? <><span aria-hidden>✓</span> Every constraint checked was met.</>
            : "No timing constraint could be checked in this trace."}
        </div>
      )}

      {byConstraint.map(([name, group]) => (
        <div className="mem-violation" key={name} data-testid={`mem-violation-${name}`}>
          <div className="mem-violation-head">
            <span className="mem-constraint">{name}</span>{" "}
            {group[0].is_maximum ? "exceeded" : "violated"} {group.length} time
            {group.length === 1 ? "" : "s"}
          </div>
          <table className="perf-chain">
            <tbody>
              {group.map((v) => (
                <tr key={`${v.constraint}-${v.at}`} data-testid="mem-violation-row">
                  <td className="perf-agent">{v.second ? at(v.second) : formatTime(v.at, timescale)}</td>
                  <td className="dim">{v.bank === null ? "device" : `bank ${v.bank}`}</td>
                  <td>
                    {v.first?.name}@{v.first ? at(v.first) : "?"}
                    <span className="dim"> → </span>
                    {v.second?.name}@{v.second ? at(v.second) : "?"}
                  </td>
                  <td className="warn">
                    {v.limit_cycles !== null ? `${v.measured_cycles} cycles` : formatTime(v.measured_ticks, timescale)}, {v.is_maximum ? "max" : "min"}{" "}
                    {v.limit_cycles !== null ? v.limit_cycles : formatTime(v.limit_ticks, timescale)}
                  </td>
                  <td>
                    {/* §8.20: click a violation -> Wave at that cycle, with the
                        command signals already loaded. */}
                    <button
                      className="link"
                      data-testid="mem-violation-jump"
                      onClick={() => jumpTo(v.at, wires, `${v.constraint} violation`)}
                    >
                      show
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {clean.length > 0 && (
        <div className="mem-clean" data-testid="mem-clean">
          <span aria-hidden>✓</span> {clean.join(", ")}: conformant
        </div>
      )}
      {Object.entries(per.skipped).map(([k, v]) => (
        <div className="pane-hint" key={k}>
          {k} not checked: {v}
        </div>
      ))}
    </div>
  );
}

// --- bank timeline -----------------------------------------------------------

/**
 * One row per bank, on Wave's own time axis (§8.20).
 *
 * Subscribed imperatively for the same reason TAB 8's Gantt is: `view` changes
 * on every frame of a pan, and a React render on that path costs the frame
 * budget.
 */
function BankTimeline({ per }: { per: MemoryInterface }) {
  const jumpTo = useWave((s) => s.jumpTo);
  const ref = useRef<HTMLDivElement>(null);
  const wires = Object.values(per.signals);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const paint = () => {
      const { view } = useWave.getState();
      const span = Math.max(1, view.t1 - view.t0);
      for (const child of Array.from(el.children) as HTMLElement[]) {
        const t0 = Number(child.dataset.t0);
        const t1 = Number(child.dataset.t1);
        if (t1 < view.t0 || t0 > view.t1) {
          child.style.display = "none";
          continue;
        }
        child.style.display = "";
        child.style.left = `${((t0 - view.t0) / span) * 100}%`;
        child.style.width = `${Math.max(0.15, ((t1 - t0) / span) * 100)}%`;
      }
    };
    paint();
    return useWave.subscribe(paint);
  }, [per.segments]);

  if (!per.segments.length) {
    return (
      <div className="mem-card" data-testid="mem-timeline">
        <div className="perf-card-title">Bank timeline</div>
        <div className="pane-note">No bank activity was decoded.</div>
      </div>
    );
  }

  return (
    <div className="mem-card" data-testid="mem-timeline">
      <div className="perf-card-title">
        Bank timeline <span className="dim">— {per.n_banks} banks, on Wave's time axis</span>
      </div>
      <div className="mem-banks">
        <div className="mem-bank-labels">
          {Array.from({ length: per.n_banks }, (_, b) => (
            <div className="mem-bank-label" key={b} style={{ height: BANK_H }}>
              bank {b}
            </div>
          ))}
        </div>
        <div
          className="mem-bank-track"
          ref={ref}
          style={{ height: per.n_banks * BANK_H }}
          data-testid="mem-bank-track"
        >
          {per.segments.map((s) => (
            <button
              key={`${s.bank}-${s.t0}-${s.state}`}
              className={`mem-seg ${s.state}`}
              style={{ top: s.bank * BANK_H + 2, height: BANK_H - 4 }}
              data-t0={s.t0}
              data-t1={s.t1}
              data-testid={`mem-seg-${s.bank}-${s.state}`}
              data-state={s.state}
              title={`bank ${s.bank} ${s.state}${s.row !== null ? ` row 0x${s.row.toString(16)}` : ""}`}
              onClick={() => jumpTo(s.t0, wires, `bank ${s.bank}`)}
            >
              {s.row !== null && s.state === "active" && (
                <span className="mem-seg-row">0x{s.row.toString(16)}</span>
              )}
            </button>
          ))}
        </div>
      </div>
      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <div className="mem-legend" data-testid="mem-legend">
      {["idle", "activating", "active", "precharging"].map((s) => (
        <span key={s}>
          <span className={`swatch mem-sw ${s}`} />
          {s}
        </span>
      ))}
    </div>
  );
}

// --- command stream ----------------------------------------------------------

/** The decoded command list, searchable — §8.20's `c1187 ACT b2 r0x1A4`. */
function CommandStream({ per }: { per: MemoryInterface }) {
  const commands = useWave((s) => s.memCommands);
  const filter = useWave((s) => s.memFilter);
  const setFilter = useWave((s) => s.setMemFilter);
  const jumpTo = useWave((s) => s.jumpTo);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const wires = Object.values(per.signals);

  const at = (c: CmdEvent) => c.cycle !== null ? `c${c.cycle}` : formatTime(c.time, timescale);

  const q = filter.trim().toLowerCase();
  const shown = useMemo(
    () =>
      commands.filter(
        (c) =>
          !q ||
          c.name.toLowerCase().includes(q) ||
          Object.entries(c.fields).some(([k, v]) => `${k}${v}`.toLowerCase().includes(q)),
      ),
    [commands, q],
  );

  return (
    <div className="mem-card" data-testid="mem-commands">
      <div className="perf-card-title">
        Command stream <span className="dim">— {shown.length} of {commands.length}</span>
      </div>
      <input
        className="checks-filter"
        placeholder="filter by command or field…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        data-testid="mem-filter"
      />
      <div className="mem-cmd-list">
        {shown.map((c) => (
          <button
            className="mem-cmd"
            key={`${c.time}-${c.name}`}
            data-testid="mem-cmd"
            onClick={() => jumpTo(c.time, wires, c.name)}
          >
            <span className="mem-cmd-t">{at(c)}</span>
            <span className="mem-cmd-name">{c.name}</span>
            <span className="mem-cmd-args">
              {Object.entries(c.fields)
                .filter(([, v]) => v !== null)
                .map(([k, v]) => `${k}=${k === "row" || k === "col" ? `0x${(v as number).toString(16)}` : v}`)
                .join(" ")}
            </span>
          </button>
        ))}
        {shown.length === 0 && <div className="empty">No command matches that.</div>}
      </div>
    </div>
  );
}

// --- efficiency --------------------------------------------------------------

/** Row hit / miss / conflict, plus the rest of §8.20's efficiency table. */
function RowHits({ per }: { per: MemoryInterface }) {
  const e = per.efficiency;
  const r = e.row_hits;
  const total = r.hits + r.misses + r.conflicts;
  const pct = (n: number) => (total ? (n / total) * 100 : 0);

  return (
    <div className="mem-card" data-testid="mem-rowhits">
      <div className="perf-card-title">
        Row hits <span className="dim">— {total} accesses</span>
      </div>
      {total > 0 ? (
        <>
          <div className="mem-bar" data-testid="mem-rowhit-bar">
            <span className="mem-bar-seg hit" style={{ width: `${pct(r.hits)}%` }} />
            <span className="mem-bar-seg miss" style={{ width: `${pct(r.misses)}%` }} />
            <span className="mem-bar-seg conflict" style={{ width: `${pct(r.conflicts)}%` }} />
          </div>
          <table className="perf-legend">
            <tbody>
              <tr>
                <td>
                  <span className="swatch mem-sw hit" />hit
                </td>
                <td className="num">{r.hits}</td>
                <td className="num dim">{pct(r.hits).toFixed(0)}%</td>
              </tr>
              <tr>
                <td>
                  <span className="swatch mem-sw miss" />miss
                </td>
                <td className="num">{r.misses}</td>
                <td className="num dim">{pct(r.misses).toFixed(0)}%</td>
              </tr>
              <tr>
                <td>
                  {/* §8.20: > 30% conflict means a bad page policy, which is why
                      this row is the one that carries a warning colour. */}
                  <span className="swatch mem-sw conflict" />conflict
                </td>
                <td className="num">{r.conflicts}</td>
                <td className="num dim">{pct(r.conflicts).toFixed(0)}%</td>
              </tr>
            </tbody>
          </table>
        </>
      ) : (
        <div className="pane-note">No row accesses were decoded.</div>
      )}
      <div className="perf-percentiles" data-testid="mem-efficiency">
        {e.bus_utilization !== null && (
          <span>bus {(e.bus_utilization * 100).toFixed(1)}%</span>
        )}
        {e.refresh_overhead !== null && (
          <span>refresh {(e.refresh_overhead * 100).toFixed(1)}%</span>
        )}
        {e.turnaround_events > 0 && (
          <span>
            turnaround {e.turnaround_cycles}c / {e.turnaround_events}
          </span>
        )}
        {e.bank_parallelism !== null && (
          <span>parallelism {e.bank_parallelism.toFixed(2)}</span>
        )}
      </div>
    </div>
  );
}

// --- address map inspector ---------------------------------------------------

/**
 * §8.20's address-map inspector: type an address, see (bank, row, col).
 *
 * The decomposition is done here rather than server-side because it is pure
 * arithmetic on bit ranges the pack already stated — a round trip per
 * keystroke would be a network call to compute three shifts.
 */
function AddressMap({ per }: { per: MemoryInterface }) {
  const [text, setText] = useState("");
  const [pattern, setPattern] = useState("");
  const decoded = useMemo(() => decodeAddress(text, per.address_map), [text, per.address_map]);
  const simulated = useMemo(() => {
    if (!pattern.trim()) return null;
    try { return addressPattern(pattern, per.address_map); }
    catch (error) { return error instanceof Error ? error.message : String(error); }
  }, [pattern, per.address_map]);

  return (
    <div className="mem-card" data-testid="mem-addrmap">
      <div className="perf-card-title">Address map</div>
      <input
        className="checks-filter"
        placeholder="an address, e.g. 0x00401004"
        value={text}
        onChange={(e) => setText(e.target.value)}
        data-testid="mem-addr-input"
      />
      {decoded === null && text.trim() !== "" && (
        <div className="pane-hint" data-testid="mem-addr-error">
          Enter a whole non-negative decimal or <code>0x</code> hex address; the entire input must be valid.
        </div>
      )}
      {decoded && (
        <table className="perf-legend" data-testid="mem-addr-out">
          <tbody>
            {decoded.map(([name, value, hi, lo]) => (
              <tr key={name}>
                <td>{name}</td>
                <td className="num">
                  {name === "bank" ? value.toString() : `0x${value.toString(16)}`}
                </td>
                <td className="num dim">
                  [{hi}:{lo}]
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {Object.keys(per.address_map).length === 0 ? (
        <div className="pane-hint">
          This pack states no <code>[address_map]</code>, so there is no mapping to
          show.
        </div>
      ) : (
        <div className="pane-hint">
          The mapping comes from the pack's <code>[address_map]</code>, not from the
          commands observed — it answers what an address <em>would</em> map to.
        </div>
      )}
      <label>Address sequence
        <textarea aria-label="Address sequence" rows={3} style={{ width: "100%" }}
          maxLength={64000} placeholder="0x0, 0x4, 0x1000, 0x0" value={pattern}
          onChange={(e) => setPattern(e.target.value)} />
      </label>
      <div className="pane-hint">Hypothetical open-page policy, initially closed banks; comma or whitespace separated addresses. No simulated traffic is changed.</div>
      {typeof simulated === "string" ? <div role="alert">{simulated}</div> : simulated &&
        <div data-testid="mem-pattern-result">{simulated.hits} hits · {simulated.misses} misses · {simulated.conflicts} conflicts</div>}
    </div>
  );
}
