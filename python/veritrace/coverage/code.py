"""§8.12 — importing code coverage from the free stack.

*Verilator `--coverage` **and** Vivado xsim, both valid sources. ModelSim's free
edition is the one with no real coverage at all.*

    verilator --binary --coverage --coverage-line --coverage-toggle rtl/*.sv tb.sv
    ./obj_dir/Vtb
    verilator_coverage --annotate cov_annotated logs/coverage.dat

    xsim ... -cov_db_name run1
    xcrg -report_format xml -dir xsim.covdb -report_dir cov_report

VeriTrace reads the database, not the annotated source: the annotation is for a
human, and re-deriving line numbers from it would lose the branch points. Both
readers produce the same `CodeCoverage`, because the whole point of TAB 7 is
that where the numbers came from changes nothing about what you do with them.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from veritrace.coverage.model import CodeCoverage, FileCoverage, LineCoverage

#: Verilator writes one record per point: `C '<fields>' <count>`, where the
#: fields are `\x01key\x02value` pairs. Documented in verilated_cov.cpp and
#: stable across the 4.x and 5.x lines.
_VERILATOR_RECORD = re.compile(r"^C\s+'(?P<fields>.*)'\s+(?P<count>-?\d+)\s*$")

#: The shape §8.12 itself describes — `point,filename,lineno,count`. Accepted as
#: well as the real thing, so a project that pre-digests its coverage into a CSV
#: (or a test that writes three lines by hand) needs no converter.
_SIMPLE_ROW = re.compile(
    r"^\s*(?P<point>[\w./-]*)\s*,\s*(?P<file>[^,]+?)\s*,\s*(?P<line>\d+)\s*,\s*(?P<count>-?\d+)\s*$"
)

#: `page` prefix -> what kind of point it is. Verilator names its pages
#: `v_line/...`, `v_branch/...`, `v_toggle/...`, `v_user/...`.
_PAGE_KIND = {"v_line": "line", "v_branch": "branch", "v_toggle": "toggle", "v_user": "user"}


class CoverageError(ValueError):
    """A coverage database that cannot be read as written."""


def _verilator_fields(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in blob.split("\x01"):
        if not part:
            continue
        key, sep, value = part.partition("\x02")
        if sep:
            out[key] = value
    return out


def read_verilator(path: Path | str) -> CodeCoverage:
    """`logs/coverage.dat`, or the simple CSV of §8.12."""
    p = Path(path)
    out = CodeCoverage(source="verilator", path=str(p))
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise CoverageError(f"cannot read {p}: {e}") from e

    points: list[LineCoverage] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        m = _VERILATOR_RECORD.match(line)
        if m:
            fields = _verilator_fields(m["fields"])
            filename, lineno = fields.get("f"), fields.get("l")
            if not filename or not lineno:
                continue
            page = (fields.get("page") or "").split("/", 1)[0]
            points.append(
                LineCoverage(
                    file=filename,
                    line=int(lineno),
                    count=int(m["count"]),
                    kind=_PAGE_KIND.get(page, "line"),
                    label=fields.get("n", ""),
                )
            )
            continue
        m = _SIMPLE_ROW.match(line)
        if m:
            points.append(
                LineCoverage(
                    file=m["file"],
                    line=int(m["line"]),
                    count=int(m["count"]),
                    kind="branch" if "branch" in m["point"] else "line",
                    label=m["point"],
                )
            )
    if not points:
        out.error = f"{p.name} carried no coverage records"
    out.files = _group(points)
    return out


#: xcrg's XML puts the location on attributes rather than in a fixed element
#: tree, and the element names have moved between Vivado releases. Reading the
#: attributes instead of the tags is what makes this survive an upgrade — and it
#: is not guesswork: a point without a file, a line and a count is not a
#: coverage point in any of the shapes.
_FILE_ATTRS = ("file", "filename", "source", "src", "path")
_LINE_ATTRS = ("line", "lineno", "line_no", "linenumber", "line_number")
_COUNT_ATTRS = ("count", "hits", "hit", "executed", "covered", "num_hits")


def _attr(el: ElementTree.Element, names: tuple[str, ...]) -> str | None:
    lowered = {k.lower(): v for k, v in el.attrib.items()}
    return next((lowered[n] for n in names if n in lowered), None)


def read_vivado(path: Path | str) -> CodeCoverage:
    """An `xcrg -report_format xml` report."""
    p = Path(path)
    out = CodeCoverage(source="vivado", path=str(p))
    try:
        root = ElementTree.parse(p).getroot()
    except (OSError, ElementTree.ParseError) as e:
        raise CoverageError(f"cannot read {p} as xcrg XML: {e}") from e

    points: list[LineCoverage] = []
    for el in root.iter():
        filename = _attr(el, _FILE_ATTRS)
        lineno = _attr(el, _LINE_ATTRS)
        count = _attr(el, _COUNT_ATTRS)
        if not filename or lineno is None or count is None:
            continue
        try:
            line, hits = int(lineno), int(float(count))
        except ValueError:
            continue
        kind = (_attr(el, ("type", "kind")) or el.tag or "line").lower()
        points.append(
            LineCoverage(
                file=filename,
                line=line,
                count=hits,
                kind=next((k for k in ("branch", "toggle", "condition") if k in kind), "line"),
                label=_attr(el, ("name", "id")) or "",
            )
        )
    if not points:
        out.error = f"{p.name} carried no coverage points with a file, a line and a count"
    out.files = _group(points)
    return out


def _group(points: list[LineCoverage]) -> list[FileCoverage]:
    """One entry per file, points ascending — and duplicates summed.

    Verilator emits a record per *instance*, so a module instantiated four times
    contributes four records for the same line. Summing them answers "was this
    line ever executed", which is the question; keeping them separate would make
    a design's coverage depend on how many times it was instantiated.
    """
    merged: dict[tuple[str, int, str, str], LineCoverage] = {}
    for p in points:
        key = (p.file, p.line, p.kind, p.label)
        got = merged.get(key)
        if got is None:
            merged[key] = LineCoverage(p.file, p.line, p.count, p.kind, p.label)
        else:
            got.count += p.count
    by_file: dict[str, FileCoverage] = {}
    for p in merged.values():
        by_file.setdefault(p.file, FileCoverage(file=p.file)).points.append(p)
    for f in by_file.values():
        f.points.sort(key=lambda p: (p.line, p.kind, p.label))
    return sorted(by_file.values(), key=lambda f: f.file)


def read(path: Path | str, source: str | None = None) -> CodeCoverage:
    """Import a coverage database, detecting which tool wrote it.

    Detection is by content and not by extension: `coverage.dat` is a name
    Verilator chose and nothing enforces it, and an XML file is unambiguous from
    its first byte.
    """
    p = Path(path)
    if source:
        picked = source.lower()
    elif p.suffix.lower() == ".xml":
        picked = "vivado"
    else:
        try:
            head = p.read_bytes()[:512].lstrip()
        except OSError as e:
            raise CoverageError(f"cannot read {p}: {e}") from e
        picked = "vivado" if head.startswith(b"<") else "verilator"
    if picked == "vivado":
        return read_vivado(p)
    if picked == "verilator":
        return read_verilator(p)
    raise CoverageError(f"unknown coverage source {source!r}; try verilator or vivado")


#: Where the two tools leave their database when nobody says otherwise. Tried in
#: order, so `veritrace serve` picks up a coverage run that already happened
#: without being told to (§8.12's point is closing the loop, and a loop with a
#: mandatory argument in it does not close).
DEFAULT_LOCATIONS = (
    "logs/coverage.dat",
    "coverage.dat",
    "obj_dir/coverage.dat",
    "cov_report/dashboard.xml",
    "cov_report/coverage.xml",
)


def discover(root: Path | str) -> Path | None:
    base = Path(root)
    for rel in DEFAULT_LOCATIONS:
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None
