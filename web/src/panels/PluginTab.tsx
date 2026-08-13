/**
 * A table a project's own plugin produced — §13.7.
 *
 * *"Rezultatele tabulare apar automat ca tab nou. **Nu trebuie sa atingi UI-ul
 * ca sa adaugi o analiza.**"* This component is what makes that true: it renders
 * whatever columns the plugin declared, and the tab strip grows to fit however
 * many tables came back. Nobody edits this file to add an analysis.
 *
 * Deliberately plain. A plugin author gets a sortable table with the house
 * typography and nothing else — a richer contract would be a second UI to keep
 * in step, and §13.7's promise is about *not* having to touch this one.
 */

import { useMemo, useState } from "react";
import type { PluginTable as Table } from "../lib/types";

export function PluginTab({ table }: { table: Table }) {
  const [sortBy, setSortBy] = useState<number | null>(null);
  const [desc, setDesc] = useState(false);

  const rows = useMemo(() => {
    if (sortBy === null) return table.rows;
    const copy = [...table.rows];
    copy.sort((a, b) => {
      const x = a[sortBy];
      const y = b[sortBy];
      if (typeof x === "number" && typeof y === "number") return x - y;
      return String(x ?? "").localeCompare(String(y ?? ""));
    });
    return desc ? copy.reverse() : copy;
  }, [table.rows, sortBy, desc]);

  return (
    <div className="plugin-tab" data-testid="plugin-tab">
      <div className="plugin-head">
        <span className="strong">{table.title}</span>
        <span className="spacer" />
        <span className="dim">
          {table.rows.length} row{table.rows.length === 1 ? "" : "s"}
        </span>
      </div>
      {table.rows.length === 0 ? (
        <div className="pane-note">
          This analysis produced no rows.
          <div className="pane-hint">
            The plugin ran and had nothing to report — which is a result, not a
            failure.
          </div>
        </div>
      ) : (
        <div className="plugin-body">
          <table className="plugin-table">
            <thead>
              <tr>
                {table.columns.map((c, i) => (
                  <th
                    key={c}
                    onClick={() => {
                      setDesc(sortBy === i ? !desc : false);
                      setSortBy(i);
                    }}
                    aria-sort={
                      sortBy === i ? (desc ? "descending" : "ascending") : "none"
                    }
                  >
                    {c}
                    {sortBy === i && <span className="dim">{desc ? " ▾" : " ▴"}</span>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {r.map((cell, j) => (
                    <td key={j} className={typeof cell === "number" ? "num mono" : "mono"}>
                      {cell === null ? "" : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
