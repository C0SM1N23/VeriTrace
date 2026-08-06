/** Shapes shared between the app, the store and the render worker. */

export type Radix = "hex" | "dec" | "bin" | "ascii";

export interface SignalMeta {
  handle: number;
  path: string;
  name: string;
  scope: string;
  width: number;
  kind: string;
  stream_id: number;
  msb: number | null;
  lsb: number | null;
  array_index: number | null;
  n_events: number;
}

/** A row in the wave list: either a signal or a group header. */
export type Row =
  | { kind: "signal"; handle: number; path: string }
  | { kind: "group"; id: string; name: string; collapsed: boolean };

/** One entry of an exact (non-reduced) window: [time, bits]. */
export type ExactPoint = [number, string];

/**
 * One entry of a reduced window: [time, min, max, n_changes, flags].
 * flags: 1 = contains X, 2 = contains Z.
 */
export type MinMaxPoint = [number, string, string, number, number];

export const FLAG_X = 1;
export const FLAG_Z = 2;

export interface WaveChunk {
  h: number;
  t0: number;
  t1: number;
  px_width: number;
  mode: "exact" | "minmax";
  /** Settled value at t0, so the leading edge can be drawn. */
  initial: string | null;
  transitions: ExactPoint[] | MinMaxPoint[];
  done: boolean;
}

export interface Layout {
  version?: number;
  signals: Row[];
  groups: unknown[];
  radix: Record<string, Radix>;
  bookmarks: unknown[];
  cursors: number[];
  zoom: { t0: number; t1: number } | null;
  /** View preferences stored alongside the layout. */
  rulerMode?: "time" | "cycle";
  rowH?: number;
}

export interface SessionStatus {
  phase: string;
  progress: number;
  correlation_rate: number | null;
  n_signals: number;
  n_events: number;
  t0: number;
  t1: number;
  timescale: string;
  source_sha256: string | null;
  /** False when no RTL was supplied: causal analysis is off (§7.4). */
  has_rtl: boolean;
  rtl_files: string[];
  rtl_sha256: string | null;
  /** RTL edited since the trace was made (§5.7) — warn, do not block. */
  rtl_changed: boolean;
  n_rtl_signals: number;
  rtl_error: string;
  /** §11.4b — which tab to open on, decided by the server from the design. */
  default_tab: string;
  n_findings: number;
  /** §8.14 — protocol interfaces detected, and what they produced. */
  n_interfaces: number;
  n_transactions: number;
  protocol_error: string;
  clock: string | null;
  clock_method: string | null;
  n_cycles: number;
}

// --- TAB 6, Checks (§11.4) -------------------------------------------------

export type FindingGroup = "stuck" | "x_sources" | "protocol" | "lint" | "parameters";
export type Severity = "error" | "warn" | "info";

export interface Finding {
  id: string;
  group: FindingGroup;
  severity: Severity;
  check: string;
  title: string;
  signal: string | null;
  loc: { file: string; line: number; col: number } | null;
  time: number | null;
  detail: string;
  /** VTQ to run for `[why]`; null when there is nothing to explain. */
  why: string | null;
  notes: string[];
  related: string[];
}

/** §8.11c — the elaborated parameter tree. */
export interface ParamNode {
  path: string;
  module: string;
  params: { name: string; value: string; overridden: boolean; shadowed: boolean }[];
  children: ParamNode[];
}

export interface ChecksReport {
  findings: Finding[];
  groups: Partial<Record<FindingGroup, number>>;
  counts: Record<string, number>;
  /** Checks that could not run, and why. Never silently empty (P7). */
  skipped: Record<string, string>;
  ms: number;
  suppressed: Record<string, string>;
  parameters: ParamNode | null;
}

/** Messages the main thread sends to the render worker. */
export type ToWorker =
  | { type: "init"; canvas: OffscreenCanvas; dpr: number }
  | { type: "resize"; width: number; height: number; dpr: number }
  | { type: "theme"; colors: Record<string, string> }
  | { type: "data"; chunks: WaveChunk[] }
  | { type: "clear" }
  | {
      type: "view";
      t0: number;
      t1: number;
      scrollY: number;
      rowH: number;
      rows: RenderRow[];
      cursor: number | null;
      markers: number[];
      rulerMode: "time" | "cycle";
      clockPeriod: number | null;
      clockOrigin: number;
      timescale: string;
    }
  | { type: "stats"; enabled: boolean };

/** A row as the worker needs it: no React, no store. */
export interface RenderRow {
  kind: "signal" | "group";
  handle: number;
  label: string;
  width: number;
  radix: Radix;
}

export type FromWorker =
  | { type: "ready" }
  | { type: "frame"; ms: number }
  | { type: "stats"; frames: number; avgMs: number; maxMs: number };

// --- causality (§8.1) ------------------------------------------------------

export interface CausalNode {
  signal: string;
  time: number;
  value: string;
  /** `txn_link` is §8.16: the chain crossed into a transaction. */
  kind: "assigned" | "hold" | "conflict" | "terminal" | "txn_link";
  reason: string;
  loc: { file: string; line: number; col: number } | null;
  last_change: number | null;
  is_primary_path: boolean;
  detail: string;
  /** `m1.WRITE[7]` on a txn_link node. */
  txn: string | null;
  /**
   * The answer is a DAG: the same question reached twice is one node. A repeat
   * arrives as a stub with no children, rendered as a back-reference.
   */
  repeated?: boolean;
  children: CausalNode[];
}

export interface WhyResult {
  query: string;
  signal: string;
  time: number;
  /** Set when the question was asked at transaction level (§8.16). */
  headline?: string;
  root: CausalNode;
  stats: { nodes: number; ms: number; truncated: boolean };
}

export interface SourceFile {
  file: string;
  text: string;
  /** line number (as a string) -> signals declared or driven there. */
  signals: Record<string, { path: string; handle: number | null; role: string }[]>;
}

/** §11.6: one selection, shared by every panel. */
export interface Selection {
  signals: string[];
  time: number | null;
  sourceLoc: { file: string; line: number } | null;
  causalNode: string | null;
}

// --- TAB 8, Transactions (§8.13-8.14, §11.4b) ------------------------------

export interface InterfaceMeta {
  name: string;
  scope: string;
  prefix: string;
  pack: string;
  pack_slug: string;
  clock: string | null;
  reset: string | null;
  /** Other names the same wires appear under (master port vs slave port). */
  aliases: string[];
  signals: Record<string, string>;
}

export interface ProtocolViolation {
  rule: string;
  severity: Severity;
  msg: string;
  time: number;
  signal: string | null;
  txn: string | null;
}

export interface InterfaceReport {
  interface: InterfaceMeta;
  n_transactions: number;
  n_open: number;
  n_events: number;
  n_matched: number;
  /** §8.14's acceptance criterion: always stated, null when nothing moved. */
  correlation: number | null;
  sampled_cycles: number;
  kinds: string[];
  violations: ProtocolViolation[];
  skipped: Record<string, string>;
  ms: number;
  parquet: string | null;
}

export interface PackMeta {
  name: string;
  slug: string;
  version: string;
  channels: string[];
  transactions: string[];
  rules: string[];
  warnings: string[];
}

export interface TxnReport {
  interfaces: InterfaceReport[];
  packs: PackMeta[];
  errors: string[];
  ms: number;
}

export interface Transaction {
  ref: string;
  iface: string;
  kind: string;
  index: number;
  start_time: number;
  end_time: number | null;
  status: "complete" | "open";
  id: number | string | null;
  fields: Record<string, number | string | null>;
  metrics: Record<string, number | string | null>;
  outstanding: number;
  n_events: number;
  violations: ProtocolViolation[];
}

export interface TxnQueryResult {
  query: string;
  transactions: Transaction[];
  n: number;
  scanned: number;
  interfaces: string[];
}

// --- TAB 9, Performance (§8.17-8.18, §11.4b) -------------------------------

export interface StallProfile {
  iface: string;
  buckets: string[];
  counts: Record<string, number>;
  /** Largest-remainder rounded, so the labels add to exactly 100. */
  shares: Record<string, number>;
  total: number;
  lost: number;
  series: { t0: number; t1: number; cycles: number; counts: Record<string, number> }[];
  reasons: Record<string, string>;
  notes: Record<string, string>;
}

export interface Histogram {
  n: number;
  min: number;
  max: number;
  mean: number;
  p50: number;
  p95: number;
  p99: number;
  bins: { lo: number; hi: number; n: number }[];
  /** The clickable tail (§8.17): each names a real transaction. */
  outliers: { ref: string; value: number }[];
  unit: string;
}

export interface PerfSeries {
  name: string;
  unit: string;
  window: number;
  points: { t: number; v: number }[];
}

export interface AgentShare {
  agent: string;
  requested: number;
  granted: number;
  bytes: number;
  starved: number;
  starved_at: number | null;
}

export interface Fairness {
  agents: AgentShare[];
  index: number;
  worst: string | null;
  worst_cycles: number;
}

export interface InterfacePerf {
  iface: string;
  stalls: StallProfile | null;
  latency: Histogram | null;
  latency_by: Record<string, Histogram>;
  throughput: PerfSeries | null;
  outstanding: PerfSeries | null;
  outstanding_peak: number;
  outstanding_plateau: boolean;
  burst_efficiency: number | null;
  bytes_moved: number;
  peak_bytes_per_cycle: number | null;
  notes: Record<string, string>;
}

export interface WaitEdge {
  agent: string;
  resource: string;
  holder: string | null;
  since: number;
  until: number;
  txn: string | null;
  channel: string;
  blocked: string;
  holder_txn: string | null;
  cycles: number;
  evidence: string;
  /** §8.18's "[Why on each link]", ready to run. */
  why: string;
}

export interface Deadlock {
  agents: string[];
  edges: WaitEdge[];
  at: number;
  cycles: number;
  first_txn: string | null;
  first_txn_at: number | null;
  first_blocked: string;
}

export interface Livelock {
  signal: string;
  since: number;
  until: number;
  toggles: number;
  states: string[];
  completed: number;
}

export interface Starvation {
  agent: string;
  since: number;
  until: number;
  cycles: number;
  served: string[];
}

export interface PerfReport {
  interfaces: InterfacePerf[];
  fairness: Fairness | null;
  liveness: {
    deadlocks: Deadlock[];
    livelocks: Livelock[];
    starvation: Starvation[];
    skipped: Record<string, string>;
  };
  wait_for: WaitEdge[];
  errors: string[];
  ms: number;
}

// --- TAB 10, Memory (§8.20, §11.4b) ----------------------------------------

export interface CmdEvent {
  time: number;
  name: string;
  fields: Record<string, number | null>;
}

export interface BankSegment {
  bank: number;
  /** idle | activating | active | precharging */
  state: string;
  t0: number;
  t1: number;
  row: number | null;
}

export interface TimingViolation {
  constraint: string;
  /** null for a device-wide constraint (tFAW, tRFC, tREFI). */
  bank: number | null;
  at: number;
  measured_cycles: number;
  limit_cycles: number;
  /** True only for tREFI, where the limit is a maximum rather than a minimum. */
  is_maximum: boolean;
  first: CmdEvent | null;
  second: CmdEvent | null;
}

export interface RowStats {
  hits: number;
  misses: number;
  conflicts: number;
  hit_rate: number | null;
}

export interface MemorySeries {
  name: string;
  unit: string;
  window: number;
  points: { t: number; v: number }[];
}

export interface MemoryEfficiency {
  row_hits: RowStats;
  per_bank: Record<string, RowStats>;
  row_hit_series: MemorySeries | null;
  bus_utilization: number | null;
  bus_utilization_series: MemorySeries | null;
  refresh_overhead: number | null;
  turnaround_cycles: number;
  turnaround_events: number;
  bank_parallelism: number | null;
  bank_parallelism_series: MemorySeries | null;
}

export interface MemoryInterface {
  iface: string;
  chip: string;
  n_banks: number;
  signals: Record<string, string>;
  /** `[hi, lo]` bit ranges the server resolved from the pack's [address_map]. */
  address_map: Record<string, [number, number]>;
  n_commands: number;
  segments: BankSegment[];
  violations: TimingViolation[];
  efficiency: MemoryEfficiency;
  /** constraint -> violation count, zero included: "conformant" is a fact. */
  checked: Record<string, number>;
  skipped: Record<string, string>;
  ms: number;
}

export interface MemoryReport {
  interfaces: MemoryInterface[];
  errors: string[];
}

export interface CmdsResult {
  kind: string;
  iface: string;
  commands: CmdEvent[];
}
