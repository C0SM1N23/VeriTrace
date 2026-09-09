/**
 * TAB 3 — Source (§11.4).
 *
 * RTL with decorations derived from the trace:
 *  - a causality gutter, an amber bar on the lines of the current chain;
 *  - a value inlay to the right of every line that declares or drives signals,
 *    showing their value **at the cursor time**. Move the cursor in Wave and
 *    the numbers here change. That loop is the point of the tab (§11.4).
 *
 * The inlay updates through an imperative store subscription rather than a
 * React render, for the same reason the canvas does: the cursor moves often.
 */

import { useEffect, useMemo, useRef } from "react";
import { StreamLanguage } from "@codemirror/language";
import { verilog } from "@codemirror/legacy-modes/mode/verilog";
import { EditorState, StateEffect, StateField } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  GutterMarker,
  gutter,
  lineNumbers,
  WidgetType,
  type DecorationSet,
} from "@codemirror/view";

import { format } from "../lib/radix";
import type { CausalNode, SourceFile } from "../lib/types";
import { fetchValues } from "../api/client";
import { enumLabelsFor, useWave } from "../state/store";

/** Lines that belong to the current causal chain. */
const setCausalLines = StateEffect.define<Set<number>>();
const causalLines = StateField.define<Set<number>>({
  create: () => new Set(),
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setCausalLines)) return e.value;
    return value;
  },
});

class CausalMarker extends GutterMarker {
  toDOM() {
    const el = document.createElement("div");
    el.className = "cm-causal-bar";
    return el;
  }
}
const causalMarker = new CausalMarker();

const causalGutter = gutter({
  class: "cm-causal-gutter",
  lineMarker(view, line) {
    const lines = view.state.field(causalLines, false);
    if (!lines) return null;
    const n = view.state.doc.lineAt(line.from).number;
    return lines.has(n) ? causalMarker : null;
  },
  initialSpacer: () => causalMarker,
});

/** Value inlays, rebuilt whenever the cursor moves. */
const setInlays = StateEffect.define<Map<number, string>>();
const inlayField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(deco, tr) {
    for (const e of tr.effects) {
      if (e.is(setInlays)) {
        const marks = [];
        for (const [line, text] of [...e.value].sort((a, b) => a[0] - b[0])) {
          if (line < 1 || line > tr.state.doc.lines) continue;
          const l = tr.state.doc.line(line);
          marks.push(
            Decoration.widget({ widget: new InlayWidget(text), side: 1 }).range(l.to),
          );
        }
        return Decoration.set(marks);
      }
    }
    return deco.map(tr.changes);
  },
  provide: (f) => EditorView.decorations.from(f),
});

class InlayWidget extends WidgetType {
  constructor(private text: string) {
    super();
  }
  eq(other: InlayWidget) {
    return other.text === this.text;
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "cm-inlay";
    el.textContent = "  " + this.text;
    return el;
  }
  ignoreEvent() {
    return true;
  }
}

const theme = EditorView.theme(
  {
    "&": { backgroundColor: "var(--bg-deep)", color: "var(--text)", height: "100%" },
    ".cm-content": {
      fontFamily: "var(--font-data)",
      fontSize: "var(--fs-data)",
      lineHeight: "1.5",
    },
    ".cm-gutters": {
      backgroundColor: "var(--bg-panel)",
      color: "var(--text-dim)",
      border: "none",
      borderRight: "1px solid var(--line)",
    },
    ".cm-causal-gutter": { width: "3px", padding: 0 },
    // The bar needs a height of its own: stretching to 100% of a gutter
    // element that has not been sized yet renders nothing at all.
    ".cm-causal-bar": {
      background: "var(--causal)",
      width: "3px",
      height: "100%",
      minHeight: "1.5em",
    },
    ".cm-activeLine": { backgroundColor: "var(--bg-raised)" },
    ".cm-inlay": { color: "var(--text-dim)", fontStyle: "normal", opacity: 0.85 },
    ".cm-cursor": { borderLeftColor: "var(--text-bright)" },
    "&.cm-focused": { outline: "none" },
  },
  { dark: true },
);

function chainLines(root: CausalNode | undefined, file: string): Set<number> {
  const out = new Set<number>();
  const walk = (n: CausalNode) => {
    if (n.loc && n.loc.file === file) out.add(n.loc.line);
    n.children.forEach(walk);
  };
  if (root) walk(root);
  return out;
}

export function SourceTab() {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const source = useWave((s) => s.source);
  const busy = useWave((s) => s.sourceBusy);
  const status = useWave((s) => s.status);
  const causal = useWave((s) => s.causal);
  const sourceLoc = useWave((s) => s.sourceLoc);
  const machines = useWave((s) => s.machines);

  const lang = useMemo(() => StreamLanguage.define(verilog), []);

  // Build the editor once the file arrives.
  useEffect(() => {
    if (!hostRef.current || !source) return;
    viewRef.current?.destroy();
    const view = new EditorView({
      state: EditorState.create({
        doc: source.text,
        extensions: [
          lineNumbers(),
          causalLines,
          causalGutter,
          inlayField,
          lang,
          theme,
          EditorView.editable.of(false),
          EditorView.domEventHandlers({
            // §11.4: click an identifier to put it in Wave; alt-click to ask why.
            mousedown: (e, v) => onClick(e, v, source),
          }),
        ],
      }),
      parent: hostRef.current,
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, [source, lang]);

  // Causality gutter follows the current chain.
  useEffect(() => {
    const view = viewRef.current;
    if (!view || !source) return;
    view.dispatch({ effects: setCausalLines.of(chainLines(causal?.root, source.file)) });
  }, [causal, source]);

  // Value inlays follow the cursor, imperatively.
  useEffect(() => {
    if (!source) return;
    let serial = 0;
    let live = true;
    const handles = [
      ...new Set(
        Object.values(source.signals)
          .flat()
          .flatMap((sig) => (sig.handle === null ? [] : [sig.handle])),
      ),
    ];
    const write = async () => {
      const view = viewRef.current;
      const s = useWave.getState();
      if (!view || s.cursor === null || !s.session) return;
      const mine = ++serial;
      let values: Map<number, string | null>;
      try {
        values = await fetchValues(s.session, handles, s.cursor);
      } catch {
        if (live && mine === serial) view.dispatch({ effects: setInlays.of(new Map()) });
        return;
      }
      if (!live || mine !== serial) return;
      const inlays = new Map<number, string>();
      for (const [line, sigs] of Object.entries(source.signals)) {
        const parts: string[] = [];
        for (const sig of sigs.slice(0, 4)) {
          if (sig.handle === null) continue;
          const meta = s.signalsByHandle.get(sig.handle);
          const bits = values.get(sig.handle) ?? null;
          if (bits === null || !meta) continue;
          parts.push(
            `${meta.name} = ${format(
              bits,
              meta.width,
              s.radix[sig.path] ?? "hex",
              enumLabelsFor(s.machines, sig.path),
            )}`,
          );
        }
        if (parts.length) inlays.set(Number(line), parts.join("   "));
      }
      view.dispatch({ effects: setInlays.of(inlays) });
    };
    void write();
    const unsubscribe = useWave.subscribe((s, prev) => {
      if (s.cursor !== prev.cursor || s.radix !== prev.radix || s.machines !== prev.machines) {
        void write();
      }
    });
    return () => {
      live = false;
      unsubscribe();
    };
  }, [source, machines]);

  // Scroll to the selected line.
  useEffect(() => {
    const view = viewRef.current;
    if (!view || !sourceLoc || !source || sourceLoc.file !== source.file) return;
    const line = Math.min(Math.max(sourceLoc.line, 1), view.state.doc.lines);
    const pos = view.state.doc.line(line).from;
    view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
  }, [sourceLoc, source]);

  if (!status?.has_rtl) {
    return (
      <div className="pane-note">
        No RTL loaded.
        <div className="pane-hint">
          Start the server with <code>--rtl &lt;dir&gt;</code> to read the source here.
        </div>
      </div>
    );
  }
  if (busy) return <div className="pane-note">Loading source…</div>;
  if (!source) {
    return (
      <div className="pane-note">
        No file open.
        <div className="pane-hint">Run a why-trace, or click a causal card, to open its file.</div>
      </div>
    );
  }

  return (
    <div className="source">
      <div className="source-head mono">
        {source.file}
        <span className="spacer" />
        {/* §11.4: FSM is a *mode* of this tab. The button is here rather than in
            the strip because that is what the consolidation decided. */}
        <button
          className="chip"
          onClick={() => useWave.getState().setFsmOpen(true)}
          data-testid="fsm-open"
          title="State machines in this design (§8.8)"
        >
          FSM <kbd>⌘M</kbd>
        </button>
      </div>
      <div className="source-body" ref={hostRef} data-testid="source-editor" />
    </div>
  );
}

function onClick(e: MouseEvent, view: EditorView, source: SourceFile): boolean {
  const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
  if (pos === null) return false;
  const line = view.state.doc.lineAt(pos);
  const sigs = source.signals[String(line.number)];
  if (!sigs || sigs.length === 0) return false;

  // Prefer the signal whose name appears under the pointer.
  const word = /[\w$]+/g;
  let hit = sigs[0];
  for (const m of line.text.matchAll(word)) {
    const start = line.from + (m.index ?? 0);
    if (pos >= start && pos <= start + m[0].length) {
      hit = sigs.find((s) => s.path.endsWith("." + m[0]) || s.path === m[0]) ?? hit;
      break;
    }
  }

  const s = useWave.getState();
  if (e.altKey) {
    // §11.4: alt-click asks why, at the current cursor time.
    void s.runWhy(hit.path, s.cursor ?? s.view.t1);
  } else if (hit.handle !== null) {
    s.addSignal(hit.handle);
    s.setSelected(hit.handle);
  }
  return true;
}
