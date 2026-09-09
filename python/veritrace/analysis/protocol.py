"""Protocol rule violations as automatic findings — §8.14 step 5, §11.4.

*Violations become findings carrying (rule, transaction, cycle, signal).*

Nothing new is computed here: the extraction already evaluated the rules while
assembling. This is the adapter that puts them in the Checks tab next to the
stuck signals and the latches, because from the user's side they are the same
kind of event — something the tool noticed without being asked.

The `[why]` on each row is what makes the entry point work: a protocol
violation is a statement about a signal at a cycle, so it opens in Causal like
any other finding, and from there §8.16's transaction hop is one node away.
"""

from __future__ import annotations

from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity, location_of

#: One check name per rule severity class, so `--fail-on protocol` works and a
#: project can silence a specific rule via `checks.disable = ["AXI_BRESP"]`.
CHECK = "protocol_rule"

_SEVERITY = {"error": Severity.ERROR, "warn": Severity.WARN, "info": Severity.INFO}


def scan(
    analysis: Any,
    clock: Any = None,
    config: Any = None,
    graph: Any = None,
) -> Iterator[Finding]:
    """Turn every rule violation in `analysis` into a `Finding`."""
    if analysis is None:
        return
    for ex in analysis.extractions:
        iface = ex.interface
        for v in ex.violations:
            signal = v.signal or _signal_for(iface, v)
            if signal and config is not None and config.is_ignored(signal):
                continue
            where = f"{iface.name}" + (f" - {v.txn}" if v.txn else "")
            notes: list[str] = [f"{iface.pack.name} rule {v.rule}"]
            note = ex.skipped.get(f"rule {v.rule}")
            if note:
                notes.append(note)
            yield Finding(
                group=Group.PROTOCOL,
                severity=_SEVERITY.get(v.severity, Severity.WARN),
                check=CHECK,
                title=f"{where}: {v.msg}",
                signal=signal,
                loc=location_of(graph, signal),
                time=v.time,
                detail=v.msg,
                why=f"why({signal} @ {v.time})" if signal else None,
                notes=tuple(notes),
                # The rule id joins the identity, so suppressing one rule on one
                # signal does not silence a different rule on the same wire.
                related=(v.rule,) + ((v.txn,) if v.txn else ()),
            )


def _signal_for(iface: Any, v: Any) -> str | None:
    """A signal to open the waveform on for a transaction-level violation.

    The closing channel's `valid`: a rule about a finished transaction is almost
    always about how it finished, and that is the wire to look at.
    """
    for spec in iface.pack.transactions:
        phase = spec.end or spec.body
        if phase is None:
            continue
        try:
            return iface.signals.get(iface.pack.channel(phase.channel).valid)
        except Exception:  # noqa: BLE001 - a pack error is reported elsewhere
            return None
    return None
