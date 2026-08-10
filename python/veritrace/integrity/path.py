"""The same write, seen twice — §8.19's "where in the path did it happen".

§8.19 is explicit about the mechanism: *do not follow the value, it can repeat.
Follow the **(address, sequence)** pair through the correlated transactions.*
That is what this does, and the pairing rule follows from it directly:

    two interfaces are comparable when their write **address sequences are
    identical**.

If interface `s` wrote 0x10, 0x14, 0x18 and interface `m` wrote 0x10, 0x14,
0x18, then `m`'s k-th write *is* `s`'s k-th write, one hop further down the
path, and any difference in the bytes is corruption in between. If the
sequences differ at all, they are not the same stream of writes and this says
nothing — which is the only way to keep two unrelated masters that happen to
share an address from being reported as a data bug (P1).

The finding is then exact in both senses the acceptance criterion asks for: the
byte lanes that changed, and the two interfaces it changed between.
"""

from __future__ import annotations

from veritrace.integrity.model import Beat, Mismatch, format_lanes


def _writes(beats: list[Beat]) -> list[Beat]:
    return [b for b in beats if b.dir == "write" and b.addr is not None]


def _sequence(writes: list[Beat]) -> tuple[tuple[int, int], ...]:
    return tuple((b.addr or 0, b.stride) for b in writes)


def compare(
    a_name: str, a_beats: list[Beat], b_name: str, b_beats: list[Beat]
) -> tuple[list[Mismatch], int]:
    """Corruption between two interfaces, and how many writes were compared.

    Returns `(mismatches, 0)` when the two are not comparable — "nothing was
    checked here" and "nothing was wrong here" have to be distinguishable.
    """
    a_writes, b_writes = _writes(a_beats), _writes(b_beats)
    if not a_writes or _sequence(a_writes) != _sequence(b_writes):
        return [], 0

    # Upstream is whichever saw the first write first. On a real path the two
    # are the same wire a few cycles apart, so this is not a heuristic about
    # topology — it is the order the bytes were observed in.
    if b_writes[0].time < a_writes[0].time:
        a_name, b_name, a_writes, b_writes = b_name, a_name, b_writes, a_writes

    out: list[Mismatch] = []
    for src, dst in zip(a_writes, b_writes):
        up, down = src.lanes(), dst.lanes()
        if not up and not down:
            continue
        base = src.addr or 0
        dropped = sorted(a - base for a in up.keys() - down.keys())
        added = sorted(a - base for a in down.keys() - up.keys())
        changed = sorted(a - base for a in up.keys() & down.keys() if up[a] != down[a])
        if not (dropped or added or changed):
            continue
        out.append(
            Mismatch(
                kind="path",
                lanes=tuple(sorted(dropped + added + changed)),
                addr=base,
                expected=_word(up, base, src.stride),
                observed=_word(down, base, dst.stride),
                time=dst.time,
                where=f"{a_name} -> {b_name}",
                source=src.txn,
                victim=dst.txn,
                detail=_detail(dropped, added, changed),
            )
        )
    return out, len(a_writes)


def _word(lanes: dict[int, int], base: int, stride: int) -> int | None:
    """The lanes as one word, or `None` when none of them were driven."""
    if not lanes:
        return None
    return sum(v << (8 * (a - base)) for a, v in lanes.items() if 0 <= a - base < stride)


def _detail(dropped: list[int], added: list[int], changed: list[int]) -> str:
    parts = []
    if dropped:
        parts.append(f"byte(s) {format_lanes(dropped)} were enabled upstream and not downstream")
    if changed:
        parts.append(f"byte(s) {format_lanes(changed)} arrived with a different value")
    if added:
        parts.append(f"byte(s) {format_lanes(added)} were written that the source did not send")
    return "; ".join(parts)


def scan(by_iface: dict[str, list[Beat]]) -> tuple[list[Mismatch], list[tuple[str, str, int]]]:
    """Every comparable pair of interfaces, and what it found."""
    names = sorted(by_iface)
    out: list[Mismatch] = []
    compared: list[tuple[str, str, int]] = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            found, n = compare(a, by_iface[a], b, by_iface[b])
            if n:
                compared.append((a, b, n))
                out += found
    return out, compared
