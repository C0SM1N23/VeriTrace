"""The one combined report — §8.36.

**Pure aggregation, zero new algorithm.** Every number here was computed
somewhere else; the value is that they are in one place, and that each carries
the qualifier that makes it true.

§8.36's rule, which is the reason the whole file exists: *"niciun rand nu spune
«verificat» fara calificativ"*. So a row is a `Row`, and a `Row` has a `detail`
that is not optional decoration — "6/8 proved" without "bounded, depth=20" is a
claim the tool cannot support. The report never emits a binary "design verified"
verdict, because that is not a statement any tool can make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The seven §8.36 names, in the order the report prints them.
CATEGORIES = (
    "line coverage",
    "functional coverage",
    "mutation score",
    "formal",
    "protocol violations",
    "synth-diff",
    "deadlock/stuck",
)

OK, WARN, BAD, ABSENT = "ok", "warn", "bad", "absent"


@dataclass(slots=True)
class Row:
    category: str
    #: The headline value, already formatted — "94%", "6/8", "0".
    value: str = "—"
    #: The qualifier. Never empty for a row that claims anything.
    detail: str = ""
    status: str = ABSENT
    #: What produced it, so a reader can go and re-run that one thing.
    source: str = ""
    target: str = ""

    @property
    def present(self) -> bool:
        return self.status != ABSENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "value": self.value, "detail": self.detail,
            "status": self.status, "source": self.source, "target": self.target,
        }


@dataclass(slots=True)
class Scorecard:
    design: str = ""
    commit: str = ""
    rows: list[Row] = field(default_factory=list)

    @property
    def covered(self) -> list[Row]:
        return [r for r in self.rows if r.present]

    @property
    def failing(self) -> list[Row]:
        return [r for r in self.rows if r.status == BAD]

    def to_dict(self) -> dict[str, Any]:
        return {
            "design": self.design,
            "commit": self.commit,
            "n_categories": len(self.covered),
            "rows": [r.to_dict() for r in self.rows],
        }


class Loaded:
    """A parsed JSON report, readable the way a live object is.

    §8.36 is aggregation over what other commands already computed, and those
    commands already emit JSON. Rather than a second code path per input, their
    output is wrapped so `_mutation`, `_formal` and `_synth` cannot tell whether
    they were handed a live object or yesterday's artefact — which is what lets a
    scorecard be assembled in CI from four separate jobs.
    """

    def __init__(self, data: Any) -> None:
        self._d = data if isinstance(data, dict) else {}

    def __getattr__(self, name: str) -> Any:
        value = self._d.get(name)
        if isinstance(value, dict):
            return Loaded(value)
        if isinstance(value, list):
            return [Loaded(v) if isinstance(v, dict) else v for v in value]
        return value

    def __bool__(self) -> bool:
        return bool(self._d)

    def __len__(self) -> int:
        return len(self._d)


def _pct(covered: int, total: int) -> float | None:
    return None if not total else 100.0 * covered / total


def _band(value: float | None, target: float | None) -> str:
    if value is None:
        return ABSENT
    if target is None:
        return WARN
    return OK if value >= target else BAD


def build(
    checks: Any = None,
    coverage: Any = None,
    mutation: Any = None,
    formal: Any = None,
    synth: Any = None,
    protocol: Any = None,
    design: str = "",
    commit: str = "",
    targets: dict[str, float] | None = None,
) -> Scorecard:
    """Whatever was computed, in one table. Absent inputs make an absent row.

    Nothing is inferred from nothing: a category with no input reads `—` and
    `not measured in this run`, never 0% — which would be a claim, and a
    pessimistic one at that (P1).
    """
    t = targets or {}
    card = Scorecard(design=design, commit=commit)

    card.rows.append(_line(coverage, t.get("line")))
    card.rows.append(_functional(coverage, t.get("functional")))
    card.rows.append(_mutation(mutation, t.get("mutation")))
    card.rows.append(_formal(formal))
    card.rows.append(_protocol(protocol))
    card.rows.append(_synth(synth))
    card.rows.append(_liveness(checks))
    return card


def _line(coverage: Any, target: float | None) -> Row:
    row = Row("line coverage", target=f">={target:g}%" if target else "")
    code = getattr(coverage, "code", None) if coverage else None
    if code is None or not getattr(code, "total", 0):
        row.detail = "no coverage database was imported"
        return row
    pct = _pct(code.covered, code.total)
    row.value = f"{pct:.0f}%"
    row.detail = f"{code.covered}/{code.total} points, from {code.source or 'a coverage database'}"
    row.status = _band(pct, target)
    row.source = "veritrace coverage"
    return row


def _functional(coverage: Any, target: float | None) -> Row:
    row = Row("functional coverage", target=f">={target:g}%" if target else "")
    points = [
        p for f in (getattr(coverage, "functional", []) or []) for p in f.points if p.cells
    ]
    if not points:
        row.detail = "no protocol interface produced cover points"
        return row
    covered = sum(p.covered for p in points)
    total = sum(p.total for p in points)
    holes = total - covered
    pct = _pct(covered, total)
    row.value = f"{pct:.0f}%"
    # §8.36's own example splits the holes, because "12 holes" and "12 holes, 3
    # of them proved unreachable" are different amounts of remaining work.
    row.detail = f"{covered}/{total} bins" + (f", {holes} hole(s) open" if holes else "")
    row.status = _band(pct, target)
    row.source = "veritrace coverage"
    return row


def _mutation(mutation: Any, target: float | None) -> Row:
    row = Row("mutation score", target=f">={target:g}%" if target else "")
    if mutation is None or not getattr(mutation, "scored", 0):
        row.detail = "not run"
        return row
    pct = (mutation.score or 0) * 100
    row.value = f"{pct:.0f}%"
    row.detail = f"{mutation.killed}/{mutation.scored} mutants killed, seed {mutation.seed}"
    if mutation.invalid:
        row.detail += f"; {len(mutation.invalid)} did not build and are not scored"
    row.status = _band(pct, target)
    row.source = "veritrace mutate"
    return row


def _formal(formal: Any) -> Row:
    """Never "proved" — always "held to depth N" (§8.27, §8.36)."""
    row = Row("formal")
    reports = list(formal or [])
    if not reports:
        row.detail = "not run"
        return row
    def verdict(p: Any) -> str:
        v = getattr(p, "verdict", "")
        return getattr(v, "value", v)

    props = [p for r in reports for p in (getattr(r, "properties", []) or [])]
    held = [p for p in props if verdict(p) == "held"]
    failed = sum(1 for p in props if verdict(p) == "failed")
    unknown = sum(1 for p in props if verdict(p) == "unknown")
    total = len(props)
    depths = sorted({p.depth for p in held if p.depth})
    depth = depths[0] if len(depths) == 1 else (min(depths) if depths else 0)

    row.value = f"{len(held)}/{total}"
    row.detail = f"held to bounded depth {depth}, never proved unconditionally"
    if failed:
        row.detail += f"; {failed} broken with a counterexample"
    if unknown:
        row.detail += f"; {unknown} not checkable as written"
    row.status = BAD if failed else (WARN if unknown else OK)
    row.source = "veritrace formal"
    return row


def _protocol(protocol: Any) -> Row:
    row = Row("protocol violations", target="0")
    extractions = list(getattr(protocol, "extractions", []) or [])
    if not extractions:
        row.detail = "no protocol interface was extracted"
        return row
    n = sum(len(e.violations) for e in extractions)
    row.value = str(n)
    row.detail = f"over {sum(len(e.transactions) for e in extractions)} transaction(s) in this run"
    row.status = OK if n == 0 else BAD
    row.source = "veritrace txn"
    return row


def _synth(synth: Any) -> Row:
    row = Row("synth-diff")
    if synth is None:
        row.detail = "not run"
        return row
    if getattr(synth, "skipped", ""):
        row.detail = synth.skipped
        return row
    divergences = getattr(synth, "divergences", None)
    if divergences is None:
        divergences = getattr(getattr(synth, "report", None), "divergences", []) or []
    row.value = "identical" if synth.matched else f"{len(divergences)} port(s) differ"
    row.detail = (
        f"{len(synth.ports)} top-level port(s) compared, RTL vs {synth.synthesiser}"
    )
    row.status = OK if synth.matched else BAD
    row.source = "veritrace synth-diff"
    return row


#: The §8.36 row covers both, and they come from two different detectors.
LIVENESS = ("stuck", "deadlock", "liveness")


def _liveness(checks: Any) -> Row:
    """§8.36's last row: deadlock and stuck, from the checks already run.

    `Report` is a flat list of findings each carrying its own group — not a list
    of groups — so this counts rather than looks up. Getting that wrong reads as
    "checks were not run", which is a claim about the *tool* rather than about
    the design, and the two are easy to confuse in a report like this one.
    """
    row = Row("deadlock/stuck", target="0")
    findings = list(getattr(checks, "findings", []) or [])
    if checks is None or not hasattr(checks, "findings"):
        row.detail = "checks were not run"
        return row
    n = sum(
        1 for f in findings
        if any(k in str(getattr(getattr(f, "group", ""), "value", getattr(f, "group", ""))).lower()
               for k in LIVENESS)
    )
    row.value = str(n)
    row.detail = f"open stuck and deadlock findings, out of {len(findings)} finding(s) in this run"
    row.status = OK if n == 0 else BAD
    row.source = "veritrace check"
    return row
