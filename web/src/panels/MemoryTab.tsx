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
import { cycleAt, formatTime } from "../lib/time";
import type { MemoryInterface, TimingViolation } from "../lib/types";
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
    if (!report && !busy) void load();
  }, [report, busy, load]);

  if (busy && !report) return <div className="pane-note">Decoding the command bus…</div>;
  if (error && !report) return <div className="pane-note">{error}</div>;
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

      <Violations per={current} />
      <BankTimeline per={current} />
      <div className="mem-row">
        <CommandStream per={current} />
        <div className="mem-col">
          <RowHits per={current} />
          <AddressMap per={current} />
        </div>
      </div>
    </div>
  );
}

const Dot = () => <span className="dot">·</span>;

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
  const period = useWave((s) => s.clockPeriod);
  const origin = useWave((s) => s.clockOrigin);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");

  const at = (t: number) =>
    period ? `c${cycleAt(t, period, origin)}` : formatTime(t, timescale);

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
          <span aria-hidden>✓</span> Every constraint checked was met.
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
                  <td className="perf-agent">{at(v.at)}</td>
                  <td className="dim">{v.bank === null ? "device" : `bank ${v.bank}`}</td>
                  <td>
                    {v.first?.name}@{v.first ? at(v.first.time) : "?"}
                    <span className="dim"> → </span>
                    {v.second?.name}@{v.second ? at(v.second.time) : "?"}
                  </td>
                  <td className="warn">
                    {v.measured_cycles} cycles, {v.is_maximum ? "max" : "min"}{" "}
                    {v.limit_cycles}
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
  const period = useWave((s) => s.clockPeriod);
  const origin = useWave((s) => s.clockOrigin);
  const timescale = useWave((s) => s.status?.timescale ?? "1ns");
  const wires = Object.values(per.signals);

  const at = (t: number) =>
    period ? `c${cycleAt(t, period, origin)}` : formatTime(t, timescale);

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
            <span className="mem-cmd-t">{at(c.time)}</span>
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
  const decoded = useMemo(() => decodeAddress(text, per), [text, per]);

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
          Not a number. Try decimal or <code>0x</code> hex.
        </div>
      )}
      {decoded && (
        <table className="perf-legend" data-testid="mem-addr-out">
          <tbody>
            {decoded.map(([name, value, hi, lo]) => (
              <tr key={name}>
                <td>{name}</td>
                <td className="num">
                  {name === "bank" ? value : `0x${value.toString(16)}`}
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
    </div>
  );
}

function slice(value: number, hi: number, lo: number): number {
  // Arithmetic rather than `>>`/`&`: a 32-bit address with bit 31 set would
  // come back negative through JS's bitwise operators, which coerce to int32.
  return Math.floor(value / 2 ** lo) % 2 ** (hi - lo + 1);
}

/**
 * Decompose an address with the ranges the *server* resolved from the pack.
 * Hard-coding the shipped pack's bits here would silently ignore a project
 * that overrode `[address_map]` with its own memory map.
 */
function decodeAddress(
  text: string,
  per: MemoryInterface,
): [string, number, number, number][] | null {
  const s = text.trim();
  if (!s) return null;
  const n = s.toLowerCase().startsWith("0x") ? parseInt(s.slice(2), 16) : Number(s);
  if (!Number.isFinite(n) || Number.isNaN(n) || n < 0) return null;
  const order = ["bank", "row", "col"];
  const out: [string, number, number, number][] = [];
  for (const name of order) {
    const range = per.address_map[name];
    if (!range) continue;
    const [hi, lo] = range;
    out.push([name, slice(n, hi, lo), hi, lo]);
  }
  return out.length ? out : null;
}
