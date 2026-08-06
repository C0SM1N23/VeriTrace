/**
 * Fuzzy signal matching for the palette and the filter box.
 *
 * Mirrors the server-side scorer in `veritrace/api/search.py` so filtering
 * locally and searching remotely rank the same way. Deterministic (P1): the
 * same query always produces the same order.
 */

export interface Scored<T> {
  item: T;
  score: number;
}

/** Lower is better; `null` means no match. */
export function score(query: string, path: string): number | null {
  if (!query) return 0;
  const q = query.toLowerCase();
  const p = path.toLowerCase();

  const idx = p.indexOf(q);
  if (idx >= 0) {
    const tailBonus = idx >= p.lastIndexOf(".") ? 0 : 40;
    return tailBonus + path.length - query.length + Math.floor(idx / 8);
  }

  let penalty = 0;
  let pos = 0;
  let prevEnd = -2;
  for (const ch of q) {
    const found = p.indexOf(ch, pos);
    if (found < 0) return null;
    if (found !== prevEnd + 1) {
      const onBoundary = found > 0 && "._[/".includes(p[found - 1]);
      penalty += onBoundary ? 2 : 10;
    }
    prevEnd = found;
    pos = found + 1;
  }
  return 200 + penalty + path.length;
}

export function filterByPath<T>(items: T[], query: string, key: (t: T) => string, limit = 500): T[] {
  if (!query) return items.slice(0, limit);
  const out: Scored<T>[] = [];
  for (const item of items) {
    const s = score(query, key(item));
    if (s !== null) out.push({ item, score: s });
  }
  out.sort((a, b) => a.score - b.score || key(a.item).localeCompare(key(b.item)));
  return out.slice(0, limit).map((s) => s.item);
}
