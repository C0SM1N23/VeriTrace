"""First divergence between two runs — §8.7.

Steps 1 and 3–6 of §8.7 (step 2, the alignment, is `align.py`):

1. match signals by normalised path into a common set,
3. merge-sort the two event streams,
4. the first `(signal, position)` where the values differ is the divergence,
5. run `why()` on both sides,
6. hand the two chains back side by side, with the first differing node marked.

**The comparison is on the shared axis, never on raw time.** Two runs with
different latencies — or different timescales — have no common timestamp, so
each transition is placed at its ordinal on the axis `align.py` built (the cycle
number, for the default strategy) and the two step functions are compared there.
That is the difference between "they diverge at c1200" meaning one moment and
meaning two.

**Transactions get their own answer**, because §8.7's readers are two different
people. The first differing *signal* tells you where the RTL parted company; the
first differing *transaction* tells you which access went wrong, which is the
question anyone working on an interconnect is actually asking. Both are reported;
neither is derived from the other.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from veritrace.analysis.whytrace import CausalNode, WhyTracer
from veritrace.diff.align import AlignError, Alignment

#: Signals whose disagreement is expected and would bury the real answer:
#: performance counters, timestamps, and the simulator's own bookkeeping. §11.4
#: puts the same idea in the UI as "ignore this signal".
DEFAULT_IGNORE = ("*cycle_count*", "*timestamp*", "*perf_*", "*_cnt", "*.$*")

#: How many divergences to collect. §11.4's `n`/`p` walks them, and a run that
#: went wrong at c1200 has thousands afterwards — all of them consequences.
MAX_DIVERGENCES = 200


@dataclass(slots=True)
class Divergence:
    signal: str
    #: Position on the shared axis — the cycle number under the default strategy.
    at: float
    time_a: int
    time_b: int
    value_a: str
    value_b: str
    #: `signal` or `transaction`.
    level: str = "signal"
    ref: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "at": self.at,
            "time_a": self.time_a,
            "time_b": self.time_b,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "level": self.level,
            "ref": self.ref,
            "detail": self.detail,
        }


@dataclass(slots=True)
class DiffReport:
    alignment: Alignment
    #: Signals present in both runs, after normalisation and the ignore list.
    compared: int = 0
    only_a: list[str] = field(default_factory=list)
    only_b: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    txn_divergences: list[Divergence] = field(default_factory=list)
    #: §8.7 step 5-6, filled in by `explain`.
    why_a: CausalNode | None = None
    why_b: CausalNode | None = None
    #: Index into the primary path where the two chains first differ (§11.4:
    #: highlighted in magenta). `None` when they never differ, or when there is
    #: no RTL to build them from.
    first_differing: int | None = None
    why_error: str = ""
    focus: str | None = None
    wave: dict[str, Any] | None = None

    @property
    def first(self) -> Divergence | None:
        return self.divergences[0] if self.divergences else None

    @property
    def first_txn(self) -> Divergence | None:
        return self.txn_divergences[0] if self.txn_divergences else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alignment": self.alignment.to_dict(),
            "compared": self.compared,
            "only_a": self.only_a[:50],
            "only_b": self.only_b[:50],
            "ignored": self.ignored[:50],
            "divergences": [d.to_dict() for d in self.divergences],
            "txn_divergences": [d.to_dict() for d in self.txn_divergences],
            "why_a": self.why_a.to_dict() if self.why_a else None,
            "why_b": self.why_b.to_dict() if self.why_b else None,
            "first_differing": self.first_differing,
            "why_error": self.why_error,
            "focus": self.focus,
            "wave": self.wave,
        }


# --- step 1: the common signal set ------------------------------------------


def normalise(path: str, top: str) -> str:
    """A path without its top-level scope, so two runs can be matched.

    §8.7 step 1. The top instance is the one name that legitimately differs
    between a golden model's harness and the DUT's own — `tb_good.dut.state` and
    `tb_bad.dut.state` are the same signal — and nothing below it is.
    """
    if top and path.startswith(top + "."):
        return path[len(top) + 1 :]
    return path


def _top_of(store: Any) -> str:
    scopes = store.scopes()
    return scopes[0].path if scopes else ""


def _ignored(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


# --- steps 3 and 4: the merge sort ------------------------------------------


def _steps(store: Any, handle: int, alignment: Alignment, side: str) -> list[tuple[Fraction, str]]:
    """A signal as `(position, value)` on the shared axis, one entry per change.

    Only events at the exact same timestamp collapse to their settled value.
    Collapsing an entire cycle hides glitches and reports values from before
    the event that actually diverged.
    """
    lo, hi = store.time_range
    start = store.value_at(handle, lo)
    out = [(alignment.ordinal_exact(lo, side), start.bits if start is not None else "?")]
    for t, value in store.transitions(handle, lo, hi + 1):
        pos = alignment.ordinal_exact(t, side)
        if out and out[-1][0] == pos:
            out[-1] = (pos, value.bits)
        else:
            out.append((pos, value.bits))
    return out


def _first_difference(a: list[tuple[Fraction, str]], b: list[tuple[Fraction, str]],
                      start: Fraction, limit: Fraction) -> Fraction | None:
    """The first position at which two step functions disagree — §8.7 step 4."""
    i = j = 0
    va, vb = a[0][1], b[0][1]
    while i + 1 < len(a) and a[i + 1][0] <= start:
        i += 1
        va = a[i][1]
    while j + 1 < len(b) and b[j + 1][0] <= start:
        j += 1
        vb = b[j][1]
    if not _same(va, vb):
        return start
    while True:
        # Next position either side changes at.
        na = a[i + 1][0] if i + 1 < len(a) else None
        nb = b[j + 1][0] if j + 1 < len(b) else None
        if na is None and nb is None:
            return None
        pos = min(x for x in (na, nb) if x is not None)
        if pos > limit:
            return None
        if na == pos:
            i += 1
            va = a[i][1]
        if nb == pos:
            j += 1
            vb = b[j][1]
        if not _same(va, vb):
            return pos


def _kind_of(store_a: Any, ha: int, store_b: Any, hb: int) -> str:
    """Say when a "divergence" is really a build difference.

    A signal that is constant in both runs and constant *at different values*
    never diverged: the two binaries were compiled differently — a parameter, an
    `ifdef`, a tie-off. That is usually the most useful line in the report, and
    it is also the one most likely to be misread as "the design went wrong at
    cycle 0", so it is labelled rather than left to be inferred.
    """
    lo_a, hi_a = store_a.time_range
    lo_b, hi_b = store_b.time_range
    if store_a.is_constant(ha, lo_a, hi_a + 1) and store_b.is_constant(hb, lo_b, hi_b + 1):
        return "constant in both runs at different values — a build difference, not a divergence"
    return ""


def _same(a: str, b: str) -> bool:
    """Equal after zero-extension, so `0` and `0000` are one value."""
    if a == b:
        return True
    n = max(len(a), len(b))
    return a.rjust(n, "0" if a[:1] not in "xzXZ" else a[0]) == b.rjust(
        n, "0" if b[:1] not in "xzXZ" else b[0]
    )


def compare(
    alignment: Alignment,
    ignore: Iterable[str] = (),
    limit: int = MAX_DIVERGENCES,
    only: Iterable[str] = (),
) -> DiffReport:
    """§8.7 steps 1, 3 and 4 — every signal that ever disagrees, earliest first.

    `only` restricts the comparison to a named set, in normalised form. §8.29
    needs it: a post-synthesis netlist keeps its top-level ports and is free to
    rename, merge or delete everything inside, so comparing internals would
    report a synthesiser doing its job as a mismatch.
    """
    if limit < 1:
        raise AlignError("diff limit must be at least 1; zero would hide every divergence")
    a, b = alignment.a, alignment.b
    only = tuple(only)
    # Explicitly requested DUT ports are never silently treated as simulator
    # bookkeeping just because a real output happens to be called *_cnt.
    patterns = tuple(ignore) + (() if only else DEFAULT_IGNORE)
    top_a, top_b = _top_of(a.store), _top_of(b.store)

    by_a = {normalise(s.path, top_a): s for s in a.store.signals()}
    by_b = {normalise(s.path, top_b): s for s in b.store.signals()}
    common = sorted(set(by_a) & set(by_b))
    if only:
        keep = set(only)
        missing = sorted(keep - (set(by_a) & set(by_b)))
        if missing:
            raise AlignError("requested signals missing from one or both traces: " + ", ".join(missing))
        common = [n for n in common if n in keep]

    report = DiffReport(
        alignment=alignment,
        only_a=sorted(set(by_a) - set(by_b)),
        only_b=sorted(set(by_b) - set(by_a)),
    )

    start = max(alignment.ordinal_exact(a.store.time_range[0], "a"),
                alignment.ordinal_exact(b.store.time_range[0], "b"))
    horizon = min(alignment.ordinal_exact(a.store.time_range[1], "a"),
                  alignment.ordinal_exact(b.store.time_range[1], "b"))
    if horizon < start:
        raise AlignError("the aligned traces have no overlapping observed time range")
    found: list[Divergence] = []
    for name in common:
        if _ignored(name, patterns):
            report.ignored.append(name)
            continue
        sa, sb = by_a[name], by_b[name]
        if not sa.n_events or not sb.n_events:
            raise AlignError(f"{name} has no observed values in one of the traces; cannot compare it")
        report.compared += 1
        at = _first_difference(
            _steps(a.store, sa.handle, alignment, "a"),
            _steps(b.store, sb.handle, alignment, "b"),
            start,
            horizon,
        )
        if at is None:
            continue
        ta, tb = alignment.time_at(at, "a"), alignment.time_at(at, "b")
        va = a.store.value_at(sa.handle, ta)
        vb = b.store.value_at(sb.handle, tb)
        found.append(
            Divergence(
                signal=name,
                at=float(at),
                time_a=ta,
                time_b=tb,
                value_a=va.bits if va is not None else "?",
                value_b=vb.bits if vb is not None else "?",
                detail=_kind_of(a.store, sa.handle, b.store, sb.handle),
            )
        )

    found.sort(key=lambda d: (d.at, d.signal))
    if not report.compared:
        raise AlignError("no common signals remain to compare after applying the filters")
    report.divergences = found[:limit]
    select_focus(report)
    return report


def select_focus(report: DiffReport, signal: str | None = None) -> None:
    """Real aligned wave segments for the divergence currently inspected."""
    chosen = (next((d for d in report.divergences if d.signal == signal), None)
              if signal is not None else report.first)
    if signal is not None and chosen is None:
        raise AlignError(f"no reported divergence for {signal!r}")
    report.focus = chosen.signal if chosen else None
    report.why_a = report.why_b = None
    report.first_differing = None
    report.why_error = ""
    if chosen is None:
        report.wave = None
        return
    al = report.alignment
    sides = (al.a, al.b)
    handles = [next(s.handle for s in side.store.signals()
                    if normalise(s.path, _top_of(side.store)) == chosen.signal) for side in sides]
    a, b = [_steps(side.store, h, al, name) for side, h, name in zip(sides, handles, ("a", "b"))]
    center = al.ordinal_exact(chosen.time_a, "a")
    lo = max(center - 4, al.ordinal_exact(al.a.store.time_range[0], "a"),
             al.ordinal_exact(al.b.store.time_range[0], "b"))
    hi = min(center + 4, al.ordinal_exact(al.a.store.time_range[1], "a"),
             al.ordinal_exact(al.b.store.time_range[1], "b"))
    points = sorted({lo, hi} | {p for p, _ in a + b if lo < p < hi})
    segments = []
    i = j = 0
    for left, right in zip(points, points[1:]):
        while i + 1 < len(a) and a[i + 1][0] <= left:
            i += 1
        while j + 1 < len(b) and b[j + 1][0] <= left:
            j += 1
        va, vb = a[i][1], b[j][1]
        segments.append({"start": float(left), "end": float(right), "a": va, "b": vb,
                         "different": not _same(va, vb), "time_a": al.time_at(left, "a"),
                         "time_b": al.time_at(left, "b")})
    report.wave = {"signal": chosen.signal, "start": float(lo), "end": float(hi),
                   "segments": segments}


# --- transaction level ------------------------------------------------------

#: Fields worth comparing between two runs of the same stimulus. Latency and
#: outstanding counts legitimately differ and are not divergences.
TXN_FIELDS = ("addr", "awaddr", "araddr", "data", "wdata", "rdata", "len", "size", "resp", "id")


def _txn_key(txn: Any) -> tuple:
    fields = tuple(
        (k, txn.fields.get(k)) for k in TXN_FIELDS if txn.fields.get(k) is not None
    )
    return (txn.kind, fields)


def compare_transactions(
    alignment: Alignment, protocol_a: Any, protocol_b: Any, limit: int = MAX_DIVERGENCES
) -> list[Divergence]:
    """The first transaction that differs, per interface — §11.4's other card.

    Compared in order within an interface, which is the only correspondence that
    exists: a transaction has no identity across two runs beyond "the n-th write
    m0 issued". Where one run issued more than the other, the extra ones are
    reported as a divergence too — a write that simply never happened is the
    answer more often than a write with the wrong data.
    """
    if protocol_a is None or protocol_b is None:
        return []
    out: list[Divergence] = []
    by_b = {ex.interface.name: ex for ex in getattr(protocol_b, "extractions", [])}
    for ex_a in getattr(protocol_a, "extractions", []):
        ex_b = by_b.get(ex_a.interface.name)
        if ex_b is None:
            continue
        for i in range(max(len(ex_a.transactions), len(ex_b.transactions))):
            ta = ex_a.transactions[i] if i < len(ex_a.transactions) else None
            tb = ex_b.transactions[i] if i < len(ex_b.transactions) else None
            if ta is not None and tb is not None and _txn_key(ta) == _txn_key(tb):
                continue
            here = ta or tb
            side = "a" if ta is not None else "b"
            at = int(alignment.ordinal(here.start_time, side))
            out.append(
                Divergence(
                    signal=ex_a.interface.name,
                    at=at,
                    time_a=ta.start_time if ta else -1,
                    time_b=tb.start_time if tb else -1,
                    value_a=_txn_text(ta),
                    value_b=_txn_text(tb),
                    level="transaction",
                    ref=here.ref,
                    detail=(
                        "only one run issued it"
                        if ta is None or tb is None
                        else "same position, different contents"
                    ),
                )
            )
            break  # first per interface; §8.7 asks for the first divergence
    out.sort(key=lambda d: (d.at, d.signal))
    return out[:limit]


def _txn_text(txn: Any) -> str:
    if txn is None:
        return "—"
    fields = " ".join(
        f"{k}={v:#x}" if isinstance(v, int) else f"{k}={v}"
        for k, v in ((k, txn.fields.get(k)) for k in TXN_FIELDS)
        if v is not None
    )
    return f"{txn.kind} {fields}".strip()


# --- steps 5 and 6: why, on both sides --------------------------------------


def explain(report: DiffReport, graph_a: Any, graph_b: Any) -> DiffReport:
    """§8.7 steps 5 and 6 — the same question asked of both runs.

    The two chains are walked down their primary paths together and the first
    index where they part is recorded. That index is what §11.4 paints magenta:
    everything above it is the two runs agreeing about why, and it is the first
    line where they stop.
    """
    first = next((d for d in report.divergences if d.signal == report.focus), report.first)
    if first is None:
        return report
    a, b = report.alignment.a, report.alignment.b
    try:
        if graph_a is not None:
            full_a = _resolve(graph_a, first.signal, _top_of(a.store))
            if full_a:
                report.why_a = WhyTracer(graph_a, a.store).why(full_a, first.time_a).root
        if graph_b is not None:
            full_b = _resolve(graph_b, first.signal, _top_of(b.store))
            if full_b:
                report.why_b = WhyTracer(graph_b, b.store).why(full_b, first.time_b).root
    except Exception as e:  # noqa: BLE001 - a diff without why is still a diff
        report.why_error = str(e)
        return report

    if report.why_a is None or report.why_b is None:
        report.why_error = report.why_error or (
            "no RTL for one of the runs, so the two chains cannot be compared"
        )
        return report
    report.first_differing = _diverging_node(report.why_a, report.why_b)
    return report


def _resolve(graph: Any, normalised: str, top: str) -> str | None:
    """The full path a normalised name has in this run's graph."""
    for candidate in (f"{top}.{normalised}" if top else normalised, normalised):
        if graph.get(candidate) is not None:
            return candidate
    return None


def _diverging_node(a: CausalNode, b: CausalNode) -> int | None:
    """First index on the two primary paths where the chains stop agreeing."""
    i = 0
    na: CausalNode | None = a
    nb: CausalNode | None = b
    seen_a: set[int] = set()
    seen_b: set[int] = set()
    while na is not None and nb is not None:
        same = (
            na.signal.name == nb.signal.name
            and na.reason is nb.reason
            and na.value == nb.value
        )
        if not same:
            return i
        seen_a.add(id(na))
        seen_b.add(id(nb))
        na = next((c for c in na.children if c.is_primary_path and id(c) not in seen_a), None)
        nb = next((c for c in nb.children if c.is_primary_path and id(c) not in seen_b), None)
        i += 1
    return None if na is None and nb is None else i
