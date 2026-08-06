/**
 * The wave data the canvas last fetched, shared with the Source tab.
 *
 * The value inlays need "what is this signal at the cursor" — exactly what the
 * canvas already holds. Re-requesting it per line would hammer the socket for
 * data that is one lookup away.
 */

import type { ExactPoint, MinMaxPoint, WaveChunk } from "../lib/types";

const chunks = new Map<number, WaveChunk>();

export function setChunks(list: WaveChunk[]): void {
  for (const c of list) chunks.set(c.h, c);
}

export function clearChunks(): void {
  chunks.clear();
}

/**
 * Value of `handle` at `t`, as canonical VCD digits, or `null` when unknown.
 *
 * A reduced window can only answer approximately — it reports the bucket's
 * upper bound — so the caller should treat it as a display aid, not a fact to
 * reason from.
 */
export function valueAt(handle: number, t: number): string | null {
  const chunk = chunks.get(handle);
  if (!chunk) return null;
  const tr = chunk.transitions;
  if (tr.length === 0) return chunk.initial;

  // Binary search for the last entry at or before t.
  let lo = 0;
  let hi = tr.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if ((tr[mid] as ExactPoint)[0] <= t) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (found < 0) return chunk.initial;
  return chunk.mode === "exact"
    ? (tr[found] as ExactPoint)[1]
    : (tr[found] as MinMaxPoint)[2];
}
