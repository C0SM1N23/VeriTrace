"""`.veritrace.toml` — §4.3.

Searched upward from the working directory, so the RTL file list, include dirs,
clock, reset and check thresholds are typed once rather than on every command.
§4.3 calls this an adoption feature, not a detail: without it you retype the
same six arguments thirty times a day and abandon the tool on the third.

Everything here is optional. A missing file yields `Config.empty()` rather than
`None`-handling at every call site, so the defaults live in one place.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Everything this tool writes into a project goes here: the converted trace,
#: the generated `$dumpvars` module (§13.4b), the repro testbench (§8.3), the
#: session file. One name, because the tool also has to *avoid* this directory
#: when it goes looking for the user's RTL — its own output is not design source.
WORK_DIR = ".veritrace"

#: §8.4 — cycles without a transition before a signal counts as stuck.
DEFAULT_STUCK_CYCLES = 100
#: §8.18. The same number as the stuck threshold on purpose: "this is not just
#: busy" should mean one thing across the detectors, not two.
DEFAULT_DEADLOCK_CYCLES = 100


class ConfigError(ValueError):
    """A readable, user-actionable error in ``.veritrace.toml``.

    TOML only validates syntax.  Without the small schema checks below a value
    such as ``rtl = "rtl/*.sv"`` is accepted and then iterated character by
    character, while ``row_height = "wide"`` is silently treated as compact.
    Both failure modes make a real option look implemented while ignoring what
    the user wrote, so configuration errors are rejected at the boundary.
    """

#: §8.10b — how a simulation log announces a failure. The named groups are the
#: contract: `time` (with an optional `unit`), `hier` and `name` when the log
#: gives them. Users add their own under `[triage] patterns` without replacing
#: these, since a log usually mixes house format with the standard ones.
DEFAULT_LOG_PATTERNS: dict[str, str] = {
    # "ERROR: assertion "p_axi_stable" failed at time 12470ns"
    "assertion": (
        r'(?i)assertion\s+"?(?P<name>[\w.$]+)"?\s+failed'
        r'(?:.*?\bat\s+(?:time\s+)?(?P<time>[\d.]+)\s*(?P<unit>[munpf]?s)?)?'
    ),
    # "UVM_ERROR @ 8900ns [SCOREBOARD] expected 0xDEADBEEF got 0xDEAD0000"
    "uvm": (
        r"(?i)UVM_(?P<sev>ERROR|FATAL)\s*@?\s*(?P<time>[\d.]+)?\s*(?P<unit>[munpf]?s)?"
        r"\s*(?:\[(?P<name>[^\]]+)\])?"
    ),
    # "$fatal: timeout waiting for done at 45000ns"
    "timeout": (
        r"(?i)\btime[- ]?out\b(?:.*?\bat\s+(?P<time>[\d.]+)\s*(?P<unit>[munpf]?s)?)?"
    ),
    # "$error: ..." / "$fatal: ..." / a bare ERROR line
    "error": (
        r"(?i)(?:\$(?:error|fatal)\b|(?<![\w.])ERROR(?![\w.]))"
        r"(?:.*?\bat\s+(?:time\s+)?(?P<time>[\d.]+)\s*(?P<unit>[munpf]?s)?)?"
    ),
}


@dataclass(slots=True)
class Config:
    root: Path
    #: Where this came from; None when nothing was found on disk.
    path: Path | None = None

    # [design]
    top: str | None = None
    rtl: list[str] = field(default_factory=list)
    incdirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)

    # [clocks] / [reset]
    primary_clock: str | None = None
    other_clocks: list[str] = field(default_factory=list)
    reset_signal: str | None = None
    # ``None`` means infer the polarity from the trace/name.  ``init`` writes an
    # explicit low/high value, but a hand-written config that omits it must not
    # silently force every active-high design to active-low.
    reset_active: str | None = None

    # [trace]
    trace: str | None = None
    ignore: list[str] = field(default_factory=list)
    #: §8.11b — this trace is an on-board capture, not a simulation. A window
    #: has a front edge, and a causal chain that reaches it has run out of
    #: evidence rather than out of design.
    capture: bool = False

    # [ui]
    row_height: str = "compact"
    radix_globs: dict[str, str] = field(default_factory=dict)
    #: §11.4b. None means "decide from what the design turned out to contain".
    default_tab: str | None = None

    # [checks]
    stuck_cycles: int = DEFAULT_STUCK_CYCLES
    #: §8.18's "a cycle that persists longer than N". A design with long, legal
    #: backpressure raises it; one with tight latency budgets lowers it.
    deadlock_cycles: int = DEFAULT_DEADLOCK_CYCLES
    disabled_checks: list[str] = field(default_factory=list)

    # [triage]
    log_patterns: dict[str, str] = field(default_factory=dict)

    # [ingest] — §8.34. A monitor that already records transactions makes the
    # signal-level extraction redundant for its interface, so naming its log
    # here is the whole configuration a team with one has to write.
    ingest_cocotb_log: str | None = None
    ingest_uvm_db: str | None = None
    #: Same mechanism as `[triage] patterns`: named regexes, first match wins.
    #: Empty means the built-in cocotb forms.
    ingest_patterns: dict[str, str] = field(default_factory=dict)

    # [protocol] — §8.14. Empty `packs` means "try every pack on the search
    # path", which is what makes interface detection automatic; naming them is
    # for a design where a generic pack would match something it should not.
    protocol_packs: list[str] = field(default_factory=list)
    protocol_ignore: list[str] = field(default_factory=list)

    # [coverage] — §8.12. The database `veritrace serve` imports on open. Left
    # empty it is searched for in the usual places, which covers a project whose
    # coverage run wrote where its tool defaults to; naming it is for one whose
    # build puts it somewhere else.
    coverage_path: str | None = None

    @classmethod
    def empty(cls, root: Path | str = ".") -> Config:
        return cls(root=Path(root))

    def rtl_files(self) -> list[Path]:
        """Expand the glob patterns in `design.rtl`, relative to the config."""
        out: list[Path] = []
        for pattern in self.rtl:
            p = Path(pattern)
            if p.is_absolute():
                out.extend(sorted(p.parent.glob(p.name)) if "*" in p.name else [p])
            else:
                out.extend(sorted(self.root.glob(pattern)))
        # Preserve order, drop duplicates.
        seen: set[Path] = set()
        return [p for p in out if p.is_file() and not (p in seen or seen.add(p))]

    def coverage_file(self) -> Path | None:
        """`coverage.path`, resolved like every other path in the file (§4.3).

        A path written in the configuration is relative to the configuration,
        not to whichever directory a later command happens to run from — the
        same rule `design.rtl` follows above. A path typed on the command line
        is the caller's, and is left exactly as given.
        """
        if not self.coverage_path:
            return None
        p = Path(self.coverage_path)
        return p if p.is_absolute() else self.root / p

    def is_ignored(self, path: str) -> bool:
        """§4.3: `trace.ignore` excludes a signal from stuck, lint and correlation.

        Matched on the full hierarchical path and on the leaf name, so both
        `top.tb.*` and `*_unused` behave the way the example in §4.3 implies.
        """
        leaf = path.rsplit(".", 1)[-1]
        return any(
            fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(leaf, pat)
            for pat in self.ignore
        )

    def radix_for(self, path: str) -> str | None:
        """First matching glob from `ui.radix`. A real economy per §4.3 — the
        alternative is setting hex by hand on twenty signals every session."""
        leaf = path.rsplit(".", 1)[-1]
        for pat, radix in self.radix_globs.items():
            if fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(leaf, pat):
                return radix
        return None

    def patterns(self) -> dict[str, str]:
        """Log patterns for §8.10b: the built-ins, with user entries layered on
        top. A house format is an addition, not a replacement — a log normally
        mixes it with `$error` and assertion output."""
        return {**DEFAULT_LOG_PATTERNS, **self.log_patterns}


def _table(data: dict[str, Any], name: str, *, parent: str = "") -> dict[str, Any]:
    got = data.get(name)
    if got is None:
        return {}
    if not isinstance(got, dict):
        dotted = f"{parent}.{name}" if parent else name
        raise ConfigError(f"{dotted} must be a TOML table")
    return got


def _strings(value: Any, name: str) -> list[str]:
    """A TOML array of strings, with no string-as-an-iterable surprises."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be an array of strings")
    return list(value)


def _optional_string(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def _positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    # bool is an int in Python, but ``stuck_cycles = true`` is not meaningful.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def find(start: Path | str = ".") -> Path | None:
    """Nearest `.veritrace.toml`, searching upward."""
    cur = Path(start).resolve()
    for d in (cur, *cur.parents):
        candidate = d / ".veritrace.toml"
        if candidate.is_file():
            return candidate
    return None


def load(start: Path | str = ".") -> Config | None:
    """Parse the nearest config, or `None` when there is none.

    Unreadable or malformed TOML is reported by raising, not by silently
    behaving as if no config existed — a typo in the file must not look like a
    tool that ignores it (P7).
    """
    path = find(start)
    if path is None:
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    design = _table(data, "design")
    clocks = _table(data, "clocks")
    reset = _table(data, "reset")
    trace = _table(data, "trace")
    ui = _table(data, "ui")
    checks = _table(data, "checks")
    triage = _table(data, "triage")
    proto = _table(data, "protocol")
    cov = _table(data, "coverage")
    ing = _table(data, "ingest")
    defines = _table(design, "defines", parent="design")
    radix = _table(ui, "radix", parent="ui")
    patterns = _table(triage, "patterns", parent="triage")
    ingest_patterns = _table(ing, "patterns", parent="ingest")

    top = _optional_string(design.get("top"), "design.top")
    primary_clock = _optional_string(clocks.get("primary"), "clocks.primary")
    reset_signal = _optional_string(reset.get("signal"), "reset.signal")
    reset_active = _optional_string(reset.get("active"), "reset.active")
    if reset_active is not None:
        reset_active = reset_active.lower()
        if reset_active not in {"low", "high"}:
            raise ConfigError("reset.active must be 'low' or 'high'")

    row_height = ui.get("row_height", "compact")
    if not isinstance(row_height, str) or row_height not in {"compact", "comfortable"}:
        raise ConfigError("ui.row_height must be 'compact' or 'comfortable'")

    allowed_radix = {"hex", "dec", "bin", "ascii", "enum"}
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in radix.items()):
        raise ConfigError("ui.radix must map string globs to radix names")
    invalid_radix = sorted({str(v) for v in radix.values()} - allowed_radix)
    if invalid_radix:
        raise ConfigError(
            "ui.radix values must be hex, dec, bin, ascii or enum; got "
            + ", ".join(invalid_radix)
        )

    if any(not isinstance(k, str) or isinstance(v, (dict, list)) for k, v in defines.items()):
        raise ConfigError("design.defines must map names to scalar values")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in patterns.items()):
        raise ConfigError("triage.patterns must map names to regular expressions")
    for name, pattern in patterns.items():
        try:
            re.compile(pattern)
        except re.error as e:
            raise ConfigError(f"triage.patterns.{name} is not a valid regex: {e}") from e
    if any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in ingest_patterns.items()
    ):
        raise ConfigError("ingest.patterns must map names to regular expressions")
    for name, pattern in ingest_patterns.items():
        try:
            re.compile(pattern)
        except re.error as e:
            raise ConfigError(f"ingest.patterns.{name} is not a valid regex: {e}") from e

    capture = trace.get("capture", False)
    if not isinstance(capture, bool):
        raise ConfigError("trace.capture must be true or false")

    return Config(
        root=path.parent,
        path=path,
        top=top,
        rtl=_strings(design.get("rtl"), "design.rtl"),
        incdirs=_strings(design.get("incdirs"), "design.incdirs"),
        defines=[f"{k}={v}" for k, v in defines.items()],
        primary_clock=primary_clock,
        other_clocks=_strings(clocks.get("others"), "clocks.others"),
        reset_signal=reset_signal,
        reset_active=reset_active,
        trace=_optional_string(trace.get("default"), "trace.default"),
        ignore=_strings(trace.get("ignore"), "trace.ignore"),
        capture=capture,
        row_height=row_height,
        radix_globs=dict(radix),
        default_tab=_optional_string(ui.get("default_tab"), "ui.default_tab"),
        stuck_cycles=_positive_int(
            checks.get("stuck_cycles"), "checks.stuck_cycles", DEFAULT_STUCK_CYCLES
        ),
        deadlock_cycles=_positive_int(
            checks.get("deadlock_cycles"), "checks.deadlock_cycles", DEFAULT_DEADLOCK_CYCLES
        ),
        disabled_checks=_strings(checks.get("disable"), "checks.disable"),
        log_patterns=dict(patterns),
        protocol_packs=_strings(proto.get("packs"), "protocol.packs"),
        protocol_ignore=_strings(proto.get("ignore"), "protocol.ignore"),
        coverage_path=_optional_string(cov.get("path"), "coverage.path"),
        ingest_cocotb_log=_optional_string(ing.get("cocotb_log"), "ingest.cocotb_log"),
        ingest_uvm_db=_optional_string(ing.get("uvm_db"), "ingest.uvm_db"),
        ingest_patterns=dict(ingest_patterns),
    )


def load_or_empty(start: Path | str = ".") -> Config:
    """`load`, but never `None` — the form every analysis call site wants."""
    return load(start) or Config.empty(start)
