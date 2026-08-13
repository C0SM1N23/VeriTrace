"""What an automatic check reports — the currency of TAB 6 (§11.4).

Every detector in §8.4, §8.5, §8.11 and §8.11c produces `Finding`s, and the
Checks tab, the CLI and CI all consume that one shape. Adding a detector means
adding a function that yields `Finding`s; nothing downstream changes.

Two design points carry weight:

* **`id` is derived from content, not from position.** Suppressing a finding
  has to survive re-running the simulation, re-elaborating the RTL and adding a
  check that happens to sort earlier. So the id hashes what the finding *is*
  (check, signal, location), never when it was produced or where it landed in a
  list.
* **A finding carries the query that explains it.** §11.4 requires a working
  `[why]` on every row; making that a field rather than something the frontend
  reconstructs means a check decides for itself what question it is an answer
  to — a stuck signal asks about its frozen value, an X source about its X.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Iterable

from veritrace.graph.model import SourceLoc


class Severity(IntEnum):
    """Sorted descending in every report, so the worst thing is the first."""

    INFO = 1
    WARN = 2
    ERROR = 3

    @property
    def label(self) -> str:
        return self.name.lower()


class Group(Enum):
    """The sections of TAB 6, in the order §11.4 lists them."""

    STUCK = "stuck"
    X_SOURCES = "x_sources"
    #: §8.14 step 5 — a pack's rules, checked against the trace. Placed with the
    #: other automatic findings deliberately: a protocol violation is not a
    #: separate kind of news, it is one more thing the tool noticed on its own.
    PROTOCOL = "protocol"
    #: §8.18 — deadlock, livelock and starvation. Next to the protocol
    #: violations because they come from the same transaction table, and above
    #: lint because a bus that stopped outranks a latch that might not matter.
    LIVENESS = "liveness"
    #: §8.20 — an SDRAM timing constraint that did not hold. Same reasoning as
    #: PROTOCOL: a controller that violates tRCD is not a separate category of
    #: news from one that violates an AXI rule, it is the same kind of thing
    #: the tool already watches for.
    MEMORY = "memory"
    #: §8.19 — a read that disagreed with the write before it, or a write that
    #: changed shape between two interfaces. Above lint and next to the other
    #: transaction-derived findings, because corrupted data outranks every
    #: structural warning in the list.
    INTEGRITY = "integrity"
    #: §8.8's static checks. Above lint because a dead state is a functional bug
    #: rather than a style warning, and below the transaction-derived groups
    #: because those carry evidence from the run while these are structural.
    FSM = "fsm"
    LINT = "lint"
    #: §13.7 — anything a project's own plugins found. Last but one, because a
    #: finding from a plugin is by definition about this project rather than
    #: about RTL in general, and the reader knows their own checks.
    PLUGIN = "plugin"
    PARAMETERS = "parameters"

    @property
    def order(self) -> int:
        return list(Group).index(self)

    @property
    def label(self) -> str:
        return {"stuck": "STUCK", "x_sources": "X SOURCES", "protocol": "PROTOCOL",
                "memory": "MEMORY", "integrity": "DATA INTEGRITY",
                "liveness": "LIVENESS", "fsm": "FSM", "lint": "LINT",
                "plugin": "PLUGINS", "parameters": "PARAMETERS"}[self.value]


@dataclass(frozen=True, slots=True)
class Finding:
    group: Group
    severity: Severity
    #: Stable check name, e.g. "inferred_latch". Used by `--fail-on` and by
    #: `checks.disable` in `.veritrace.toml`, so it is API.
    check: str
    title: str
    signal: str | None = None
    loc: SourceLoc | None = None
    #: Timestamp the evidence sits at, if the finding has one.
    time: int | None = None
    detail: str = ""
    #: VTQ that opens this finding in Causal. `None` when nothing is traced to
    #: explain — an honest empty `[why]` beats one that errors (P7).
    why: str | None = None
    #: Extra lines shown under the row: sampled cycles, honesty notes (§8.11).
    notes: tuple[str, ...] = ()
    #: Signals other than `signal` this finding is about, for cone/export.
    related: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Stable identity, for suppressions that outlive a re-run.

        `related` is part of it because check, signal and location are not
        always enough to tell two findings apart: two unsynchronised crossings
        into the same flop, from the same `always_ff`, differ only in their
        source. Without it they would share an id, and suppressing one would
        silently hide the other.

        Deliberately excludes prose (`title`, `detail`) and anything positional:
        a suppression has to survive re-running the simulation, re-elaborating
        the RTL, and a reworded message.
        """
        key = "|".join(
            (self.check, self.signal or "", str(self.loc or ""), ",".join(sorted(self.related)))
        )
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    @property
    def sort_key(self) -> tuple:
        # Worst first; inside a severity, group order, then oldest evidence,
        # then path — total and deterministic, so two runs agree (P1).
        return (
            -int(self.severity),
            self.group.order,
            self.time if self.time is not None else 1 << 62,
            self.signal or "",
            self.check,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group.value,
            "severity": self.severity.label,
            "check": self.check,
            "title": self.title,
            "signal": self.signal,
            "loc": (
                {"file": self.loc.file, "line": self.loc.line, "col": self.loc.col}
                if self.loc
                else None
            ),
            "time": self.time,
            "detail": self.detail,
            "why": self.why,
            "notes": list(self.notes),
            "related": list(self.related),
        }


@dataclass(slots=True)
class Report:
    """Everything a session found, plus what it could not look at.

    `skipped` exists because §8.10b's rule of honesty applies to checks too: a
    detector that silently does nothing when it lacks a clock or a trace is
    indistinguishable from one that found nothing wrong.
    """

    findings: list[Finding] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    #: §13.7 — tables a project's plugins produced. Carried here rather than
    #: returned separately because §13.7 promises a tabular result reaches the
    #: interface without anyone touching it, and the report is what reaches it.
    plugin_tables: list[Any] = field(default_factory=list)
    #: Wall time of the whole scan, milliseconds.
    elapsed_ms: float = 0.0

    def __iter__(self):
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.sort_key)

    def by_group(self) -> dict[Group, list[Finding]]:
        out: dict[Group, list[Finding]] = {g: [] for g in Group}
        for f in self.sorted():
            out[f.group].append(f)
        return {g: v for g, v in out.items() if v}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.check] = out.get(f.check, 0) + 1
        return out

    def without(self, suppressed: Iterable[str]) -> "Report":
        dead = set(suppressed)
        return Report(
            findings=[f for f in self.findings if f.id not in dead],
            skipped=dict(self.skipped),
            plugin_tables=list(self.plugin_tables),
            elapsed_ms=self.elapsed_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.sorted()],
            "groups": {g.value: len(v) for g, v in self.by_group().items()},
            "counts": self.counts(),
            "skipped": dict(self.skipped),
            "plugin_tables": [t.to_dict() for t in self.plugin_tables],
            "ms": round(self.elapsed_ms, 2),
        }

    def summary(self) -> str:
        """The one line §13.4 prints after `init` and `serve`."""
        if not self.findings:
            return "no automatic findings"
        parts = [f"{len(v)} {g.label.lower()}" for g, v in self.by_group().items()]
        return " · ".join(parts)
