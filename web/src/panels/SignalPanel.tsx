/**
 * Left column: the ordered signal list.
 *
 * Rows here are DOM because they are interactive (drag, menus, focus). Only the
 * wave traces are forbidden from the DOM (§11.4) — a few hundred list rows are
 * fine, and they get the accessibility and hit-testing for free.
 *
 * Vertical scroll is shared with the canvas: this panel owns the scrollbar and
 * pushes `scrollY` into the store, which the worker reads.
 */

import { useEffect, useRef, useState } from "react";
import { shortLabels } from "../lib/names";
import { RADICES, radixLabel } from "../lib/radix";
import type { Radix, Row } from "../lib/types";
import { useWave, visibleRows } from "../state/store";

/** Ruler height inside the canvas; must match the worker's own constant. */
const RULER_H = 26;
/** Height of this panel's header strip, from app.css. */
const HEAD_H = 24;
/**
 * The canvas column has no header, so its row 0 begins RULER_H below the top of
 * the split. This panel's rows begin HEAD_H below it, so they need only the
 * difference to line up. Getting this wrong shifts every trace against its name
 * by a whole row, which is the sort of thing you stare at for an hour.
 */
const ROW_OFFSET = RULER_H - HEAD_H;

export function SignalPanel() {
  const rows = useWave((s) => s.rows);
  const radix = useWave((s) => s.radix);
  const rowH = useWave((s) => s.rowH);
  const selected = useWave((s) => s.selected);
  const signalsByHandle = useWave((s) => s.signalsByHandle);
  const setRadix = useWave((s) => s.setRadix);
  const cycleRadix = useWave((s) => s.cycleRadix);
  const moveRow = useWave((s) => s.moveRow);
  const removeRow = useWave((s) => s.removeRow);
  const toggleGroup = useWave((s) => s.toggleGroup);
  const setSelected = useWave((s) => s.setSelected);
  const stashedRows = useWave((s) => s.stashedRows);
  const stashedLabel = useWave((s) => s.stashedLabel);
  const focusLabel = useWave((s) => s.focusLabel);
  const swapRows = useWave((s) => s.swapRows);
  const addGroup = useWave((s) => s.addGroup);

  const scrollRef = useRef<HTMLDivElement>(null);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);
  const [menuFor, setMenuFor] = useState<number | null>(null);

  const shown = visibleRows(rows);
  // Labels are decided against the rows on screen, so a name is only as
  // long as it needs to be to be unambiguous *here*.
  const labels = shortLabels(
    shown.filter((r) => r.kind === "signal").map((r) => (r as { path: string }).path),
  );
  // Map a visible index back to its index in the full row array.
  const realIndex = (visIdx: number): number => rows.indexOf(shown[visIdx]);

  // Keep the store's scrollY in step with this panel's scrollbar.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => useWave.getState().setScrollY(el.scrollTop);
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // The canvas can also change scrollY (shift+wheel); reflect it back here.
  useEffect(
    () =>
      useWave.subscribe((s, prev) => {
        if (s.scrollY !== prev.scrollY && scrollRef.current) {
          if (Math.abs(scrollRef.current.scrollTop - s.scrollY) > 1) {
            scrollRef.current.scrollTop = s.scrollY;
          }
        }
      }),
    [],
  );

  function onDrop(toVis: number): void {
    if (dragFrom === null) return;
    const from = realIndex(dragFrom);
    const to = realIndex(Math.min(toVis, shown.length - 1));
    if (from >= 0 && to >= 0) moveRow(from, to);
    setDragFrom(null);
    setDragOver(null);
  }

  return (
    <div className="signal-panel">
      <div className="signal-panel-head">
        <span className="panel-title">Signals</span>
        <span className="spacer" />
        <button className="chip" onClick={() => addGroup("group")} title="New group">
          + group
        </button>
      </div>
      {stashedRows && (
        /* A click-through replaced the list. Say what it is showing and offer
         * the way back — §11.4b's focus is meant to help, not to cost someone
         * the list they spent ten minutes assembling. */
        <div className="signal-focus" data-testid="signal-focus">
          <span className="signal-focus-what" title={focusLabel}>
            {focusLabel || "focused"}
          </span>
          <button
            className="link"
            onClick={swapRows}
            data-testid="signal-focus-back"
            title={`Show ${stashedLabel} again (${stashedRows.length} row(s))`}
          >
            ← {stashedLabel}
          </button>
        </div>
      )}
      <div className="signal-rows" ref={scrollRef} data-testid="signal-rows">
        <div style={{ height: ROW_OFFSET }} className="signal-rows-spacer" />
        {shown.map((row, i) => (
          <SignalRow
            key={row.kind === "group" ? row.id : `${row.handle}-${i}`}
            row={row}
            index={i}
            height={rowH}
            selected={row.kind === "signal" && row.handle === selected}
            radix={row.kind === "signal" ? (radix[row.path] ?? "hex") : "hex"}
            label={row.kind === "signal" ? (labels.get(row.path) ?? row.path) : row.name}
            width={row.kind === "signal" ? (signalsByHandle.get(row.handle)?.width ?? 1) : 0}
            derived={
              row.kind === "signal" && (signalsByHandle.get(row.handle)?.derived ?? false)
            }
            dragging={dragFrom === i}
            dropTarget={dragOver === i}
            menuOpen={menuFor === i}
            onDragStart={() => setDragFrom(i)}
            onDragOver={() => setDragOver(i)}
            onDrop={() => onDrop(i)}
            onDragEnd={() => {
              setDragFrom(null);
              setDragOver(null);
            }}
            onSelect={() => row.kind === "signal" && setSelected(row.handle)}
            onToggle={() => row.kind === "group" && toggleGroup(row.id)}
            onCycleRadix={() => row.kind === "signal" && cycleRadix(row.path)}
            onOpenMenu={() => setMenuFor(menuFor === i ? null : i)}
            onPickRadix={(r) => {
              if (row.kind === "signal") setRadix(row.path, r);
              setMenuFor(null);
            }}
            onRemove={() => {
              const idx = realIndex(i);
              if (idx >= 0) removeRow(idx);
              setMenuFor(null);
            }}
          />
        ))}
      </div>
    </div>
  );
}

interface RowProps {
  row: Row;
  /** Shortest unambiguous form of the path, for the visible list. */
  label: string;
  index: number;
  height: number;
  selected: boolean;
  radix: Radix;
  width: number;
  derived: boolean;
  dragging: boolean;
  dropTarget: boolean;
  menuOpen: boolean;
  onDragStart: () => void;
  onDragOver: () => void;
  onDrop: () => void;
  onDragEnd: () => void;
  onSelect: () => void;
  onToggle: () => void;
  onCycleRadix: () => void;
  onOpenMenu: () => void;
  onPickRadix: (r: Radix) => void;
  onRemove: () => void;
}

function SignalRow(p: RowProps) {
  const { row } = p;

  if (row.kind === "group") {
    return (
      <div
        className={`sig-row group${p.dropTarget ? " drop" : ""}`}
        style={{ height: p.height }}
        draggable
        onDragStart={p.onDragStart}
        onDragOver={(e) => {
          e.preventDefault();
          p.onDragOver();
        }}
        onDrop={p.onDrop}
        onDragEnd={p.onDragEnd}
        data-testid="group-row"
      >
        <button className="twisty" onClick={p.onToggle} aria-label="toggle group">
          {row.collapsed ? "▸" : "▾"}
        </button>
        <span className="group-name">{row.name}</span>
      </div>
    );
  }

  return (
    <div
      className={`sig-row${p.selected ? " selected" : ""}${p.derived ? " derived" : ""}${p.dragging ? " dragging" : ""}${
        p.dropTarget ? " drop" : ""
      }`}
      style={{ height: p.height }}
      draggable
      onDragStart={p.onDragStart}
      onDragOver={(e) => {
        e.preventDefault();
        p.onDragOver();
      }}
      onDrop={p.onDrop}
      onDragEnd={p.onDragEnd}
      onClick={p.onSelect}
      data-testid="signal-row"
      data-path={row.path}
      data-derived={p.derived ? "reconstructed" : undefined}
    >
      <span className="grip" aria-hidden>
        ⠿
      </span>
      <span className="sig-name" title={row.path}>
        {p.label}{p.derived ? " · derived" : ""}
      </span>
      {p.width > 1 && (
        <button
          className="radix"
          onClick={(e) => {
            e.stopPropagation();
            p.onCycleRadix();
          }}
          onContextMenu={(e) => {
            e.preventDefault();
            p.onOpenMenu();
          }}
          title="Click to cycle radix, right-click to choose"
          data-testid="radix-button"
        >
          {radixLabel(p.radix)}
        </button>
      )}
      <button
        className="row-x"
        onClick={(e) => {
          e.stopPropagation();
          p.onRemove();
        }}
        title="Remove"
      >
        ×
      </button>
      {p.menuOpen && (
        <div className="radix-menu" onClick={(e) => e.stopPropagation()}>
          {RADICES.map((r) => (
            <button key={r} onClick={() => p.onPickRadix(r)} className={r === p.radix ? "on" : ""}>
              {radixLabel(r)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
