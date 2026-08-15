"""On-board captures as traces — §8.11b.

*"O parte serioasa din bug-urile tale reale se intampla acolo unde nu exista
simulare — si acolo tool-ul ar fi fost inutil."* Vivado's ILA and Quartus's
SignalTap both export CSV, and the same engine runs on it.

What a capture is not is a simulation, and the difference has to reach the user
rather than be papered over:

* **It is shallow.** 1k–64k samples, not 10^8. A causal chain that walks off the
  front of the window stops at `CAPTURE_BOUNDARY` (`analysis/whytrace.py`), with
  a suggestion for what to trigger on next time.
* **It is narrow.** Only probed signals exist. Everything else is `NOT_TRACED`,
  and the correlation rate says so.
* **It is sampled, not timed.** A sample index is not a timestamp. The converted
  trace carries one tick per sample and a timescale that says as much, so
  nothing downstream can mistake a sample count for nanoseconds.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Capture", "FORMATS", "read", "to_vcd"]

#: The two exporters §8.11b names. Both write CSV; the difference is the
#: preamble and how a sample column is labelled.
FORMATS = ("vivado-ila", "signaltap")

#: Vivado writes `Sample in Buffer`, `Sample in Window`, `TRIGGER`; SignalTap
#: writes an unnamed index column. None of them is a probed signal.
_NOT_A_SIGNAL = re.compile(
    r"^\s*(sample[\s_]*(in)?[\s_]*(buffer|window)?|trigger|index|time\s*\(?\w*\)?)\s*$",
    re.I,
)

#: `slv_reg[3]`, `/i_top|u_dma|state`, `top.dma.state` — one probe, many
#: spellings. Quartus separates hierarchy with `|`, Vivado with `/` or `.`.
_SEP = re.compile(r"[|/]")


@dataclass(slots=True)
class Capture:
    """One exported capture: named probes and their samples."""

    fmt: str
    names: list[str] = field(default_factory=list)
    #: One row per sample, values as written by the exporter.
    rows: list[list[str]] = field(default_factory=list)
    #: Width from the header, when the exporter wrote one (`probe[7:0]`). Zero
    #: means "work it out from the samples".
    declared: list[int] = field(default_factory=list)
    #: Sample index the trigger fired on, when the file says. §8.11b: a capture
    #: is a window *around* an event, and where that event sits matters.
    trigger: int | None = None

    @property
    def n_samples(self) -> int:
        return len(self.rows)


def read(path: Path | str, fmt: str = "vivado-ila", scope: str = "") -> Capture:
    """Parse an ILA/SignalTap CSV export.

    `scope` prefixes every probe, so a capture of `u_dma|state` can be lined up
    with an RTL hierarchy that calls it `top.u_dma.state` without editing the
    file.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown capture format {fmt!r}; try one of {', '.join(FORMATS)}")

    text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    # Both tools put a preamble above the table. The header is the first line
    # that parses as CSV *and* names more than one column — anything earlier is
    # metadata, and guessing a fixed line number breaks on the next tool version.
    start = 0
    header: list[str] = []
    for i, line in enumerate(text):
        cells = next(csv.reader([line]), [])
        if len(cells) > 1 and any(c.strip() for c in cells):
            header, start = [c.strip() for c in cells], i + 1
            break
    if not header:
        raise ValueError(f"{path}: no table header found")

    keep = [i for i, name in enumerate(header) if name and not _NOT_A_SIGNAL.match(name)]
    if not keep:
        raise ValueError(f"{path}: the header names no probes, only sample columns")

    cap = Capture(
        fmt=fmt,
        names=[_probe(header[i], scope) for i in keep],
        declared=[_declared_width(header[i]) for i in keep],
    )
    for line in text[start:]:
        cells = next(csv.reader([line]), [])
        if len(cells) <= max(keep):
            continue
        row = [cells[i].strip() for i in keep]
        if any(row):
            cap.rows.append(row)
    cap.trigger = _trigger(text[: start - 1])
    return cap


def _probe(name: str, scope: str) -> str:
    """A probe name as a hierarchical path."""
    path = _SEP.sub(".", name.strip()).strip(".")
    # Vivado brackets a bus as `probe[7:0]`; the width is in the data, not the
    # name, and keeping it here would make the path unmatchable.
    path = re.sub(r"\[\d+:\d+\]$", "", path)
    return f"{scope}.{path}" if scope else path


def _trigger(preamble: list[str]) -> int | None:
    """Sample index the trigger fired on, if the preamble records one.

    Only the metadata above the table is searched. The header names a `TRIGGER`
    *column*, and reading a bus width out of it — `probe[4:0]` — as a sample
    number is exactly the kind of confident nonsense this tool must not produce.
    """
    for line in preamble:
        m = re.search(r"trigger\s*(?:position|sample|at)?\s*[:=]\s*(-?\d+)", line, re.I)
        if m:
            return int(m.group(1))
    return None


def to_vcd(cap: Capture, timescale: str = "1ns") -> str:
    """The capture as a VCD, one tick per sample.

    Going through VCD rather than writing a `.vtx` directly is deliberate: the
    converter, the delta-cycle rules and the store's own tests all sit behind
    that one door, and a second writer would be a second place for §5.5 to be
    got wrong.
    """
    widths = [_width(cap, i) for i in range(len(cap.names))]
    codes = [_code(i) for i in range(len(cap.names))]

    # No wrapper scope: a probe named `top.dma.state` has to arrive as exactly
    # that, or nothing in the correlation layer will match it.
    out = [f"$timescale {timescale} $end"]
    scopes: list[str] = []
    for name, code, w in zip(cap.names, codes, widths):
        parts = name.split(".")
        leaf, hier = parts[-1], parts[:-1]
        while scopes and scopes != hier[: len(scopes)]:
            out.append("$upscope $end")
            scopes.pop()
        for s in hier[len(scopes) :]:
            out.append(f"$scope module {s} $end")
            scopes.append(s)
        decl = f"$var wire {w} {code} {leaf}" + (f" [{w - 1}:0]" if w > 1 else "")
        out.append(decl + " $end")
    out += ["$upscope $end"] * len(scopes)
    out.append("$enddefinitions $end")

    previous: list[str | None] = [None] * len(cap.names)
    for t, row in enumerate(cap.rows):
        changes = []
        for i, raw in enumerate(row):
            bits = _bits(raw, widths[i])
            if bits == previous[i]:
                continue
            previous[i] = bits
            changes.append(f"{bits}{codes[i]}" if widths[i] == 1 else f"b{bits} {codes[i]}")
        if changes or t == 0:
            out.append(f"#{t}")
            out.extend(changes)
    return "\n".join(out) + "\n"


def _declared_width(header: str) -> int:
    """Width from a header cell such as `probe[7:0]`, or 0 when it has none."""
    m = re.search(r"\[(\d+):(\d+)\]\s*$", header)
    return abs(int(m.group(1)) - int(m.group(2))) + 1 if m else 0


def _width(cap: Capture, col: int) -> int:
    """Width of a probe: what the header declared, else the widest sample."""
    if col < len(cap.declared) and cap.declared[col]:
        return cap.declared[col]
    widest = 1
    for row in cap.rows:
        widest = max(widest, len(_digits(row[col])))
    return widest


def _digits(raw: str) -> str:
    """The sample as 4-state binary digits.

    Vivado exports hex or binary depending on the radix the user chose, and
    SignalTap exports binary. An unparseable cell becomes X rather than 0 —
    inventing a value here is exactly what P1 forbids.
    """
    v = raw.strip().lower().replace("_", "")
    if not v:
        return "x"
    if v.startswith("0x"):
        v = v[2:]
    if set(v) <= set("01xz"):
        return v
    try:
        return format(int(v, 16), "b")
    except ValueError:
        pass
    try:
        return format(int(v, 10), "b")
    except ValueError:
        return "x"


def _bits(raw: str, width: int) -> str:
    d = _digits(raw)
    if len(d) >= width:
        return d[-width:]
    return d[0].rjust(width, "0" if d[0] not in "xz" else d[0]) if len(d) == 1 else d.rjust(width, "0")


def _code(i: int) -> str:
    """A VCD identifier code: printable ASCII, 33..126."""
    out = ""
    n = i
    while True:
        out = chr(33 + (n % 94)) + out
        n = n // 94 - 1
        if n < 0:
            return out
