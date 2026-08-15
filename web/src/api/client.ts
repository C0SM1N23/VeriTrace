/**
 * Client for the VeriTrace backend (§10).
 *
 * The wave socket is request/response with a `done` flag rather than a stream
 * of unrelated messages: a request supersedes whatever was in flight, because
 * during a pan the only window that matters is the current one.
 */

import { decode, encode } from "@msgpack/msgpack";
import type {
  ChecksReport,
  CmdsResult,
  CorrelationReport,
  CoverageReport,
  DiffReport,
  Layout,
  Machine,
  MemoryReport,
  OpenSession,
  PerfReport,
  Repro,
  SessionStatus,
  SignalMeta,
  SourceFile,
  StuckResult,
  SubtraceResult,
  TxnQueryResult,
  TxnReport,
  WaveChunk,
  WhyResult,
} from "../lib/types";

export interface Root {
  name: string;
  version: string;
  default_session: string | null;
  max_px: number;
}

/**
 * REST goes through `/api` in dev so the Vite dev server can tell an API call
 * from a request for the app itself — `GET /` would otherwise return index.html.
 * In a production build the app is served by the backend, so there is no prefix.
 * The WebSocket always uses the real path, which the dev server proxies as-is.
 */
const API = import.meta.env.DEV ? "/api" : "";

/**
 * Origin for the WebSocket.
 *
 * In development it points straight at the backend instead of the dev server:
 * Vite runs its own HMR socket and its proxy refuses the upgrade for our path
 * with a 404. A production build is served by the backend, so the page origin
 * is already correct.
 *
 * `VITE_BACKEND` is baked in by `vite.config.ts` from the one value that also
 * configures the REST proxy, so `VERITRACE_BACKEND=...` moves both. The literal
 * below is only the fallback for a client built without that config.
 */
const WS_ORIGIN: string = import.meta.env.DEV
  ? (import.meta.env.VITE_BACKEND ?? "http://127.0.0.1:8765")
  : location.origin;

/**
 * `Accept: application/json` on every read, and it is not decoration.
 *
 * The API root answers a browser with the application itself and everything
 * else with JSON, so a request that does not say what it wants is answered by
 * whatever the engine's default `Accept` happens to be. That default is not the
 * same everywhere, and when it carried `text/html` the app asked for its own
 * configuration and was handed its own index page — "Unexpected token '<'".
 * Saying what we want makes it the server's decision instead of the browser's.
 */
async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} for ${url}`);
  return (await r.json()) as T;
}

/**
 * POST a JSON body and read a JSON answer, surfacing the server's own `detail`.
 *
 * The message matters: every refusal in §11.8 is written to say what to do next,
 * and a client that replaces it with "request failed: 409" throws that away.
 */
async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await post(url, body);
  return (await r.json()) as T;
}

async function post(url: string, body: unknown): Promise<Response> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(detail.detail ?? `${r.status} ${r.statusText} for ${url}`);
  }
  return r;
}

export async function fetchRoot(): Promise<Root> {
  return getJson<Root>(`${API}/`);
}

export async function fetchStatus(session: string): Promise<SessionStatus> {
  return getJson<SessionStatus>(`${API}/session/${session}/status`);
}

export async function fetchSignals(session: string, q = "", limit = 5000): Promise<SignalMeta[]> {
  const body = await getJson<{ signals: SignalMeta[] }>(
    `${API}/session/${session}/signals?q=${encodeURIComponent(q)}&limit=${limit}`,
  );
  return body.signals;
}

export async function fetchLayout(session: string): Promise<Layout> {
  return getJson<Layout>(`${API}/session/${session}/layout`);
}

export async function putLayout(session: string, layout: Layout): Promise<void> {
  const r = await fetch(`${API}/session/${session}/layout`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(layout),
  });
  if (!r.ok) throw new Error(`layout save failed: ${r.status}`);
}

export interface WaveRequest {
  signals: number[];
  t0: number;
  t1: number;
  pxWidth: number;
}

type ChunkHandler = (chunks: WaveChunk[], req: WaveRequest) => void;

/** WebSocket that keeps at most one wave request outstanding. */
export class WaveSocket {
  private ws: WebSocket | null = null;
  private pending: WaveRequest | null = null;
  private inflight: WaveRequest | null = null;
  private buffer: WaveChunk[] = [];
  private onChunks: ChunkHandler;
  private onOpenCb: (() => void) | null = null;
  private closed = false;

  constructor(
    private session: string,
    onChunks: ChunkHandler,
  ) {
    this.onChunks = onChunks;
  }

  connect(): Promise<void> {
    const url = `${WS_ORIGIN.replace(/^http/, "ws")}/session/${this.session}/ws`;
    this.ws = new WebSocket(url);
    this.ws.binaryType = "arraybuffer";
    return new Promise((resolve, reject) => {
      if (!this.ws) return reject(new Error("no socket"));
      this.ws.onopen = () => {
        this.onOpenCb?.();
        this.flush();
        resolve();
      };
      this.ws.onerror = () => reject(new Error("websocket error"));
      this.ws.onmessage = (ev) => this.handle(ev);
      this.ws.onclose = () => {
        if (!this.closed) {
          // A dropped socket must not wedge the UI; retry quietly.
          setTimeout(() => !this.closed && this.connect().catch(() => {}), 1000);
        }
      };
    });
  }

  private handle(ev: MessageEvent): void {
    if (!(ev.data instanceof ArrayBuffer)) return;
    const msg = decode(new Uint8Array(ev.data)) as Record<string, unknown>;
    const op = msg.op as string;
    if (op === "wave_chunk") {
      const chunk = msg as unknown as WaveChunk;
      if (chunk.h !== null && chunk.h !== undefined) this.buffer.push(chunk);
      if (chunk.done) {
        const req = this.inflight;
        const chunks = this.buffer;
        this.buffer = [];
        this.inflight = null;
        if (req) this.onChunks(chunks, req);
        this.flush();
      }
    } else if (op === "error") {
      // Report and keep going: one bad signal must not stall the viewport.
      console.warn("veritrace:", msg.message);
      if (msg.done) {
        this.inflight = null;
        this.buffer = [];
        this.flush();
      }
    }
  }

  /** Queue a window. A newer request replaces an unsent older one. */
  request(req: WaveRequest): void {
    this.pending = req;
    this.flush();
  }

  private flush(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (this.inflight || !this.pending) return;
    const req = this.pending;
    this.pending = null;
    this.inflight = req;
    this.buffer = [];
    this.ws.send(
      encode({
        op: "wave",
        signals: req.signals,
        t0: Math.floor(req.t0),
        t1: Math.ceil(req.t1),
        px_width: Math.max(1, Math.round(req.pxWidth)),
      }),
    );
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
  }
}

export async function runQuery(session: string, vtq: string): Promise<WhyResult> {
  return postJson<WhyResult>(`${API}/session/${session}/query`, { vtq });
}

// --- §8.2, §8.3, §11.5, §12 — subtrace, repro, replay, report --------------

/** §8.2's minimised chain, narrated — the steps Replay mode walks (§11.5). */
export async function fetchSubtrace(session: string, vtq: string): Promise<SubtraceResult> {
  return postJson<SubtraceResult>(`${API}/session/${session}/subtrace`, { vtq });
}

/** §8.3's testbench. `validate` compiles and runs it, which takes seconds. */
export async function buildRepro(
  session: string,
  vtq: string,
  opts: { mode?: string; validate?: boolean } = {},
): Promise<Repro> {
  return postJson<Repro>(`${API}/session/${session}/repro`, {
    vtq,
    mode: opts.mode ?? "auto",
    validate: opts.validate ?? true,
  });
}

/**
 * §12's report, downloaded as a file.
 *
 * A blob rather than a new tab: the page is an attachment the server names, and
 * `window.open` on a POST route is not a thing.
 */
export async function downloadReport(session: string, vtq: string): Promise<string> {
  const r = await post(`${API}/session/${session}/export`, { vtq, validate: true });
  const blob = await r.blob();
  const name =
    /filename="([^"]+)"/.exec(r.headers.get("content-disposition") ?? "")?.[1] ?? "bug_report.html";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  return name;
}

// --- §8.8 — FSM mode -------------------------------------------------------

export async function fetchMachines(session: string): Promise<Machine[]> {
  const got = await getJson<{ machines: Machine[] }>(`${API}/session/${session}/fsm`);
  return got.machines;
}

/** The URL "Export SVG" downloads — the server renders it, so CLI and UI agree. */
export function fsmSvgUrl(session: string, signal: string): string {
  return `${API}/session/${session}/fsm/${encodeURIComponent(signal)}/svg`;
}

// --- §8.7, TAB 5 — diff ----------------------------------------------------

/** Every trace this server has open, for the two-trace selector of §11.4. */
export async function fetchSessions(): Promise<OpenSession[]> {
  const got = await getJson<{ sessions: OpenSession[] }>(`${API}/sessions`);
  return got.sessions;
}

export async function runDiff(
  session: string,
  trace: string,
  opts: { strategy?: string; anchor?: string | null; ignore?: string[] } = {},
): Promise<DiffReport> {
  return postJson<DiffReport>(`${API}/session/${session}/diff`, {
    trace,
    strategy: opts.strategy ?? "cycle",
    anchor: opts.anchor ?? null,
    ignore: opts.ignore ?? [],
  });
}

export async function fetchSource(session: string, file: string): Promise<SourceFile> {
  return getJson<SourceFile>(`${API}/session/${session}/source/${file}`);
}

export async function fetchChecks(session: string): Promise<ChecksReport> {
  return getJson<ChecksReport>(`${API}/session/${session}/checks`);
}

/**
 * §8.4 at another threshold. Only the stuck scan re-runs, not the suite: the
 * right window depends on the run, so it has to be cheap enough to try.
 */
export async function fetchStuck(session: string, cycles: number): Promise<StuckResult> {
  return getJson<StuckResult>(`${API}/session/${session}/stuck?cycles=${cycles}`);
}

/** §7.2 — the rate in the status bar, and the names it is a summary of. */
export async function fetchCorrelation(session: string): Promise<CorrelationReport> {
  return getJson<CorrelationReport>(`${API}/session/${session}/correlation`);
}

/** §11.4: suppress a finding. The reason is required by the server too. */
export async function suppressFinding(
  session: string,
  id: string,
  reason: string,
): Promise<void> {
  await post(`${API}/session/${session}/checks/${id}/suppress`, { reason });
}

export async function unsuppressFinding(session: string, id: string): Promise<void> {
  const r = await fetch(`${API}/session/${session}/checks/${id}/suppress`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(`unsuppress failed: ${r.status}`);
}

// --- TAB 8, Transactions (§8.13-8.14) --------------------------------------

export async function fetchTransactions(session: string): Promise<TxnReport> {
  return getJson<TxnReport>(`${API}/session/${session}/transactions`);
}

/** Run a `txn(...)` pipeline (§10.1). */
export async function runTxnQuery(session: string, vtq: string): Promise<TxnQueryResult> {
  return postJson<TxnQueryResult>(`${API}/session/${session}/transactions/query`, { vtq });
}

// --- TAB 9, Performance (§8.17-8.18) ---------------------------------------

export async function fetchPerformance(session: string): Promise<PerfReport> {
  return getJson<PerfReport>(`${API}/session/${session}/performance`);
}

// --- TAB 10, Memory (§8.20) ------------------------------------------------

export async function fetchMemory(session: string): Promise<MemoryReport> {
  return getJson<MemoryReport>(`${API}/session/${session}/memory`);
}

/**
 * The decoded command stream, fetched separately from the summary: it is one
 * row per command over a whole run, so `/memory` deliberately leaves it out
 * and the tab asks for it only when the command list is actually shown.
 */
export async function fetchCommands(session: string, iface: string): Promise<CmdsResult> {
  const r = await fetch(`${API}/session/${session}/transactions/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ vtq: `cmds(${iface})` }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(detail.detail ?? `query failed: ${r.status}`);
  }
  return (await r.json()) as CmdsResult;
}

// --- TAB 7, Coverage (§8.21, §8.12) ----------------------------------------

export async function fetchCoverage(session: string): Promise<CoverageReport> {
  return getJson<CoverageReport>(`${API}/session/${session}/coverage`);
}
