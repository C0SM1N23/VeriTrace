"""Steps 2-3 of §8.20: the per-bank state machine and the timing checker.

*IDLE -> ACTIVATING -> ACTIVE -> PRECHARGING -> IDLE, run over the command
sequence.* Both halves below walk the same small event lists — a bank issues
maybe a few thousand commands even in a long run, nothing like the millions of
clock edges §8.17 has to sweep — so this is a handful of linear passes over
lists, not a per-cycle simulation. Simpler, and it is what the acceptance
criterion actually asks to be checked: exact commands, exact cycles.

**Auto-precharge is decoded but not modelled here.** A READ/WRITE with `ap=1`
closes its own bank once its burst finishes, on a schedule that also depends on
burst length and CL/CWL — genuinely more state than the plain
ACTIVATE/PRECHARGE pairing below carries. `args.ap` is still in every decoded
command for display and for a pack's own rules; a design that relies on it for
the bank *timeline* will see that bank stay `active` until an explicit
PRECHARGE, which is reported rather than guessed at (P7) rather than silently
producing a wrong close time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from veritrace.memory.model import ACTIVATING, ACTIVE, IDLE, PRECHARGING, BankSegment, CmdEvent, TimingViolation
from veritrace.memory.timing import ChipTiming, NS_FIELDS, to_ticks
from veritrace.clocks import Clock

#: Constraints §8.20 checks that are gaps between two specific commands, or a
#: sliding window over one command type — every one of the twelve except
#: CL/CWL, which need a data-bus signal no pin-level pack is guaranteed to
#: declare (see `check_timing`'s note when a pack has none).
ALL_CONSTRAINTS = (*NS_FIELDS, "CL", "CWL")


def _bank_of(c: CmdEvent) -> int | None:
    b = c.fields.get("bank")
    return b if isinstance(b, int) else None


def banks_touched(c: CmdEvent, n_banks: int) -> list[int]:
    """Which banks a command acts on.

    Almost always one — except PRECHARGE ALL, which every real controller uses
    to close the array between refreshes: the datasheet reuses `a[10]` (the
    auto-precharge bit on READ/WRITE) to mean "all banks" here, the pack
    decodes it as `all`, and filing such a command under `ba` alone would leave
    every other bank believing its row was still open. That is wrong for the
    timeline *and* for tRP on the next ACTIVATE to any of them.
    """
    if c.name == "PRECHARGE" and c.fields.get("all"):
        return list(range(n_banks))
    b = _bank_of(c)
    return [b] if b is not None and 0 <= b < n_banks else []


def _by_bank(commands: Sequence[CmdEvent], n_banks: int) -> dict[int, list[CmdEvent]]:
    out: dict[int, list[CmdEvent]] = {b: [] for b in range(n_banks)}
    for c in commands:
        for b in banks_touched(c, n_banks):
            out[b].append(c)
    return out


def segments(
    commands: Sequence[CmdEvent],
    n_banks: int,
    tRCD: int,
    tRP: int,
    run_start: int,
    run_end: int,
) -> list[BankSegment]:
    """The bank timeline of TAB 10: one contiguous run of segments per bank,
    covering `[run_start, run_end]` with no gaps and no overlaps.

    `tRCD`/`tRP` are already in trace time units (converted once by the
    caller) — this function does no unit conversion of its own, so it is
    testable against a hand-built command list without a `Clock` at all.

    The timeline shows **what the controller did**, not what it should have
    done. On a design that violates tRP the next ACTIVATE lands while the bank
    is still precharging, so that `precharging` segment is cut short at the
    ACTIVATE rather than allowed to overlap it — the too-early command is
    exactly the finding, and the timing checker reports it as one. A timeline
    that quietly stretched the segments to stay legal would be hiding the bug
    it exists to make visible.
    """
    out: list[BankSegment] = []
    # `_by_bank` already fans a PRECHARGE ALL out to every bank, so a bank
    # closed by one gets its `precharging` segment like any other.
    by_bank = _by_bank(commands, n_banks)
    for b in range(n_banks):
        cmds = sorted(
            (c for c in by_bank[b] if c.name in ("ACTIVATE", "PRECHARGE")), key=lambda c: c.time
        )
        segs: list[BankSegment] = []
        cursor = run_start
        row: int | None = None
        open_since: int | None = None

        def emit(state: str, t0: int, t1: int, r: int | None) -> None:
            if t1 > t0:
                segs.append(BankSegment(b, state, t0, t1, r))

        def truncate_to(t: int) -> None:
            """Cut the tail back to `t` when a command arrives early."""
            while segs and segs[-1].t0 >= t:
                segs.pop()
            if segs and segs[-1].t1 > t:
                segs[-1].t1 = t

        for c in cmds:
            if c.time < cursor:
                truncate_to(c.time)
                cursor = c.time
            if c.name == "ACTIVATE":
                emit(IDLE, cursor, c.time, None)
                row = c.fields.get("row")
                open_since = c.time
                cursor = min(c.time + tRCD, run_end)
                emit(ACTIVATING, c.time, cursor, row)
            elif c.name == "PRECHARGE":
                if open_since is None:
                    continue  # precharging an already-idle bank: a no-op, not a segment
                emit(ACTIVE, cursor, c.time, row)
                cursor = min(c.time + tRP, run_end)
                emit(PRECHARGING, c.time, cursor, row)
                open_since, row = None, None
        emit(ACTIVE if open_since is not None else IDLE, cursor, run_end, row)
        out.extend(segs)
    out.sort(key=lambda s: (s.bank, s.t0))
    return out


@dataclass(slots=True)
class _Units:
    """§8.20's twelve parameters, converted once from the datasheet's
    nanoseconds into the trace's own time units — every check after this is
    integer arithmetic on trace timestamps, never a unit conversion."""

    tRCD: int = 0
    tRP: int = 0
    tRAS: int = 0
    tRC: int = 0
    tRRD: int = 0
    tWR: int = 0
    tWTR: int = 0
    tRTP: int = 0
    tFAW: int = 0
    tRFC: int = 0
    tREFI: int = 0


def _to_units(chip: ChipTiming, timescale: str) -> tuple[_Units, list[str]]:
    out = _Units()
    failed: list[str] = []
    for f in NS_FIELDS:
        v = to_ticks(getattr(chip, f), timescale, maximum=f == "tREFI")
        if v is None:
            failed.append(f)
        else:
            setattr(out, f, v)
    return out, failed


def _violation(
    constraint: str,
    bank: int | None,
    second: CmdEvent,
    first: CmdEvent,
    gap: int,
    limit: int,
    clock: Clock | None,
    is_maximum: bool = False,
) -> TimingViolation:
    period = clock.period if clock is not None else None
    regular = period is not None and all(b - a == period for a, b in zip(clock.edges, clock.edges[1:]))
    # A minimum rounds UP, a maximum DOWN; neither is a nearest-cycle estimate.
    # Gated clocks have no single conversion of a wall-time bound to cycles.
    limit_cycles = ((limit // period if is_maximum else -(-limit // period))
                    if regular else None)
    return TimingViolation(
        constraint=constraint,
        bank=bank,
        at=second.time,
        measured_cycles=clock.cycles_between(first.time, second.time) if clock else None,
        limit_cycles=limit_cycles,
        measured_ticks=gap,
        limit_ticks=limit,
        is_maximum=is_maximum,
        first=first,
        second=second,
    )


def _same_bank_violations(cmds: list[CmdEvent], bank: int, u: _Units, clock: Clock | None,
                          observed: set[str] | None = None) -> list[TimingViolation]:
    """tRCD, tRAS, tRC, tWR, tRTP — every constraint measured between two
    commands on the *same* bank, in one linear pass over that bank's stream."""
    out: list[TimingViolation] = []
    observed = observed if observed is not None else set()
    #: The row currently open, cleared by PRECHARGE — what tRAS/tRCD measure from.
    open_activate: CmdEvent | None = None
    #: The last ACTIVATE regardless of what came after, which is what tRC
    #: measures from. Kept separate because tRC spans the PRECHARGE in between:
    #: ACTIVATE -> PRECHARGE -> ACTIVATE is the *normal* sequence, so clearing
    #: this on PRECHARGE would mean tRC is never checked at all.
    last_activate: CmdEvent | None = None
    last_precharge: CmdEvent | None = None
    awaiting_first_access = False
    last_write: CmdEvent | None = None
    last_read: CmdEvent | None = None

    for c in sorted(cmds, key=lambda c: c.time):
        if c.name == "ACTIVATE":
            if last_activate is not None:
                observed.add("tRC")
                gap = c.time - last_activate.time
                if gap < u.tRC:
                    out.append(_violation("tRC", bank, c, last_activate, gap, u.tRC, clock))
            if last_precharge is not None:
                observed.add("tRP")
                gap = c.time - last_precharge.time
                if gap < u.tRP:
                    out.append(_violation("tRP", bank, c, last_precharge, gap, u.tRP, clock))
            open_activate = last_activate = c
            awaiting_first_access = True
            last_write = last_read = None
        elif c.name in ("READ", "WRITE"):
            if open_activate is not None and awaiting_first_access:
                observed.add("tRCD")
                gap = c.time - open_activate.time
                if gap < u.tRCD:
                    out.append(_violation("tRCD", bank, c, open_activate, gap, u.tRCD, clock))
                awaiting_first_access = False
            if c.name == "WRITE":
                last_write = c
            else:
                last_read = c
        elif c.name == "PRECHARGE":
            if open_activate is not None:
                observed.add("tRAS")
                gap = c.time - open_activate.time
                if gap < u.tRAS:
                    out.append(_violation("tRAS", bank, c, open_activate, gap, u.tRAS, clock))
            if last_write is not None:
                observed.add("tWR")
                gap = c.time - last_write.time
                if gap < u.tWR:
                    out.append(_violation("tWR", bank, c, last_write, gap, u.tWR, clock))
            if last_read is not None:
                observed.add("tRTP")
                gap = c.time - last_read.time
                if gap < u.tRTP:
                    out.append(_violation("tRTP", bank, c, last_read, gap, u.tRTP, clock))
            last_precharge = c
            # `last_activate` deliberately survives: tRC spans this PRECHARGE.
            open_activate, last_write, last_read = None, None, None
    return out


def _device_wide_violations(commands: Sequence[CmdEvent], u: _Units, clock: Clock | None,
                           observed: set[str] | None = None) -> list[TimingViolation]:
    """tRRD, tFAW, tWTR, tRFC, tREFI — every constraint that spans banks or
    has no bank of its own."""
    out: list[TimingViolation] = []
    observed = observed if observed is not None else set()
    ordered = sorted(commands, key=lambda c: c.time)
    activates = [c for c in ordered if c.name == "ACTIVATE"]

    # tRRD: each ACTIVATE against the most recent one on a *different* bank —
    # which is what the constraint says. Comparing against the immediately
    # preceding command instead would skip the check entirely whenever two
    # same-bank ACTIVATEs sit between two different-bank ones. The per-bank
    # record is scanned rather than kept sorted because a device has four to
    # eight banks, so "scan them all" is both the fastest and the obvious way.
    last_per_bank: dict[int, CmdEvent] = {}
    for cur in activates:
        b = _bank_of(cur)
        nearest = max(
            (e for other, e in last_per_bank.items() if other != b),
            key=lambda e: e.time,
            default=None,
        )
        if nearest is not None:
            observed.add("tRRD")
            gap = cur.time - nearest.time
            if gap < u.tRRD:
                out.append(_violation("tRRD", None, cur, nearest, gap, u.tRRD, clock))
        if b is not None:
            last_per_bank[b] = cur

    # tFAW permits four ACTIVATEs; it is the fifth that must wait.
    for i in range(4, len(activates)):
        observed.add("tFAW")
        gap = activates[i].time - activates[i - 4].time
        if gap < u.tFAW:
            out.append(_violation("tFAW", None, activates[i], activates[i - 4], gap, u.tFAW, clock))

    # tWTR: bus turnaround, last WRITE to the next READ, whichever bank.
    last_write: CmdEvent | None = None
    for c in ordered:
        if c.name == "WRITE":
            last_write = c
        elif c.name == "READ" and last_write is not None:
            observed.add("tWTR")
            gap = c.time - last_write.time
            if gap < u.tWTR:
                out.append(_violation("tWTR", None, c, last_write, gap, u.tWTR, clock))
            last_write = None

    # tRFC: REFRESH to the very next command, of any kind — the nearest one is
    # the binding case, so no window is needed.
    for i, r in enumerate(ordered):
        if r.name != "REFRESH" or i + 1 >= len(ordered):
            continue
        nxt = ordered[i + 1]
        observed.add("tRFC")
        gap = nxt.time - r.time
        if gap < u.tRFC:
            out.append(_violation("tRFC", None, nxt, r, gap, u.tRFC, clock))

    refreshes = [c for c in ordered if c.name == "REFRESH"]

    # tREFI: a *maximum* interval between consecutive REFRESHes.
    for prev, cur in zip(refreshes, refreshes[1:]):
        observed.add("tREFI")
        gap = cur.time - prev.time
        if gap > u.tREFI:
            out.append(_violation("tREFI", None, cur, prev, gap, u.tREFI, clock, is_maximum=True))

    return out


def check_timing(
    commands: Sequence[CmdEvent],
    n_banks: int,
    chip: ChipTiming,
    timescale: str,
    clock: Clock | None,
) -> tuple[list[TimingViolation], dict[str, int], dict[str, str]]:
    """Every §8.20 constraint the pack's decoded commands can be checked
    against.

    Returns the violations, a `{constraint: count}` map covering every
    constraint that *was* checked (0 included — "conforme" is a stated fact,
    §8.20's own report format), and a `{constraint: reason}` map for the ones
    that were not, most notably CL/CWL: verifying data latency needs a signal
    on the DQ bus this pack does not declare, so it is skipped and said to be,
    rather than silently reported as passing.
    """
    units, unit_failures = _to_units(chip, timescale)
    skipped = {f: "chip timing file gave an unusable value" for f in unit_failures}
    skipped["CL"] = skipped["CWL"] = (
        "needs a data-bus signal correlated to the command; this pack's [[command]] "
        "table declares none, so read/write latency cannot be measured"
    )
    skipped["tWR"] = (
        "the last write data beat is not observed; command-to-PRECHARGE is only "
        "a lower bound, so violations can be reported but compliance cannot be certified"
    )
    if any(c.name in ("READ", "WRITE") and c.fields.get("ap") for c in commands):
        skipped["auto-precharge"] = (
            "burst completion is not observed; the bank timeline models explicit "
            "PRECHARGE only and does not establish the auto-precharge close time"
        )

    # A per-bank command whose bank could not be decoded (X on `ba` that cycle)
    # takes part in no same-bank check. Saying how many were dropped is the
    # difference between "this design is clean" and "this many commands were
    # never examined" (P1).
    banked = {"ACTIVATE", "READ", "WRITE", "PRECHARGE"}
    undecodable = sum(
        1 for c in commands if c.name in banked and not banks_touched(c, n_banks)
    )
    if undecodable:
        skipped["bank"] = (
            f"{undecodable} command(s) carried no decodable bank number and took part "
            "in no per-bank check"
        )

    out: list[TimingViolation] = []
    by_bank = _by_bank(commands, n_banks)
    observed: set[str] = set()
    for b, cmds in by_bank.items():
        out.extend(_same_bank_violations(cmds, b, units, clock, observed))
    out.extend(_device_wide_violations(commands, units, clock, observed))
    out.sort(key=lambda v: v.at)

    for constraint in NS_FIELDS:
        if constraint not in observed and constraint not in skipped:
            skipped[constraint] = "no qualifying command pair/window was observed in this trace"
    checked = {c: 0 for c in NS_FIELDS if c not in skipped}
    for v in out:
        checked[v.constraint] = checked.get(v.constraint, 0) + 1

    return out, checked, skipped
