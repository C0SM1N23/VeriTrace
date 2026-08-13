"""State diagram as SVG — §8.8 step 4 and "Export SVG" from the FSM mode.

§8.8 names elkjs for the layout, and the browser uses it: it is a real layered
graph layout and it belongs where there is a layout engine. This module is the
*other* consumer — `veritrace fsm --svg`, and the diagram in a bug report — and
it lays out in a ring rather than pulling a JavaScript runtime into the Python
side to draw a picture.

A ring is not a compromise for what this has to draw. A state machine has 3–8
states and every state is reachable from the ring, so the edges are short and
the labels do not collide; the layered layout earns its place when a human is
panning around one interactively, which is the browser's job.

The overlay of §8.8 step 5 is drawn when it is there: a state nothing visited
gets the dotted red outline, an edge nothing took gets a dotted thin line, and
the counts go on the labels.
"""

from __future__ import annotations

import html
import math
from typing import Any

WIDTH = 720
HEIGHT = 520
R_NODE = 34
#: Radius of the ring the states sit on.
R_RING = 170


def _pos(i: int, n: int) -> tuple[float, float]:
    """State `i` of `n`, on the ring. Reset at the top, clockwise from there."""
    angle = -math.pi / 2 + 2 * math.pi * i / max(1, n)
    return WIDTH / 2 + R_RING * math.cos(angle), HEIGHT / 2 + R_RING * math.sin(angle)


def _arc(x1: float, y1: float, x2: float, y2: float, bow: float = 0.18) -> str:
    """A quadratic curve, bowed so the two directions of a pair do not overlap."""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    return f"M{x1:.1f},{y1:.1f} Q{mx - dy * bow:.1f},{my + dx * bow:.1f} {x2:.1f},{y2:.1f}"


def _trim(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float, float]:
    """Pull the endpoints back to the circles' edges, so arrows are not buried."""
    dx, dy = x2 - x1, y2 - y1
    d = math.hypot(dx, dy) or 1.0
    ux, uy = dx / d, dy / d
    return x1 + ux * R_NODE, y1 + uy * R_NODE, x2 - ux * (R_NODE + 8), y2 - uy * (R_NODE + 8)


def render(machine: Any, title: str = "") -> str:
    """One machine as a standalone SVG document."""
    states = machine.states
    if not states:
        return _empty(machine)
    n = len(states)
    order = sorted(range(n), key=lambda i: (not states[i].is_reset, states[i].value))
    at = {states[i].value: _pos(k, n) for k, i in enumerate(order)}
    has_overlay = bool(machine.visits)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" '
        f'aria-label="state diagram for {html.escape(machine.signal)}">',
        "<style>"
        ".n{fill:#f6f8fa;stroke:#2b3138;stroke-width:1.5}"
        ".n.unvisited{fill:none;stroke:#c0392b;stroke-dasharray:4 3}"
        ".n.reset{stroke-width:3}"
        ".lbl{font:600 12px ui-monospace,monospace;fill:#2b3138;text-anchor:middle}"
        ".sub{font:10px ui-monospace,monospace;fill:#6b7785;text-anchor:middle}"
        ".e{fill:none;stroke:#2b3138;stroke-width:1.4;marker-end:url(#a)}"
        ".e.untaken{stroke:#9aa4b0;stroke-width:1;stroke-dasharray:4 3}"
        ".g{font:10px ui-monospace,monospace;fill:#6b7785;text-anchor:middle}"
        ".t{font:600 13px system-ui,sans-serif;fill:#2b3138}"
        "</style>",
        '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#2b3138"/></marker></defs>',
        f'<text class="t" x="16" y="24">{html.escape(title or machine.signal)}</text>',
    ]

    for t in machine.transitions:
        if t.src is None or t.src not in at or t.dst not in at:
            continue
        taken = machine.taken.get((t.src, t.dst), 0)
        cls = "e" + (" untaken" if has_overlay and not taken else "")
        label = t.guard if len(t.guard) <= 22 else t.guard[:21] + "…"
        if taken:
            label += f"  {taken}x"
        if t.src == t.dst:
            x, y = at[t.src]
            parts.append(
                f'<path class="{cls}" d="M{x - 14:.1f},{y - R_NODE + 4:.1f} '
                f"C{x - 46:.1f},{y - R_NODE - 42:.1f} {x + 46:.1f},{y - R_NODE - 42:.1f} "
                f'{x + 12:.1f},{y - R_NODE + 2:.1f}"/>'
            )
            parts.append(
                f'<text class="g" x="{x:.1f}" y="{y - R_NODE - 34:.1f}">'
                f"{html.escape(label)}</text>"
            )
            continue
        x1, y1 = at[t.src]
        x2, y2 = at[t.dst]
        ax, ay, bx, by = _trim(x1, y1, x2, y2)
        parts.append(f'<path class="{cls}" d="{_arc(ax, ay, bx, by)}"/>')
        mx, my = (ax + bx) / 2, (ay + by) / 2
        dx, dy = bx - ax, by - ay
        parts.append(
            f'<text class="g" x="{mx - dy * 0.11:.1f}" y="{my + dx * 0.11:.1f}">'
            f"{html.escape(label)}</text>"
        )

    for s in states:
        x, y = at[s.value]
        visited = machine.visits.get(s.value, 0)
        cls = "n" + (" reset" if s.is_reset else "")
        if has_overlay and not visited:
            cls += " unvisited"
        parts.append(f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{R_NODE}"/>')
        parts.append(
            f'<text class="lbl" x="{x:.1f}" y="{y + 2:.1f}">{html.escape(s.name)}</text>'
        )
        note = ""
        if has_overlay:
            note = f"{visited}x" if visited else "never"
            if machine.cycles_in.get(s.value):
                note += f" · {machine.cycles_in[s.value]}c"
        elif s.is_reset:
            note = "reset"
        if note:
            parts.append(
                f'<text class="sub" x="{x:.1f}" y="{y + R_NODE + 14:.1f}">'
                f"{html.escape(note)}</text>"
            )

    parts.append("</svg>")
    return "".join(parts)


def _empty(machine: Any) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} 80" '
        f'width="{WIDTH}" height="80"><text x="16" y="40" '
        'font-family="system-ui" font-size="13">'
        f"{html.escape(machine.signal)}: no states extracted</text></svg>"
    )
