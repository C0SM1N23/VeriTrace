"""`track(addr=0x4000)` and `track(data=0xDEADBEEF)` — §8.19's chain.

The display §8.19 draws: every level the word passed through, in time order,
with the mismatch marked where it stopped agreeing. Nothing is recomputed —
this is a filter over the beats and mismatches the session already has, which is
also why a `track()` on a large trace is instant.

`data=` is a convenience, not a mechanism: it resolves the value to the
addresses that carried it and then tracks *those*, because §8.19 says plainly
that following a value does not work — the same word appears a thousand times in
a real trace.
"""

from __future__ import annotations

from veritrace.integrity.model import Beat, IntegrityReport, Mismatch, TrackResult, TrackStep


def _covers(beat: Beat, addr: int) -> bool:
    return beat.addr is not None and beat.addr <= addr < beat.addr + beat.stride


def by_addr(report: IntegrityReport, addr: int, iface: str | None = None) -> TrackResult:
    """Every beat that touched the byte at `addr`."""
    out = TrackResult(query=f"track(addr=0x{addr:x})")
    hits = [
        b for b in report.beats if _covers(b, addr) and (iface is None or b.iface == iface)
    ]
    bad = {
        (m.time, m.where)
        for m in report.mismatches
        if m.addr is not None and m.addr <= addr < m.addr + 64 and _lane_hit(m, addr)
    }
    for b in sorted(hits, key=lambda b: b.cycle_key):
        out.steps.append(
            TrackStep(
                time=b.time,
                iface=b.iface,
                txn=b.txn,
                dir=b.dir,
                addr=b.addr,
                data=b.data,
                strobe=b.strobe,
                stride=b.stride,
                note=_note(b, addr),
                mismatch=any(t == b.time and b.iface in w for t, w in bad),
            )
        )
    out.mismatches = [
        m for m in report.mismatches if m.addr is not None and _lane_hit(m, addr)
    ]
    if not out.steps:
        out.note = f"no transaction on any tracked interface touched 0x{addr:x}"
    return out


def _lane_hit(m: Mismatch, addr: int) -> bool:
    return m.addr is not None and (addr - m.addr) in m.lanes


def _note(beat: Beat, addr: int) -> str:
    lane = addr - (beat.addr or 0)
    if beat.dir == "write" and not beat.strobe >> lane & 1:
        return f"byte {lane} was not enabled by the strobe"
    return ""


def by_data(report: IntegrityReport, value: int) -> TrackResult:
    """Addresses that ever carried `value`, tracked as addresses."""
    out = TrackResult(query=f"track(data=0x{value:x})")
    seen = sorted(
        {b.addr for b in report.beats if b.data == value and b.addr is not None}
    )
    if not seen:
        out.note = f"0x{value:x} never appeared on a tracked data bus"
        return out
    if len(seen) > 1:
        # §8.19's own warning, made visible rather than resolved silently.
        out.note = (
            f"0x{value:x} appeared at {len(seen)} addresses "
            f"({', '.join(f'0x{a:x}' for a in seen[:4])}"
            f"{', …' if len(seen) > 4 else ''}); all of them are tracked"
        )
    for addr in seen:
        part = by_addr(report, addr)
        out.steps += part.steps
        out.mismatches += part.mismatches
    out.steps.sort(key=lambda s: (s.time, s.iface))
    return out
