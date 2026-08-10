"""Data corruption as automatic findings — §8.19, §11.4.

*Report every mismatch as a finding.* §8.19 says so directly, and it belongs
with the stuck signals and the protocol violations for the same reason §8.20's
timing checks do: nobody goes looking for silent data corruption, so it has to
arrive unasked or it is not found at all.

The severity is ERROR without qualification. A latch might not matter and a
CDC crossing might be benign; a word that came back different from the word
that went in is always a bug.
"""

from __future__ import annotations

from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity

CHECK = "data_mismatch"

CHECKS: dict[str, str] = {
    CHECK: "a read disagreed with what was written, or a write changed along the path (§8.19)",
}


def scan(report: Any, config: Any = None) -> Iterator[Finding]:
    """Every §8.19 mismatch, as a finding that names the bytes."""
    if report is None:
        return
    for m in report.mismatches:
        where = "on" if m.kind == "scoreboard" else "between"
        addr = "" if m.addr is None else f" at 0x{m.addr:x}"
        yield Finding(
            group=Group.INTEGRITY,
            severity=Severity.ERROR,
            check=CHECK,
            title=f"byte(s) {m.lane_text}{addr} changed {where} {m.where}",
            signal=m.signal,
            time=m.time,
            detail=m.detail,
            why=m.why,
            # Both transactions, so a suppression survives a re-run and two
            # corruptions of the same byte from different writes stay distinct.
            related=tuple(x for x in (m.source, m.victim) if x),
        )
