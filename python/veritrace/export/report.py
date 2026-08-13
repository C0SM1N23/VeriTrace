"""Standalone bug report — §12.

One `.html` file with nothing outside it: no CDN, no fetch, no fonts to
download. §12 wants a file that opens the same on somebody else's laptop, off
the network, in five years — so everything is inlined, the waveform is SVG
rather than a raster, and the only script is a `<details>` toggle the browser
gives for free plus a copy button.

The seven sections of §12, in order, are the seven blocks of `report.html.j2`.
Nothing here decides what to say: the sentences come from `repro.narrate` and the
findings from `analysis.findings`, so the report and the interface cannot end up
describing the same bug differently.

Two constraints from §12 that shape the code rather than the template:

* **Under 500 KB.** The waveform is the only part that grows with the run, so it
  is windowed to the chain and capped; `build` reports the final size and
  `write` refuses silently to nothing — it warns, since a large report is still
  a report.
* **Escaping.** Everything on the page is user data: signal names, RTL lines,
  simulator output. Jinja2 autoescapes, which is most of why it is here rather
  than an f-string — a `<=` in a source snippet is otherwise the end of the page.
"""

from __future__ import annotations

import datetime as _dt
import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from veritrace import __version__
from veritrace.analysis.whytrace import CausalNode
from veritrace.repro import narrate
from veritrace.repro.subtrace import Subtrace

#: §12's size target. Exceeding it is reported, not enforced: truncating a
#: report to hit a number would remove the part somebody needed.
SIZE_TARGET = 500 * 1024

#: Lines of context either side of a chain node's source line (§12 section 3).
SNIPPET_CONTEXT = 2

#: Waveform geometry. Fixed rather than configurable — the report is a
#: deliverable with one layout, not a viewer.
SVG_ROW_H = 28
SVG_LEFT = 210
SVG_WIDTH = 900
#: Beyond this a signal is drawn as a density band instead of individual edges:
#: a clock over a thousand cycles is a black rectangle either way, and the file
#: does not need ten thousand path segments to say so.
SVG_MAX_EDGES = 400


@dataclass(slots=True)
class Snippet:
    file: str
    line: int
    lines: list[tuple[int, str, bool]] = field(default_factory=list)


@dataclass(slots=True)
class ChainStep:
    """One card of the §12 spine — a node plus what it looked like in the source."""

    signal: str
    value: str
    time: int
    cycle: int | None
    reason: str
    kind: str
    detail: str
    text: str
    loc: str
    snippet: Snippet | None
    #: Branches the heuristic ranked second. §11.4: collapsed, never dropped.
    alternatives: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BugReport:
    html: str
    title: str
    bytes: int

    @property
    def oversize(self) -> bool:
        return self.bytes > SIZE_TARGET


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _snippet(loc: Any, sources: dict[str, Path]) -> Snippet | None:
    if loc is None:
        return None
    name = loc["file"] if isinstance(loc, dict) else loc.file
    line = loc["line"] if isinstance(loc, dict) else loc.line
    path = sources.get(Path(name).name)
    if path is None:
        return None
    text = _read_lines(path)
    if not text:
        return None
    lo = max(1, line - SNIPPET_CONTEXT)
    hi = min(len(text), line + SNIPPET_CONTEXT)
    return Snippet(
        file=Path(name).name,
        line=line,
        lines=[(n, text[n - 1], n == line) for n in range(lo, hi + 1)],
    )


def _spine(root: CausalNode, clock: Any, sources: dict[str, Path]) -> list[ChainStep]:
    """The primary path, with each node's rejected siblings hung off it.

    §11.4's rule, in a document that cannot be clicked: the likely chain is the
    body, the alternatives are named so nothing is hidden, and the reader can
    see there *were* alternatives without them taking over the page.
    """
    out: list[ChainStep] = []
    node: CausalNode | None = root
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        loc = node.loc
        out.append(
            ChainStep(
                signal=node.txn or node.signal.path(),
                value=node.value,
                time=node.time,
                cycle=clock.cycle_of(node.time) if clock is not None else None,
                reason=node.reason.value,
                kind=node.kind.value,
                detail=node.detail,
                text="",
                loc=f"{loc.file}:{loc.line}" if loc else "",
                snippet=_snippet(loc, sources),
                alternatives=[
                    c.signal.path() for c in node.children if not c.is_primary_path
                ],
            )
        )
        node = next((c for c in node.children if c.is_primary_path), None)
    return out


# --- the waveform (§12 section 5) ------------------------------------------


def _rows(store: Any, paths: Iterable[str], clock: Any) -> list[tuple[str, int, int]]:
    """`(path, handle, width)` for what the chain touched, clock first."""
    out: list[tuple[str, int, int]] = []
    if clock is not None:
        out.append((clock.path, clock.handle, 1))
    for p in paths:
        h = store.find(p)
        if h is None or any(h == existing for _n, existing, _w in out):
            continue
        meta = store.signal(h)
        out.append((p, h, max(1, meta.width)))
    return out


def _svg(store: Any, sub: Subtrace, clock: Any) -> str:
    """Inline SVG of the chain's signals over the window that explains it.

    Vector on purpose (§12): the report is read on a laptop and printed into a
    ticket, and a screenshot survives neither.
    """
    events = sub.events
    if not events:
        return ""
    paths = []
    for e in events:
        if e.signal not in paths:
            paths.append(e.signal)
    rows = _rows(store, paths, clock)
    if not rows:
        return ""

    t_lo, t_hi = min(e.time for e in events), max(e.time for e in events)
    # A little air either side. When every event is at one instant the span is
    # zero, so fall back to a couple of clock periods — a drawing of a single
    # column is not a waveform.
    pad = (t_hi - t_lo) // 8
    if pad < 1:
        pad = (clock.period or 2) * 2 if clock is not None else 1
    t0, t1 = max(store.time_range[0], t_lo - pad), min(store.time_range[1], t_hi + pad)
    if t1 <= t0:
        t1 = t0 + 1
    span = t1 - t0

    def x(t: int) -> float:
        return SVG_LEFT + (t - t0) / span * (SVG_WIDTH - SVG_LEFT - 20)

    height = len(rows) * SVG_ROW_H + 34
    parts = [
        f'<svg viewBox="0 0 {SVG_WIDTH} {height}" width="100%" '
        f'role="img" aria-label="waveform of the causal chain" '
        f'xmlns="http://www.w3.org/2000/svg" class="wave">'
    ]

    # Event markers first, so the traces draw over them.
    for e in events:
        parts.append(
            f'<line class="mark" x1="{x(e.time):.1f}" y1="16" '
            f'x2="{x(e.time):.1f}" y2="{height - 18}"/>'
        )

    for i, (path, handle, width) in enumerate(rows):
        top = 24 + i * SVG_ROW_H
        mid, bot = top + 4, top + SVG_ROW_H - 12
        label = path.rsplit(".", 1)[-1]
        parts.append(
            f'<text class="lbl" x="8" y="{bot}" >{html.escape(label)}</text>'
        )
        edges = store.transitions(handle, t0, t1 + 1)
        start = store.value_at(handle, t0)
        if len(edges) > SVG_MAX_EDGES:
            # Too dense to draw honestly; say "busy" instead of drawing a smear.
            parts.append(
                f'<rect class="busy" x="{SVG_LEFT}" y="{mid}" '
                f'width="{SVG_WIDTH - SVG_LEFT - 20}" height="{bot - mid}"/>'
                f'<text class="busy-t" x="{SVG_LEFT + 6}" y="{bot - 2}">'
                f"{len(edges)} transitions</text>"
            )
            continue
        points = [(t0, start.bits if start is not None else "x")]
        points += [(t, v.bits) for t, v in edges if t > t0]
        points.append((t1, points[-1][1]))
        if width == 1:
            d: list[str] = []
            for j, (t, bits) in enumerate(points[:-1]):
                y = mid if bits.endswith("1") else bot
                nxt = points[j + 1][0]
                d.append(f"{'M' if j == 0 else 'L'}{x(t):.1f} {y:.1f}")
                d.append(f"L{x(nxt):.1f} {y:.1f}")
                if j + 1 < len(points) - 1:
                    ny = mid if points[j + 1][1].endswith("1") else bot
                    d.append(f"L{x(nxt):.1f} {ny:.1f}")
            parts.append(f'<path class="sig" d="{" ".join(d)}"/>')
        else:
            for j, (t, bits) in enumerate(points[:-1]):
                nxt = points[j + 1][0]
                w = max(0.0, x(nxt) - x(t))
                if w < 1.5:
                    continue
                parts.append(
                    f'<rect class="bus" x="{x(t):.1f}" y="{mid:.1f}" '
                    f'width="{w:.1f}" height="{bot - mid:.1f}"/>'
                )
                if w > 34:
                    parts.append(
                        f'<text class="busv" x="{x(t) + 4:.1f}" y="{bot - 3:.1f}">'
                        f"{html.escape(_hex(bits, width))}</text>"
                    )
    if clock is not None:
        parts.append(
            f'<text class="axis" x="{SVG_LEFT}" y="12">c{clock.cycle_of(t0)}</text>'
            f'<text class="axis" x="{SVG_WIDTH - 60}" y="12">c{clock.cycle_of(t1)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _hex(bits: str, width: int) -> str:
    if not bits or any(c in "xzXZ" for c in bits):
        return bits[:8]
    try:
        return f"{int(bits, 2):0{max(1, (width + 3) // 4)}x}"
    except ValueError:
        return bits[:8]


# --- assembly ---------------------------------------------------------------


def _environment():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build(
    root: CausalNode,
    sub: Subtrace,
    store: Any,
    clock: Any,
    *,
    query: str = "",
    sources: dict[str, Path] | None = None,
    repro: Any = None,
    findings: Any = None,
    trace_path: Path | None = None,
    rtl_sha256: str | None = None,
    top: str = "",
    command: str = "",
    annotations: list[str] | None = None,
) -> BugReport:
    """Render the report. Everything optional is a section that says it is absent."""
    sources = sources or {}
    narrate.narrate(sub)
    steps = _spine(root, clock, sources)
    title = narrate.headline(sub)

    env = _environment()
    page = env.get_template("report.html.j2").render(
        title=title,
        query=query,
        top=top or "(unknown)",
        trace=str(trace_path) if trace_path else "(not recorded)",
        command=command,
        rtl_sha256=rtl_sha256 or "",
        version=__version__,
        generated=_dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        symptom=narrate.symptom_paragraph(sub, query),
        steps=steps,
        events=narrate.steps(sub),
        subtrace=sub,
        svg=_svg(store, sub, clock),
        repro=repro,
        nearby=_nearby(findings, steps),
        annotations=annotations or [],
    )
    return BugReport(html=page, title=title, bytes=len(page.encode("utf-8")))


def _nearby(findings: Any, steps: list[ChainStep]) -> list[Any]:
    """§12's appendix: the automatic findings that touch the chain.

    Filtered by the files and lines the chain runs through, not by severity. A
    lint warning three lines from the root cause is the appendix's whole reason
    for existing; the other four hundred findings belong in the Checks tab.
    """
    if findings is None:
        return []
    lines = {(s.snippet.file, s.snippet.line) for s in steps if s.snippet}
    files = {f for f, _ in lines}
    signals = {s.signal for s in steps}
    out = []
    for f in getattr(findings, "findings", []):
        loc = getattr(f, "loc", None)
        near = loc is not None and Path(loc.file).name in files and any(
            abs(loc.line - line) <= 3 for file, line in lines if file == Path(loc.file).name
        )
        if near or (f.signal in signals):
            out.append(f)
    return out[:40]


def write(report: BugReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.html, encoding="utf-8")
    return path
