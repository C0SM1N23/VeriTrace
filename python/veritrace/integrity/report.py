"""One call that produces everything §8.19 shows.

The same seam `perf.report.build` and `memory.report.build` are for their
stages: the CLI, the API and the tests all go through it, so a headless run and
a browser cannot disagree about what the scoreboard found.
"""

from __future__ import annotations

import time as _time
from typing import Any

from veritrace.integrity import beats as beats_mod, path, scoreboard
from veritrace.integrity.model import IfaceStats, IntegrityReport, Mismatch


def _wires(analysis: Any) -> dict[str, dict[str, str]]:
    """Interface -> the trace paths of its data and strobe wires.

    Read out of the pack's `[integrity]` declaration rather than guessed from
    names: the pack already says which field is the write data and which is the
    byte enable, and `Interface.signals` already maps those to full paths.
    """
    out: dict[str, dict[str, str]] = {}
    for ex in getattr(analysis, "extractions", []) or []:
        integrity = ex.interface.pack.integrity
        if integrity is None:
            continue
        signals = ex.interface.signals
        roles: dict[str, str] = {}
        for role, field in (
            ("read_data", integrity.read.data if integrity.read else None),
            ("write_data", integrity.write.data if integrity.write else None),
            ("write_strobe", integrity.write.strobe if integrity.write else None),
        ):
            if field and signals.get(field):
                roles[role] = signals[field]
        out[ex.interface.name] = roles
    return out


def _explain(m: Mismatch, wires: dict[str, dict[str, str]]) -> None:
    """Point the finding at the wire that carried the wrong bytes.

    A scoreboard mismatch asks about the *read* — the value came back wrong, so
    the question is where that value came from. A path mismatch asks about the
    *downstream strobe* when lanes were dropped and the downstream data when
    they merely changed, because those are two different bugs and they live on
    two different wires.
    """
    if m.kind == "scoreboard":
        m.signal = (wires.get(m.where) or {}).get("read_data")
    elif m.kind == "path":
        downstream = m.where.split(" -> ")[-1].strip()
        roles = wires.get(downstream) or {}
        dropped = "not downstream" in m.detail
        m.signal = roles.get("write_strobe") if dropped else None
        m.signal = m.signal or roles.get("write_data")
    if m.signal:
        m.why = f"why({m.signal} @ {m.time})"


def build(analysis: Any, store: Any = None, clock: Any = None) -> IntegrityReport:
    """§8.19 over an already-extracted set of interfaces.

    Costs one pass over the beat table and nothing over the trace: every value
    it needs was sampled when the transactions were assembled, and the parquet
    cache carries it, so a re-opened session scoreboards without re-extracting.
    """
    started = _time.perf_counter()
    out = IntegrityReport()
    extractions = list(getattr(analysis, "extractions", []) or [])
    if not extractions:
        out.skipped["extraction"] = "no protocol interface was extracted"
        out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
        return out

    by_iface = beats_mod.by_iface(analysis, out.skipped)
    for ex in extractions:
        found = by_iface.get(ex.interface.name)
        if not found:
            continue
        stats = IfaceStats(iface=ex.interface.name, pack=ex.interface.pack.name)
        out.mismatches += scoreboard.check(found, stats)
        out.interfaces.append(stats)
        out.beats += found

    # §8.19's cross-interface half. Runs after every interface's own scoreboard
    # so a read that already disagreed with its own writes is reported at the
    # interface it happened on, and the path comparison adds *where* it changed.
    found, compared = path.scan(by_iface)
    out.mismatches += found
    out.compared_paths = compared

    if by_iface and not compared and len(by_iface) > 1:
        out.skipped["path"] = (
            "no two interfaces wrote the same address sequence, so nothing "
            "could be compared across the path"
        )

    wires = _wires(analysis)
    for m in out.mismatches:
        _explain(m, wires)

    out.mismatches.sort(key=lambda m: (m.time, m.where, m.lanes))
    out.beats.sort(key=lambda b: b.cycle_key)
    out.interfaces.sort(key=lambda i: i.iface)
    out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return out
