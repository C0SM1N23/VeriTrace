"""Per-chip SDRAM timing parameters — §8.20.

`packs/timing/<chip>.toml` states the twelve numbers of §8.20's constraint
table exactly as the datasheet gives them: nanoseconds for the delay
parameters, clock cycles for CL/CWL. Converting a nanosecond figure into the
trace's own time units happens once per session (`clocks.to_trace_units`), not
once per comparison — the checker in `banks.py` works entirely in trace-native
integers after that, the same discipline §5.5 uses everywhere else.

Discovery mirrors `protocol.pack`'s search path on purpose: nearest first, a
project's own file shadowing the one shipped in the wheel, so swapping in a
board's real datasheet numbers is dropping a file next to the RTL rather than
editing the tool.
"""

from __future__ import annotations

import tomllib
import math
from dataclasses import dataclass, fields
from fractions import Fraction
from pathlib import Path
import re
from typing import Any

from veritrace.protocol.pack import builtin_dir as _packs_dir

SUFFIX = ".toml"

#: §8.20's table. `CL`/`CWL` are cycle counts; everything else is nanoseconds,
#: checked against `clocks.to_trace_units(value, "ns", store.timescale)`.
NS_FIELDS = (
    "tRCD", "tRP", "tRAS", "tRC", "tRRD", "tWR", "tWTR", "tRTP", "tFAW", "tRFC", "tREFI",
)
CYCLE_FIELDS = ("CL", "CWL")


class TimingError(ValueError):
    """A chip timing file that cannot be used as written."""


@dataclass(frozen=True, slots=True)
class ChipTiming:
    name: str
    tRCD: float
    tRP: float
    tRAS: float
    tRC: float
    tRRD: float
    tWR: float
    tWTR: float
    tRTP: float
    tFAW: float
    tRFC: float
    tREFI: float
    CL: int
    CWL: int
    path: Path | None = None

    @property
    def slug(self) -> str:
        if self.path is not None:
            return self.path.stem
        return self.name.lower().replace(" ", "_")

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "path"} | {
            "slug": self.slug
        }


def loads(text: str, path: Path | None = None) -> ChipTiming:
    """Parse a chip timing file from TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TimingError(f"{path or '<timing>'}: {e}") from e

    name = str(data.get("name") or (path.stem if path else "chip"))
    unknown = set(data) - {"name", *NS_FIELDS, *CYCLE_FIELDS}
    if unknown:
        raise TimingError(f"{name}: unknown timing parameter(s): {', '.join(sorted(unknown))}")
    missing = [k for k in (*NS_FIELDS, *CYCLE_FIELDS) if k not in data]
    if missing:
        raise TimingError(f"{name}: missing timing parameter(s): {', '.join(missing)}")
    try:
        kwargs: dict[str, Any] = {k: float(data[k]) for k in NS_FIELDS}
        kwargs.update({k: int(data[k]) for k in CYCLE_FIELDS})
    except (TypeError, ValueError, OverflowError) as e:
        raise TimingError(f"{name}: timing parameters must be numbers") from e
    for k, v in kwargs.items():
        if isinstance(data[k], bool) or not math.isfinite(v):
            raise TimingError(f"{name}: {k} must be a finite number")
        if k in CYCLE_FIELDS and float(data[k]) != v:
            raise TimingError(f"{name}: {k} must be a whole number of cycles")
        if v < 0:
            raise TimingError(f"{name}: {k} cannot be negative")
    return ChipTiming(name=name, path=path, **kwargs)


def to_ticks(ns: float, timescale: str, *, maximum: bool = False) -> int | None:
    """Round a minimum up and a maximum down, without weakening either bound."""
    from veritrace.clocks import UNIT_FS

    match = re.fullmatch(r"\s*(\d+)\s*(s|ms|us|ns|ps|fs)\s*", timescale.lower())
    if match is None or int(match[1]) == 0 or not math.isfinite(ns) or ns < 0:
        return None
    exact = Fraction(str(ns)) * UNIT_FS["ns"] / (int(match[1]) * UNIT_FS[match[2]])
    return exact.numerator // exact.denominator if maximum else -(-exact.numerator // exact.denominator)


def load(path: Path | str) -> ChipTiming:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise TimingError(f"cannot read timing file {p}: {e}") from e
    return loads(text, p)


def builtin_dir() -> Path:
    """Timing files shipped with the tool, beside the protocol packs."""
    return _packs_dir() / "timing"


def search_path(project_root: Path | str | None = None) -> list[Path]:
    out: list[Path] = []
    if project_root is not None:
        root = Path(project_root)
        out += [d for d in (root / "packs" / "timing", root / "timing") if d.is_dir()]
    out.append(builtin_dir())
    return out


def discover(project_root: Path | str | None = None, errors: list[str] | None = None) -> list[ChipTiming]:
    """Every chip timing file on the search path, project ones shadowing
    built-ins — for a `chip=` dropdown that needs to list what is available."""
    seen: set[str] = set()
    out: list[ChipTiming] = []
    for d in search_path(project_root):
        for f in sorted(d.glob(f"*{SUFFIX}")):
            if f.stem in seen:
                continue
            seen.add(f.stem)
            try:
                out.append(load(f))
            except TimingError as exc:
                if errors is None:
                    raise
                errors.append(str(exc))
    return out


def resolve_choice(choice: Any, project_root: Path | str | None = None) -> ChipTiming:
    """Validate a persisted/UI choice without writing arbitrary project files."""
    if not isinstance(choice, dict) or set(choice) not in ({"chip"}, {"toml"}):
        raise TimingError("choose exactly one chip name or custom TOML document")
    key = next(iter(choice))
    if not isinstance(choice[key], str) or not choice[key].strip():
        raise TimingError(f"{key} must be non-empty text")
    return loads(choice["toml"]) if key == "toml" else find(choice["chip"], project_root)


def find(chip: str, project_root: Path | str | None = None) -> ChipTiming:
    """A chip by slug (file stem) or explicit path."""
    p = Path(chip)
    if p.is_file():
        return load(p)
    slug = chip.lower()
    for d in search_path(project_root):
        f = d / f"{slug}{SUFFIX}"
        if f.is_file():
            return load(f)
    available = ", ".join(sorted({c.slug for c in discover(project_root)})) or "none"
    raise TimingError(f"no timing file named {chip!r}; available: {available}")
