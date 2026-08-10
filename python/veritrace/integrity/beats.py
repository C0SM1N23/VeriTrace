"""Where §8.19 gets its beats.

The extraction produces them (`protocol.beats`) while the channel events are
still in hand, and the parquet cache carries them, so this is a read. Kept as a
named seam rather than an attribute access because the three checks all start
here, and one of them looking somewhere else would be a bug that only shows up
on a re-opened session.
"""

from __future__ import annotations

from typing import Any

from veritrace.protocol.model import Beat, Extraction


def of(extraction: Extraction, notes: dict[str, str]) -> list[Beat]:
    """One interface's data beats, in trace order.

    An interface whose pack declares no `[integrity]` yields nothing and records
    why: §8.19 cannot guess which of a pack's transactions is a write, and a
    scoreboard that silently watched nothing would be indistinguishable from one
    that found no bugs (P1).
    """
    iface = extraction.interface
    if iface.pack.integrity is None:
        notes.setdefault(
            iface.name, f"{iface.pack.name} declares no [integrity], so no data was tracked"
        )
        return []
    for key, why in extraction.skipped.items():
        if key.startswith(("beats", "integrity")):
            notes.setdefault(iface.name, why)
    return sorted(extraction.beats, key=lambda b: b.cycle_key)


def by_iface(analysis: Any, notes: dict[str, str]) -> dict[str, list[Beat]]:
    out: dict[str, list[Beat]] = {}
    for ex in getattr(analysis, "extractions", []) or []:
        found = of(ex, notes)
        if found:
            out[ex.interface.name] = found
    return out
