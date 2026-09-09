"""Save files for the waveform viewers you already use — §13.3.

The honest premise of §13.3: *nobody is going to abandon their waveform viewer,
so do not compete with it — feed it.* Any analysis that produces a set of
signals — a cone, a causal chain, the stuck list — can be written out as a save
file for GTKWave, ModelSim, Vivado's xsim or Surfer. Someone who never opens
the VeriTrace UI still gets most of the value from one command, and that is the
lowest adoption barrier there is.

Everything here takes the same input — a `Selection` — and differs only in
syntax, so adding a viewer is one function.

Path syntax is the one difference that matters. VeriTrace, GTKWave and Surfer
use the dotted hierarchy from the dump; ModelSim and xsim want slash-separated
absolute paths. `to_slash` is the only conversion, and it is applied per format
rather than to the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from xml.etree import ElementTree as ET

#: Radix names as each viewer spells them. `None` means "leave it to the tool".
_GTKW_RADIX = {"hex": "hex", "dec": "dec", "bin": "bin", "ascii": "ascii"}
_DO_RADIX = {"hex": "hexadecimal", "dec": "unsigned", "bin": "binary", "ascii": "ascii"}
_WCFG_RADIX = {"hex": "HEXRADIX", "dec": "UNSIGNEDDECRADIX", "bin": "BINARYRADIX",
               "ascii": "ASCIIRADIX"}


@dataclass(slots=True)
class Marker:
    """A named cursor. The root cause gets one, so the file opens on it."""

    time: int
    name: str = ""


@dataclass(slots=True)
class Selection:
    """What to show, and where to look — the input every exporter takes."""

    #: Hierarchical paths, in the order they should appear.
    signals: list[str] = field(default_factory=list)
    #: Group heading, e.g. "Causal chain". Viewers that support groups use it.
    group: str = "VeriTrace"
    #: path -> radix name ("hex", "dec", "bin", "ascii").
    radix: dict[str, str] = field(default_factory=dict)
    markers: list[Marker] = field(default_factory=list)
    #: Window to open on, in trace time units.
    window: tuple[int, int] | None = None
    #: Timescale of the trace, e.g. "1ps" — needed to write times with units.
    timescale: str = "1ns"
    #: One line of provenance written into every file.
    origin: str = ""

    def unique(self) -> list[str]:
        """Signals in order, without repeats."""
        seen: set[str] = set()
        return [s for s in self.signals if not (s in seen or seen.add(s))]


def to_slash(path: str) -> str:
    """`tb.dut.ctrl.ready` -> `/tb/dut/ctrl/ready`, for ModelSim and xsim.

    Array indices stay attached to their element (`g_lane[2].u_fifo.q` becomes
    `/g_lane[2]/u_fifo/q`), which is what both tools expect.
    """
    return "/" + path.replace(".", "/")


def _time_with_unit(t: int, timescale: str) -> str:
    """A raw timestamp plus the trace's unit, which is how viewers read times."""
    unit = timescale.lstrip("0123456789 ") or "ns"
    return f"{t}{unit}"


def _header(comment: str, sel: Selection) -> list[str]:
    line = sel.origin or "VeriTrace selection"
    return [f"{comment} {line}", f"{comment} {len(sel.unique())} signal(s)"]


# --- GTKWave ---------------------------------------------------------------


def gtkw(sel: Selection) -> str:
    """A GTKWave save file (`.gtkw`).

    Plain text: comments, a few `[...]` directives, then one line per signal
    with an optional `@<flags>` prefix carrying the radix. Group headings are
    `-name` markers, which is how GTKWave renders a labelled block.
    """
    #: GTKWave encodes the display format in a hex flag word on its own line.
    flags = {"hex": "22", "dec": "20", "bin": "28", "ascii": "24"}
    out = _header("[*]", sel)
    if sel.window:
        out.append(f"[timestart] {sel.window[0]}")
    for i, m in enumerate(sel.markers[:26]):
        # GTKWave has markers A..Z; the first is the primary cursor.
        out.append(f"[markername] {chr(ord('A') + i)} {m.name}" if m.name else "")
        out.append(f"[marker] {m.time}")
    out.append("@800200")
    out.append(f"-{sel.group}")
    for path in sel.unique():
        radix = sel.radix.get(path)
        if radix in flags:
            out.append(f"@{flags[radix]}")
        out.append(path)
    out.append("[pattrace_enable] 1")
    return "\n".join(x for x in out if x != "") + "\n"


# --- ModelSim --------------------------------------------------------------


def do(sel: Selection) -> str:
    """A ModelSim macro (`.do`), in the exact shape §13.3 gives.

        add wave -group "Causal chain" -radix hex /tb/dut/ctrl/ready
        wave cursor add -time 1247ns -name "root cause"
    """
    out = _header("#", sel)
    for path in sel.unique():
        radix = _DO_RADIX.get(sel.radix.get(path, ""), "")
        parts = ["add wave", "-group", f'"{sel.group}"']
        if radix:
            parts += ["-radix", radix]
        parts.append(to_slash(path))
        out.append(" ".join(parts))
    for m in sel.markers:
        at = _time_with_unit(m.time, sel.timescale)
        name = f' -name "{m.name}"' if m.name else ""
        out.append(f"wave cursor add -time {at}{name}")
    if sel.window:
        lo, hi = sel.window
        out.append(
            f"wave zoom range {_time_with_unit(lo, sel.timescale)} "
            f"{_time_with_unit(hi, sel.timescale)}"
        )
    return "\n".join(out) + "\n"


# --- Vivado xsim -----------------------------------------------------------


def wcfg(sel: Selection) -> str:
    """A Vivado xsim waveform configuration (`.wcfg`), which is XML.

    Built with ElementTree rather than string formatting: a path containing
    `<`, `&` or a quote would otherwise produce a file Vivado refuses to open,
    and generated-block indices make those characters plausible.
    """
    root = ET.Element("wave_config")
    ET.SubElement(root, "wave_state")
    db = ET.SubElement(root, "db_ref_list")
    ET.SubElement(db, "db_ref", {"path": "", "id": "1"})
    group = ET.SubElement(
        root, "wvobject", {"fp_name": sel.group, "type": "group"}
    )
    ET.SubElement(group, "obj_property", {"name": "label", "value": sel.group})

    for path in sel.unique():
        slash = to_slash(path)
        obj = ET.SubElement(
            root, "wvobject", {"fp_name": slash, "type": "logic"}
        )
        ET.SubElement(obj, "obj_property", {"name": "ElementShortName",
                                            "value": path.rsplit(".", 1)[-1]})
        ET.SubElement(obj, "obj_property", {"name": "ObjectShortName",
                                            "value": path.rsplit(".", 1)[-1]})
        radix = _WCFG_RADIX.get(sel.radix.get(path, ""))
        if radix:
            ET.SubElement(obj, "obj_property", {"name": "Radix", "value": radix})

    for m in sel.markers:
        ET.SubElement(
            root,
            "marker",
            {"time": _time_with_unit(m.time, sel.timescale), "name": m.name or "marker"},
        )

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f"<?xml version='1.0' encoding='UTF-8'?>\n<!-- {sel.origin} -->\n{body}\n"


# --- Surfer ----------------------------------------------------------------


def surfer(sel: Selection) -> str:
    """A Surfer state file (`.surf`), which is TOML-shaped.

    §13.3 lists it alongside the other three. Surfer reads dotted paths, so no
    conversion is needed.
    """
    out = _header("#", sel)
    if sel.window:
        out += ["", "[viewports.0]", f"left = {sel.window[0]}", f"right = {sel.window[1]}"]
    for path in sel.unique():
        out += ["", "[[displayed_items]]", 'type = "Variable"', f'name = "{path}"']
        radix = sel.radix.get(path)
        if radix:
            out.append(f'format = "{radix.capitalize()}"')
    for m in sel.markers:
        out += ["", "[[markers]]", f"time = {m.time}", f'name = "{m.name or "marker"}"']
    return "\n".join(out) + "\n"


#: Format name -> (writer, conventional extension). The CLI's `--gtkw`/`--do`/
#: `--wcfg`/`--surfer` flags and `-o` extension sniffing both read this.
FORMATS = {
    "gtkw": (gtkw, ".gtkw"),
    "do": (do, ".do"),
    "wcfg": (wcfg, ".wcfg"),
    "surfer": (surfer, ".surf"),
}


def render(fmt: str, sel: Selection) -> str:
    """Write `sel` in the named format."""
    try:
        writer, _ext = FORMATS[fmt]
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}; try one of {', '.join(FORMATS)}") from None
    return writer(sel)


def selection_from_paths(
    paths: Iterable[str],
    group: str,
    store=None,
    markers: Iterable[Marker] = (),
    config=None,
    origin: str = "",
) -> Selection:
    """Build a `Selection`, keeping only signals the trace actually has.

    A save file naming signals the viewer cannot find is worse than a short
    one: GTKWave silently drops them and ModelSim prints an error per line. The
    radix globs from `.veritrace.toml` (§4.3) are applied here, so the exported
    file opens with the same formatting as the UI.
    """
    kept: list[str] = []
    for p in paths:
        if store is not None and store.find(p) is None:
            continue
        kept.append(p)
    radix = {}
    if config is not None:
        for p in kept:
            got = config.radix_for(p)
            if got:
                radix[p] = got
    window = None
    if store is not None:
        t0, t1 = store.time_range
        window = (t0, t1)
    return Selection(
        signals=kept,
        group=group,
        radix=radix,
        markers=list(markers),
        window=window,
        timescale=store.timescale if store is not None else "1ns",
        # ASCII: these files are read by tools and consoles with no agreed
        # encoding, and a mojibake comment in a .do file is a support question.
        origin=origin or f"generated by VeriTrace - {group}",
    )
