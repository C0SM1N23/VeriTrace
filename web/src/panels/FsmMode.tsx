/**
 * FSM mode — §8.8, and §11.4's "MOD FSM (in tab-ul Source, nu tab propriu)".
 *
 * §11.4 is explicit about the consolidation and about why: *"FSM e o vedere
 * asupra structurii codului, nu un domeniu separat. Se activeaza din Source cu
 * ⌘M. 10 tab-uri era prea mult."* So this takes over the Source pane rather
 * than claiming a strip position, and leaving it puts the code back.
 *
 * What is drawn, from §8.8 step 5:
 *
 * * states filled when the run visited them, dotted red outline when it did not;
 * * node size proportional to the time spent in the state;
 * * edges thick in proportion to how often they were taken, thin and dotted
 *   when they never were, labelled with the guard and the count;
 * * a timeline under the diagram, coloured per state — scrubbing it lights the
 *   state that was current then and moves the shared cursor with it (§11.6).
 *
 * The overlay is *additive*. Everything above works with no trace at all, which
 * is the whole argument of §8.8: the findings in the Checks tab come from the
 * RTL, and the colours are a view on top of them.
 *
 * Layout is elkjs, as §8.8 names, loaded on demand — it is half a megabyte and
 * a session that never opens this mode should never pay for it.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { fsmSvgUrl } from "../api/client";
import type { Machine } from "../lib/types";
import { useWave } from "../state/store";

/**
 * The part of elk's answer this pane reads.
 *
 * Named rather than cast away: elk's own types describe every option it has,
 * and threading them through would say nothing that these six fields do not.
 */
interface ElkPoint {
  x: number;
  y: number;
}

interface ElkResult {
  width?: number;
  height?: number;
  children?: { id: string; x: number; y: number; width: number; height: number }[];
  edges?: {
    id: string;
    sections?: { startPoint: ElkPoint; endPoint: ElkPoint; bendPoints?: ElkPoint[] }[];
  }[];
}

interface Placed {
  nodes: { id: number; x: number; y: number; w: number; h: number }[];
  edges: { id: string; points: { x: number; y: number }[]; label: string }[];
  width: number;
  height: number;
}

const NODE_W = 96;
const NODE_H = 44;
/** How much a long-lived state may grow, relative to the base height. */
const MAX_GROW = 1.8;

export function FsmMode() {
  const open = useWave((s) => s.fsmOpen);
  const machines = useWave((s) => s.machines);
  const busy = useWave((s) => s.fsmBusy);
  const error = useWave((s) => s.fsmError);
  const signal = useWave((s) => s.fsmSignal);
  const session = useWave((s) => s.session);

  const machine = useMemo(
    () => machines.find((m) => m.signal === signal) ?? machines[0],
    [machines, signal],
  );

  if (!open) return null;

  return (
    <div className="fsm" data-testid="fsm-mode">
      <div className="fsm-head">
        <span className="dim">FSM</span>
        <select
          className="diff-select"
          value={machine?.signal ?? ""}
          onChange={(e) => useWave.getState().selectMachine(e.target.value)}
          data-testid="fsm-select"
        >
          {machines.map((m) => (
            <option key={m.signal} value={m.signal}>
              {m.signal.split(".").slice(-2).join(".")} — {m.states.length} states
              {coverage(m)}
            </option>
          ))}
        </select>
        <span className="spacer" />
        {machine && session && (
          <a
            className="chip"
            href={fsmSvgUrl(session, machine.signal)}
            download
            data-testid="fsm-svg"
            title="§8.8: export the diagram, for documentation and READMEs"
          >
            export SVG
          </a>
        )}
        <button
          className="chip"
          onClick={() => useWave.getState().setFsmOpen(false)}
          data-testid="fsm-close"
        >
          back to source <kbd>⌘M</kbd>
        </button>
      </div>

      {busy && <div className="pane-note">Extracting state machines…</div>}
      {error && <div className="pane-note error">{error}</div>}
      {!busy && !error && machines.length === 0 && (
        <div className="pane-note">
          No state machines in this design.
          <div className="pane-hint">
            §8.8 looks for a register assigned in an <code>always_ff</code> and compared
            against constants in its own guards. A counter is not one, whatever it is
            called.
          </div>
        </div>
      )}

      {machine && !busy && (
        <>
          <Diagram machine={machine} />
          <Timeline machine={machine} />
          <div className="fsm-why pane-hint">{machine.why_candidate}</div>
        </>
      )}
    </div>
  );
}

function coverage(m: Machine): string {
  const seen = m.states.filter((s) => (m.visits[String(s.value)] ?? 0) > 0).length;
  if (!Object.keys(m.visits).length) return "";
  return `, ${Math.round((100 * seen) / Math.max(1, m.states.length))}% covered`;
}

/** elkjs, loaded the first time this mode is opened and kept afterwards. */
function useLayout(machine: Machine): Placed | null {
  const [placed, setPlaced] = useState<Placed | null>(null);

  useEffect(() => {
    let live = true;
    setPlaced(null);
    void (async () => {
      const { default: ELK } = await import("elkjs/lib/elk.bundled.js");
      const elk = new ELK();
      const heights = new Map(
        machine.states.map((s) => [s.value, NODE_H * grow(machine, s.value)]),
      );
      // Only the edges elk can route: a `null` source means "from every state",
      // which is the reset, and drawing it from everywhere would be a hairball.
      const edges = machine.transitions
        .filter((t) => t.src !== null)
        .map((t, i) => ({
          id: `e${i}`,
          sources: [`s${t.src}`],
          targets: [`s${t.dst}`],
          label: label(machine, t.src as number, t.dst, t.guard),
        }));
      const graph = {
        id: "root",
        layoutOptions: {
          "elk.algorithm": "layered",
          "elk.direction": "DOWN",
          "elk.layered.spacing.nodeNodeBetweenLayers": "56",
          "elk.spacing.nodeNode": "44",
          "elk.edgeRouting": "SPLINES",
        },
        children: machine.states.map((s) => ({
          id: `s${s.value}`,
          width: NODE_W,
          height: heights.get(s.value) ?? NODE_H,
        })),
        edges: edges.map(({ id, sources, targets }) => ({ id, sources, targets })),
      };
      const out = (await elk.layout(graph)) as ElkResult;
      if (!live) return;
      const byId = new Map(edges.map((e) => [e.id, e.label]));
      setPlaced({
        width: out.width ?? 640,
        height: out.height ?? 480,
        nodes: (out.children ?? []).map((n) => ({
          id: Number(n.id.slice(1)),
          x: n.x,
          y: n.y,
          w: n.width,
          h: n.height,
        })),
        edges: (out.edges ?? []).map((e) => {
          const s = e.sections?.[0];
          return {
            id: e.id,
            label: byId.get(e.id) ?? "",
            points: s ? [s.startPoint, ...(s.bendPoints ?? []), s.endPoint] : [],
          };
        }),
      });
    })().catch(() => live && setPlaced(null));
    return () => {
      live = false;
    };
  }, [machine]);

  return placed;
}

function grow(m: Machine, value: number): number {
  const total = Object.values(m.cycles_in).reduce((a, b) => a + b, 0);
  if (!total) return 1;
  // §8.8: "marimea nodului ∝ timp petrecut in stare".
  return 1 + (MAX_GROW - 1) * ((m.cycles_in[String(value)] ?? 0) / total);
}

function label(m: Machine, src: number, dst: number, guard: string): string {
  const n = m.taken[`${src}->${dst}`] ?? 0;
  const short = guard.length > 26 ? guard.slice(0, 25) + "…" : guard;
  return n ? `${short}  ${n}×` : short;
}

function Diagram({ machine }: { machine: Machine }) {
  const placed = useLayout(machine);
  const scrub = useWave((s) => s.fsmScrub);
  const overlay = Object.keys(machine.visits).length > 0;
  const current = scrub === null ? null : stateAt(machine, scrub);

  if (!placed) return <div className="pane-note">Laying out…</div>;

  return (
    <div className="fsm-canvas">
      <svg
        viewBox={`-20 -20 ${placed.width + 40} ${placed.height + 40}`}
        className="fsm-svg"
        data-testid="fsm-diagram"
      >
        <defs>
          <marker
            id="fsm-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" />
          </marker>
        </defs>

        {placed.edges.map((e) => {
          const taken = /(\d+)×$/.exec(e.label);
          const n = taken ? Number(taken[1]) : 0;
          return (
            <g key={e.id} className={overlay && !n ? "fsm-edge untaken" : "fsm-edge"}>
              <path
                d={e.points.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ")}
                style={n ? { strokeWidth: 1.2 + Math.min(3, Math.log2(1 + n) / 2) } : undefined}
              />
              {e.points.length > 1 && (
                <text
                  x={e.points[Math.floor(e.points.length / 2)].x + 6}
                  y={e.points[Math.floor(e.points.length / 2)].y - 4}
                >
                  {e.label}
                </text>
              )}
            </g>
          );
        })}

        {placed.nodes.map((n) => {
          const state = machine.states.find((s) => s.value === n.id);
          if (!state) return null;
          const visits = machine.visits[String(n.id)] ?? 0;
          const cls = [
            "fsm-node",
            state.is_reset ? "reset" : "",
            overlay && !visits ? "unvisited" : "",
            current === n.id ? "current" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <g
              key={n.id}
              className={cls}
              transform={`translate(${n.x},${n.y})`}
              onClick={() => useWave.getState().selectMachine(machine.signal)}
              data-state={state.name}
            >
              <rect width={n.w} height={n.h} rx={8} />
              <text x={n.w / 2} y={n.h / 2 + 4} className="fsm-node-name">
                {state.name}
              </text>
              {overlay && (
                <text x={n.w / 2} y={n.h + 13} className="fsm-node-sub">
                  {visits ? `${visits}× · ${machine.cycles_in[String(n.id)] ?? 0}c` : "never"}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/**
 * §8.8 step 6 — the timeline under the diagram, one segment per stay.
 *
 * Rebuilt from the overlay's counts rather than from the waveform: the store
 * already has the state's transitions through the machine, and asking the
 * socket for them again would be a second source of truth about the same run.
 */
function Timeline({ machine }: { machine: Machine }) {
  const ref = useRef<HTMLDivElement>(null);
  const scrub = useWave((s) => s.fsmScrub);
  const segments = useMemo(() => segmentsOf(machine), [machine]);
  if (!segments.length) return null;

  const first = segments[0].start;
  const total = segments[segments.length - 1].end;
  const span = Math.max(1, total - first);
  const pct = (c: number) => (100 * (c - first)) / span;
  const index = (value: number) => machine.states.findIndex((s) => s.value === value);
  const onMove = (e: React.MouseEvent) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const at = Math.round(first + ((e.clientX - box.left) / box.width) * span);
    useWave.getState().setFsmScrub(Math.max(first, Math.min(at, total)));
  };

  return (
    <div className="fsm-timeline">
      <div
        className="fsm-track"
        ref={ref}
        onMouseMove={(e) => e.buttons === 1 && onMove(e)}
        onMouseDown={onMove}
        data-testid="fsm-timeline"
      >
        {segments.map((s, i) => (
          <div
            key={i}
            className="fsm-seg"
            style={{
              left: `${pct(s.start)}%`,
              width: `${Math.max(0.15, pct(s.end) - pct(s.start))}%`,
              background: shade(index(s.state), machine.states.length),
            }}
            title={`${machine.states[index(s.state)]?.name}: c${s.start}–c${s.end}`}
          />
        ))}
        {scrub !== null && <div className="fsm-cursor" style={{ left: `${pct(scrub)}%` }} />}
      </div>
      <div className="fsm-legend">
        {machine.states.map((s, i) => (
          <span key={s.value} className="fsm-key">
            <i style={{ background: shade(i, machine.states.length) }} />
            {s.name}
          </span>
        ))}
        <span className="spacer" />
        {machine.sequence_truncated && (
          <span className="dim">first {segments.length} stays only</span>
        )}
        <span className="dim">{scrub === null ? "drag to scrub" : `c${scrub}`}</span>
      </div>
    </div>
  );
}

/**
 * Grey ramp by state index.
 *
 * §11.1 spends colour on causality and on logic states, and nothing else. A
 * rainbow here would be the one thing the palette reserves, used for decoration.
 */
function shade(i: number, n: number): string {
  return `hsl(210 12% ${28 + 46 * (i / Math.max(1, n - 1))}%)`;
}

interface Segment {
  /** State *value*, not an index — the timeline is about the run, not the list. */
  state: number;
  start: number;
  end: number;
}

/**
 * The run, as the stays it actually made.
 *
 * Built from `machine.sequence`, which is the ordered list of state changes.
 * The obvious shortcut — laying the per-state cycle totals out side by side —
 * draws the states in *declaration* order and reads as a timeline of a run that
 * never happened. Totals cannot answer "what was the sequence", and §8.8 step 6
 * asks exactly that.
 */
function segmentsOf(machine: Machine): Segment[] {
  const seq = machine.sequence ?? [];
  if (seq.length < 2) return [];
  const out: Segment[] = [];
  for (let i = 0; i < seq.length - 1; i++) {
    const [cycle, state] = seq[i];
    const [next] = seq[i + 1];
    if (next > cycle) out.push({ state, start: cycle, end: next });
  }
  return out;
}

function stateAt(machine: Machine, cycle: number): number | null {
  const segs = segmentsOf(machine);
  for (const s of segs) {
    if (cycle >= s.start && cycle < s.end) return s.state;
  }
  return segs.length ? segs[segs.length - 1].state : null;
}
