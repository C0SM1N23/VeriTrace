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
 * Layout is elkjs, as §8.8 names, loaded with this lazy pane and executed in a
 * Web Worker. A session that never opens this mode neither downloads the
 * worker nor spends main-thread time on graph layout.
 */

import ELK, { type ELK as ElkInstance } from "elkjs/lib/elk-api.js";
import elkWorkerUrl from "elkjs/lib/elk-worker.min.js?url";
import { useEffect, useMemo, useRef, useState } from "react";
import { fsmSvgUrl } from "../api/client";
import type { FsmTransition, Machine, Row } from "../lib/types";
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
    labels?: { x?: number; y?: number; width?: number; height?: number }[];
  }[];
}

interface Placed {
  nodes: { id: number; x: number; y: number; w: number; h: number }[];
  edges: {
    id: string;
    points: { x: number; y: number }[];
    label: string;
    at: { x: number; y: number } | null;
    transition: FsmTransition;
  }[];
  width: number;
  height: number;
}

const NODE_W = 96;
const NODE_H = 44;
/** How much a long-lived state may grow, relative to the base height. */
const MAX_GROW = 1.8;
/**
 * Size of an edge label, so elk can reserve room for it.
 *
 * Without this elk lays the graph out as if the labels were not there, every
 * edge between the same pair of states gets the same route, and two guards land
 * on top of each other — which is what a reader notices first and trusts least.
 * 6px per character is `--font-data` at the 10px `.fsm-edge text` sets.
 */
const LABEL_CHAR_W = 6;
const LABEL_H = 13;

// One worker can multiplex layout requests and lives for the lifetime of the
// lazily loaded FSM module. Constructing one in every effect leaked a worker on
// each machine selection and made larger projects progressively more costly.
let layoutEngine: ElkInstance | null = null;

function getLayoutEngine(): ElkInstance {
  layoutEngine ??= new ELK({ workerUrl: elkWorkerUrl });
  return layoutEngine;
}

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

/** elkjs runs in the shared worker loaded with this mode. */
function useLayout(machine: Machine): { placed: Placed | null; error: string | null; retry: () => void } {
  const [placed, setPlaced] = useState<Placed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setPlaced(null);
    setError(null);
    void (async () => {
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
          transition: t,
        }));
      const graph = {
        id: "root",
        layoutOptions: {
          "elk.algorithm": "layered",
          "elk.direction": "DOWN",
          "elk.layered.spacing.nodeNodeBetweenLayers": "56",
          "elk.spacing.nodeNode": "44",
          // Drawn as polylines below, so ask for the routing that is actually
          // rendered: SPLINES spends effort on control points that were then
          // thrown away and joined with straight segments anyway.
          "elk.edgeRouting": "POLYLINE",
          "elk.spacing.edgeLabel": "6",
          "elk.layered.edgeLabels.sideSelection": "SMART_DOWN",
        },
        children: machine.states.map((s) => ({
          id: `s${s.value}`,
          width: NODE_W,
          height: heights.get(s.value) ?? NODE_H,
        })),
        // The label goes to elk, not just to the renderer: it is what stops two
        // guards between the same pair of states from being drawn on one spot.
        edges: edges.map(({ id, sources, targets, label: text }) => ({
          id,
          sources,
          targets,
          labels: [{ text, width: text.length * LABEL_CHAR_W, height: LABEL_H }],
        })),
      };
      const out = (await getLayoutEngine().layout(graph)) as ElkResult;
      if (!live) return;
      const byId = new Map(edges.map((e) => [e.id, e]));
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
          const l = e.labels?.[0];
          return {
            id: e.id,
            label: byId.get(e.id)?.label ?? "",
            transition: byId.get(e.id)!.transition,
            // Where elk put the label, in its own top-left coordinates; the
            // renderer only has to turn that into a baseline.
            at: l && l.x !== undefined && l.y !== undefined ? { x: l.x, y: l.y } : null,
            points: s ? [s.startPoint, ...(s.bendPoints ?? []), s.endPoint] : [],
          };
        }),
      });
    })().catch((e: unknown) => {
      if (live) setError(e instanceof Error ? e.message : String(e));
    });
    return () => {
      live = false;
    };
  }, [machine, attempt]);

  return { placed, error, retry: () => setAttempt((n) => n + 1) };
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
  const { placed, error, retry } = useLayout(machine);
  const scrub = useWave((s) => s.fsmScrub);
  const overlay = Object.keys(machine.visits).length > 0;
  const current = scrub === null ? null : stateAt(machine, scrub);

  const focusState = (value: number) => {
    const state = useWave.getState();
    const named = machine.states.find((candidate) => candidate.value === value);
    const rows = [machine.signal, ...machine.companions]
      .map((path) => state.signals.find((signal) => signal.path === path))
      .filter((signal) => signal !== undefined)
      .map(
        (signal) =>
          ({ kind: "signal", handle: signal.handle, path: signal.path }) satisfies Row,
      );
    state.focusRows(rows, `FSM ${named?.name ?? value}`);

    const intervals = segmentsOf(machine).filter((segment) => segment.state === value);
    if (intervals.length) {
      const first = intervals[0];
      const t0 = first.start;
      const t1 = first.end;
      state.setView({ t0, t1: Math.max(t0 + 1, t1) });
      state.setCursor(t0);
      useWave.setState({
        note: `${named?.name ?? value}: showing interval 1 of ${intervals.length} in Wave.`,
      });
    } else {
      useWave.setState({
        note: `${named?.name ?? value} was not visited in the loaded run.`,
      });
    }
    state.setTab(1);
  };

  const explainEdge = (transition: FsmTransition) => {
    if (!transition.loc) return;
    const query = `uncovered(${transition.loc.file})`;
    void useWave
      .getState()
      .runQueryText(query)
      .then(() => {
        const state = useWave.getState();
        const hole = state.coverage?.holes.find(
          (candidate) =>
            candidate.kind === "fsm-transition" &&
            candidate.signal === machine.signal &&
            candidate.file.endsWith(transition.loc!.file) &&
            candidate.line === transition.loc!.line,
        );
        if (hole) state.selectHole(`${hole.file}:${hole.line}:${hole.label}`);
      });
  };

  if (error) return <div className="pane-note error" role="alert">FSM layout failed: {error} <button onClick={retry}>Retry</button></div>;
  if (!placed) return <div className="pane-note">Laying out…</div>;

  return (
    <div className="fsm-canvas">
      {/* Natural size, capped by the pane rather than stretched to it: a
          three-state machine blown up to fill a 1600px canvas has 260px boxes
          and reads as a poster, not a diagram. `max-width` in the stylesheet
          shrinks the ones that genuinely are too big. */}
      <svg
        viewBox={`-20 -20 ${placed.width + 40} ${placed.height + 40}`}
        width={placed.width + 40}
        height={placed.height + 40}
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
            <g
              key={e.id}
              className={overlay && !n ? "fsm-edge untaken" : "fsm-edge"}
              data-testid={`fsm-edge-${e.id}`}
              onClick={overlay && !n ? () => explainEdge(e.transition) : undefined}
              role={overlay && !n ? "button" : undefined}
              tabIndex={overlay && !n ? 0 : undefined}
              onKeyDown={
                overlay && !n
                  ? (event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        explainEdge(e.transition);
                      }
                    }
                  : undefined
              }
            >
              {overlay && !n ? (
                <path
                  className="fsm-edge-hit"
                  d={e.points.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ")}
                  aria-hidden="true"
                />
              ) : null}
              <path
                className="fsm-edge-line"
                d={e.points.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ")}
                style={n ? { strokeWidth: 1.2 + Math.min(3, Math.log2(1 + n) / 2) } : undefined}
              />
              {e.points.length > 1 &&
                (() => {
                  // elk's placement when it gave one, the middle of the route
                  // when it did not. `at` is a top-left box, `text` wants a
                  // baseline, hence the drop of one line.
                  const mid = e.points[Math.floor(e.points.length / 2)];
                  const x = e.at ? e.at.x : mid.x + 6;
                  const y = e.at ? e.at.y + LABEL_H - 3 : mid.y - 4;
                  return (
                    <text x={x} y={y}>
                      {e.label}
                    </text>
                  );
                })()}
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
              onClick={() => focusState(n.id)}
              data-state={state.name}
              data-testid={`fsm-state-${state.name}`}
              role="button"
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  focusState(n.id);
                }
              }}
            >
              <rect width={n.w} height={n.h} rx={8} />
              {/* The counts go inside the box. Below it they sat exactly where
                  the outgoing edges leave, and every state with a transition
                  had its own numbers struck through by an arrow. */}
              <text
                x={n.w / 2}
                y={overlay ? n.h / 2 : n.h / 2 + 4}
                className="fsm-node-name"
              >
                {state.name}
              </text>
              {overlay && (
                <text x={n.w / 2} y={n.h / 2 + 13} className="fsm-node-sub">
                  {visits ? `${visits}× · ${machine.cycles_in[String(n.id)] ?? 0}${machine.clock_path ? "c" : " ticks"}` : "never"}
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
            title={`${machine.states[index(s.state)]?.name}: ${s.start}–${s.end} ticks`}
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
        <span className="dim">{scrub === null ? "drag to scrub" : `${scrub} ticks`}</span>
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
 * Built from the exact observed stays, including the last one before EOF.
 * The obvious shortcut — laying the per-state cycle totals out side by side —
 * draws the states in *declaration* order and reads as a timeline of a run that
 * never happened. Totals cannot answer "what was the sequence", and §8.8 step 6
 * asks exactly that.
 */
function segmentsOf(machine: Machine): Segment[] {
  return (machine.intervals ?? []).map(([start, end, state]) => ({ start, end, state }));
}

function stateAt(machine: Machine, time: number): number | null {
  const segs = segmentsOf(machine);
  for (const s of segs) {
    if (time >= s.start && time < s.end) return s.state;
  }
  const last = segs[segs.length - 1];
  return last && time === last.end ? last.state : null;
}
