"""Putting two traces on one timeline — §8.7.

§8.7 is emphatic about the order of operations, and both steps are here:

**Normalise the timescale first.** Icarus, Verilator, ModelSim and xsim each pick
their own `timescale`, and an ILA capture has another unit entirely. A `.vtx`
records its own in `meta.json`, so every timestamp is converted to femtoseconds
*before* anything is compared. Without this, "first divergence at c1200" can name
two completely different moments in the two runs — the failure mode that makes a
diff tool worse than no diff tool, because the answer looks precise.

**Then align on anchor events, not on absolute time.** Two runs of the same
design with different latencies do not share a clock phase, and subtracting
timestamps compares cycle 300 of one against cycle 288 of the other. §8.7 lists
the anchors: clock edges, completed handshakes, retired instructions, or marks
the user placed. Matching them gives a piecewise map, and everything between two
matched anchors is mapped affinely.

The matching itself is `difflib.SequenceMatcher`. §8.7 says LCS; matching blocks
*are* the LCS, computed by a stdlib implementation that has been correct for
twenty years — and `autojunk` is off, because anchor keys repeat by design and
the heuristic that discards "popular" elements would throw away exactly the
clock edges being matched on.

The common axis is an **ordinal**: for the clock strategy it is the cycle
number, and for the others the index of the anchor plus the fraction of the way
to the next one. That is what makes "they diverge at c1200" mean one thing in
both runs, and it is why the comparison in `report.py` never subtracts two raw
timestamps.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Sequence

from veritrace.clocks import UNIT_FS

STRATEGIES = ("cycle", "handshake", "retire", "manual")

#: Names that mark a retired instruction when the user has not said which signal
#: does (§8.7 names `pc_valid` for a CPU). Narrow on purpose: a wrong anchor
#: renumbers the whole comparison.
RETIRE_NAME_RE = re.compile(r"(^|[._])(retire|pc_valid|instr_valid|commit)([._\d]|$)", re.I)


class AlignError(ValueError):
    """The two traces cannot be put on one axis, and why."""


def fs_per_tick(timescale: str) -> int:
    """Femtoseconds in one trace time unit — the §8.7 normalisation.

    Femtoseconds because that is the finest unit VCD can express, so every
    conversion stays integer and no rounding creeps into a comparison.
    """
    text = (timescale or "").strip().lower()
    digits = re.match(r"\s*(\d+)", text)
    unit = text.lstrip("0123456789 ")
    scale = UNIT_FS.get(unit)
    if scale is None:
        raise AlignError(
            f"cannot read a time unit from timescale {timescale!r}; "
            "the two traces cannot be compared without one"
        )
    return int(digits.group(1)) * scale if digits else scale


@dataclass(slots=True)
class Anchor:
    """One matchable event. `key` is what makes it comparable across runs."""

    time_fs: int
    key: str


@dataclass(slots=True)
class Side:
    """One trace, as the alignment sees it."""

    name: str
    store: Any
    clock: Any
    fs: int
    anchors: list[Anchor] = field(default_factory=list)

    def to_fs(self, t: int) -> int:
        return t * self.fs

    def from_fs(self, t_fs: int) -> int:
        return t_fs // self.fs


@dataclass(slots=True)
class Alignment:
    strategy: str
    a: Side
    b: Side
    #: Matched anchor indices, ascending: `(index in a, index in b)`.
    pairs: list[tuple[int, int]] = field(default_factory=list)
    #: Anchors each side offered, for the UI to say how well they matched.
    counts: tuple[int, int] = (0, 0)
    note: str = ""
    #: Matched anchor times per side, in femtoseconds. Built once: `ordinal` is
    #: called once per transition of every signal — on a 1400-signal trace that
    #: is hundreds of thousands of calls, and rebuilding this list inside each
    #: one turns a comparison that takes a second into one that takes minutes.
    _times: tuple[list[int], list[int]] | None = field(default=None, repr=False)

    @property
    def matched(self) -> int:
        return len(self.pairs)

    def _anchor_times(self, pick: int) -> list[int]:
        if self._times is None:
            self._times = (
                [self.a.anchors[p[0]].time_fs for p in self.pairs],
                [self.b.anchors[p[1]].time_fs for p in self.pairs],
            )
        return self._times[pick]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "matched": self.matched,
            "anchors_a": self.counts[0],
            "anchors_b": self.counts[1],
            "note": self.note,
            "timescale_a": self.a.fs,
            "timescale_b": self.b.fs,
        }

    # -- the common axis -------------------------------------------------

    def ordinal(self, t: int, side: str) -> float:
        """Where a native timestamp sits on the shared axis.

        Integer part is the matched-anchor index; the fraction is how far along
        to the next one. For the clock strategy the integer part is the cycle
        number, which is why `report` can say "c1200" and mean it in both runs.
        """
        this = self.a if side == "a" else self.b
        pick = 0 if side == "a" else 1
        t_fs = this.to_fs(t)
        times = self._anchor_times(pick)
        if not times:
            return float(t_fs)
        i = bisect_right(times, t_fs) - 1
        if i < 0:
            # Before the first matched anchor: extrapolate backwards on the
            # first interval rather than clamp, so a divergence during reset is
            # still placed rather than piled onto ordinal 0.
            span = (times[1] - times[0]) if len(times) > 1 else this.fs
            return (t_fs - times[0]) / span if span else 0.0
        if i >= len(times) - 1:
            span = (times[-1] - times[-2]) if len(times) > 1 else this.fs
            return i + ((t_fs - times[i]) / span if span else 0.0)
        span = times[i + 1] - times[i]
        return i + ((t_fs - times[i]) / span if span else 0.0)

    def time_at(self, ordinal: float, side: str) -> int:
        """The inverse: a native timestamp for a position on the shared axis."""
        this = self.a if side == "a" else self.b
        pick = 0 if side == "a" else 1
        times = self._anchor_times(pick)
        if not times:
            return this.from_fs(int(ordinal))
        i = max(0, min(int(ordinal), len(times) - 1))
        frac = ordinal - i
        if i + 1 < len(times):
            t_fs = times[i] + frac * (times[i + 1] - times[i])
        else:
            span = (times[-1] - times[-2]) if len(times) > 1 else this.fs
            t_fs = times[i] + frac * span
        return this.from_fs(int(t_fs))

    @property
    def span(self) -> int:
        """How many positions the two runs have in common."""
        return max(0, self.matched - 1)


# --- anchors ----------------------------------------------------------------


def _clock_anchors(side: Side) -> list[Anchor]:
    if side.clock is None:
        raise AlignError(
            f"{side.name} has no primary clock, so cycles cannot be numbered. "
            "Set clocks.primary in .veritrace.toml, or align on handshakes."
        )
    return [Anchor(side.to_fs(t), "c") for t in side.clock.edges]


def _handshake_anchors(side: Side, protocol: Any) -> list[Anchor]:
    """Completed transactions — §8.7's "handshake-uri complete".

    Taken from the transaction layer rather than re-derived from `valid && ready`:
    §8.14 already decided what a completed handshake is on this interface, and a
    second opinion here would align on events the Transactions tab does not show.
    """
    if protocol is None or not getattr(protocol, "extractions", None):
        raise AlignError(
            f"no protocol interfaces were detected in {side.name}, so there are no "
            "handshakes to align on. Try --align cycle."
        )
    out: list[Anchor] = []
    for ex in protocol.extractions:
        for txn in ex.transactions:
            if txn.end_time is None:
                continue
            out.append(Anchor(side.to_fs(txn.end_time), f"{ex.interface.name}:{txn.kind}"))
    out.sort(key=lambda a: a.time_fs)
    return out


def _retire_anchors(side: Side, signal: str | None) -> list[Anchor]:
    """Rising edges of the retire strobe — §8.7's "instructiuni retirate"."""
    path = signal
    if path is None:
        for meta in side.store.signals():
            if meta.width == 1 and RETIRE_NAME_RE.search(meta.name):
                path = meta.path
                break
    if path is None:
        raise AlignError(
            f"nothing in {side.name} looks like a retire strobe. Name one with "
            "--anchor <signal>, or align on cycles."
        )
    handle = side.store.find(path)
    if handle is None:
        raise AlignError(f"{path} is not in {side.name}")
    return [Anchor(side.to_fs(t), "r") for t in side.store.rising_edges(handle)]


def _manual_anchors(side: Side, times: Sequence[int]) -> list[Anchor]:
    return [Anchor(side.to_fs(int(t)), f"m{i}") for i, t in enumerate(sorted(times))]


def collect(
    side: Side,
    strategy: str,
    protocol: Any = None,
    signal: str | None = None,
    times: Sequence[int] = (),
) -> list[Anchor]:
    if strategy == "cycle":
        return _clock_anchors(side)
    if strategy == "handshake":
        return _handshake_anchors(side, protocol)
    if strategy == "retire":
        return _retire_anchors(side, signal)
    if strategy == "manual":
        if not times:
            raise AlignError("manual alignment needs at least one anchor time per trace")
        return _manual_anchors(side, times)
    raise AlignError(f"unknown alignment strategy {strategy!r}; try one of {', '.join(STRATEGIES)}")


# --- matching ---------------------------------------------------------------

#: Past this many anchors the sequence match is skipped in favour of positional
#: pairing. Only reachable on a strategy whose keys vary; the clock strategy is
#: positional by construction and has no such limit.
MAX_MATCHED_ANCHORS = 20000


def _pair(a: list[Anchor], b: list[Anchor]) -> tuple[list[tuple[int, int]], str]:
    keys_a = [x.key for x in a]
    keys_b = [x.key for x in b]
    if len(set(keys_a)) <= 1 and len(set(keys_b)) <= 1:
        # Every anchor is the same kind of event — clock edges. Then the n-th of
        # one *is* the n-th of the other, which is what cycle numbering means,
        # and running a sequence match would burn O(n·m) to rediscover it.
        n = min(len(a), len(b))
        return [(i, i) for i in range(n)], (
            "" if len(a) == len(b) else f"one run is {abs(len(a) - len(b))} anchor(s) longer"
        )
    if len(a) > MAX_MATCHED_ANCHORS or len(b) > MAX_MATCHED_ANCHORS:
        n = min(len(a), len(b))
        return [(i, i) for i in range(n)], (
            f"too many anchors to match by content ({len(a)} vs {len(b)}); paired by position"
        )
    # autojunk=False: anchor keys repeat by design, and the heuristic that drops
    # elements appearing in more than 1% of a long sequence would discard
    # precisely the events being aligned on.
    matcher = SequenceMatcher(None, keys_a, keys_b, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for block in matcher.get_matching_blocks():
        pairs.extend((block.a + k, block.b + k) for k in range(block.size))
    dropped = min(len(a), len(b)) - len(pairs)
    return pairs, (f"{dropped} anchor(s) matched nothing on the other side" if dropped else "")


def align(
    a: Side,
    b: Side,
    strategy: str = "cycle",
    protocol_a: Any = None,
    protocol_b: Any = None,
    signal: str | None = None,
    times_a: Sequence[int] = (),
    times_b: Sequence[int] = (),
) -> Alignment:
    """Put two traces on one axis. Timescales are normalised first (§8.7)."""
    a.anchors = collect(a, strategy, protocol_a, signal, times_a)
    b.anchors = collect(b, strategy, protocol_b, signal, times_b)
    if not a.anchors or not b.anchors:
        raise AlignError(
            f"no {strategy} anchors in "
            f"{a.name if not a.anchors else b.name}; nothing to align on"
        )
    pairs, note = _pair(a.anchors, b.anchors)
    if not pairs:
        raise AlignError(
            f"the {strategy} anchors of the two runs have nothing in common. "
            "They may not be the same design, or the strategy does not suit it."
        )
    return Alignment(
        strategy=strategy,
        a=a,
        b=b,
        pairs=pairs,
        counts=(len(a.anchors), len(b.anchors)),
        note=note,
    )


def side(name: str, store: Any, clock: Any) -> Side:
    """A `Side` with its timescale already read (§8.7's mandatory first step)."""
    return Side(name=name, store=store, clock=clock, fs=fs_per_tick(store.timescale))
