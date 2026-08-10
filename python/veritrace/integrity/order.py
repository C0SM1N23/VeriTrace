"""§8.19 for streams: the n-th word in must be the n-th word out.

*Detects reordering, duplication and loss — the three classic FIFO and CDC
bugs.* With no address there is no memory model to keep, so the only invariant
left is the sequence itself, and this is a longest-common-subsequence alignment
between the two: what the alignment skips on the source side was lost, what it
skips on the sink side is a duplicate or an insertion, and a pair that appears
in one order on the way in and the other on the way out was reordered.

Which two interfaces form a stream is the one thing that cannot be derived: a
source and a sink are not distinguishable from two unrelated buses by anything
in the trace. So this is asked for — `track(from=src, to=sink)` — rather than
guessed at, and the automatic pass says so instead of pairing at random (P1).
"""

from __future__ import annotations

from difflib import SequenceMatcher

from veritrace.integrity.model import Beat, OrderResult, Payload


def _words(beats: list[Beat]) -> list[Payload]:
    return [b.data for b in beats if b.dir == "write"]


def compare(source: str, src_beats: list[Beat], sink: str, sink_beats: list[Beat]) -> OrderResult:
    a, b = _words(src_beats), _words(sink_beats)
    out = OrderResult(source=source, sink=sink, n_in=len(a), n_out=len(b))

    # `autojunk` off: on a long stream a value that repeats in more than 1% of
    # beats would be treated as noise and dropped from the alignment, which on
    # a bus carrying mostly zeros is every beat that matters.
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    matched_in: set[int] = set()
    matched_out: set[int] = set()
    for i, j, n in matcher.get_matching_blocks():
        matched_in.update(range(i, i + n))
        matched_out.update(range(j, j + n))

    unmatched_in = [(i, v) for i, v in enumerate(a) if i not in matched_in]
    unmatched_out = [(j, v) for j, v in enumerate(b) if j not in matched_out]

    # A value the alignment had to drop from *both* sides went in and came out —
    # just not in that order. Pairing those off first is what separates a
    # reordered word from a lost one, and doing it before anything else keeps a
    # single swap from being reported as one loss plus one spurious word.
    leftover_out = list(unmatched_out)
    for i, v in unmatched_in:
        hit = next(((j, w) for j, w in leftover_out if w == v), None)
        if hit is None:
            out.lost.append((i, v))
        else:
            leftover_out.remove(hit)
            out.reordered.append((i, hit[0]))

    sent = set(a)
    out.duplicated = [(j, v) for j, v in leftover_out if v in sent]
    out.spurious = [(j, v) for j, v in leftover_out if v not in sent]
    return out
