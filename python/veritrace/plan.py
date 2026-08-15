"""The verification plan — §8.37.

Declarative, like a protocol pack: one file, not a system. A coverage number
says what ran; this says whether what *matters* was tested, and the difference
between the two is the difference between code coverage and coverage of intent.

```toml
[[item]]
id = "DMA-01"
desc = "A descriptor chain of variable length is processed correctly"
covered_by = ["fcov:dma0.desc_len_bins", "test:tb_dma_chain"]
status = "covered"
```

`covered_by` entries are `kind:reference`, and the kinds are the things the tool
already produces — so an item is not a claim in a document, it is a link to a
cover point or a formal property that either holds or does not. Resolving those
links is what `link()` does, and an item whose reference resolves to nothing is
reported as such rather than counted.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The reference kinds an item may cite. Anything else is a typo, and saying so
#: beats silently ignoring it.
KINDS = ("fcov", "cov", "formal", "test", "check", "txn")

PLANNED, COVERED, WAIVED = "planned", "covered", "waived"
STATUSES = (PLANNED, COVERED, WAIVED)


@dataclass(slots=True)
class Link:
    kind: str
    ref: str
    #: `hit`, `missing`, `unknown` — filled in by `link()`.
    state: str = "unknown"
    detail: str = ""

    @property
    def text(self) -> str:
        return f"{self.kind}:{self.ref}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "state": self.state, "detail": self.detail}


@dataclass(slots=True)
class Item:
    id: str
    desc: str = ""
    status: str = PLANNED
    links: list[Link] = field(default_factory=list)
    note: str = ""

    @property
    def evidence(self) -> str:
        """What the *tool* found, which may not be what the file claims.

        A plan is a document and documents drift. `status = "covered"` written by
        hand is an intention; `hit` on every link is a measurement, and where the
        two disagree the measurement is what gets shown.
        """
        if self.status == WAIVED:
            return WAIVED
        if not self.links:
            return "unlinked"
        states = {l.state for l in self.links}
        if states == {"hit"}:
            return "covered"
        if "hit" in states:
            return "partial"
        return "missing" if "missing" in states else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "desc": self.desc, "status": self.status,
            "evidence": self.evidence, "note": self.note,
            "links": [l.to_dict() for l in self.links],
        }


@dataclass(slots=True)
class Plan:
    items: list[Item] = field(default_factory=list)
    path: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        """Share of items the tool can *show* are covered, waivers excluded.

        Not the share whose `status` says covered — see `Item.evidence`.
        """
        scored = [i for i in self.items if i.status != WAIVED]
        if not scored:
            return None
        return 100.0 * sum(1 for i in scored if i.evidence == "covered") / len(scored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "n": len(self.items),
            "score": None if self.score is None else round(self.score, 1),
            "items": [i.to_dict() for i in self.items],
            "errors": list(self.errors),
        }


def load(path: Path | str) -> Plan:
    """Read a `testplan.toml`. A malformed entry is reported, not skipped."""
    path = Path(path)
    out = Plan(path=path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for i, raw in enumerate(data.get("item", []) or []):
        ident = str(raw.get("id") or "").strip()
        if not ident:
            out.errors.append(f"item {i + 1} has no id")
            continue
        status = str(raw.get("status") or PLANNED)
        if status not in STATUSES:
            out.errors.append(f"{ident}: unknown status {status!r}; try {', '.join(STATUSES)}")
            status = PLANNED
        item = Item(id=ident, desc=str(raw.get("desc") or ""), status=status,
                    note=str(raw.get("note") or ""))
        for ref in raw.get("covered_by", []) or []:
            kind, _, target = str(ref).partition(":")
            if not target or kind not in KINDS:
                out.errors.append(
                    f"{ident}: {ref!r} is not `kind:reference`; kinds are {', '.join(KINDS)}"
                )
                continue
            item.links.append(Link(kind=kind, ref=target))
        out.items.append(item)
    return out


def link(plan: Plan, coverage: Any = None, formal: Any = None, checks: Any = None) -> Plan:
    """Resolve every `covered_by` against what the run actually produced.

    This is the whole point of §8.37 being a thin layer rather than a document:
    an item is only covered if the thing it points at exists *and* was reached.
    """
    points = _cover_points(coverage)
    properties = _properties(formal)
    findings = _check_ids(checks)

    for item in plan.items:
        for l in item.links:
            match l.kind:
                case "fcov" | "cov":
                    hit = points.get(l.ref)
                    if hit is None:
                        l.state, l.detail = "unknown", "no cover point of that name in this run"
                    else:
                        covered, total = hit
                        l.state = "hit" if covered == total and total else "missing"
                        l.detail = f"{covered}/{total} bins"
                case "formal":
                    p = properties.get(l.ref)
                    if p is None:
                        l.state, l.detail = "unknown", "no formal property of that name"
                    else:
                        l.state = "hit" if p[0] else "missing"
                        l.detail = p[1]
                case "check":
                    # A `check:` item is a claim that the detector is quiet. Any
                    # finding whose name starts with the reference counts, so
                    # `check:stuck` covers `stuck` and `stuck_x` alike.
                    open_now = [f for f in findings if f == l.ref or f.startswith(l.ref)]
                    l.state = "missing" if open_now else "hit"
                    l.detail = (
                        f"{len(open_now)} open: {', '.join(sorted(open_now)[:3])}"
                        if open_now else "no finding from this check"
                    )
                case _:
                    # `test:` and `txn:` name things outside VeriTrace — a
                    # testbench, a transaction stream. The plan may cite them and
                    # the tool will not pretend to have checked one.
                    l.state, l.detail = "unknown", f"{l.kind} references are not resolved here"
    return plan


def _cover_points(coverage: Any) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for f in getattr(coverage, "functional", []) or []:
        for p in f.points:
            out[p.name] = (p.covered, p.total)
            out[f"{f.iface}.{p.name}"] = (p.covered, p.total)
    return out


def _properties(formal: Any) -> dict[str, tuple[bool, str]]:
    """Formal properties by name, from a live report or from its JSON.

    `verdict` is an enum in one and a string in the other, and a scorecard
    assembled in CI from four separate jobs sees the second — so both are read
    rather than one of them being made canonical.
    """
    out: dict[str, tuple[bool, str]] = {}
    for report in formal or []:
        for p in getattr(report, "properties", []) or []:
            verdict = getattr(p, "verdict", "")
            verdict = getattr(verdict, "value", verdict)
            label = getattr(p, "label", None) or f"{verdict} at depth {getattr(p, 'depth', 0)}"
            out[p.id] = (verdict == "held", label)
            iface = getattr(report, "iface", "")
            if iface:
                out[f"{iface}.{p.id}"] = out[p.id]
    return out


def _check_ids(checks: Any) -> set[str]:
    """Every check name with an open finding.

    `Report` is a flat list of findings, each carrying its own `check` — the
    stable name `--fail-on` and `checks.disable` already use, so a plan can cite
    the same one. Reading it as a list of groups silently finds nothing, and an
    item then reads `covered` because nothing contradicted it.
    """
    return {
        str(getattr(f, "check", ""))
        for f in (getattr(checks, "findings", []) or [])
        if getattr(f, "check", "")
    }
