/**
 * §11.3's left column: the design tree.
 *
 * The layout in §11.3 has three columns — HIERARCHY, main panel, INSPECTOR —
 * and this is the first of them. It was specified, the backend for it was
 * built (`GET /hierarchy`, lazy per level, §10.1), and the frontend never
 * called it: the only way to put a signal on screen was the command palette,
 * one signal per invocation. On a design with a few thousand signals that is
 * not a viewer anybody uses next to ModelSim's `add wave -r /*`.
 *
 * Two rules shape it:
 *
 * * **Lazy per level.** A 50k-signal design must not serialise its whole tree
 *   to show the top of it, so a scope's children are fetched when it is opened
 *   and kept after that.
 * * **Adding is bulk and idempotent.** Clicking a scope adds everything under
 *   it, under a group header named after the scope; clicking it again adds
 *   nothing, because everything is already there.
 */

import { useEffect, useState } from "react";
import { fetchHierarchy, fetchSignals, fetchSubtreeSignals } from "../api/client";
import type { HierarchyLevel, HierarchyScope, SignalMeta } from "../lib/types";
import { useWave } from "../state/store";

export function HierarchyPanel() {
  const session = useWave((s) => s.session);
  const addSignals = useWave((s) => s.addSignals);
  const registerSignals = useWave((s) => s.registerSignals);
  const [levels, setLevels] = useState<Record<string, HierarchyLevel>>({});
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  // §11.3 spells this column "arbore + cautare". The tree was built and the
  // search was not, so finding one signal in a deep design meant opening every
  // scope on the way down to it.
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SignalMeta[] | null>(null);

  useEffect(() => {
    const q = query.trim();
    if (!session || q.length < 2) {
      setHits(null);
      return;
    }
    // Debounced: this is a keystroke handler talking to the server.
    let live = true;
    const id = window.setTimeout(() => {
      void fetchSignals(session, q, 200)
        .then((rows) => live && setHits(rows))
        .catch(() => live && setHits([]));
    }, 150);
    return () => {
      live = false;
      window.clearTimeout(id);
    };
  }, [session, query]);

  const load = async (path: string) => {
    if (!session || levels[path]) return;
    try {
      const level = await fetchHierarchy(session, path);
      setLevels((prev) => ({ ...prev, [path]: level }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // The root level, and the top scope opened for you. A tree that starts fully
  // collapsed makes the first click mean nothing, and designs have exactly one
  // top module far more often than not.
  useEffect(() => {
    if (!session) return;
    void (async () => {
      const root = await fetchHierarchy(session, "").catch(() => null);
      if (!root) return;
      setLevels({ "": root });
      if (root.scopes.length === 1) {
        const only = root.scopes[0].path;
        setOpen(new Set([only]));
        const level = await fetchHierarchy(session, only).catch(() => null);
        if (level) setLevels((prev) => ({ ...prev, [only]: level }));
      }
    })();
  }, [session]);

  const toggle = (scope: HierarchyScope) => {
    const next = new Set(open);
    if (next.has(scope.path)) {
      next.delete(scope.path);
    } else {
      next.add(scope.path);
      void load(scope.path);
    }
    setOpen(next);
  };

  /** `add wave -r`: everything at or below a scope, as one group. */
  const addScope = async (scope: HierarchyScope) => {
    if (!session) return;
    setBusy(scope.path);
    setError("");
    try {
      const rows = await fetchSubtreeSignals(session, scope.path);
      // The session bootstrap intentionally fetches only the first 5k signal
      // descriptors.  A lazy subtree may contain handles beyond that prefix;
      // register the metadata returned by this request before adding them.
      registerSignals(rows);
      const n = addSignals(
        rows.map((r) => r.handle),
        scope.name,
      );
      setNote(
        n === 0
          ? `${scope.name}: already on screen`
          : `+${n} signal${n === 1 ? "" : "s"} from ${scope.name}`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  if (!session) return <div className="tree" data-testid="hierarchy" />;

  return (
    <div className="tree" data-testid="hierarchy">
      <div className="tree-head">
        <span className="dim">DESIGN</span>
        <span className="spacer" />
        <span className="dim mono" data-testid="hierarchy-note">
          {error || note}
        </span>
      </div>
      <input
        className="tree-search"
        placeholder="find a signal…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        data-testid="tree-search"
      />
      {hits !== null ? (
        <div className="tree-body" data-testid="tree-hits">
          {hits.length === 0 ? (
            <div className="pane-hint">
              Nothing matches <span className="mono">{query}</span> in this trace.
            </div>
          ) : (
            hits.map((sig) => <SignalRow key={sig.handle} sig={sig} depth={0} />)
          )}
        </div>
      ) : (
      <div className="tree-body">
        {(levels[""]?.scopes ?? []).map((s) => (
          <Node
            key={s.path}
            scope={s}
            depth={0}
            levels={levels}
            open={open}
            busy={busy}
            onToggle={toggle}
            onAddScope={addScope}
          />
        ))}
        {(levels[""]?.signals ?? []).map((sig) => (
          <SignalRow key={sig.handle} sig={sig} depth={0} />
        ))}
      </div>
      )}
    </div>
  );
}

function Node({
  scope,
  depth,
  levels,
  open,
  busy,
  onToggle,
  onAddScope,
}: {
  scope: HierarchyScope;
  depth: number;
  levels: Record<string, HierarchyLevel>;
  open: Set<string>;
  busy: string | null;
  onToggle: (s: HierarchyScope) => void;
  onAddScope: (s: HierarchyScope) => void;
}) {
  const isOpen = open.has(scope.path);
  const level = levels[scope.path];
  return (
    <>
      <div className="tree-row" style={{ paddingLeft: `${depth * 12 + 4}px` }}>
        <button
          className="tree-twisty"
          onClick={() => onToggle(scope)}
          aria-expanded={isOpen}
          data-testid="tree-scope"
          data-path={scope.path}
          title={scope.path}
        >
          {scope.n_children > 0 || scope.n_signals > 0 ? (isOpen ? "▾" : "▸") : " "}
          <span className="tree-name">{scope.name}</span>
        </button>
        {/* The count is the point of the button next to it: you are told how
            much "add everything here" means before you press it. */}
        <span className="dim tree-count">{scope.n_signals}</span>
        <button
          className="chip tree-add"
          onClick={() => onAddScope(scope)}
          disabled={busy === scope.path}
          title={`Add all ${scope.n_signals} signals under ${scope.path} to Wave`}
          data-testid="tree-add-scope"
          data-path={scope.path}
        >
          {busy === scope.path ? "…" : "+ all"}
        </button>
      </div>
      {isOpen &&
        level &&
        level.scopes.map((child) => (
          <Node
            key={child.path}
            scope={child}
            depth={depth + 1}
            levels={levels}
            open={open}
            busy={busy}
            onToggle={onToggle}
            onAddScope={onAddScope}
          />
        ))}
      {isOpen &&
        level &&
        level.signals.map((sig) => <SignalRow key={sig.handle} sig={sig} depth={depth + 1} />)}
    </>
  );
}

function SignalRow({ sig, depth }: { sig: SignalMeta; depth: number }) {
  const addSignals = useWave((s) => s.addSignals);
  const shown = useWave((s) =>
    s.rows.some((r) => r.kind === "signal" && r.handle === sig.handle),
  );
  const width = sig.msb !== null ? `[${sig.msb}:${sig.lsb}]` : sig.width > 1 ? `${sig.width}b` : "";
  return (
    <div className="tree-row" style={{ paddingLeft: `${depth * 12 + 16}px` }}>
      <button
        className={`tree-sig${shown ? " on" : ""}`}
        onClick={() => addSignals([sig.handle])}
        title={shown ? `${sig.path} — already in Wave` : `Add ${sig.path} to Wave`}
        data-testid="tree-signal"
        data-path={sig.path}
      >
        <span className="tree-name mono">{sig.name}</span>
      </button>
      <span className="dim tree-count mono">{width}</span>
    </div>
  );
}
