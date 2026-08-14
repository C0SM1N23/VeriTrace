"""WaveDrom export — §8.33.

For a thesis chapter or a README, a vector diagram is the difference between
"looks made" and "looks like a screenshot of GTKWave". The JSON is WaveDrom's
own; the SVG is rendered here rather than by shelling out to node, for the same
reason `export/fsmsvg.py` draws its own: a documentation export that needs a
JavaScript toolchain installed is one nobody runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: WaveDrom's alphabet for the states this exports. `.` continues the previous
#: cell, which is what makes a diagram readable rather than a wall of repeats.
SAME = "."


@dataclass(slots=True)
class Row:
    name: str
    wave: str = ""
    data: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "wave": self.wave}
        if self.data:
            out["data"] = self.data
        return out


@dataclass(slots=True)
class Diagram:
    rows: list[Row] = field(default_factory=list)
    #: Cycle numbers under the diagram, when the range is anchored to a clock.
    head: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"signal": [r.to_dict() for r in self.rows]}
        if self.head:
            out["head"] = {"text": self.head}
        out["config"] = {"hscale": 1}
        return out

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _cell(bits: str, width: int) -> tuple[str, str | None]:
    """One sample as a WaveDrom character, plus its label for a bus."""
    low = bits.lower()
    if "x" in low:
        return "x", None
    if "z" in low:
        return "z", None
    if width <= 1:
        return ("1" if low.endswith("1") else "0"), None
    return "=", "0x%x" % int(low, 2)


def build(
    store: Any,
    paths: list[str],
    t0: int,
    t1: int,
    clock: Any = None,
    max_cells: int = 256,
) -> Diagram:
    """Sample `paths` over `[t0, t1]`, one cell per clock edge.

    Sampling on the clock rather than at fixed time steps is what makes the
    diagram a *protocol* picture instead of a picture of a waveform viewer: a
    handshake is about cycles, and a cell per cycle is what a reader counts.
    Without a clock the range is divided evenly, which is the honest fallback.
    """
    edges = [e for e in (getattr(clock, "edges", None) or []) if t0 <= e <= t1]
    if len(edges) < 2:
        n = min(max_cells, 32)
        step = max(1, (t1 - t0) // n)
        edges = list(range(t0, t1 + 1, step))
    if len(edges) > max_cells:
        edges = edges[:max_cells]

    diagram = Diagram()
    if clock is not None and getattr(clock, "signal", None):
        # WaveDrom draws `p` as a clock; one character is one cycle.
        diagram.rows.append(Row(name=clock.signal.rsplit(".", 1)[-1], wave="p" * len(edges)))

    for path in paths:
        handle = store.find(path)
        if handle is None:
            continue
        sig = store.signal(handle)
        row = Row(name=path.rsplit(".", 1)[-1])
        previous: str | None = None
        for at in edges:
            value = store.value_at(handle, at)
            char, label = _cell(value.bits if value else "x", sig.width)
            if char == previous and (label is None or (row.data and row.data[-1] == label)):
                row.wave += SAME
                continue
            row.wave += char
            previous = char
            if label is not None:
                row.data.append(label)
        diagram.rows.append(row)

    if edges and clock is not None and hasattr(clock, "cycle_of"):
        diagram.head = f"c{clock.cycle_of(edges[0])}–c{clock.cycle_of(edges[-1])}"
    return diagram


# --- SVG --------------------------------------------------------------------

CELL_W = 28
ROW_H = 32
LABEL_W = 120
PAD = 8
#: Straight from §11.2's palette, so a diagram in a README and the application
#: on screen are recognisably the same tool.
INK = {
    "bg": "#0e1116", "line": "#262f3a", "text": "#a8b4c2", "bright": "#e4eaf1",
    "one": "#7fd1a8", "zero": "#4a5a6b", "x": "#e5484d", "z": "#8b7ab8",
}


def to_svg(diagram: Diagram, light: bool = False) -> str:
    """The same diagram as a standalone SVG.

    `light` swaps to ink-on-white, which is what a printed thesis needs; the
    default matches the application.
    """
    ink = dict(INK)
    if light:
        ink |= {"bg": "#ffffff", "line": "#c8ced6", "text": "#333a42", "bright": "#111417"}

    cells = max((len(r.wave) for r in diagram.rows), default=0)
    width = LABEL_W + cells * CELL_W + PAD * 2
    height = PAD * 2 + len(diagram.rows) * ROW_H + (18 if diagram.head else 0)
    top = PAD + (18 if diagram.head else 0)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="JetBrains Mono, monospace">',
        f'<rect width="{width}" height="{height}" fill="{ink["bg"]}"/>',
    ]
    if diagram.head:
        out.append(
            f'<text x="{PAD}" y="{PAD + 12}" fill="{ink["text"]}" font-size="11">'
            f"{_esc(diagram.head)}</text>"
        )

    for i, row in enumerate(diagram.rows):
        y = top + i * ROW_H
        out.append(
            f'<text x="{PAD}" y="{y + ROW_H // 2 + 4}" fill="{ink["text"]}" '
            f'font-size="12">{_esc(row.name)}</text>'
        )
        out += _wave(row, LABEL_W, y, ink)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _wave(row: Row, x0: int, y: int, ink: dict[str, str]) -> list[str]:
    """One row, drawn as a logic-analyser trace."""
    out: list[str] = []
    hi, lo = y + 6, y + ROW_H - 10
    state, label_at = "x", 0
    labels = list(row.data)

    for i, char in enumerate(row.wave):
        x = x0 + i * CELL_W
        if char != SAME:
            state = char
            if char == "=":
                label_at = i
                text = labels.pop(0) if labels else ""
                out.append(
                    f'<text x="{x + 3}" y="{(hi + lo) // 2 + 4}" fill="{ink["bright"]}" '
                    f'font-size="10">{_esc(text)}</text>'
                )
        if state == "p":
            mid = x + CELL_W // 2
            out.append(
                f'<path d="M{x},{lo} L{x},{hi} L{mid},{hi} L{mid},{lo} L{x + CELL_W},{lo}" '
                f'fill="none" stroke="{ink["one"]}" stroke-width="1.4"/>'
            )
        elif state in "01":
            level = hi if state == "1" else lo
            colour = ink["one"] if state == "1" else ink["zero"]
            out.append(
                f'<path d="M{x},{level} L{x + CELL_W},{level}" stroke="{colour}" '
                f'stroke-width="1.6" fill="none"/>'
            )
            if char in "01" and i:
                out.append(
                    f'<path d="M{x},{hi} L{x},{lo}" stroke="{colour}" stroke-width="1.2"/>'
                )
        elif state == "=":
            colour = ink["line"]
            out.append(
                f'<path d="M{x},{hi} L{x + CELL_W},{hi} M{x},{lo} L{x + CELL_W},{lo}" '
                f'stroke="{ink["text"]}" stroke-width="1.2" fill="none"/>'
            )
            if i == label_at:
                out.append(f'<path d="M{x},{hi} L{x},{lo}" stroke="{ink["text"]}" stroke-width="1.2"/>')
        else:
            colour = ink["x"] if state == "x" else ink["z"]
            out.append(
                f'<rect x="{x}" y="{hi}" width="{CELL_W}" height="{lo - hi}" '
                f'fill="{colour}" opacity="0.35"/>'
            )
    return out


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
