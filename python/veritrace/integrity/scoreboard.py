"""The free scoreboard of §8.19.

*A dictionary `addr -> value`, updated from the write transactions observed and
compared against every read. Report every mismatch as a finding.*

Two rules make it usable rather than noisy:

* **Byte granularity.** A partial `wstrb` writes some lanes and leaves the rest
  alone, so a word-granular model would either lose the write or invent the
  bytes it did not carry. Bytes also happen to be the unit the answer has to be
  given in — "bytes 0-1 came back wrong" is the whole point.
* **A read of a byte nobody wrote is not a mismatch.** The model knows what the
  *bus* did, not what the memory was initialised with, and reporting reset
  contents as corruption would bury the real finding under every boot sequence
  ever traced (P1).
"""

from __future__ import annotations

from dataclasses import dataclass

from veritrace.integrity.model import Beat, IfaceStats, Mismatch


@dataclass(slots=True)
class _Cell:
    value: int
    writer: str
    time: int


def check(beats: list[Beat], stats: IfaceStats) -> list[Mismatch]:
    """Walk one interface's beats in order, comparing reads to the model."""
    memory: dict[int, _Cell] = {}
    out: list[Mismatch] = []

    for beat in beats:
        if beat.dir == "write":
            stats.writes += 1
            for addr, byte in beat.lanes().items():
                memory[addr] = _Cell(byte, beat.txn, beat.time)
            continue

        stats.reads += 1
        if beat.addr is None or not isinstance(beat.data, int):
            continue
        bad: list[int] = []
        writer = ""
        expected = 0
        for i in range(beat.stride):
            cell = memory.get(beat.addr + i)
            if cell is None:
                continue
            stats.compared += 1
            expected |= cell.value << (8 * i)
            if cell.value != (beat.data >> (8 * i)) & 0xFF:
                bad.append(i)
                writer = writer or cell.writer
        if not bad:
            continue
        # Only the lanes the model actually knows about are quoted back, so
        # `expected` never contains a byte this scoreboard invented.
        known = sum(0xFF << (8 * i) for i in range(beat.stride) if beat.addr + i in memory)
        out.append(
            Mismatch(
                kind="scoreboard",
                lanes=tuple(bad),
                addr=beat.addr,
                expected=expected,
                observed=beat.data & known,
                time=beat.time,
                where=beat.iface,
                source=writer,
                victim=beat.txn,
                detail=(
                    f"read back {_hex(beat.data & known)} where {_hex(expected)} was written"
                ),
            )
        )
    return out


def _hex(v: int) -> str:
    return f"0x{v:08x}" if v < 1 << 32 else f"0x{v:x}"
