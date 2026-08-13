/**
 * Row labels that stay short without becoming ambiguous.
 *
 * A signal row shows the leaf name, because `tb_cpu_axi.dmem_inst.arready` in a
 * 240-pixel column is a path nobody can read. That is right until the list
 * holds two signals with the same leaf — and on a real design it does
 * constantly: seven AXI4-Lite interfaces all carry `arready`, `awready`,
 * `bresp` and `rdata`, so a focused list reads as a column of identical names
 * against completely different buses.
 *
 * So the leaf is used only while it is unique *among the rows on screen*, and
 * anything that collides grows leftwards, one scope at a time, until it is not
 * ambiguous any more. A single-interface design is unaffected; a design where
 * the distinction matters gets exactly as much path as the distinction needs.
 *
 * The separator is a parameter because the same problem turns up with file
 * paths: every design in a project dumps to `dump.vtx`, so the Diff tab's trace
 * picker offers a column of identical labels for completely different runs. Same
 * question, same answer — grow the path until it distinguishes.
 */

/** How many segments a label may grow to before it is elided in the middle. */
const MAX_SEGMENTS = 3;

function tail(path: string, n: number, sep: string): string {
  const parts = path.split(sep);
  return parts.slice(Math.max(0, parts.length - n)).join(sep);
}

/**
 * `path -> label`, shortest suffix of each path that is unique among `paths`.
 *
 * Paths that are genuinely equal share a label: they are the same signal listed
 * twice, and inventing a difference would be a lie about the design.
 */
export function shortLabels(paths: string[], sep = "."): Map<string, string> {
  const out = new Map<string, string>();
  const unique = [...new Set(paths)];

  for (const path of unique) {
    const depth = path.split(sep).length;
    let label = tail(path, 1, sep);
    // Grow only against the paths it actually collides with, so one deep
    // hierarchy elsewhere in the list cannot lengthen everything.
    for (let n = 1; n <= depth; n++) {
      label = tail(path, n, sep);
      const clashes = unique.some((other) => other !== path && tail(other, n, sep) === label);
      if (!clashes) break;
    }
    out.set(path, elide(label, sep));
  }
  return out;
}

/**
 * `a.b.c.d.e` -> `a.…​.d.e`. A label that has grown past what the column can
 * show is cut in the middle rather than at either end: the leaf says what the
 * signal is and the first segment says which block it is in, and those are the
 * two parts a reader is using it for.
 */
function elide(label: string, sep = "."): string {
  const parts = label.split(sep);
  if (parts.length <= MAX_SEGMENTS) return label;
  return [parts[0], "…", ...parts.slice(-(MAX_SEGMENTS - 1))].join(sep);
}
