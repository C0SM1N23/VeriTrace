"""VeriTrace command-line interface."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import click
import tomli_w

from veritrace import __version__
from veritrace.config import WORK_DIR
#: §7.2's 90% bar, needed at import time because it is a default in a decorator.
#: The module it comes from is stdlib-only, so this costs nothing at startup.
from veritrace.correlate.resolver import FLOOR as _FLOOR
from veritrace.export import viewers
from veritrace.ingest.capture import FORMATS as _CAPTURE_FORMATS
from veritrace.stim.emit import TARGETS as _STIM_TARGETS

EXCLUDED_DIRS = {".git", "node_modules", "target", ".venv", "venv", "__pycache__", "build", "dist"}

MODULE_RE = re.compile(r"^\s*module\s+(\w+)", re.MULTILINE)
INSTANCE_RE = re.compile(r"^\s*(\w+)\s*(?:#\s*\([^;]*?\))?\s*(\w+)\s*\(", re.MULTILINE)
POSEDGE_RE = re.compile(r"@\s*\(\s*posedge\s+(\w+)")
IF_COND_RE = re.compile(r"if\s*\(\s*(!?)\s*(\w+)\s*\)")

VERILOG_KEYWORDS = {
    "if", "else", "begin", "end", "always", "always_ff", "always_comb",
    "always_latch", "initial", "assign", "case", "endcase", "for", "while",
    "function", "task", "posedge", "negedge", "module", "endmodule",
}


def find_rtl_files(root: Path) -> list[Path]:
    """Recursively find .sv/.v files under root, skipping build/VCS dirs."""
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith((".sv", ".v")):
                matches.append(Path(dirpath) / name)
    return sorted(matches)


def guess_top_module(files: list[Path]) -> str | None:
    """Top module = the one declared but never instantiated by another."""
    modules: set[str] = set()
    sources: dict[Path, str] = {}
    for f in files:
        text = f.read_text(errors="ignore")
        sources[f] = text
        modules.update(MODULE_RE.findall(text))

    instantiated: set[str] = set()
    for text in sources.values():
        body = MODULE_RE.sub("", text)
        for mod_name, _inst_name in INSTANCE_RE.findall(body):
            if mod_name in modules and mod_name not in VERILOG_KEYWORDS:
                instantiated.add(mod_name)

    candidates = sorted(modules - instantiated)
    return candidates[0] if candidates else None


def guess_clock(files: list[Path]) -> str | None:
    """Clock = the signal in the first @(posedge X) found."""
    for f in files:
        match = POSEDGE_RE.search(f.read_text(errors="ignore"))
        if match:
            return match.group(1)
    return None


def guess_reset(files: list[Path]) -> tuple[str, str] | None:
    """Reset = the signal in the first reset-looking `if` condition found."""
    for f in files:
        text = f.read_text(errors="ignore")
        for negation, name in IF_COND_RE.findall(text):
            if "rst" in name.lower() or "reset" in name.lower():
                active = "low" if negation else "high"
                return name, active
    return None


def confirm_or_override(field_name: str, guess: str | None) -> str:
    if guess is None:
        return click.prompt(f"{field_name} (not detected)")
    if click.confirm(f"{field_name}: detected '{guess}' - use this?", default=True):
        return guess
    return click.prompt(field_name, default=guess)


def build_config(
    top: str,
    rtl_globs: list[str],
    clock: str,
    reset_signal: str,
    reset_active: str,
    trace: str | None = None,
) -> dict:
    """The `.veritrace.toml` of §4.3, filled in from what `init` detected.

    §13.4: *never make the user write TOML from scratch.* Every section is
    present with a working value, so editing it is a change rather than a
    lookup in the documentation.
    """
    return {
        "design": {
            "top": top,
            "rtl": rtl_globs,
            "incdirs": [],
            "defines": {},
        },
        "clocks": {
            "primary": clock,
            "others": [],
        },
        "reset": {
            "signal": reset_signal,
            "active": reset_active,
        },
        "trace": {
            "default": trace or "sim/dump.vcd",
            # Excluded from stuck, lint and correlation (§4.3).
            "ignore": [],
        },
        "ui": {
            "row_height": "compact",
            "radix": {"*_addr": "hex", "*_state": "enum", "*count*": "dec"},
        },
        "checks": {
            # §8.4's default: cycles without a transition before a signal counts
            # as frozen.
            "stuck_cycles": 100,
            "disable": [],
        },
    }


def _speak_utf8() -> None:
    """Write UTF-8 whatever the console's code page is.

    Python picks the ANSI code page for stdout on Windows, so every `§` and `—`
    the reports are written with left as cp1252 bytes: fine on a native console,
    mojibake in Git Bash, in a CI log, and in any file the output is redirected
    to. Section references are how findings point back at the spec, so they are
    worth keeping legible.

    Only the encoding changes, and only when it is not already UTF-8 — a stream
    a test or a pipe has replaced with something else is left alone.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc in ("utf8", "") or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # an unusual stream is not a reason to fail the command (P7)


@click.group()
@click.version_option(version=__version__, prog_name="veritrace")
def main() -> None:
    """VeriTrace command-line interface."""
    _speak_utf8()


def _resolve_rtl(rtl: tuple[Path, ...], top: str | None) -> tuple[list[Path], list[str], list[str], str | None]:
    """RTL sources from the command line, falling back to `.veritrace.toml`.

    A config is only allowed to fill in `top`, `incdirs` and `defines` when it
    is talking about the *same* design. `config.load()` searches upward, so a
    `.veritrace.toml` left in a repository root — which `veritrace run` writes
    there (§13.4b) — otherwise supplied its `design.top` to a command that had
    been pointed somewhere else with `--rtl`. Elaborating a folder under a top
    module that is not in it yields an empty graph, and every answer built on
    that graph is wrong: `fsm` reported no state machines on a design with six,
    and `why` rejected a signal while suggesting the same name back.
    """
    from veritrace import config as cfg
    from veritrace.graph.elaborate import discover

    files: list[Path] = []
    for r in rtl:
        files.extend(discover(r))
    incdirs: list[str] = []
    defines: list[str] = []

    conf = cfg.load()
    if conf is not None and (not files or _config_covers(conf, files)):
        if not files:
            files = conf.rtl_files()
        incdirs = [str(conf.root / d) for d in conf.incdirs]
        defines = conf.defines
        top = top or conf.top
    return files, incdirs, defines, top


def _config_covers(conf: object, files: list[Path]) -> bool:
    """Is this config describing the sources the command was given?

    True when the config names any of them in `design.rtl`. Nothing stricter:
    a project config that lists `rtl/**/*.sv` and a command narrowed to one of
    those files is still the same design, and should keep its defines.
    """
    try:
        known = {p.resolve() for p in conf.rtl_files()}  # type: ignore[attr-defined]
    except OSError:
        return False
    return any(f.resolve() in known for f in files)


# --- shared plumbing for the analysis commands -----------------------------
#
# `why`, `cone`, `stuck`, `check`, `probes` and `triage` all need the same
# four things: a store, a graph correlated against it, the project config and
# the primary clock. Assembling that once keeps the commands to their actual
# subject, and keeps them consistent about which clock cycles are numbered in.


@dataclass(slots=True)
class Context:
    store: object
    config: object
    clock: object = None
    elaboration: object = None
    graph: object = None
    correlation: object = None
    trace_path: Path | None = None
    #: §8.13 extraction, filled in on demand — most commands do not need it, and
    #: a scan of every interface is not free.
    protocol: object = None
    txn_index: object = None
    #: §8.17/§8.18, likewise on demand.
    performance: object = None
    wait_for: list = field(default_factory=list)
    #: §8.20's per-interface memory reports, likewise.
    memory: object = None
    #: §8.19's scoreboard and §8.21/§8.12's coverage, likewise on demand.
    integrity: object = None
    coverage: object = None
    #: Every automatic finding (§8.4–§8.11), likewise.
    checks: object = None

    def transactions(self) -> object:
        """Extract transactions once per command run (§8.14)."""
        from veritrace.protocol import engine
        from veritrace.protocol.link import TxnIndex

        if self.protocol is None:
            root = getattr(self.config, "root", None) or (
                self.trace_path.parent if self.trace_path else None
            )
            self.protocol = engine.extract(
                self.store, self.trace_path, self.clock, self.config, project_root=root
            )
            self.txn_index = TxnIndex.build(self.protocol, self.store)
        return self.protocol

    def measure(self) -> object:
        """§8.17's metrics and §8.18's liveness scan, once per command run."""
        from veritrace.perf import report as perf_report

        if self.performance is None:
            analysis = self.transactions()
            self.performance, self.wait_for = perf_report.build(
                self.store, analysis.extractions, self.graph, self.clock, self.config
            )
        return self.performance

    def memory_reports(self, chip: str | None = None) -> object:
        """§8.20's memory analysis, once per command run."""
        from veritrace.memory import report as mem_report

        if self.memory is None:
            analysis = self.transactions()
            root = getattr(self.config, "root", None) or (
                self.trace_path.parent if self.trace_path else None
            )
            self.memory = mem_report.build_all(
                self.store, analysis.packs, self.clock, self.config, root, chip
            )
        return self.memory

    def data_integrity(self) -> object:
        """§8.19's scoreboard and path comparison, once per command run."""
        from veritrace.integrity import report as int_report

        if self.integrity is None:
            self.integrity = int_report.build(self.transactions(), self.store, self.clock)
        return self.integrity

    def check_report(self) -> object:
        """Every automatic finding, once per command run (§8.4–§8.11).

        `check`, `run` and `export` all need the same list; three calls to
        `run_all` would be three chances for the CI gate, the terminal report and
        the bug report's appendix to disagree about what this trace contains.
        """
        from veritrace.analysis import checks as checks_mod

        if self.checks is None:
            self.checks = checks_mod.run_all(
                self.store,
                self.graph,
                self.elaboration,
                self.clock,
                self.config,
                self.transactions(),
                self.measure().liveness,
                self.memory_reports(),
                self.data_integrity(),
                project_root=(
                    getattr(self.config, "root", None)
                    or (self.trace_path.parent if self.trace_path else None)
                ),
            )
        return self.checks

    def coverage_report(self, coverage_path: Path | None = None, source: str | None = None) -> object:
        """§8.21's functional coverage and §8.12's imported code coverage."""
        from veritrace.coverage import report as cov_report

        if self.coverage is None:
            root = getattr(self.config, "root", None) or (
                self.trace_path.parent if self.trace_path else None
            )
            self.coverage = cov_report.build(
                self.transactions(),
                self.store,
                self.clock,
                self.graph,
                coverage_path=coverage_path,
                project_root=root,
                source=source,
            )
        return self.coverage


def _load(
    trace: Path | None,
    rtl: tuple[Path, ...] = (),
    top: str | None = None,
    need_rtl: bool = False,
) -> Context:
    from veritrace import TraceStore, clocks
    from veritrace import config as cfg
    from veritrace.correlate.resolver import correlate
    from veritrace.graph.elaborate import elaborate

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    if need_rtl and not files:
        # §7.4 makes "no RTL" a supported mode, but not for these commands.
        raise click.ClickException(
            "This needs RTL. Pass --rtl <file|dir>, or set design.rtl in .veritrace.toml."
        )

    # §13's examples pass raw dumps, so accept one and convert transparently —
    # and §4.3's whole point is that the trace is named once, in the config, so
    # a command with no argument finds the one recorded there rather than
    # asking for it again.
    trace = _default_trace(trace, flag="a trace path")
    ctx = Context(
        store=TraceStore(str(trace)),
        # The project you are standing in decides its own settings; the trace's
        # directory is only the fallback for a dump kept outside it. The other
        # way round lets a trace's *location* pick the packs, the ignore list and
        # the ingest log — which is how a cocotb log named in the project config
        # went unread because the waveform lived one directory over.
        config=cfg.load() or cfg.load_or_empty(trace.parent),
        trace_path=trace,
    )
    if files:
        try:
            ctx.elaboration = elaborate(files, incdirs, defines, top)
            ctx.graph = ctx.elaboration.graph
            ctx.correlation = correlate(
                ctx.graph,
                {s.path: s.handle for s in ctx.store.signals()},
                ctx.elaboration.aliases,
            )
        except Exception as e:  # noqa: BLE001 - RTL trouble degrades, never crashes
            if need_rtl:
                raise click.ClickException(f"could not elaborate the RTL: {e}") from e
            click.echo(f"  warning: could not elaborate the RTL: {e}", err=True)
    ctx.clock = clocks.resolve(ctx.store, ctx.graph, ctx.config)
    _warn_if_rtl_moved(ctx, files)
    return ctx


def _warn_if_rtl_moved(ctx: "Context", files: list[Path]) -> None:
    """§5.7, in the terminal — not only in the server.

    *"Daca modifici RTL-ul dupa simulare si apoi rulezi why-trace pe dump-ul
    vechi, primesti raspunsuri gresite cu incredere totala."* The session records
    the hash on first open; every analysis command has to check it, because a
    CI job and a script never go through the server at all — and those are
    exactly where a stale dump survives longest.
    """
    from veritrace.api.sessions import LayoutFile, sha256_files

    if ctx.graph is None or not files or ctx.trace_path is None:
        return
    try:
        stored = LayoutFile.for_trace(ctx.trace_path).load().get("provenance") or {}
        if stored.get("trace_sha256") != ctx.store.source_sha256:
            return  # a different run: nothing to compare against
        was = stored.get("rtl_sha256")
        now = sha256_files(files)
        if was and was != now:
            click.echo(
                f"  warning: the RTL has changed since this trace was made "
                f"({was[:12]}… -> {now[:12]}…). Line numbers and causal chains "
                "may not match what was simulated (§5.7).",
                err=True,
            )
    except OSError:
        # Provenance is a safety net; failing to read it must not stop the
        # command the user actually asked for (P7).
        pass


#: How many candidates an ambiguous name lists before it stops.
_CANDIDATES = 10


def resolve_signal(graph, name: str, store=None) -> str:
    """`resolver.resolve_path`, with the terminal's phrasing for a refusal.

    The rule itself is §7.2's and lives in the correlator; what belongs here is
    what to say when it declines — including the nearest names the *trace* has,
    which the graph cannot know about.
    """
    from veritrace.correlate.resolver import resolve_path

    path, candidates = resolve_path(graph, name)
    if path is not None:
        if path != name:
            # Say which one, on stderr: the answer that follows is about a path
            # the user did not type, and a script piping stdout is unaffected.
            click.echo(f"  {name} -> {path}", err=True)
        return path
    if candidates:
        listed = "\n".join(f"    {p}" for p in candidates[:_CANDIDATES])
        more = (
            f"\n    ... and {len(candidates) - _CANDIDATES} more"
            if len(candidates) > _CANDIDATES
            else ""
        )
        raise click.ClickException(
            f"{name!r} is ambiguous — {len(candidates)} signals end with it:\n{listed}{more}"
        )
    raise click.ClickException(_unknown_signal(name, store))


def _unknown_signal(name: str, store=None) -> str:
    """`unknown signal`, with the nearest names the trace actually has.

    The same subsequence ranking the command palette uses, so the terminal and
    the interface suggest the same thing for the same typo.
    """
    from veritrace.api.search import search_signals

    near = [r["path"] for r in search_signals(store.signals(), name, limit=5)] if store else []
    if not near:
        return f"unknown signal: {name}"
    listed = "\n".join(f"    {p}" for p in near)
    return (
        f"unknown signal: {name}\n  did you mean:\n{listed}\n"
        f"  `veritrace signals {name}` searches the whole trace."
    )


def _at(ctx: Context, when: str | None) -> int:
    """Resolve `--at`: a raw timestamp, `cN` for a clock cycle, or the end."""
    _t0, t1 = ctx.store.time_range
    if not when:
        return t1
    text = when.strip()
    if text.lower().startswith("c"):
        if ctx.clock is None:
            raise click.ClickException(
                "Cycle times need a primary clock; none was found in this trace."
            )
        at = ctx.clock.time_of(int(text[1:]))
        if at is None:
            raise click.ClickException("the primary clock never rises")
        return at
    try:
        return int(text)
    except ValueError:
        raise click.ClickException(f"cannot read a time from {when!r}") from None


def rtl_options(fn):
    """Add `--rtl/--top` to a command, so they mean the same thing everywhere."""
    fn = click.option("--top", default=None, help="Top module (default: inferred).")(fn)
    return click.option(
        "--rtl",
        multiple=True,
        type=click.Path(exists=True, path_type=Path),
        help="RTL file or directory. Repeatable. Defaults to design.rtl in .veritrace.toml.",
    )(fn)


#: The four viewer formats of §13.3, as CLI flags shared by several commands.
def viewer_options(fn):
    """Add `--gtkw/--do/--wcfg/--surfer -o` to a command (§13.3)."""
    for name in reversed(list(viewers.FORMATS)):
        fn = click.option(
            f"--{name}",
            name,
            is_flag=True,
            help=f"Write a {name} save file instead of a text report.",
        )(fn)
    return click.option(
        "-o",
        "--output",
        type=click.Path(path_type=Path),
        default=None,
        help="Write to this file instead of stdout.",
    )(fn)


def _emit_selection(
    ctx: Context,
    paths: list[str],
    group: str,
    flags: dict[str, bool],
    output: Path | None,
    markers: list = (),
) -> bool:
    """Write a viewer save file if one was asked for. Returns True if it did."""
    chosen = [name for name in viewers.FORMATS if flags.get(name)]
    if not chosen:
        return False
    if len(chosen) > 1:
        raise click.ClickException(f"pick one format, not {', '.join(chosen)}")
    fmt = chosen[0]
    sel = viewers.selection_from_paths(
        paths, group, store=ctx.store, markers=markers, config=ctx.config
    )
    if not sel.signals:
        raise click.ClickException("none of those signals are in the trace")
    text = viewers.render(fmt, sel)
    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"{len(sel.unique())} signal(s) -> {output}", err=True)
    else:
        click.echo(text, nl=False)
    return True


@main.command()
@click.argument(
    "trace", type=click.Path(exists=True, path_type=Path), required=False, default=None
)
@rtl_options
@click.option("--limit", default=20, type=int, help="How many unmatched paths to list.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--fail-under",
    default=None,
    type=float,
    # A value, never an optional one: click's optional-value options bind only
    # with `=`, so `--fail-under 90` would silently use the default and let a
    # 12%-correlated run pass a gate that looks like it demands 90.
    help=f"Exit non-zero below this percentage. §7.2's bar is {_FLOOR:g}.",
)
def correlate(
    trace: Path | None,
    rtl: tuple[Path, ...],
    top: str | None,
    limit: int,
    as_json: bool,
    fail_under: float | None,
) -> None:
    """Match RTL signals against a trace and report the correlation rate.

    §7 makes this a first-class number rather than something buried: a low rate
    means every later answer is built on sand, and it is nearly always a
    missing simulator flag (§4.0).

    \b
      veritrace correlate dump.vcd --rtl src/
      veritrace correlate dump.vcd --rtl src/ --fail-under 90   # the CI gate

    `--fail-under` is what makes §7.2's *first-class metric* first-class in a
    build: without it the only way to gate on the rate is to grep a percentage
    out of prose, which passes silently the day the sentence is reworded.
    """
    from veritrace.correlate.resolver import format_report

    # §7.4: no RTL is a supported mode, but this command has nothing to do
    # without it.
    ctx = _load(_default_trace(trace, flag="a trace path"), rtl, top, need_rtl=True)
    el = ctx.elaboration

    if as_json:
        click.echo(
            json.dumps(
                {
                    "top": el.graph.top,
                    "n_rtl_signals": len(el.graph),
                    "n_trace_signals": ctx.store.n_signals,
                    "blackboxes": sorted(el.graph.blackboxes),
                    "elaboration_errors": list(el.errors),
                    **ctx.correlation.to_dict(limit=limit),
                },
                indent=2,
            )
        )
    else:
        click.echo(f"top = {el.graph.top or '?'}")
        click.echo(f"{len(el.graph)} RTL signals, {ctx.store.n_signals} trace signals")
        click.echo(format_report(ctx.correlation, limit=limit))
        if el.graph.blackboxes:
            click.echo(f"  {len(el.graph.blackboxes)} black-box instance(s) without source")
        if el.errors:
            click.echo(f"  {len(el.errors)} elaboration error(s); first: {el.errors[0]}")

    if fail_under is not None and ctx.correlation.percent < fail_under:
        raise click.ClickException(
            f"correlation is {ctx.correlation.percent}%, below the required "
            f"{fail_under:g}% — see §4.0 for the dump flags this usually means."
        )


@main.command()
@click.argument(
    "traces", type=click.Path(exists=True, path_type=Path), nargs=-1
)
@click.option("--host", default="127.0.0.1", help="Interface to bind.")
@click.option("--port", default=8765, type=int, help="Port to bind.")
@click.option("--browser/--no-browser", default=True, help="Open a browser on start.")
@click.option(
    "--rtl",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="RTL file or directory, for why-trace and source view. Repeatable.",
)
@click.option("--top", default=None, help="Top module (default: inferred).")
def serve(
    traces: tuple[Path, ...],
    host: str,
    port: int,
    browser: bool,
    rtl: tuple[Path, ...],
    top: str | None,
) -> None:
    """Serve one or more traces over HTTP and WebSocket.

    Each TRACE may be a `.vtx` store or a raw `.vcd`/`.fst`, which is converted
    on the way in. With no argument it uses `trace.default` from
    `.veritrace.toml`, so §13.4's `veritrace init && veritrace serve` works
    with nothing typed in between.

    \b
      veritrace serve dump.vtx --rtl src/
      veritrace serve good.vcd bad.vcd --rtl src/   # both runs, for TAB 5

    A second trace is opened as a session of its own (§10.1) and becomes the
    other side of the Diff tab — which is the only way §8.7's comparison has a
    second run to compare against.
    """
    import uvicorn

    from veritrace.api import create_app

    trace = _default_trace(traces[0] if traces else None, flag="a trace path")
    files, _incdirs, _defines, top = _resolve_rtl(rtl, top)
    app = create_app(default_trace=trace, rtl=[str(f) for f in files], top=top)
    session_id = app.state.default_session_id
    for extra in traces[1:]:
        try:
            other = app.state.registry.open(extra, [str(f) for f in files], top)
            click.echo(f"  also open: {extra}    session {other.session_id}")
        except (FileNotFoundError, ValueError, OSError) as e:
            # §11.8: a second trace that will not open is a message, not a
            # reason to refuse to serve the first.
            click.echo(f"  could not open {extra}: {e}", err=True)
    url = f"http://{host}:{port}"

    click.echo(f"VeriTrace serving {trace}")
    click.echo(f"  {url}    session {session_id}")

    session = app.state.registry.get(session_id)
    if session is not None and session.graph is not None:
        click.echo(
            f"  RTL: {len(files)} file(s), {len(session.graph)} signals, "
            f"{session.correlation.percent}% correlated"
        )
        if session.rtl_changed:
            # §5.7: warn, never block.
            click.echo("  WARNING: RTL has changed since this trace was made.")
    else:
        # §7.4: a waveform viewer is a perfectly good thing to be.
        click.echo("  No RTL: why-trace and source view are off. Pass --rtl to enable them.")

    # §13.4: say what the checks already found, before the browser even opens.
    if session is not None and session.report is not None:
        findings = session.findings()
        if len(findings):
            for group, group_findings in findings.by_group().items():
                worst = group_findings[0]
                more = f" (+{len(group_findings) - 1} more)" if len(group_findings) > 1 else ""
                click.echo(f"  ! {group.label}: {worst.signal or '-'} {worst.title}{more}")
        else:
            click.echo("  no automatic findings")

    if browser:
        import threading
        import webbrowser

        # Fire after the server is accepting connections; a failure to open a
        # browser must never take the server down with it.
        def open_later() -> None:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

        threading.Timer(0.5, open_later).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


@main.command()
@click.argument(
    "trace", type=click.Path(path_type=Path), required=False, default=None
)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@viewer_options
def why(trace, query, rtl, top, as_json, output, **flags):
    """Explain a value: `veritrace why dump.vtx "top.ctrl.ready == 0 @ c1247"`.

    Also answers at transaction level (§8.16):
    `veritrace why dump.vtx "why(txn.m0.WRITE[0].not_issued)"`.

    With a viewer flag, writes the causal chain as a save file instead — the
    signals in the chain plus a cursor on the cause (§13.3).
    """
    from veritrace.analysis.whytrace import root_cause

    trace, query = _trace_and_query(trace, query)
    query = _need_query("why", query)
    ctx = _load(trace, rtl, top, need_rtl=True)
    signal, at, headline, result = _ask(ctx, query)

    chain = list(result.root.walk())
    cause = root_cause(result.root) or result.root
    if _emit_selection(
        ctx,
        [n.signal.path() for n in chain],
        "Causal chain",
        flags,
        output,
        markers=[viewers.Marker(cause.time, "root cause")],
    ):
        return

    # §8.1's `expected`: if the question named a value the trace disagrees with,
    # say so before the chain rather than answering a different question.
    note = _expectation_note(query, result)
    if as_json:
        payload = {"query": query, "time": at, **result.to_dict()}
        if headline:
            payload["headline"] = headline
        if note:
            payload["note"] = note
        click.echo(json.dumps(payload, indent=2))
        return
    if note:
        click.echo(note + "\n", err=True)
    if headline:
        click.echo(headline + "\n")
    _print_chain(result.root, ctx.clock)
    click.echo(f"\n{result.nodes} nodes in {result.elapsed_ms:.1f} ms")


def _expectation_note(query: str, result) -> str | None:
    from veritrace.analysis import vtq

    try:
        return vtq.expectation(vtq.parse(query), result.root.value)
    except vtq.QueryError:
        return None


def _need_query(command: str, query: str | None) -> str:
    """Refuse a missing question before anything expensive is loaded.

    Order matters here: `_load` elaborates the RTL, so checking the question
    afterwards answers `veritrace why` with "this needs RTL" — true, and not the
    thing that is wrong.
    """
    if not query:
        raise click.ClickException(
            f'{command} needs a question, e.g. veritrace {command} "why(top.dut.full)"'
        )
    return query


def _ask(ctx: "Context", query: str | None):
    """Resolve a why-question and answer it: `(signal, time, headline, result)`.

    Shared by `why`, `repro` and `export` (§13's P6 — every feature in the
    terminal). A second copy of this would be a second set of rules for what
    `@c1247` means and for when a transaction question becomes a signal one.
    """
    from veritrace.analysis import vtq
    from veritrace.analysis.whytrace import WhyTracer

    try:
        parsed = vtq.parse(query)
    except vtq.QueryError as e:
        raise click.ClickException(str(e)) from e

    headline = ""
    if parsed.txn is not None:
        signal, at, headline = _txn_question(ctx, parsed.txn)
    else:
        # A partial name is resolved the way §7.2 resolves one, or refused with
        # the candidates — never guessed at.
        signal = resolve_signal(ctx.graph, parsed.signal, ctx.store)
        at = _at(
            ctx,
            None
            if parsed.time is None
            else f"c{parsed.time}"
            if parsed.is_cycle
            else str(parsed.time),
        )
        ctx.transactions()

    # §8.11b: on a capture the first sample is a wall, not a fact about the
    # design, and the walk has to say so instead of calling a value constant.
    start = ctx.store.time_range[0] if getattr(ctx.config, "capture", False) else None
    result = WhyTracer(
        ctx.graph, ctx.store, txn_index=ctx.txn_index, capture_start=start
    ).why(signal, at)
    return signal, at, headline, result


def _txn_question(ctx: "Context", ref) -> tuple[str, int, str]:
    """§8.16: reduce a transaction question to the signal question behind it."""
    from veritrace.protocol import link

    analysis = ctx.transactions()
    if not analysis.extractions:
        raise click.ClickException(
            "no protocol interfaces were detected in this trace, so there are "
            "no transactions to ask about"
        )
    try:
        q = link.question(analysis, ctx.txn_index, ctx.store, ctx.clock, ref)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    return q.signal, q.time, q.headline


def _print_chain(node, clock, depth: int = 0, seen: set | None = None) -> None:
    """The chain as an indented tree, symptom first, root cause last.

    The answer is a DAG — the same question reached by several paths is one
    memoised node — so a node already printed is shown as a back-reference
    rather than expanded again.
    """
    from veritrace import clocks as _clocks
    from veritrace.analysis.whytrace import NodeKind

    seen = set() if seen is None else seen
    marker = "  " * depth + ("-> " if depth else "")
    repeat = id(node) in seen
    seen.add(id(node))
    at = _clocks.format_time(node.time, clock)

    if node.kind is NodeKind.TXN_LINK:
        # §8.16: the one node that is about a transaction, not a signal.
        click.echo(f"{marker}[txn] {node.txn}   at {at}" + ("   (as above)" if repeat else ""))
        if not repeat:
            click.echo("  " * (depth + 1) + f"   {node.detail}")
    else:
        where = f"   {node.loc}" if node.loc else ""
        # §7.3: a value computed from the graph is marked, never shown as if it
        # had been measured.
        value = f"~{node.value} (derived)" if node.derived else node.value
        click.echo(
            f"{marker}{node.signal} = {value}   [{node.reason.value}] at {at}"
            + ("   (as above)" if repeat else where)
        )
        if node.detail and not repeat:
            click.echo("  " * (depth + 1) + f"   {node.detail}")
    if repeat:
        return
    for child in node.children:
        _print_chain(child, clock, depth + 1, seen)


@main.command()
@click.argument(
    "trace", type=click.Path(exists=True, path_type=Path), required=False, default=None
)
@click.argument("signal")
@rtl_options
@click.option("--depth", default=4, type=int, help="How many graph edges out.")
@click.option(
    "--direction",
    type=click.Choice(["fanin", "fanout", "both"]),
    default="fanin",
    help="fanin: what produced it. fanout: what breaks if it changes.",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Keep only signals that toggled in the window (§8.6) — cuts most of the noise.",
)
@click.option("--from", "t_from", default=None, help="Window start, or cN.")
@click.option("--to", "t_to", default=None, help="Window end, or cN.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@viewer_options
def cone(
    trace, signal, rtl, top, depth, direction, active_only, t_from, t_to, as_json, output, **flags
):
    """Signals within N edges of SIGNAL. From 4000 signals you keep 8 (§8.6)."""
    from veritrace.analysis import cone as cone_mod

    ctx = _load(trace, rtl, top, need_rtl=True)
    signal = resolve_signal(ctx.graph, signal, ctx.store)
    lo, hi = ctx.store.time_range
    window = (_at(ctx, t_from) if t_from else lo, _at(ctx, t_to) if t_to else hi + 1)
    try:
        result = cone_mod.cone(
            ctx.graph,
            signal,
            depth=depth,
            direction=direction,
            store=ctx.store if active_only else None,
            window=window if active_only else None,
        )
    except KeyError:
        raise click.ClickException(f"unknown signal: {signal}") from None

    if as_json:
        click.echo(
            json.dumps(
                {
                    "signal": signal,
                    "direction": direction,
                    "depth": depth,
                    "window": list(window) if active_only else None,
                    "n_inactive": result.n_inactive,
                    "nodes": [
                        {"path": n.path, "depth": n.depth, "role": n.role} for n in result.nodes
                    ],
                },
                indent=2,
            )
        )
        return

    if _emit_selection(ctx, result.paths(), f"Cone of {signal}", flags, output):
        return

    click.echo(f"{direction} cone of {signal}, depth {depth}: {len(result.nodes)} signal(s)")
    if result.n_inactive:
        click.echo(f"  {result.n_inactive} dropped as inactive in the window")
    for node in result.nodes:
        role = f" [{node.role}]" if node.role else ""
        click.echo(f"  {'  ' * node.depth}{node.path}{role}")


@main.command()
@click.argument(
    "trace", type=click.Path(exists=True, path_type=Path), required=False, default=None
)
@rtl_options
@click.option("--cycles", default=None, type=int, help="Threshold, in clock cycles.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@viewer_options
def stuck(trace, rtl, top, cycles, as_json, output, **flags):
    """Signals frozen for longer than the threshold (§8.4)."""
    from veritrace.analysis import stuck as stuck_mod

    ctx = _load(trace, rtl, top)
    if ctx.clock is None:
        raise click.ClickException("No clock could be identified, so cycles have no meaning.")
    found = sorted(
        stuck_mod.scan(ctx.store, ctx.clock, ctx.graph, ctx.config, cycles),
        key=lambda f: f.sort_key,
    )
    # Whether the window could have fired at all — the difference between "the
    # design is fine" and "you asked about a wider window than the run has".
    unreachable = stuck_mod.too_short(ctx.store, ctx.clock, ctx.config, cycles)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "clock": ctx.clock.path,
                    "n_cycles": ctx.clock.n_cycles,
                    "threshold_cycles": (
                        cycles
                        if cycles is not None
                        else getattr(ctx.config, "stuck_cycles", stuck_mod.DEFAULT_CYCLES)
                    ),
                    "unreachable": unreachable,
                    "findings": [f.to_dict() for f in found],
                },
                indent=2,
            )
        )
        return

    if _emit_selection(ctx, [f.signal for f in found if f.signal], "Stuck", flags, output):
        return
    if not found:
        click.echo(unreachable or "nothing has been frozen for longer than the threshold")
        return
    click.echo(f"{len(found)} frozen signal(s), clock {ctx.clock.path}")
    for f in found:
        click.echo(f"  {f.signal}   {f.title}   ({f.detail})   {f.loc or ''}")


@main.command()
@click.argument(
    "trace", type=click.Path(exists=True, path_type=Path), required=False, default=None
)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--fail-on",
    default="",
    help="Exit non-zero if any of these fire. Names or groups: "
    "stuck,x,lint,cdc,protocol,deadlock,memory,integrity.",
)
def check(trace, rtl, top, as_json, fail_on):
    """Every automatic finding: stuck, X sources, lint, parameters (§13, §13.9).

    With `--fail-on` this is the CI gate: a failing build carries the cause in
    its log rather than a dump nobody will open.
    """
    ctx = _load(trace, rtl, top)
    report = ctx.check_report()

    if as_json:
        click.echo(
            json.dumps(
                {
                    **report.to_dict(),
                    # §7.2: the rate the findings were produced under. A gate
                    # reading this file can tell "clean" from "matched nothing".
                    "correlation": (
                        ctx.correlation.to_dict(limit=20) if ctx.correlation else None
                    ),
                },
                indent=2,
            )
        )
    else:
        click.echo(_format_report(report, ctx.clock, ctx.correlation))

    _gate(report, fail_on)


def _gate(report, fail_on: str) -> None:
    """§13.9's exit code. Shared by `check` and `run`, so the CI verdict cannot
    differ depending on which one a project uses."""
    from veritrace.analysis import checks as checks_mod

    if not fail_on:
        return
    wanted = checks_mod.expand_checks(fail_on.split(","))
    if any(f.check in wanted for f in report):
        raise SystemExit(1)


@main.command()
@click.argument("sources", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--top", default=None, help="Top module. Detected when not given.")
@click.option(
    "--sim",
    type=click.Choice(["icarus", "verilator", "modelsim", "xsim"]),
    default="icarus",
    help="Simulator. Only Icarus is driven from here; the others print their recipe.",
)
@click.option(
    "--work",
    type=click.Path(path_type=Path),
    default=None,
    help="Where the build and the waveform land. Default: .veritrace/ beside the sources.",
)
@click.option("--timeout", default=120.0, help="Seconds before the simulation is given up on.")
@click.option("-D", "--define", "defines", multiple=True, help="Passed to the compiler as -D.")
@click.option("-I", "--incdir", "incdirs", multiple=True, help="Passed to the compiler as -I.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--fail-on",
    default="",
    help="Exit non-zero if any of these fire — the same gate `check` uses.",
)
@click.option("--serve", "then_serve", is_flag=True, help="Open the interface when it is done.")
@click.pass_context
def run(
    click_ctx, sources, top, sim, work, timeout, defines, incdirs, as_json, fail_on, then_serve
):
    """Simulate a folder of SystemVerilog and report what is wrong (§13.4b).

    The whole of the first sixty seconds in one command: find the sources, work
    out the top module, run the simulation, convert the waveform and print every
    automatic finding.

    \b
      veritrace run rtl/
      veritrace run rtl/ tb/ --top tb_cpu --serve
      veritrace run . --fail-on stuck,x,cdc          # the CI gate, no Makefile

    A testbench that does not call `$dumpfile` still produces a waveform: a
    generated module is compiled alongside it, and your sources are never
    touched.
    """
    from veritrace import simulate

    roots = list(sources) or [Path.cwd()]
    # A `.f` filelist is passed through to the compiler rather than expanded:
    # it is the portable way a project already describes its build, `+incdir+`
    # and all, and re-implementing that parser here would be a second opinion
    # about what the project contains.
    filelists = [r.resolve() for r in roots if r.is_file() and r.suffix.lower() == ".f"]
    files: list[Path] = list(filelists)
    for r in roots:
        if r in filelists or (r.is_file() and r.suffix.lower() == ".f"):
            continue
        files.extend([r] if r.is_file() else find_rtl_files(r))
    files = list(dict.fromkeys(f.resolve() for f in files))
    if not files:
        raise click.ClickException(
            f"No .sv/.v/.f files under {', '.join(str(r) for r in roots)}."
        )

    # Where the project's own flow runs from, and therefore where `$readmemh`
    # and a filelist's relative paths resolve. A filelist names it exactly;
    # otherwise it is the directory that was pointed at.
    run_dir = (filelists[0].parent if filelists else (
        roots[0] if roots[0].is_dir() else roots[0].parent
    )).resolve()

    # What the *graph* should elaborate. A filelist is read here — the compiler
    # still gets `-f` and remains the authority on the build; pyslang simply has
    # no notion of a command file, and a graph built from nothing would turn
    # every `why()` into "no RTL loaded" on exactly the projects that have their
    # build written down properly.
    rtl: list[Path] = [f for f in files if f.suffix.lower() != ".f"]
    for fl in filelists:
        named, dirs, defs = simulate.read_filelist(fl)
        rtl += named
        incdirs = tuple(incdirs) + tuple(str(d) for d in dirs)
        defines = tuple(defines) + tuple(defs)
    rtl = list(dict.fromkeys(rtl))

    top = top or guess_top_module([f for f in rtl if f.is_file()])
    if not top:
        raise click.ClickException(
            "Could not work out the top module: every module in these sources is "
            "instantiated by another one. Name it with --top."
        )

    if sim != "icarus":
        # §4.0 holds the flags for the other three and the Makefile implements
        # them. Printing the recipe beats reimplementing it in a second place
        # that would then drift.
        raise click.ClickException(
            f"`--sim {sim}` is not driven from here; its flags live in §4.0 and in the "
            f"Makefile, which is where they are kept right:\n"
            f"    make sim-{sim} convert DESIGN=<dir> TOP={top}\n"
            f"Then: veritrace check <dir>/dump.vcd --rtl <dir>"
        )

    base = run_dir
    work = (work or base / WORK_DIR).resolve()
    # What to name in the "open it" line: the directory the RTL came from, which
    # is not the run directory when a filelist points somewhere else.
    rtl_hint = rtl[0].parent if rtl else base

    # `--json` has to be parseable on stdout, so the running commentary is
    # silenced rather than interleaved with the document.
    say = (lambda *_a, **_k: None) if as_json else click.echo

    say(f"{len(files)} source file(s), top module {top!r}")
    try:
        got = simulate.icarus(
            files, top, work, list(defines), list(incdirs), timeout, run_dir
        )
    except simulate.SimulationError as e:
        raise click.ClickException(str(e)) from e
    say(f"simulated with Icarus Verilog in {got.seconds:.1f} s")
    # §12's header wants the simulation command, and nothing else in the tool
    # ever learns it. Written beside the waveform so `export` — run minutes or
    # days later, in another process — can put it in the report.
    _remember_command(got.dump, got.command)
    if got.injected:
        say("  no $dumpfile in your sources, so the whole design was dumped")
    for w in got.warnings[:3]:
        say(f"  {w.strip()}")

    ctx = _load(got.dump, tuple(rtl) or tuple(roots), top)
    say(f"waveform: {_rel(got.dump)} ({ctx.store.n_signals} signals)")
    if ctx.correlation is not None:
        say(
            f"correlation: {ctx.correlation.matched}/{ctx.correlation.total} signals "
            f"({ctx.correlation.percent}%)"
        )
    elif ctx.graph is None:
        say("no RTL graph: causal analysis is off; pass the RTL to enable it")

    report = ctx.check_report()

    if as_json:
        click.echo(json.dumps({"dump": str(got.dump), "top": top, **report.to_dict()}, indent=2))
    else:
        click.echo("")
        click.echo(_format_report(report, ctx.clock))
        _write_config_if_missing(base, top, files, got)
        click.echo("")
        click.echo(f"  veritrace serve {_rel(ctx.trace_path)} --rtl {_rel(rtl_hint)}")

    _gate(report, fail_on)
    if then_serve:
        # The real `serve` command, invoked rather than reimplemented, so the
        # banner, the browser and the RTL handling stay in one place.
        click_ctx.invoke(
            serve,
            traces=(ctx.trace_path,),
            rtl=tuple(rtl) or tuple(roots),
            top=top,
            browser=True,
        )


def _rel(path: Path) -> str:
    """A path as short as it can be said from here."""
    try:
        return Path(path).resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def _under(path: Path, root: Path) -> str:
    """`path` as said from `root` — the anchor every path in the config uses."""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(path).resolve().as_posix()


def _source_globs(files: list[Path], root: Path) -> list[Path]:
    """One glob per directory the sources came from.

    `**/*.sv` from the project root would sweep in `.veritrace/` and any vendor
    tree beside it; naming the directories that actually hold RTL keeps the next
    run reading the same files this one did.
    """
    dirs = sorted({f.resolve().parent for f in files})
    return [d / "*.sv" for d in dirs] + [d / "*.v" for d in dirs]


def _write_config_if_missing(base: Path, top: str, files: list[Path], got) -> None:
    """Keep `init`'s promise without asking for a second command (§13.4b).

    Written at the **project root as the user means it**: the working directory
    when the sources are under it — `veritrace run rtl/` is run from the project,
    not from `rtl` — and the source directory otherwise, which is the only
    sensible home for `veritrace run /somewhere/else`.

    That distinction is the whole promise. `config.find` searches *upward*, so a
    file left in `rtl/` after `veritrace run rtl/` is invisible from where the
    user is standing, and the next command answers "No trace given".

    Never overwrites: a project that already configured itself has decided
    things this cannot re-derive.
    """
    here = Path.cwd().resolve()
    root = here if base.resolve().is_relative_to(here) else base.resolve()
    out = root / ".veritrace.toml"
    if out.exists() or (base / ".veritrace.toml").exists():
        return
    clock = guess_clock(files) or "clk"
    reset = guess_reset(files) or ("rst_n", "low")
    # Every path in the file is resolved against the file's own directory, so
    # that is what they have to be relative to. Anchoring them on the working
    # directory instead is how `rtl/dump.vcd` became `rtl/rtl/dump.vcd`.
    config = build_config(
        top,
        [_under(d, root) for d in _source_globs(files, root)],
        clock,
        reset[0],
        reset[1],
        _under(got.dump, root),
    )
    try:
        with out.open("wb") as fh:
            tomli_w.dump(config, fh)
    except OSError:
        return  # a read-only tree is not a reason to fail a run
    click.echo(f"\nWrote {_rel(out)} - later commands need no arguments.")


@main.command()
@click.argument(
    "trace", type=click.Path(path_type=Path), required=False, default=None
)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--packs", default="", help="Restrict to these packs, by slug. Comma separated.")
@viewer_options
def txn(trace, query, rtl, top, as_json, packs, output, **flags):
    """Transactions extracted from the trace (§8.13-8.14).

    With no query, lists the interfaces that were detected and what each
    produced. With one, runs it:

    \b
      veritrace txn dump.vtx
      veritrace txn dump.vtx "txn(m0)"
      veritrace txn dump.vtx "txn(m0, type=WRITE, addr=0x0:0x4)"
      veritrace txn dump.vtx "txn(m0) | slowest(5)"
      veritrace txn dump.vtx "txn(m0) | open()"

    With a viewer flag, writes the interface's signals as a save file (§13.3).
    """
    from veritrace.analysis import vtq
    from veritrace.protocol import query as txn_query

    trace, query = _trace_and_query(trace, query)
    ctx = _load(trace, rtl, top)
    if packs:
        ctx.config.protocol_packs = [p.strip() for p in packs.split(",") if p.strip()]
    analysis = ctx.transactions()

    for err in analysis.errors:
        click.echo(f"  warning: {err}", err=True)

    if not query:
        if as_json:
            click.echo(json.dumps(analysis.to_dict(), indent=2))
            return
        click.echo(_format_interfaces(analysis, ctx.clock))
        return

    try:
        result = txn_query.run(analysis, vtq.parse_pipeline(query))
    except vtq.QueryError as e:
        raise click.ClickException(str(e)) from e

    if flags and any(flags.values()):
        ex = analysis.get(result.ifaces[0]) if result.ifaces else None
        paths = list(ex.interface.signals.values()) if ex else []
        markers = [
            viewers.Marker(t.start_time, t.label) for t in result.transactions[:16]
        ]
        if _emit_selection(ctx, paths, "Transactions", flags, output, markers=markers):
            return

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    click.echo(_format_transactions(result, ctx.clock))


def _format_interfaces(analysis, clock) -> str:
    if not analysis.extractions:
        return (
            "no protocol interfaces detected\n"
            "  A pack matches a scope only when every one of its required signals is\n"
            "  in the dump. Check that the interface is dumped, or write a pack for it\n"
            "  (see the packs/ directory of the installed package)."
        )
    out = [analysis.summary()]
    for ex in analysis.extractions:
        i = ex.interface
        out.append("")
        out.append(f"{i.name}   [{i.pack.name}]   {i.scope}" + (f"   prefix {i.prefix!r}" if i.prefix else ""))
        if i.aliases:
            out.append(f"    also seen as: {', '.join(i.aliases)}")
        out.append(f"    clock {i.clock or '-'}   reset {i.reset or '-'}   {ex.sampled_cycles} cycles sampled")
        kinds = {}
        for t in ex.transactions:
            kinds[t.kind] = kinds.get(t.kind, 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "none"
        rate = "-" if ex.correlation is None else f"{ex.correlation:.0f}%"
        # §8.14's acceptance criterion: the rate is always stated, never implied.
        out.append(f"    {summary}   ({ex.n_matched}/{ex.n_events} channel events correlated, {rate})")
        if ex.open_transactions:
            out.append(f"    {len(ex.open_transactions)} never completed")
        if ex.violations:
            out.append(f"    {len(ex.violations)} protocol violation(s)")
        if ex.parquet:
            out.append(f"    table: {ex.parquet}")
        for name, why_not in ex.skipped.items():
            out.append(f"    not run: {name} - {why_not}")
    return "\n".join(out)


def _format_transactions(result, clock) -> str:
    from veritrace import clocks as _clocks

    if not result.transactions:
        return f"no transactions matched (of {result.scanned} on {', '.join(result.ifaces) or 'no interface'})"
    out = [f"{len(result.transactions)} of {result.scanned} transaction(s)"]
    for t in result.transactions:
        span = (
            f"{_clocks.format_time(t.start_time, clock)}..{_clocks.format_time(t.end_time, clock)}"
            if t.closed
            else f"{_clocks.format_time(t.start_time, clock)}..OPEN"
        )
        fields = " ".join(
            f"{k}={v:#x}" if isinstance(v, int) and "addr" in k else f"{k}={v}"
            for k, v in t.fields.items()
            if v is not None
        )
        metrics = " ".join(f"{k}={v}" for k, v in t.metrics.items() if v is not None)
        out.append(f"  {t.ref:<24} {span:<20} {fields}")
        if metrics:
            out.append(f"      {metrics}")
        for v in t.violations:
            out.append(f"      ! {v.rule}: {v.msg}")
    return "\n".join(out)


@main.command()
@click.argument(
    "trace", type=click.Path(path_type=Path), required=False, default=None
)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def perf(trace, query, rtl, top, as_json):
    """Why it is slow, and why it stopped (§8.17, §8.18).

    With no query, the whole picture: stall attribution, latency, outstanding,
    fairness, and any deadlock, livelock or starvation found. With one, runs it:

    \b
      veritrace perf dump.vtx
      veritrace perf dump.vtx "stalls(m0)"
      veritrace perf dump.vtx "latency(m0, by=master)"
      veritrace perf dump.vtx "deadlock()"

    The wait-for graph needs RTL: naming who holds a wire is a question about
    the design, not about the dump. Without `--rtl` the blockages are still
    listed, and the report says why they have no holder.
    """
    from veritrace.analysis import vtq
    from veritrace.perf import query as perf_query

    trace, query = _trace_and_query(trace, query)
    ctx = _load(trace, rtl, top)
    report = ctx.measure()

    if not query:
        if as_json:
            click.echo(
                json.dumps(
                    {**report.to_dict(), "wait_for": [e.to_dict() for e in ctx.wait_for]},
                    indent=2,
                )
            )
            return
        click.echo(_format_performance(report, ctx.wait_for, ctx.clock))
        return

    try:
        got = perf_query.run(report, ctx.transactions(), ctx.wait_for, vtq.parse_pipeline(query))
    except vtq.QueryError as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(got, indent=2))


@main.command()
@click.argument(
    "trace", type=click.Path(path_type=Path), required=False, default=None
)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--chip",
    default=None,
    help="Timing file to check against, by name (see packs/timing/). Default: mt48lc16m16a2.",
)
def memory(trace, query, rtl, top, as_json, chip):
    """SDRAM/DDR command stream, bank state and timing checks (§8.20).

    With no query, the whole picture: decoded commands, per-bank timeline,
    every timing constraint checked, and the efficiency metrics. With one:

    \b
      veritrace memory dump.vtx
      veritrace memory dump.vtx "cmds(sdram)"
      veritrace memory dump.vtx "banks(sdram)"
      veritrace memory dump.vtx "timing(sdram, chip=mt48lc16m16a2)"
      veritrace memory dump.vtx "rowhits(sdram)"

    Needs no RTL: everything here is decoded from the command bus itself.
    """
    from veritrace.analysis import vtq
    from veritrace.memory import query as mem_query

    trace, query = _trace_and_query(trace, query)
    ctx = _load(trace, rtl, top)
    reports = ctx.memory_reports(chip)

    if not query:
        if as_json:
            click.echo(json.dumps([r.to_dict() for r in reports], indent=2))
            return
        click.echo(_format_memory(reports, ctx.clock))
        return

    root = getattr(ctx.config, "root", None)
    try:
        got = mem_query.run(reports, vtq.parse_pipeline(query), ctx.store, ctx.clock, root)
    except vtq.QueryError as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(got, indent=2))


def _format_memory(reports, clock) -> str:
    from veritrace import clocks as _clocks

    if not reports:
        return (
            "no memory interfaces detected\n"
            "  A memory pack matches a scope only when every signal it requires is in\n"
            "  the dump (cs_n, ras_n, cas_n, we_n, ba, a for SDR SDRAM). Check that the\n"
            "  command bus is dumped, or write a pack for your device."
        )

    out: list[str] = []
    for r in reports:
        out.append("")
        out.append(f"{r.iface}   [{r.chip}]   {r.n_banks} banks   {len(r.commands)} commands")

        counts: dict[str, int] = {}
        for c in r.commands:
            counts[c.name] = counts.get(c.name, 0) + 1
        if counts:
            out.append("    " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

        # §8.20's report format: violations by constraint, then what was clean.
        by_constraint: dict[str, list] = {}
        for v in r.violations:
            by_constraint.setdefault(v.constraint, []).append(v)
        for name, group in sorted(by_constraint.items()):
            word = "exceeded" if group[0].is_maximum else "violated"
            out.append(f"    ! {name} {word} {len(group)} time(s)")
            for v in group[:5]:
                where = f"bank {v.bank}  " if v.bank is not None else ""
                first = f"{v.first.name}@{_clocks.format_time(v.first.time, clock)}" if v.first else "?"
                second = f"{v.second.name}@{_clocks.format_time(v.second.time, clock)}" if v.second else "?"
                bound = "max" if v.is_maximum else "min"
                out.append(
                    f"        {_clocks.format_time(v.at, clock)}  {where}{first} -> {second}"
                    f"  ({v.measured_cycles} cycles, {bound} {v.limit_cycles})"
                )
            if len(group) > 5:
                out.append(f"        ... and {len(group) - 5} more")
        clean = sorted(k for k, n in r.checked.items() if n == 0)
        if clean:
            out.append(f"    ok  {', '.join(clean)}: conformant")

        e = r.efficiency
        if e.row_hits.total:
            rate = e.row_hits.hit_rate or 0.0
            out.append(
                f"    rows  {e.row_hits.hits} hit / {e.row_hits.misses} miss / "
                f"{e.row_hits.conflicts} conflict  ({rate * 100:.0f}% hit)"
            )
        if e.bus_utilization is not None:
            out.append(f"    bus utilization {e.bus_utilization * 100:.1f}%")
        if e.refresh_overhead:
            out.append(f"    refresh overhead {e.refresh_overhead * 100:.1f}%")
        if e.turnaround_events:
            out.append(
                f"    read/write turnaround {e.turnaround_cycles} cycles "
                f"over {e.turnaround_events} switches"
            )
        if e.bank_parallelism is not None:
            out.append(f"    bank parallelism {e.bank_parallelism:.2f} banks active on average")

        for name, why_not in r.skipped.items():
            out.append(f"    not checked: {name} - {why_not}")
    return "\n".join(out)


@main.command()
@click.argument(
    "trace", type=click.Path(path_type=Path), required=False, default=None
)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def track(trace, query, rtl, top, as_json):
    """Follow data through the design, and check it arrived (§8.19).

    With no query, the whole scoreboard: what was compared on each interface,
    which stretches of the path could be compared across two of them, and every
    mismatch with the byte lanes named. With one:

    \b
      veritrace track dump.vtx
      veritrace track dump.vtx "track(addr=0x10)"
      veritrace track dump.vtx "track(data=0xdeadbeef)"
      veritrace track dump.vtx "track(from=src, to=sink)"
      veritrace track dump.vtx "scoreboard(mem)"

    Needs no testbench code: the reference model is built from the write
    transactions the protocol packs already extract.
    """
    trace, query = _trace_and_query(trace, query)
    ctx = _load(trace, rtl, top)
    report = ctx.data_integrity()

    if not query:
        if as_json:
            click.echo(json.dumps(report.to_dict(), indent=2))
            return
        click.echo(_format_integrity(report, ctx.clock))
        return

    click.echo(json.dumps(_run_data_query(ctx, query), indent=2))


def _run_data_query(ctx: Context, query: str, coverage_path=None, source=None) -> dict:
    """Dispatch a §8.19/§8.21 query, computing only the half it asks for.

    `veritrace track` and `veritrace coverage` accept the same language (§10.1
    is one language), so both come through here — and neither pays for the
    other's analysis unless the query actually names it.
    """
    from veritrace.analysis import vtq
    from veritrace.coverage import query as cov_query

    try:
        pipeline = vtq.parse_pipeline(query)
        return cov_query.run(
            ctx.data_integrity() if pipeline.name in cov_query.INTEGRITY_COMMANDS else None,
            (
                ctx.coverage_report(coverage_path, source)
                if pipeline.name in cov_query.COVERAGE_COMMANDS
                else None
            ),
            pipeline,
        )
    except vtq.QueryError as e:
        raise click.ClickException(str(e)) from e


def _format_integrity(report, clock) -> str:
    from veritrace import clocks as _clocks

    out: list[str] = []
    if not report.interfaces:
        out.append("no interface carried data that could be tracked")
    for i in report.interfaces:
        out.append(
            f"{i.iface}   [{i.pack}]   {i.writes} write(s), {i.reads} read(s), "
            f"{i.compared} byte(s) compared"
        )
    for a, b, n in report.compared_paths:
        out.append(f"    path {a} -> {b}: {n} write(s) matched by address and order")

    if not report.mismatches:
        # §8.19's own standard: "nothing found" has to be a stated result, not
        # an empty screen that could equally mean nothing ran.
        if report.interfaces:
            out.append("")
            out.append("ok  every read matched what was written, on every path compared")
    else:
        out.append("")
        out.append(f"! {len(report.mismatches)} data mismatch(es)")
        for m in report.mismatches:
            addr = "" if m.addr is None else f"0x{m.addr:x}"
            out.append(
                f"    {_clocks.format_time(m.time, clock)}  {m.where}  {addr}"
                f"  byte(s) {m.lane_text}"
            )
            out.append(f"        {m.detail}")
            if m.source or m.victim:
                out.append(f"        {m.source or '?'} -> {m.victim or '?'}")
    for name, why_not in report.skipped.items():
        out.append(f"    not tracked: {name} - {why_not}")
    return "\n".join(out)


@main.command()
@click.argument(
    "trace", type=click.Path(path_type=Path), required=False, default=None
)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--coverage",
    "coverage_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Coverage database to import (verilator coverage.dat or xcrg XML). "
    "Found automatically next to the project when not given.",
)
@click.option(
    "--source",
    type=click.Choice(["verilator", "vivado"]),
    default=None,
    help="Which tool wrote it. Detected from the file when not given.",
)
def coverage(trace, query, rtl, top, as_json, coverage_path, source):
    """What was not tested — functional and code, in one place (§8.21, §8.12).

    The functional half is computed from the extracted transactions and needs
    no covergroup and no simulator feature. The code half is imported from
    Verilator or Vivado when a database is there.

    \b
      veritrace coverage dump.vtx
      veritrace coverage dump.vtx --coverage logs/coverage.dat
      veritrace coverage dump.vtx "fcov(cpu)"
      veritrace coverage dump.vtx "uncovered()" --rtl rtl/
    """
    trace, query = _trace_and_query(trace, query)
    ctx = _load(trace, rtl, top)
    report = ctx.coverage_report(coverage_path, source)

    if not query:
        if as_json:
            click.echo(json.dumps(report.to_dict(), indent=2))
            return
        click.echo(_format_coverage(report))
        return

    click.echo(json.dumps(_run_data_query(ctx, query, coverage_path, source), indent=2))


def _format_coverage(report) -> str:
    out: list[str] = []

    for f in report.functional:
        out.append("")
        score = "-" if f.score is None else f"{f.score * 100:.0f}%"
        auto = "  (automatic bins: the pack declares no [[cover]])" if f.automatic else ""
        out.append(f"{f.iface}   [{f.pack}]   {f.n_transactions} transactions   {score}{auto}")
        for p in f.points:
            if p.skipped:
                out.append(f"    {p.name:<22} not measured - {p.skipped}")
                continue
            out.append(f"    {p.name:<22} {p.shape:<9} {p.covered}/{p.total}")
            holes = p.holes()
            for key in holes[:6]:
                label = " x ".join(key)
                out.append(f"        never: {label}" + (f"   {p.msg}" if p.msg else ""))
            if len(holes) > 6:
                out.append(f"        ... and {len(holes) - 6} more empty cell(s)")

    code = report.code
    if code is not None:
        out.append("")
        score = "-" if code.score is None else f"{code.score * 100:.1f}%"
        out.append(f"code coverage from {code.source}: {code.covered}/{code.total} points {score}")
        if code.error:
            out.append(f"    {code.error}")
        for f in code.files:
            fscore = "-" if f.score is None else f"{f.score * 100:.0f}%"
            out.append(f"    {f.file:<40} {f.covered}/{len(f.points)}  {fscore}")

    if report.holes:
        out.append("")
        out.append(f"{len(report.holes)} uncovered point(s), with what would close them (§8.12)")
        for h in report.holes[:10]:
            out.append("")
            label = f' "{h.label}"' if h.label else ""
            out.append(f"    {h.file}:{h.line} [{h.kind}]{label}")
            if h.text:
                out.append(f"        {h.text}")
            if h.note:
                out.append(f"        {h.note}")
            for c in h.conditions:
                state = (
                    "not evaluated against this trace"
                    if c.held is None
                    else (f"{c.held}/{c.sampled} cycles" if c.held else "NEVER observed")
                )
                out.append(f"        {c.text:<34} {state}")
                for p in c.produced_by:
                    out.append(f"            <- {p}")

    for name, why_not in report.skipped.items():
        out.append(f"    {name}: {why_not}")
    return "\n".join(out) if out else "nothing to report"


def _bar(share: float, width: int = 28) -> str:
    """A share as a bar. Text, because §8.17's headline is a shape — which
    slice dominates — and a column of numbers hides that."""
    filled = int(round(share / 100.0 * width))
    return "#" * filled + "." * (width - filled)


def _format_performance(report, wait_for, clock) -> str:
    from veritrace import clocks as _clocks

    out: list[str] = []
    if not report.interfaces:
        out.append("no protocol interfaces detected, so there is nothing to measure")

    for per in report.interfaces:
        out.append("")
        out.append(per.iface)
        prof = per.stalls
        if prof is None:
            out.append(f"    no per-cycle profile - {per.notes.get('perf', 'unknown')}")
        else:
            shares = prof.shares()
            out.append(
                f"    {prof.total} cycles sampled, {prof.lost} lost "
                f"({100 * prof.lost / prof.total:.0f}%)"
            )
            for bucket in prof.buckets:
                n = prof.counts.get(bucket, 0)
                if not n:
                    continue
                out.append(f"      {bucket:<16} {shares[bucket]:>5.1f}%  {_bar(shares[bucket])}  {n}")
            # §8.17's claim, stated where it can be checked rather than trusted.
            out.append(f"      {'= total':<16} {sum(shares.values()):>5.1f}%")
            for name, why_not in prof.notes.items():
                out.append(f"      not evaluated: {name} - {why_not}")
        h = per.latency
        if h is not None and h.n:
            out.append(
                f"    latency  n={h.n}  min={h.min}  p50={h.p50}  p95={h.p95}  "
                f"p99={h.p99}  max={h.max}"
            )
        if per.outstanding_peak:
            plateau = "  (plateau - this is the limit)" if per.outstanding_plateau else ""
            out.append(f"    outstanding peak {per.outstanding_peak}{plateau}")
        if per.bytes_moved:
            out.append(f"    {per.bytes_moved} bytes moved")
        if per.burst_efficiency is not None:
            out.append(f"    burst efficiency {per.burst_efficiency:.2f}")

    fair = report.fairness
    if fair is not None and len(fair.agents) > 1:
        out.append("")
        out.append(f"fairness (Jain) {fair.index:.3f}")
        for a in fair.agents:
            out.append(
                f"    {a.agent:<20} requested {a.requested:>6}  granted {a.granted:>6}"
                + (f"  longest unserved run {a.starved}" if a.starved else "")
            )

    live = report.liveness
    out.append("")
    if not live:
        out.append("no deadlock, livelock or starvation found")
    for d in live.deadlocks:
        out.append(
            f"DEADLOCK at {_clocks.format_time(d.at, clock)}, persistent {d.cycles} cycles"
        )
        out.append("")
        width = max(len(e.agent) for e in d.edges)
        for e in d.edges:
            out.append(
                f"  {e.agent:<{width}}  waits on  {e.resource}  held by  {e.holder}"
            )
        out.append(f"{' ' * (width + 30)}`-- cycle")
        out.append("")
        out.append(
            f"  Cycle of {len(d.agents)} agents. First blocked: "
            f"{d.first_txn or d.first_blocked} at "
            f"{_clocks.format_time(d.first_txn_at, clock)}"
        )
        for e in d.edges:
            out.append(f"  [why] {e.to_dict()['why']}")
    for lv in live.livelocks:
        out.append(
            f"LIVELOCK  {lv.signal} changed {lv.toggles} times between "
            f"{len(lv.states)} states with nothing completing "
            f"({_clocks.format_time(lv.since, clock)}..{_clocks.format_time(lv.until, clock)})"
        )
    for s in live.starvation:
        out.append(
            f"STARVATION  {s.agent} asked for {s.cycles} cycles unserved while "
            f"{', '.join(s.served)} progressed"
        )
    for name, why_not in live.skipped.items():
        out.append(f"  not run: {name} - {why_not}")

    if wait_for and not live.deadlocks:
        out.append("")
        out.append(f"{len(wait_for)} persistent blockage(s), none of them circular:")
        for e in wait_for:
            held = f" held by {e.holder}" if e.holder else ""
            out.append(f"  {e.agent} waits on {e.resource}{held}  ({e.cycles} cycles)")
    return "\n".join(out)


def _format_report(report, clock, correlation=None) -> str:
    """The console form of a check report — reservations first, verdict after.

    Two things sit above the findings on purpose:

    * **The correlation rate** (§7.2). `check` reads the RTL, and RTL that does
      not match the dump produces confident findings about a different design —
      pointing `--rtl` at the wrong directory is a far commoner mistake than
      editing the sources after the run, and nothing else in the output says a
      word about it.
    * **What did not run** (P7). "no automatic findings" printed above a list
      of checks that never happened reads as a clean bill of health for a scan
      that mostly did not take place.
    """
    from veritrace import clocks as _clocks

    out: list[str] = []
    if correlation is not None:
        out.append(f"RTL: {correlation.summary()}")
        if correlation.percent < _FLOOR:
            out.append(
                f"  WARNING: below §7.2's {_FLOOR:g}%. These findings are about RTL "
                "that barely matches this dump — check --rtl points at the right "
                "sources, and see §4.0 for the simulator dump flags."
            )
        out.append("")

    # P7: a check that could not run says so; silence would read as a pass.
    if report.skipped:
        out.append(f"{len(report.skipped)} check(s) did not run:")
        out += [f"  {name} - {why_not}" for name, why_not in report.skipped.items()]
        out.append("")

    if not len(report):
        out.append(
            "no automatic findings from the checks that ran"
            if report.skipped
            else "no automatic findings"
        )
        return "\n".join(out)

    out.append(f"{len(report)} finding(s) in {report.elapsed_ms:.0f} ms")
    for group, findings in report.by_group().items():
        out.append("")
        out.append(f"{group.label} ({len(findings)})")
        for f in findings:
            at = f"  at {_clocks.format_time(f.time, clock)}" if f.time is not None else ""
            out.append(f"  {f.signal or '-'}   {f.title}{at}   {f.loc or ''}")
            for note in f.notes:
                out.append(f"      {note}")
    return "\n".join(out)


@main.command()
@click.argument("signal")
@rtl_options
@click.option("--trace", type=click.Path(exists=True, path_type=Path), default=None,
              help="Optional: widths and names come from the graph, so this is not required.")
@click.option("--depth", default=3, type=int, help="How many graph edges out.")
@click.option(
    "--format", "fmt", type=click.Choice(["vivado", "quartus"]), default="vivado",
    help="Vivado ILA or Quartus SignalTap.",
)
@click.option("--samples", default=1024, type=int, help="Capture depth.")
@click.option("--clock", "clock_path", default=None, help="Sampling clock (default: inferred).")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)
def probes(signal, rtl, top, trace, depth, fmt, samples, clock_path, output):
    """Turn a causal cone into an ILA or SignalTap configuration (§8.11b).

    The signals in the cone are exactly the ones worth probing, so this makes
    the tool useful *before* the bug, when the capture is being set up. Needs
    only the graph — no trace, no capture.
    """
    from veritrace.analysis import cone as cone_mod
    from veritrace.export import probes as probes_mod
    from veritrace.graph.elaborate import elaborate

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    if not files:
        raise click.ClickException(
            "This needs RTL. Pass --rtl <file|dir>, or set design.rtl in .veritrace.toml."
        )
    graph = elaborate(files, incdirs, defines, top).graph
    # No trace here by design, so there is nothing to suggest names from — but
    # a unique suffix still resolves against the graph.
    signal = resolve_signal(graph, signal)
    result = cone_mod.cone(graph, signal, depth=depth)

    plan = probes_mod.plan_from_cone(
        result,
        graph,
        clock=clock_path or probes_mod.guess_clock(graph, result.paths()),
        depth=samples,
    )
    text = probes_mod.render(fmt, plan)
    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"{len(plan.signals)} probe(s) -> {output}", err=True)
    else:
        click.echo(text, nl=False)


@main.command()
@click.argument("log", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--trace",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="The .vtx store. Defaults to trace.default in .veritrace.toml.",
)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@viewer_options
def triage(log, trace, rtl, top, as_json, output, **flags):
    """Turn a simulation log into root causes (§8.10b).

    The real entry point into a session: nobody starts by knowing which signal
    to query, they start with a log full of failures. Each failure becomes the
    question it implies, they run in parallel, and failures sharing a cause are
    grouped — because forty-seven failures look like a catastrophe and three
    causes look like a morning's work.
    """
    from veritrace.analysis import triage as triage_mod

    trace = _default_trace(trace)
    ctx = _load(trace, rtl, top, need_rtl=True)
    report = triage_mod.triage(
        triage_mod.read_log(log), ctx.store, ctx.graph, ctx.clock, ctx.config
    )

    if _emit_selection(
        ctx,
        list(triage_mod.failure_signals(report)),
        "Root causes",
        flags,
        output,
        markers=[viewers.Marker(c.time, c.signal) for c in report.causes[:4]],
    ):
        return
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    click.echo(triage_mod.format_report(report, ctx.clock))


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.argument("pattern", required=False, default=None)
@click.option("--limit", default=40, type=int, help="How many rows.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@viewer_options
def signals(trace, pattern, limit, as_json, output, **flags):
    """Find a signal's full path (§13's P6 — every feature in the terminal).

    \b
      veritrace signals full            # the trace from .veritrace.toml
      veritrace signals dump.vtx wr_ptr
      veritrace signals dump.vtx --gtkw -o all.gtkw

    Every other command wants a hierarchical path, and until now the terminal
    was the one surface with no way to find out what they are: the API has
    `/signals?q=`, the interface has ⌘P, and `veritrace why "why(full)"` simply
    said the signal was unknown. The ranking is the palette's, so the same typo
    suggests the same names in both places.

    Needs no RTL — this is a question about the dump.
    """
    from veritrace import TraceStore
    from veritrace import config as cfg
    from veritrace.api.search import search_signals

    trace, pattern = _trace_and_query(trace, pattern)
    # Deliberately not `_load`: this is a question about the dump, and going
    # through the usual path would elaborate whatever `design.rtl` names — a
    # multi-second wait for a lookup that has to feel like `grep`.
    path = _default_trace(trace, flag="a trace path")
    ctx = Context(
        store=TraceStore(str(path)),
        config=cfg.load() or cfg.load_or_empty(path.parent),
        trace_path=path,
    )
    rows = search_signals(ctx.store.signals(), pattern or "", limit=limit)
    total = ctx.store.n_signals

    if as_json:
        click.echo(json.dumps({"pattern": pattern or "", "total": total, "signals": rows}, indent=2))
        return
    if _emit_selection(ctx, [r["path"] for r in rows], pattern or "All signals", flags, output):
        return
    if not rows:
        click.echo(f"nothing in this trace matches {pattern!r} ({total} signals)")
        return
    click.echo(f"{len(rows)} of {total} signal(s)" + (f" matching {pattern!r}" if pattern else ""))
    for r in rows:
        width = f"[{r['msb']}:{r['lsb']}]" if r["msb"] is not None else f"{r['width']}b"
        click.echo(f"  {r['path']:<52} {width:>10}  {r['kind']:<10} {r['n_events']} event(s)")


def _trace_and_query(trace: Path | None, query: str | None) -> tuple[Path | None, str | None]:
    """Sort out `veritrace txn "txn(m0)"` from `veritrace txn dump.vtx`.

    Click cannot: with the trace optional (§4.3 — it is named once, in the
    config) both positionals are strings and the first one wins. A query is
    never a path that exists, and a trace never contains `(`, so the two are
    told apart by what they are rather than by where they sit.
    """
    if query is None and trace is not None and not Path(trace).exists():
        return None, str(trace)
    return trace, query

def _default_trace(trace: Path | None, flag: str = "--trace") -> Path:
    """`--trace`, else `trace.default` from `.veritrace.toml` (§4.3).

    §13.4's onboarding is `veritrace init && veritrace serve`, with nothing
    typed in between, so every command that needs a trace has to be able to
    find the one `init` recorded.
    """
    if trace is not None:
        return _ensure_store(trace)
    from veritrace import config as cfg

    conf = cfg.load()
    if conf is not None and conf.trace:
        recorded = (conf.root / conf.trace).resolve()
        # Prefer a store next to the recorded dump, then the dump itself —
        # which `_ensure_store` converts.
        for path in (recorded.with_name(recorded.name + ".vtx"), recorded):
            if path.exists():
                return _ensure_store(path)
    raise click.ClickException(
        f"No trace given. Pass {flag} <dump.vtx>, or set trace.default in .veritrace.toml."
    )


def _ensure_store(path: Path) -> Path:
    """A `.vtx` store for `path` — `store.ensure`, with the CLI's own voice."""
    from veritrace import store

    return store.ensure(path, lambda msg: click.echo(msg, err=True))


@main.command()
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output .vtx directory (default: alongside the dump, as <source>.vtx).",
)
def convert(source: Path, output: Path | None) -> None:
    """Convert a VCD or FST waveform into a .vtx store."""
    from veritrace import _native

    # §6.3 caches the store next to the dump so it can be reused.
    out = output or source.with_name(source.name + ".vtx")

    if source.suffix.lower() == ".fst" and not _native.has_fst_support():
        raise click.ClickException(
            "This build cannot read FST files. Rebuild with the `fst` feature "
            "(needs zlib and libclang), or dump VCD instead."
        )

    try:
        n_events = _native.convert(str(source), str(out))
    except ValueError as e:
        # A dump the engine cannot read is a message, not a stack trace: the
        # reader already says which half failed and what to do instead.
        raise click.ClickException(str(e)) from e
    store = _native.TraceStore(str(out))
    t0, t1 = store.time_range
    click.echo(
        f"{source} -> {out}\n"
        f"  {store.n_signals} signals, {n_events} events, "
        f"t={t0}..{t1} ({store.timescale})"
    )


@main.command()
def init() -> None:
    """Detect RTL files in the current directory and write .veritrace.toml."""
    cwd = Path.cwd()
    files = find_rtl_files(cwd)
    if not files:
        raise click.ClickException("No .sv/.v files found under the current directory.")

    click.echo(f"Found {len(files)} RTL file(s):")
    for f in files:
        click.echo(f"  {f.relative_to(cwd)}")

    top = confirm_or_override("top module", guess_top_module(files))
    clock = confirm_or_override("clock signal", guess_clock(files))

    reset_guess = guess_reset(files)
    reset_signal_guess = reset_guess[0] if reset_guess else None
    reset_active_guess = reset_guess[1] if reset_guess else "low"
    reset_signal = confirm_or_override("reset signal", reset_signal_guess)
    if reset_guess and reset_signal == reset_signal_guess:
        reset_active = reset_active_guess
    else:
        reset_active = click.prompt(
            "reset active level",
            type=click.Choice(["low", "high"]),
            default=reset_active_guess,
        )

    found_trace = find_trace(cwd)
    config = build_config(
        top, ["**/*.sv", "**/*.v"], clock, reset_signal, reset_active, found_trace
    )

    out_path = cwd / ".veritrace.toml"
    if out_path.exists() and not click.confirm(f"{out_path} already exists - overwrite?", default=False):
        raise click.ClickException("Aborted: .veritrace.toml already exists.")

    with out_path.open("wb") as fh:
        tomli_w.dump(config, fh)

    click.echo(f"Wrote {out_path}")

    # §13.4: the first impression has to be "the tool already knows something I
    # did not", not an empty config file. So if a dump is lying around, convert
    # it and run the checks right now.
    if found_trace is None:
        click.echo("\nNo waveform found yet. Run your simulation, then `veritrace serve`.")
        return
    click.echo(f"\nTrace found: {found_trace}")
    _first_look(cwd, found_trace, files, top)


def find_trace(root: Path) -> str | None:
    """The most recent dump under `root`, as a path relative to it."""
    candidates = [
        p
        for p in root.rglob("*")
        if p.suffix.lower() in (".vcd", ".fst") and p.is_file()
        and not any(d in EXCLUDED_DIRS or d.startswith(".") for d in p.relative_to(root).parts[:-1])
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.relative_to(root).as_posix()


def _first_look(cwd: Path, trace_rel: str, files: list[Path], top: str | None) -> None:
    """Convert the dump and print what the checks already found (§13.4)."""
    from veritrace import TraceStore, _native, clocks
    from veritrace import config as cfg
    from veritrace.analysis import checks as checks_mod

    source = cwd / trace_rel
    out = source.with_name(source.name + ".vtx")
    try:
        _native.convert(str(source), str(out))
        store = TraceStore(str(out))
    except Exception as e:  # noqa: BLE001 - a bad dump must not fail `init`
        click.echo(f"  could not read it: {e}")
        return
    click.echo(f"  converted to {out.relative_to(cwd).as_posix()} - {store.n_signals} signals")

    graph = elaboration = None
    try:
        from veritrace.correlate.resolver import correlate
        from veritrace.graph.elaborate import elaborate

        elaboration = elaborate(files, top=top)
        graph = elaboration.graph
        report = correlate(graph, {s.path: s.handle for s in store.signals()}, elaboration.aliases)
        click.echo(f"  correlation: {report.matched}/{report.total} signals ({report.percent}%)")
    except Exception as e:  # noqa: BLE001 - §7.4, a viewer is still useful
        click.echo(f"  could not elaborate the RTL: {e}")

    config = cfg.load_or_empty(cwd)
    clock = clocks.resolve(store, graph, config)
    findings = checks_mod.run_all(store, graph, elaboration, clock, config)
    if len(findings):
        click.echo(f"  {findings.summary()}")
        for f in findings.sorted()[:3]:
            click.echo(f"    ! {f.signal or '-'}  {f.title}   {f.loc or ''}")
        if len(findings) > 3:
            click.echo(f"    ... {len(findings) - 3} more in the Checks tab")
    else:
        click.echo("  no automatic findings")
    click.echo("\nRun `veritrace serve` to open it.")


def _minimise(ctx: "Context", result) -> object:
    """§8.2's subtrace of a causal answer, narrated."""
    from veritrace.repro import narrate, subtrace

    return narrate.narrate(subtrace.minimise(result.root, ctx.store, ctx.clock))


def _repro_work(ctx: "Context") -> Path:
    """Where a generated testbench and its build land — beside the trace."""
    base = ctx.trace_path.parent if ctx.trace_path else Path.cwd()
    return base / WORK_DIR / "repro"


def _rtl_sources(ctx: "Context", rtl: tuple[Path, ...], top: str | None) -> list[Path]:
    """Every RTL file behind this run, expanded the way the graph saw them."""
    from veritrace.graph.elaborate import discover

    files, _incdirs, _defines, _top = _resolve_rtl(rtl, top)
    out: list[Path] = []
    for f in files:
        out.extend(discover(f))
    return out


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.argument("query", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the testbench here instead of to stdout.",
)
@click.option(
    "--mode",
    type=click.Choice(["auto", "minimal", "focused"]),
    default="auto",
    help="Override §8.3's choice. `auto` lets the DUT decide, which is usually right.",
)
@click.option(
    "--validate/--no-validate",
    default=True,
    help="Compile and run the result, and report whether it reproduced (§8.3).",
)
@click.option("--timeout", default=120.0, help="Seconds before the validation run is given up on.")
def repro(trace, query, rtl, top, as_json, output, mode, validate, timeout):
    """Generate a testbench that reproduces a failure (§8.2, §8.3).

    \b
      veritrace repro dump.vcd "why(tb.dut.full)" -o tb_repro.sv
      veritrace repro --mode focused          # window cut, for a CPU

    For a control module the stimulus is minimised and the result is checked by
    running it. For a design with a program image inside it, §8.3 says the
    honest answer is a focused window rather than a minimisation, and that is
    what comes out — named accordingly.
    """
    from veritrace.repro import testbench

    trace, query = _trace_and_query(trace, query)
    query = _need_query("repro", query)
    ctx = _load(trace, rtl, top, need_rtl=True)
    _signal, _at, _headline, result = _ask(ctx, query)
    sub = _minimise(ctx, result)

    sources = _rtl_sources(ctx, rtl, top)
    try:
        built = testbench.build(
            sub,
            ctx.graph,
            ctx.store,
            ctx.clock,
            ctx.elaboration,
            root=result.root,
            sources=sources,
            work=_repro_work(ctx),
            validate_it=validate,
            timeout=timeout,
            mode=None if mode == "auto" else mode,
        )
    except testbench.ReproError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps({"query": query, **built.to_dict()}, indent=2))
        return

    if output:
        Path(output).write_text(built.code, encoding="utf-8")
    else:
        click.echo(built.code, nl=False)

    label = "minimal repro" if built.mode == "minimal" else "focused testbench"
    say = lambda text: click.echo(text, err=True)  # noqa: E731 - stdout is the code
    say("")
    say(f"{label}: {built.module} · {built.cycles} cycles · {built.events} stimulus event(s)")
    say(f"  {built.mode_reason}")
    if built.tied:
        say(f"  tied off as don't-care: {', '.join(built.tied)}")
    for note in built.notes:
        say(f"  {note}")
    v = built.validation
    if v.reproduced:
        say(f"  verified with {v.tool}: reproduces the failure in {built.cycles} cycles")
    elif v.ran:
        say(f"  {v.tool}: ran, but did not reproduce — {v.error}")
    elif v.error:
        say(f"  not validated: {v.error}")
    if output:
        say(f"  -> {output}")


@main.command("export")
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.option("--why", "query", required=True, help='The question, e.g. "top.ctrl.ready == 0 @ c1247".')
@rtl_options
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Where to write the report.",
)
@click.option(
    "--repro/--no-repro",
    "with_repro",
    default=True,
    help="Include the generated testbench, validated by running it (§8.3).",
)
def export_report(trace, query, rtl, top, output, with_repro):
    """Write a standalone HTML bug report (§12).

    \b
      veritrace export dump.vcd --why "why(top.ctrl.ready)" -o bug.html

    One file, no network: it opens the same on a machine that has never heard of
    this tool, which is the only form a bug report attached to a ticket can take.
    """
    from veritrace.api.sessions import sha256_files
    from veritrace.export import report as report_mod
    from veritrace.repro import testbench

    ctx = _load(_default_trace(trace, flag="a trace path"), rtl, top, need_rtl=True)
    _signal, _at, headline, result = _ask(ctx, query)
    sub = _minimise(ctx, result)

    sources = _rtl_sources(ctx, rtl, top)
    built = None
    if with_repro:
        try:
            built = testbench.build(
                sub,
                ctx.graph,
                ctx.store,
                ctx.clock,
                ctx.elaboration,
                root=result.root,
                sources=sources,
                work=_repro_work(ctx),
            )
        except testbench.ReproError as e:
            # §12 section 6 is one of seven. A design this tool cannot reduce
            # still deserves the other six.
            click.echo(f"  no testbench: {e}", err=True)

    page = report_mod.build(
        result.root,
        sub,
        ctx.store,
        ctx.clock,
        query=headline or query,
        sources={p.name: p for p in sources},
        repro=built,
        findings=ctx.check_report(),
        trace_path=ctx.trace_path,
        rtl_sha256=sha256_files(sources) if sources else None,
        top=getattr(ctx.graph, "top", "") or "",
        command=_recall_command(ctx.trace_path),
    )
    report_mod.write(page, Path(output))
    size = f"{page.bytes / 1024:.0f} KB"
    click.echo(f"{page.title} -> {output}  ({size})", err=True)
    if page.oversize:
        click.echo(
            f"  over §12's {report_mod.SIZE_TARGET // 1024} KB target. Nothing was dropped; "
            "a narrower question makes a smaller report.",
            err=True,
        )


@main.command()
@click.option(
    "--root",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Project to look in. Default: the one holding .veritrace.toml.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def plugins(root, as_json):
    """What analysis plugins are installed, and what could not load (§13.7).

    Looked for in `~/.veritrace/plugins/` and in `plugins/` beside your
    `.veritrace.toml`. No install step: a plugin is a file, versioned with the
    design. See docs/PLUGINS.md.
    """
    from veritrace import config as cfg
    from veritrace import plugin as plugin_mod

    base = root
    if base is None:
        conf = cfg.load()
        base = conf.root if conf is not None else Path.cwd()

    plugin_mod.clear()
    found, errors = plugin_mod.discover(base)
    paths = plugin_mod.search_path(base)

    if as_json:
        # P6: a project that vendors plugins wants to assert in CI that they all
        # loaded — `errors` empty is the assertion, and it needs a shape.
        click.echo(
            json.dumps(
                {
                    "searched": [str(p) for p in paths],
                    "plugins": [
                        {
                            "name": c.name,
                            "needs": list(c.needs),
                            "description": c.description,
                        }
                        for c in found
                    ],
                    "errors": dict(errors),
                },
                indent=2,
            )
        )
        return

    click.echo("looked in: " + (", ".join(str(p) for p in paths) or "nowhere — no plugin directory exists"))

    if found:
        click.echo(f"\n{len(found)} plugin(s):")
        for cls in found:
            needs = ", ".join(cls.needs) or "nothing"
            click.echo(f"  {cls.name:<24} needs {needs}")
            if cls.description:
                click.echo(f"    {cls.description}")
    else:
        click.echo("\nno plugins found")

    if errors:
        click.echo(f"\n{len(errors)} file(s) could not be loaded:")
        for name, why in errors.items():
            click.echo(f"  {name}: {why}")


@main.command()
@click.option("--root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Project to look in (default: the one you are standing in).")
@click.option("--trace", type=click.Path(path_type=Path), default=None,
              help="Also say which packs match this dump, and why the others do not.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def packs(root, trace, as_json):
    """What protocol packs are loaded, and what could not load (§8.15).

    \b
      veritrace packs
      veritrace packs --trace dump.vcd

    Looked for in `packs/` beside your `.veritrace.toml`, then the project root,
    then the built-ins — nearest first, so `packs/axi4.vtp.toml` shadows the
    built-in AXI4 pack by file name. See docs/PACKS.md.
    """
    from veritrace import config as cfg
    from veritrace.protocol import detect as detect_mod
    from veritrace.protocol import pack as pack_mod

    base = root
    if base is None:
        conf = cfg.load()
        base = conf.root if conf is not None else Path.cwd()

    errors: list[str] = []
    found = pack_mod.discover(base, errors)

    if as_json:
        # Same reason as `plugins --json`: "every pack I ship still loads, and
        # the bus I care about is still detected" is a CI assertion (P6).
        doc: dict = {
            "searched": [str(p) for p in pack_mod.search_path(base)],
            "packs": [
                {
                    "name": p.name,
                    "version": p.version,
                    "channels": len(p.channels),
                    "transactions": len(p.transactions),
                    "rules": len(p.rules),
                    "cover": len(p.cover),
                    "needs": list(p.detect.required_suffixes),
                }
                for p in sorted(found, key=lambda x: x.name.lower())
            ],
            "errors": list(errors),
        }
        if trace is not None:
            from veritrace import TraceStore
            from veritrace import store as store_mod

            store = TraceStore(str(store_mod.ensure(_default_trace(trace, flag="a trace path"))))
            matched = {i.pack.name: i for i in detect_mod.detect(store, found)}
            doc["matched"] = [
                {"pack": name, "interface": i.name, "scope": i.scope}
                for name, i in sorted(matched.items())
            ]
            doc["near_misses"] = [
                {"pack": p.name, "scope": where, "missing": list(missing)}
                for p in sorted(found, key=lambda x: x.name.lower())
                if p.name not in matched
                for where, missing in detect_mod.near_misses(store, p)
            ]
        click.echo(json.dumps(doc, indent=2))
        return

    click.echo("looked in: " + ", ".join(str(p) for p in pack_mod.search_path(base)))
    click.echo(f"\n{len(found)} pack(s):")
    for p in sorted(found, key=lambda x: x.name.lower()):
        parts = [f"{len(p.channels)} channel(s)", f"{len(p.transactions)} transaction(s)"]
        if p.rules:
            parts.append(f"{len(p.rules)} rule(s)")
        if p.cover:
            parts.append(f"{len(p.cover)} cover point(s)")
        click.echo(f"  {p.name:<16} {p.version:<6} {', '.join(parts)}")
        click.echo(f"    needs {', '.join(p.detect.required_suffixes)}")

    if trace is not None:
        from veritrace import TraceStore
        from veritrace import store as store_mod

        store = TraceStore(str(store_mod.ensure(_default_trace(trace, flag="a trace path"))))
        matched = {i.pack.name: i for i in detect_mod.detect(store, found)}
        click.echo("")
        for p in sorted(found, key=lambda x: x.name.lower()):
            hit = matched.get(p.name)
            if hit is not None:
                click.echo(f"  {p.name:<16} matched {hit.name} ({hit.scope})")
                continue
            # A pack that matches nothing looks the same as a protocol the design
            # does not speak. Naming the suffix that was missing is the whole
            # difference between "not this bus" and "your pack has a typo".
            near = detect_mod.near_misses(store, p)
            if not near:
                click.echo(f"  {p.name:<16} not in this trace")
            for where, missing in near:
                why = (
                    f"no {', '.join(missing)}"
                    if missing
                    else "complete, but a more specific pack claimed these wires"
                )
                click.echo(f"  {p.name:<16} {where}: {why}")

    if errors:
        click.echo(f"\n{len(errors)} pack(s) could not be loaded:")
        for e in errors:
            click.echo(f"  {e}")


@main.command("gen-sva")
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@rtl_options
@click.option("--pack", "pack_name", default=None, help="Pack to take the rules from.")
@click.option("--iface", default=None, help="Interface to bind the checker to.")
@click.option(
    "--target",
    type=click.Choice(["verilator", "portable"]),
    default="verilator",
    help="SVA subset, or plain Verilog for simulators that have none (§8.22).",
)
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None, help="Write here."
)
def gen_sva(trace, rtl, top, pack_name, iface, target, output):
    """Emit a protocol checker from a pack's rules (§8.22).

    \b
      veritrace gen-sva --iface tb.dut.m_axi --target verilator -o chk.sv
      veritrace gen-sva --iface tb.dut.m_axi --target portable  -o chk.sv

    §8.22: SVA support across the free stack is thin — Verilator takes a subset,
    ModelSim's free edition varies, Icarus has effectively none — so the same
    rules come out two ways. `portable` is plain Verilog and runs anywhere.

    The interface is detected from the trace, so the widths in the generated
    ports are the design's own rather than a guess.
    """
    from veritrace.export import sva

    ctx = _load(trace, rtl, top)
    analysis = ctx.transactions()
    if not analysis.extractions:
        raise click.ClickException(
            "no protocol interfaces were detected in this trace, so there is nothing "
            "to generate a checker for."
        )

    found = [e.interface for e in analysis.extractions]
    if iface:
        chosen = next((i for i in found if i.name == iface or i.scope == iface), None)
        if chosen is None:
            raise click.ClickException(
                f"no interface called {iface!r}. Found: "
                + ", ".join(f"{i.name} ({i.pack.name})" for i in found)
            )
    elif pack_name:
        chosen = next((i for i in found if i.pack.slug == pack_name), None)
        if chosen is None:
            raise click.ClickException(
                f"no interface uses the {pack_name!r} pack. Found: "
                + ", ".join(f"{i.name} ({i.pack.slug})" for i in found)
            )
    elif len(found) == 1:
        chosen = found[0]
    else:
        raise click.ClickException(
            "several interfaces were detected; name one with --iface: "
            + ", ".join(f"{i.name} ({i.pack.name})" for i in found)
        )

    widths = {}
    for suffix, path in chosen.signals.items():
        handle = ctx.store.find(path)
        if handle is not None:
            widths[suffix] = max(1, ctx.store.signal(handle).width)

    try:
        checker = sva.render(
            chosen.pack,
            iface=chosen.scope or chosen.name,
            target=target,
            widths=widths,
            clock=_leaf(chosen.clock) or "clk",
            reset=_leaf(chosen.reset) or "rst_n",
        )
    except sva.SvaError as e:
        raise click.ClickException(str(e)) from e

    if output:
        Path(output).write_text(checker.code, encoding="utf-8")
    else:
        click.echo(checker.code, nl=False)

    say = lambda t: click.echo(t, err=True)  # noqa: E731 - stdout is the code
    say(f"{checker.module} ({target}): {len(checker.emitted)} rule(s) from {chosen.pack.name}")
    for rule_id, why in checker.skipped.items():
        say(f"  skipped {rule_id}: {why}")
    if output:
        say(f"  -> {output}")


def _leaf(path: str | None) -> str | None:
    return path.rsplit(".", 1)[-1] if path else None


#: §8.7's alignment strategies. Spelled here rather than imported so that
#: building the CLI does not pull in the diff engine; the strings are the
#: contract either way, and `diff.align` refuses anything it does not know.
_ALIGN_STRATEGIES = ("cycle", "handshake", "retire", "manual")


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.argument("signal", required=False)
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--svg",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the state diagram as SVG (§8.8 step 4).",
)
def fsm(trace, signal, rtl, top, as_json, svg):
    """Extract state machines and check them statically (§8.8).

    \b
      veritrace fsm --rtl rtl/                 # every machine, and its findings
      veritrace fsm dump.vcd uut.lsu.state_q   # one machine, with the overlay

    **The trace is optional and the checks never use it.** §8.8's whole argument
    is that a dead state is invisible to a simulation whose stimulus never went
    there, so `--rtl` alone reports everything. A dump only adds the overlay:
    which states were visited and how often each edge was taken.
    """
    from veritrace.analysis import fsm as fsm_mod
    from veritrace.analysis import fsmchecks

    # `veritrace fsm --rtl src/ uut.lsu.state_q` names a machine and no trace.
    # Click cannot tell that from a dump, and this is the same problem `txn` and
    # `why` already have — one resolver, one rule: a path that does not exist is
    # not a path.
    trace, signal = _trace_and_query(trace, signal)
    if trace is None:
        # §7.4 in reverse: this is the one analysis that works with no trace at
        # all, so a missing dump is a supported mode rather than an error.
        files, incdirs, defines, top = _resolve_rtl(rtl, top)
        if not files:
            raise click.ClickException(
                "fsm needs RTL. Pass --rtl <file|dir>, or set design.rtl in .veritrace.toml."
            )
        from veritrace.graph.elaborate import elaborate

        el = elaborate(files, incdirs, defines, top)
        ctx = Context(store=None, config=None, elaboration=el, graph=el.graph)
    else:
        ctx = _load(trace, rtl, top, need_rtl=True)

    machines = fsm_mod.extract(ctx.graph, ctx.elaboration, ctx.store)
    if signal:
        one = fsm_mod.find(machines, signal)
        if one is None:
            raise click.ClickException(
                f"no state machine on {signal!r}. Found: "
                + (", ".join(m.signal for m in machines) or "none")
            )
        machines = [one]
    if ctx.store is not None:
        for m in machines:
            fsm_mod.overlay(m, ctx.store, ctx.graph, ctx.clock)

    findings = [
        f
        for m in machines
        for f in _fsm_findings(m, ctx.graph, fsmchecks)
    ]
    if svg:
        from veritrace.export import fsmsvg

        Path(svg).write_text(fsmsvg.render(machines[0]), encoding="utf-8")
        click.echo(f"{machines[0].signal} -> {svg}", err=True)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "machines": [m.to_dict() for m in machines],
                    "findings": [f.to_dict() for f in findings],
                },
                indent=2,
            )
        )
        return
    click.echo(_format_fsm(machines, findings, ctx.store is not None))


def _fsm_findings(machine, graph, fsmchecks):
    yield from fsmchecks.dead_states(machine)
    yield from fsmchecks.unreachable_states(machine)
    yield from fsmchecks.impossible_transitions(machine)
    yield from fsmchecks.incomplete_guards(machine)
    yield from fsmchecks.incomplete_reset(machine, graph)


def _format_fsm(machines, findings, has_trace: bool) -> str:
    out: list[str] = []
    if not machines:
        return (
            "No state machines found.\n"
            "  §8.8 looks for a register assigned in an always_ff and compared against\n"
            "  constants in its own guards. A counter is not one, whatever it is called."
        )
    for m in machines:
        out.append(f"{m.signal}   {len(m.states)} states, {len(m.transitions)} transitions")
        out.append(f"  {m.decl_loc or ''}   [{m.why_candidate}]")
        for s in m.states:
            marks = []
            if s.is_reset:
                marks.append("reset")
            if has_trace:
                seen = m.visits.get(s.value, 0)
                marks.append(f"{seen} visit(s)" if seen else "NEVER VISITED")
                if m.cycles_in.get(s.value):
                    marks.append(f"{m.cycles_in[s.value]} cycles")
            out.append(f"    {s.name:<14} {'  '.join(marks)}")
        for t in m.transitions:
            src = "any" if t.src is None else m.name_of(t.src)
            count = ""
            if has_trace and t.src is not None:
                n = m.taken.get((t.src, t.dst), 0)
                count = f"   {n}x" if n else "   never taken"
            out.append(f"    {src:>14} -> {m.name_of(t.dst):<14} [{t.guard}]{count}")
        out.append("")

    if findings:
        out.append(f"{len(findings)} finding(s), from the RTL alone:")
        for f in sorted(findings, key=lambda f: f.sort_key):
            out.append(f"  [{f.severity.label}] {f.check}   {f.title}")
            out.append(f"      {f.loc or ''}")
    else:
        out.append("No static findings.")
    return "\n".join(out)


@main.command()
@click.argument("trace_a", type=click.Path(exists=True, path_type=Path))
@click.argument("trace_b", type=click.Path(exists=True, path_type=Path))
@rtl_options
@click.option(
    "--align",
    "strategy",
    type=click.Choice(list(_ALIGN_STRATEGIES)),
    default="cycle",
    help="What to align on (§8.7). Clock edges unless the latencies differ.",
)
@click.option(
    "--anchor",
    default=None,
    help="Signal whose rising edges are the anchors, for --align retire.",
)
@click.option(
    "--ignore",
    multiple=True,
    help="Glob of signals to leave out. Repeatable — counters and timestamps "
    "legitimately differ.",
)
@click.option("--limit", default=12, type=int, help="How many divergences to list.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def diff(trace_a, trace_b, rtl, top, strategy, anchor, ignore, limit, as_json):
    """Where two runs part company (§8.7).

    \b
      veritrace diff good.vcd bad.vcd --rtl src/
      veritrace diff a.vcd b.vcd --align handshake --ignore "*cycle_count*"

    Timescales are normalised before anything is compared, and the alignment is
    on anchor events rather than on absolute time — so the cycle it reports means
    the same moment in both runs.
    """
    from veritrace import diff as diff_mod

    ctx_a = _load(trace_a, rtl, top)
    ctx_b = _load(trace_b, rtl, top)
    try:
        side_a = diff_mod.side(trace_a.name, ctx_a.store, ctx_a.clock)
        side_b = diff_mod.side(trace_b.name, ctx_b.store, ctx_b.clock)
        alignment = diff_mod.align(
            side_a,
            side_b,
            strategy,
            protocol_a=ctx_a.transactions() if strategy == "handshake" else None,
            protocol_b=ctx_b.transactions() if strategy == "handshake" else None,
            signal=anchor,
        )
    except diff_mod.AlignError as e:
        raise click.ClickException(str(e)) from e

    patterns = list(ignore) + list(getattr(ctx_a.config, "ignore", []) or [])
    report = diff_mod.compare(alignment, patterns)
    report.txn_divergences = diff_mod.compare_transactions(
        alignment, ctx_a.transactions(), ctx_b.transactions()
    )
    if report.first is not None and ctx_a.graph is not None:
        diff_mod.explain(report, ctx_a.graph, ctx_b.graph)

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    click.echo(_format_diff(report, limit))


def _format_diff(report, limit: int) -> str:
    al = report.alignment
    out = [
        f"aligned on {al.strategy}: {al.matched} anchor(s) matched "
        f"({al.counts[0]} vs {al.counts[1]})",
        f"timescale: {al.a.fs} fs vs {al.b.fs} fs per tick — normalised before comparing",
    ]
    if al.note:
        out.append(f"  note: {al.note}")
    out.append(f"{report.compared} signal(s) compared, {len(report.ignored)} ignored")
    if report.only_a or report.only_b:
        out.append(
            f"  {len(report.only_a)} only in the first run, {len(report.only_b)} only in the second"
        )

    if not report.divergences:
        out.append("\nThe two runs agree on every signal they share.")
    else:
        first = report.first
        out.append(f"\nFIRST DIVERGENCE at c{first.at}")
        out.append(f"  {first.signal}   {first.value_a}  vs  {first.value_b}")
        if first.detail:
            out.append(f"  {first.detail}")
        out.append(f"\n{len(report.divergences)} diverging signal(s), earliest first:")
        for d in report.divergences[:limit]:
            out.append(f"  c{d.at:<8} {d.signal:<44} {d.value_a}  vs  {d.value_b}")
        if len(report.divergences) > limit:
            out.append(f"  ... {len(report.divergences) - limit} more")

    if report.txn_divergences:
        out.append("\nFIRST DIVERGING TRANSACTION per interface:")
        for d in report.txn_divergences[:limit]:
            out.append(f"  c{d.at:<8} {d.ref:<24} {d.value_a}  vs  {d.value_b}")
            if d.detail:
                out.append(f"             {d.detail}")

    if report.first_differing is not None:
        out.append(
            f"\nThe two causal chains agree for {report.first_differing} step(s) "
            "and part at the next one."
        )
    elif report.why_error:
        out.append(f"\nno side-by-side chain: {report.why_error}")
    elif report.why_a is not None:
        out.append("\nThe two causal chains are identical — the cause is upstream of both.")
    return "\n".join(out)


# --- §8.28 mutation testing ------------------------------------------------


@main.command()
@rtl_options
@click.option(
    "--run",
    "command",
    default=None,
    help="Your suite, e.g. \"make sim-verilator\". A non-zero exit kills the mutant. "
    "Without it, the sources are built and run with Icarus.",
)
@click.option("--tb", "tb", multiple=True, type=click.Path(path_type=Path),
              help="Testbench file(s) for the built-in Icarus suite. Repeatable.")
@click.option("--sample", default=200, type=int, help="How many mutants (0 = every one).")
@click.option("--seed", default=0, type=int, help="Sampling seed — same seed, same mutants.")
@click.option("--operator", "operators", multiple=True,
              help="Restrict to these operators. Repeatable.")
@click.option("--jobs", "-j", default=0, type=int, help="Parallel mutants (default: CPUs).")
@click.option("--timeout", default=120.0, type=float, help="Seconds one mutant may take.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def mutate(rtl, top, command, tb, sample, seed, operators, jobs, timeout, as_json):
    """How good your tests are, not how much they ran (§8.28).

    \b
      veritrace mutate --rtl designs/mutation --top tb_fifo
      veritrace mutate --rtl rtl/ --run "make sim-verilator" --sample 200

    A mutant the suite does not notice is a line of the design nothing checks.
    The survivors are the report; the score is the headline.
    """
    import os

    from veritrace.mutate import OPERATORS
    from veritrace.mutate.run import Suite, run as run_mutants

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    if not files:
        raise click.ClickException("Nothing to mutate. Pass --rtl <file|dir>.")
    unknown = sorted(set(operators) - set(OPERATORS))
    if unknown:
        raise click.ClickException(
            f"unknown operator(s): {', '.join(unknown)}; try {', '.join(OPERATORS)}"
        )

    # The testbench is not mutated: §8.28 asks whether the tests notice a change
    # in the *design*, and a mutated testbench answers a different question.
    benches = [Path(t).resolve() for t in tb]
    design = [f for f in files if f.resolve() not in benches]
    if not benches:
        benches = [f for f in files if f.name.startswith("tb_") or "_tb" in f.stem]
        design = [f for f in design if f not in benches]
    if not command and not top:
        raise click.ClickException(
            "The built-in suite needs a top module: pass --top, or --run with your own command."
        )

    root = Path(os.path.commonpath([str(f.parent.resolve()) for f in files]))
    suite = Suite(
        command=command or "",
        sources=tuple(str(f.resolve().relative_to(root)) for f in (*design, *benches)),
        top=top or "",
        timeout=timeout,
    )
    report = run_mutants(
        design, suite, root / WORK_DIR / "mutants", root,
        sample=sample, seed=seed, operators=set(operators) or None,
        jobs=jobs or (os.cpu_count() or 4),
    )
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    click.echo(_format_mutation(report))


def _format_mutation(report) -> str:
    pct = "n/a" if report.score is None else f"{round(100 * report.score)}%"
    out = [
        f"Mutation score: {pct} ({report.killed}/{report.scored})",
        f"  {report.sampled} of {report.total_sites} sites, seed {report.seed}, "
        f"{report.elapsed_s:.1f}s · {report.command}",
    ]
    if report.invalid:
        out.append(
            f"  {len(report.invalid)} mutant(s) did not build and are not scored — "
            "a broken mutant tests the compiler, not the tests."
        )
    if not report.survivors:
        out.append("\nNo survivors: every mutation the suite was shown, it caught.")
        return "\n".join(out)

    out.append("\nSurvivors grouped by file:")
    for path, group in report.by_file().items():
        out.append(f"  {path}")
        for s in group:
            m = s.mutation
            out.append(f"    :{m.line:<5} {m.operator:11} {_clip(m.was)}  ->  {_clip(m.now)}")
            if m.context:
                out.append(f"           {_clip(m.context, 74)}")
    out.append(
        "\nEach one is a change to the design that nothing observed — a test to write, "
        "not a line to run."
    )
    return "\n".join(out)


def _clip(text: str, width: int = 30) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


# --- §8.29 RTL vs post-synthesis -------------------------------------------


@main.command("synth-diff")
@rtl_options
@click.option("--tb", "tb", multiple=True, required=True, type=click.Path(path_type=Path),
              help="Testbench file(s). The same ones drive both runs. Repeatable.")
@click.option("--dut", default=None, help="Instance to synthesise (default: the top's only child).")
@click.option("--rtl-trace", type=click.Path(path_type=Path), default=None,
              help="An RTL waveform you already have, instead of re-simulating.")
@click.option("--limit", default=12, type=int, help="How many divergences to list.")
@click.option("--timeout", default=300.0, type=float, help="Seconds per simulation.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def synth_diff(rtl, top, tb, dut, rtl_trace, limit, timeout, as_json):
    """Prove a sim/synth mismatch instead of suspecting one (§8.29).

    \b
      veritrace synth-diff --rtl rtl/ --tb tb.sv --top tb_top

    Synthesises with Yosys, simulates the netlist with the *same* testbench, and
    runs §8.7's diff on the DUT's top-level ports. A divergence here is a
    mismatch demonstrated, not a pattern matched.
    """
    from veritrace import tools
    from veritrace.graph.elaborate import elaborate
    from veritrace.synth import diff as synth_mod
    from veritrace.synth.yosys import dut_of

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    benches = [Path(t).resolve() for t in tb]
    design = [f for f in files if f.resolve() not in benches]
    if not design:
        raise click.ClickException("No RTL to synthesise. Pass --rtl <file|dir>.")
    if not top:
        raise click.ClickException("synth-diff needs the testbench's top module: pass --top.")

    try:
        elaboration = elaborate([*design, *benches], incdirs, defines, top)
        instance = dut_of(elaboration, top, dut)
        report = synth_mod.run(
            design, benches, top, instance,
            Path(WORK_DIR) / "synth", incdirs, defines,
            rtl_trace=rtl_trace, limit=limit, timeout=timeout,
        )
    except tools.ToolError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    click.echo(_format_synth(report, instance))


def _format_synth(report, dut) -> str:
    out = [
        f"{report.synthesiser} · {dut.module} at {dut.path}",
        f"netlist: {report.netlist}",
        f"{report.report.compared} top-level port(s) compared "
        f"({report.rtl_trace.name} vs {report.gate_trace.name}), {report.elapsed_s:.1f}s",
    ]
    if report.matched:
        out.append(
            "\nRTL and netlist agree on every port. No sim/synth mismatch is "
            "demonstrated by this stimulus."
        )
        return "\n".join(out)
    first = report.report.divergences[0]
    out.append(f"\nFIRST DIVERGENCE at c{first.at}")
    out.append(f"  {first.signal}   rtl = {first.value_a}   gate = {first.value_b}")
    if first.detail:
        out.append(f"  {first.detail}")
    if len(report.report.divergences) > 1:
        out.append(f"\n{len(report.report.divergences)} diverging port(s), earliest first:")
        for d in report.report.divergences:
            out.append(f"  c{d.at:<8} {d.signal:<28} {d.value_a}  vs  {d.value_b}")
    out.append(
        "\nThe two runs used the same testbench, so this is a mismatch demonstrated "
        "rather than a pattern matched (§8.29)."
    )
    return "\n".join(out)


# --- §8.27 formal, §8.35 reachability --------------------------------------


@main.command()
@rtl_options
@click.option("--pack", "packs", multiple=True, help="Protocol pack(s) to prove. Default: all.")
@click.option("--iface", default=None, help="Interface to prove (default: every one found).")
@click.option("--mode", type=click.Choice(["bmc", "prove", "cover"]), default="bmc",
              help="bmc searches to --depth; prove attempts k-induction as well.")
@click.option("--depth", default=20, type=int, help="How many cycles to search.")
@click.option("--engine", default="smtbmc z3", help="SymbiYosys engine line.")
@click.option("--reset-active-high", is_flag=True, help="The reset asserts high, not low.")
@click.option("--no-why", is_flag=True, help="Do not open a counterexample and explain it.")
@click.option("--timeout", default=900.0, type=float, help="Seconds the solver may take.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def formal(rtl, top, packs, iface, mode, depth, engine, reset_active_high, no_why, timeout, as_json):
    """Prove a pack's rules, not just observe them (§8.27).

    \b
      veritrace formal --rtl rtl/ --pack axi4lite --depth 20
      veritrace formal --rtl rtl/ --iface top.dma.m_axi --mode prove

    §8.14 says a rule held in one run. This says no run of up to `--depth`
    cycles breaks it — and when one does, the counterexample is opened and
    why-traced without leaving the terminal.

    Results are always reported with the depth that produced them. Bounded model
    checking does not prove anything about cycle N+1, and saying so would be the
    one dishonest thing this command could do.
    """
    from veritrace import tools
    from veritrace.formal import harness
    from veritrace.formal.prove import prove as run_prove
    from veritrace.graph.elaborate import elaborate
    from veritrace.protocol import pack as pack_mod

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    if not files:
        raise click.ClickException("This needs RTL. Pass --rtl <file|dir>.")
    # A testbench is not part of the design under proof: it is not synthesisable,
    # and handing it to yosys makes the whole run come back "unknown" — a result
    # that looks like a failed proof rather than like a build that never started.
    # Same rule `mutate` uses for what it will not mutate.
    design = [f for f in files if not (f.name.startswith("tb_") or f.stem.endswith("_tb"))]
    try:
        elaboration = elaborate(files, incdirs, defines, top)
        found = harness.interfaces(elaboration, pack_mod.resolve(list(packs)))
    except Exception as e:  # noqa: BLE001 - a pack or an RTL problem, both worth saying
        raise click.ClickException(str(e)) from e

    chosen = [i for i in found if iface in (None, i.name, i.scope)]
    if not chosen:
        names = ", ".join(i.name for i in found) or "none"
        raise click.ClickException(
            f"no interface to prove{f' called {iface}' if iface else ''}; found: {names}"
        )

    work = Path(WORK_DIR) / "formal"
    reports = []
    for target in chosen:
        try:
            reports.append(
                run_prove(
                    elaboration, target, design or files, work / _slug(target.name),
                    mode, depth, engine, timeout, not reset_active_high,
                )
            )
        except tools.ToolError as e:
            raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps([r.to_dict() for r in reports], indent=2))
        return
    for report in reports:
        click.echo(_format_formal(report))
        if not no_why and report.counterexample is not None:
            _explain_counterexample(report)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _format_formal(report) -> str:
    out = [
        f"{report.pack} on {report.iface} — {report.mode}, depth {report.depth}, "
        f"{report.engine}  ({report.elapsed_s:.1f}s)",
        "",
    ]
    for p in report.properties:
        out.append(f"  {p}")
        if p.text and p.verdict.value != "unknown":
            out.append(f"      {p.text}")
    if report.failed:
        out.append(f"\n{len(report.failed)} propert(ies) broken. Counterexample: {report.counterexample}")
    elif report.held:
        out.append(
            f"\n{len(report.held)} propert(ies) held to depth {report.depth}. "
            "That is not a proof for every depth — bounded model checking cannot make one."
        )
    return "\n".join(out)


def _explain_counterexample(report) -> None:
    """§8.27's last step: open the trace the solver produced, and ask why.

    This is the part that makes the feature worth having. A counterexample is a
    waveform like any other, so the causal engine works on it unchanged — the
    only thing needed is to point it at the generated harness, which is the
    design the trace is of.
    """
    from veritrace.analysis.whytrace import root_cause

    broken = report.failed[0]
    click.echo(f"\n--- {broken.id}: the counterexample, explained ---")
    sources = sorted(Path(report.work).glob("*.sv"))
    try:
        ctx = _load(broken.trace, tuple(sources), "vt_formal_top", need_rtl=True)
    except click.ClickException as e:
        click.echo(f"  could not open the counterexample: {e}", err=True)
        return

    subject = _subject_of(ctx, broken)
    if subject is None:
        click.echo("  no signal in this rule is in the counterexample; nothing to trace.")
        return
    try:
        signal, at, _, result = _ask(ctx, f"why({subject})")
    except click.ClickException as e:
        click.echo(f"  {e}", err=True)
        return
    click.echo(f"  why({subject}) at the step the assertion broke:")
    _print_chain(result.root, ctx.clock)
    cause = root_cause(result.root)
    if cause is not None:
        click.echo(f"\n  root cause: {cause.signal.path()} = {cause.value}")


def _subject_of(ctx: "Context", broken) -> str | None:
    """The signal to ask why about, confirmed against the counterexample.

    The pack already decided which signal the rule is about (`Property.subject`).
    This only checks it survived into the trace and into the elaborated harness —
    a solver writes witness wires of its own into the VCD, and picking one of
    those would produce a chain about the solver rather than about the design.
    """
    if broken.subject and ctx.graph.get(broken.subject) is not None:
        return broken.subject
    for sig in ctx.store.signals():
        if ".u_dut." in sig.path and "_witness_" not in sig.path:
            if ctx.graph.get(sig.path) is not None:
                return sig.path
    return None


@main.command()
@rtl_options
@click.option("--uncovered", type=click.Path(exists=True, path_type=Path), required=True,
              help="Coverage report from `veritrace coverage --json`.")
@click.option("--dut", default=None, help="Instance to check (default: --top).")
@click.option("--depth", default=30, type=int, help="How many cycles to search.")
@click.option("--engine", default="smtbmc z3", help="SymbiYosys engine line.")
@click.option("--limit", default=25, type=int, help="How many holes to classify.")
@click.option("--timeout", default=900.0, type=float, help="Seconds the solver may take.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def reach(rtl, top, uncovered, dut, depth, engine, limit, timeout, as_json):
    """Is this coverage hole reachable, or is it dead code (§8.35)?

    \b
      veritrace coverage dump.vcd --rtl rtl/ --json > cov.json
      veritrace reach --rtl rtl/ --uncovered cov.json --depth 30

    §8.12 says what it would take to reach an uncovered point. This says whether
    anything can: a reachable hole is a test worth writing, an unreachable one is
    dead code, and telling them apart is the difference between "81% and I do not
    know what to do with the rest" and "81%, and three of the rest cannot happen".
    """
    from veritrace import tools
    from veritrace.formal.prove import reach as run_reach
    from veritrace.graph.elaborate import elaborate

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    if not files:
        raise click.ClickException("This needs RTL. Pass --rtl <file|dir>.")
    if not top:
        raise click.ClickException("reach needs the module to check: pass --top.")

    holes = _holes_from(uncovered)[:limit]
    if not holes:
        raise click.ClickException(f"{uncovered} lists no uncovered points.")

    try:
        elaboration = elaborate(files, incdirs, defines, top)
        results = run_reach(
            elaboration, holes, files, dut or top,
            Path(WORK_DIR) / "reach", depth, engine, timeout,
        )
    except tools.ToolError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps([r.to_dict() for r in results], indent=2))
        return
    click.echo(_format_reach(results, depth))


@dataclass(slots=True)
class _Hole:
    """A coverage hole read back from JSON, in the shape `formal.reach` wants."""

    file: str
    line: int
    label: str = ""
    note: str = ""
    conditions: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Cond:
    text: str


def _holes_from(path: Path) -> list[_Hole]:
    """The `holes` array of a coverage report, whoever produced it.

    Reading the JSON rather than recomputing coverage keeps §8.35 a step in a
    pipeline — `veritrace coverage --json | veritrace reach` — instead of a
    second implementation of §8.12 that could disagree with the first.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("holes", data) if isinstance(data, dict) else data
    out: list[_Hole] = []
    for h in raw:
        out.append(
            _Hole(
                file=h.get("file", ""),
                line=int(h.get("line", 0)),
                label=h.get("label", "") or "",
                note=h.get("note", "") or "",
                conditions=[_Cond(c["text"]) for c in h.get("conditions", []) if c.get("text")],
            )
        )
    return out


def _format_reach(results, depth: int) -> str:
    counts = {"reachable": 0, "unreachable": 0, "unknown": 0}
    out = []
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        out.append(f"{r.file}:{r.line}  {r.label or 'branch'} \"{_clip(r.condition, 46)}\"")
        if r.status == "reachable":
            out.append(f"  -> REACHABLE (formal), counterexample at depth {r.step}")
            if r.trace:
                out.append(f"     {r.trace}")
            out.append("     a test can close this one, and stimgen can target it")
        elif r.status == "unreachable":
            out.append(f"  -> PROVED UNREACHABLE (depth={depth}) — dead code, not a missing test")
            out.append("     remove the branch, or relax the condition that guards it")
        else:
            out.append(f"  -> UNKNOWN — {r.reason}")
        out.append("")
    out.append(
        f"{counts['reachable']} reachable · {counts['unreachable']} unreachable "
        f"· {counts['unknown']} unclassified, searched to depth {depth}"
    )
    return "\n".join(out)


# --- §8.32 stimgen, §8.31 SAIF, §8.33 WaveDrom, §8.30 timing ---------------


@main.command()
@rtl_options
@click.option("--pack", "packs", multiple=True, help="Pack to generate for. Default: all detected.")
@click.option("--iface", default=None, help="Interface to drive (default: the only one found).")
@click.option("--n", default=100, type=int, help="How many transactions.")
@click.option("--seed", default=0, type=int, help="Same seed, same stimulus.")
@click.option("--cover-holes", type=click.Path(exists=True, path_type=Path), default=None,
              help="`veritrace coverage --json` output. Every never-hit bin is targeted.")
@click.option("--target", type=click.Choice(list(_STIM_TARGETS)), default="sv",
              help="cocotb runs under any free simulator; sv needs none.")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Where to write it (default: stdout).")
@click.option("--json", "as_json", is_flag=True, help="The plan, not the testbench.")
def stimgen(rtl, top, packs, iface, n, seed, cover_holes, target, output, as_json):
    """Legal transactions, aimed at the holes you have left (§8.32).

    \b
      veritrace coverage dump.vcd --rtl rtl/ --json > fcov.json
      veritrace stimgen --rtl rtl/ --top axil_slave --cover-holes fcov.json -o tb.sv

    The pack knows the field domains, so the transactions are legal by
    construction; `--cover-holes` makes the first few of them aim at bins nothing
    has reached yet. A hole no stimulus can close — a response field the DUT
    drives — is reported as such instead of generating traffic that will not work.
    """
    from veritrace.formal import harness
    from veritrace.graph.elaborate import elaborate
    from veritrace.protocol import pack as pack_mod
    from veritrace.stim import generate, holes_from, render

    files, incdirs, defines, top = _resolve_rtl(rtl, top)
    if not files:
        raise click.ClickException("This needs RTL. Pass --rtl <file|dir>.")
    if not top:
        raise click.ClickException("stimgen needs the module to drive: pass --top.")

    elaboration = elaborate(files, incdirs, defines, top)
    found = harness.interfaces(elaboration, pack_mod.resolve(list(packs)))
    chosen = [i for i in found if iface in (None, i.name, i.scope)]
    if not chosen:
        names = ", ".join(i.name for i in found) or "none"
        raise click.ClickException(f"no interface to drive; found: {names}")
    target_iface = chosen[0]

    holes: list[tuple[str, str]] = []
    if cover_holes:
        holes = holes_from(json.loads(Path(cover_holes).read_text(encoding="utf-8")))

    dut = harness.instance_of(elaboration, top)
    plan = generate(
        target_iface.pack, target_iface, elaboration.graph, n=n, seed=seed, holes=holes
    )
    if as_json:
        click.echo(json.dumps(plan.to_dict(), indent=2))
        return

    text = render(plan, target_iface.pack, target_iface, dut, target)
    for t in plan.unreachable:
        click.echo(f"  not targeted — {t.point}/{t.bin}: {t.reason}", err=True)
    if plan.targeted:
        click.echo(
            f"  targeting {len(plan.targeted)} hole(s): "
            + ", ".join(f"{t.point}/{t.bin}" for t in plan.targeted),
            err=True,
        )
    _write(text, output, f"{len(plan.items)} transaction(s)")


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Where to write the SAIF (default: stdout).")
@click.option("--design", default="design", help="Design name recorded in the file.")
@click.option("--gating", is_flag=True, help="List clock-gating candidates instead.")
@click.option("--top-n", default=20, type=int, help="How many rows for --gating.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def saif(trace, output, design, gating, top_n, as_json):
    """Switching activity, for power estimation (§8.31).

    \b
      veritrace saif dump.fst -o activity.saif
      veritrace saif dump.vcd --gating

    xsim writes SAIF natively, so a Vivado flow already has this. It exists for
    Icarus, Verilator and ModelSim, which write none — and there the alternative
    is Vivado's flat 12.5% toggle-rate default.
    """
    from veritrace import TraceStore
    from veritrace.export import saif as saif_mod

    path = _ensure_store(_default_trace(trace, flag="a trace path"))
    report = saif_mod.measure(TraceStore(str(path)))

    if as_json:
        # Both modes in one document: the toggle counts are what a power script
        # wants, and `held` is the only thing --gating adds on top of them.
        _write(
            json.dumps(
                {
                    **report.to_dict(),
                    "gating_candidates": [
                        {"path": a.path, "held": a.held, "tc": a.tc}
                        for a in saif_mod.gating_candidates(report)[:top_n]
                    ],
                },
                indent=2,
            ),
            output,
            f"{len(report.signals)} net(s)",
        )
        return

    if gating:
        rows = saif_mod.gating_candidates(report)[:top_n]
        if not rows:
            click.echo("No register holds its value for more than 90% of the run.")
            return
        click.echo(f"{len(rows)} clock-gating candidate(s) — held > 90% of the run:")
        for a in rows:
            click.echo(f"  {(a.held or 0) * 100:5.1f}%  {a.tc:6} toggles  {a.path}")
        click.echo(
            "\nCandidates, not findings: whether a clock can be gated is a question "
            "about the design, and this only says it would have been worth it."
        )
        return

    _write(saif_mod.render(report, design), output, f"{len(report.signals)} net(s)")


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@rtl_options
@click.option("--signals", required=True, help="Comma-separated signal paths, in order.")
@click.option("--range", "window", default=None, help="`c1200:c1230` or `100ns:300ns`.")
@click.option("--svg", is_flag=True, help="Render an SVG instead of WaveDrom JSON.")
@click.option("--light", is_flag=True, help="Ink on white, for print.")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Where to write it (default: stdout).")
def wavedrom(trace, rtl, top, signals, window, svg, light, output):
    """A diagram for a README or a thesis, not a screenshot (§8.33).

    \b
      veritrace wavedrom dump.vcd --signals "clk,awvalid,awready,awaddr" --range c120:c150
      veritrace wavedrom dump.vcd --signals "..." --svg --light -o handshake.svg
    """
    from veritrace.export import wavedrom as wd

    ctx = _load(trace, rtl, top)
    paths = [s.strip() for s in signals.split(",") if s.strip()]
    missing = [p for p in paths if ctx.store.find(p) is None]
    if missing:
        raise click.ClickException(f"not in the trace: {', '.join(missing)}")

    t0, t1 = ctx.store.time_range
    if window:
        a, _, b = window.partition(":")
        if not b:
            raise click.ClickException("--range wants two points, as `c100:c130`")
        t0, t1 = _at(ctx, a.strip()), _at(ctx, b.strip())

    diagram = wd.build(ctx.store, paths, t0, t1, ctx.clock)
    text = wd.to_svg(diagram, light) if svg else diagram.to_json()
    _write(text, output, f"{len(diagram.rows)} row(s)")


@main.command()
@click.argument("report", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@rtl_options
@click.option("--limit", default=10, type=int, help="How many paths to list.")
@click.option("--violated", is_flag=True, help="Only paths that missed timing.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def timing(report, trace, rtl, top, limit, violated, as_json):
    """Vivado's critical paths, over your RTL and your run (§8.30).

    \b
      veritrace timing timing_summary.rpt dump.vcd --rtl rtl/

    This is not static timing analysis — Vivado already did that. What it adds is
    the two joins Vivado cannot make: each path's endpoint against the line of RTL
    it comes from, and against how often the run actually switched it. A critical
    path that never toggles is a false priority, and nothing else will tell you.
    """
    from veritrace import timing as timing_mod

    parsed = timing_mod.load(report)
    if not parsed.paths:
        raise click.ClickException(
            f"{report} has no timing paths in it. It should be the output of "
            "`report_timing_summary`, not `report_timing`."
        )
    ctx = _load(trace, rtl, top) if trace else None
    timing_mod.correlate(parsed, ctx.graph if ctx else None, ctx.store if ctx else None)

    if as_json:
        click.echo(json.dumps(parsed.to_dict(), indent=2))
        return
    click.echo(_format_timing(parsed, limit, violated))


def _format_timing(report, limit: int, only_violated: bool) -> str:
    out = []
    if report.wns is not None:
        out.append(f"WNS {report.wns:+.3f} ns · TNS {report.tns:+.3f} ns · {len(report.paths)} path(s)")
    paths = report.violated if only_violated else report.paths
    if not paths:
        out.append("\nEvery path in this report met timing.")
        return "\n".join(out)

    for p in paths[:limit]:
        mark = "VIOLATED" if p.violated else "met"
        out.append(f"\n{p.slack:+.3f} ns  {mark}   {p.group or 'no group'}")
        out.append(f"  from {p.source}")
        if p.source_signal:
            out.append(f"       -> {p.source_signal}{'   ' + p.source_loc if p.source_loc else ''}")
        out.append(f"  to   {p.destination}")
        if p.dest_signal:
            out.append(f"       -> {p.dest_signal}{'   ' + p.dest_loc if p.dest_loc else ''}")
        worst = sorted(p.hops, key=lambda h: -h.delay_ns)[:3]
        for h in worst:
            where = f"   {h.loc}" if h.loc else ""
            out.append(f"  {h.delay_ns:6.3f} ns  {h.resource}{where}")
        if p.toggles is not None:
            out.append(f"  switched {p.toggles}x in the run")
        if p.note:
            out.append(f"  {p.note}")

    if report.correlated and report.false_priorities:
        out.append(
            f"\n{len(report.false_priorities)} violated path(s) never switched in this run. "
            "Vivado cannot know that; it has never run the design."
        )
    elif not report.correlated:
        out.append("\nNo trace given, so nothing is said about which paths matter. Pass one.")
    return "\n".join(out)


@main.command()
@click.option("--cocotb-log", type=click.Path(exists=True, path_type=Path), default=None,
              help="A cocotb monitor's log. Parsed with [ingest] patterns, or the built-ins.")
@click.option("--uvm-tr-db", "uvm_db", type=click.Path(exists=True, path_type=Path), default=None,
              help="A uvm_text_tr_database file (+UVM_TR_RECORD).")
@click.option("--trace", type=click.Path(path_type=Path), default=None,
              help="The waveform the monitor ran against, for timescale and for the UI.")
@rtl_options
@click.option("--serve", is_flag=True, help="Open the interface on the result.")
@click.option("--port", default=8765, type=int, help="Port for --serve.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def ingest(cocotb_log, uvm_db, trace, rtl, top, serve, port, as_json):
    """Transactions your monitor already recorded (§8.34).

    \b
      veritrace ingest --cocotb-log sim.log --trace dump.fst --rtl rtl/
      veritrace ingest --uvm-tr-db uvm_tr.dat --trace dump.fst

    A UVM or cocotb monitor has already written down what a transaction is.
    Re-deriving it from wires is twice the work and can disagree with whoever
    wrote the monitor, so it is read instead — and once read it gets latency,
    stalls, deadlock, integrity and transaction-level why-trace for free,
    because none of those care where a transaction came from.
    """
    from veritrace import ingest as ingest_mod

    if not cocotb_log and not uvm_db:
        raise click.ClickException("Nothing to ingest. Pass --cocotb-log or --uvm-tr-db.")

    ctx = _load(trace, rtl, top) if trace else None
    timescale = str(ctx.store.timescale) if ctx else "1ns"
    patterns = dict(getattr(ctx.config, "ingest_patterns", {}) or {}) if ctx else {}
    try:
        streams = ingest_mod.read(uvm_db, cocotb_log, patterns or None, timescale)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if not streams:
        raise click.ClickException(
            "No transaction matched. The log is read with `[ingest] patterns` from "
            ".veritrace.toml, or the built-in cocotb forms — add one that fits yours."
        )

    if as_json:
        click.echo(json.dumps(
            {"streams": [e.interface.to_dict() | {"n": len(e.transactions)} for e in streams],
             "transactions": [t.to_dict() for e in streams for t in e.transactions]},
            indent=2, default=str,
        ))
        return

    click.echo(_format_ingest(streams, cocotb_log or uvm_db))
    if serve:
        if not trace:
            raise click.ClickException("--serve needs --trace: the interface shows a waveform.")
        _remember_ingest(ctx, cocotb_log, uvm_db)
        click.echo("\nRecorded in .veritrace.toml, so `veritrace serve` keeps using it.")
        click.get_current_context().invoke(
            serve, traces=(trace,), host="127.0.0.1", port=port, browser=True, rtl=rtl, top=top
        )


def _format_ingest(streams, source) -> str:
    total = sum(len(e.transactions) for e in streams)
    out = [f"{total} transaction(s) from {source}, no signal extraction involved:"]
    for e in streams:
        kinds: dict[str, int] = {}
        for t in e.transactions:
            kinds[t.kind] = kinds.get(t.kind, 0) + 1
        shape = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        out.append(f"  {e.interface.name:24} {len(e.transactions):5}  ({shape})")
        done = [t for t in e.transactions if t.closed and t.duration()]
        if done:
            lat = sorted(t.duration() for t in done)
            out.append(
                f"    latency: min {lat[0]}, median {lat[len(lat) // 2]}, max {lat[-1]} ticks"
            )
        if e.open_transactions:
            out.append(f"    {len(e.open_transactions)} never closed in the log")
        fields = sorted({k for t in e.transactions for k in t.fields})
        if fields:
            out.append(f"    fields: {', '.join(fields)}")
    out.append(
        "\nThese are the monitor's own transactions. Everything downstream — latency,"
        "\ndeadlock, integrity, why(txn...) — works on them unchanged (§8.34)."
    )
    return "\n".join(out)


def _remember_ingest(ctx, cocotb_log: Path | None, uvm_db: Path | None) -> None:
    """Write the log's path into `.veritrace.toml` so `serve` finds it again.

    §13.4's rule: the trace is named once. The same applies to the log — a team
    whose monitor writes it every run should not re-type the path.
    """
    import tomli_w

    root = getattr(ctx.config, "root", None) or Path.cwd()
    path = getattr(ctx.config, "path", None) or (root / ".veritrace.toml")
    data = {}
    if Path(path).exists():
        import tomllib

        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    section = data.setdefault("ingest", {})
    if cocotb_log:
        section["cocotb_log"] = str(Path(cocotb_log).resolve().relative_to(Path(root).resolve()))
    if uvm_db:
        section["uvm_db"] = str(Path(uvm_db).resolve().relative_to(Path(root).resolve()))
    Path(path).write_text(tomli_w.dumps(data), encoding="utf-8")


# --- §13.6 regression database, §8.36 scorecard, §8.37 test plan -----------


def _regress_db(db_path: Path | None) -> Path:
    """Where §13.6's database lives, resolved the same way for every command.

    It has to be one rule. `record` anchored the default to the project root and
    the two readers anchored it to the working directory, so the documented
    sequence contradicted itself the moment they were run from different places
    — which is the normal case, since you record beside a simulation and query
    from the repository root:

        $ cd designs/axi_lite && veritrace record …
        run 1 recorded
        $ veritrace history
        Error: regressions.duckdb does not exist yet

    The project root is the right anchor: the database is the project's memory,
    not one directory's. With no config there is no project, and the working
    directory is all there is.
    """
    if db_path is not None:
        return db_path
    from veritrace import config as cfg
    from veritrace.regress.db import DEFAULT_DB

    conf = cfg.load()
    root = getattr(conf, "root", None) if conf is not None else None
    return Path(root or Path.cwd()) / DEFAULT_DB


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@rtl_options
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None,
              help="The DuckDB file (default: regressions.duckdb at the project root).")
@click.option("--tag", default="", help='Free label, e.g. "commit=$(git rev-parse --short HEAD)".')
@click.option("--seed", type=int, default=None,
              help="The randomisation seed this run used. Mandatory (§13.6).")
@click.option("--simulator", default=None, help="Which simulator ran it.")
@click.option("--simulator-version", default=None, help="And which version.")
@click.option("--command", default="", help="The exact command, so `reproduce` can repeat it.")
def record(trace, rtl, top, db_path, tag, seed, simulator, simulator_version, command):
    """Write this run into the regression database (§13.6).

    \b
      veritrace record sim.fst --rtl rtl/ --seed 3910182 \\
          --simulator icarus --simulator-version 12.0 \\
          --tag "commit=$(git rev-parse --short HEAD)"

    The seed, the simulator with its version, and a hash of the RTL are
    mandatory: without them a nightly failure is not reproducible and the row is
    an anecdote. The command refuses rather than recording one.
    """
    from veritrace import regress

    ctx = _load(trace, rtl, top)
    files, *_ = _resolve_rtl(rtl, top)
    if seed is None:
        raise click.ClickException(
            "--seed is mandatory (§13.6): a recorded run without its seed cannot be "
            "reproduced, and a regression nobody can reproduce is a story."
        )
    if not simulator or not simulator_version:
        raise click.ClickException(
            "--simulator and --simulator-version are mandatory (§13.6): the same RTL "
            "and the same seed can fail on one simulator and pass on another."
        )

    root = getattr(ctx.config, "root", None) or Path.cwd()
    run = regress.Run(
        seed=seed,
        simulator=simulator,
        simulator_version=simulator_version,
        rtl_sha256=regress.sha256_of(files) if files else "no-rtl",
        tag=tag,
        commit_sha=regress.git_commit(root),
        command=command,
        work_dir=str(Path.cwd()),
        trace_path=str(ctx.trace_path),
        top=top or "",
    )
    _collect(ctx, run)

    con = regress.connect(_regress_db(db_path))
    run_id = regress.record(con, run)
    con.close()
    click.echo(
        f"run {run_id} recorded: seed {seed}, {simulator} {simulator_version}, "
        f"RTL {run.rtl_sha256[:12]}"
    )
    click.echo(f"  {len(run.txn)} interface(s), {len(run.findings)} finding group(s)")
    click.echo(f"  veritrace reproduce --run-id {run_id}")


def _collect(ctx: "Context", run) -> None:
    """Everything §13.6 lists, from the analyses that already ran."""
    protocol = ctx.transactions()
    for e in getattr(protocol, "extractions", []) or []:
        done = sorted(t.duration() for t in e.transactions if t.closed and t.duration())
        pick = lambda q: (done[min(len(done) - 1, int(len(done) * q))] if done else None)  # noqa: E731
        run.txn.append({
            "iface": e.interface.name, "pack": e.interface.pack.name,
            "n": len(e.transactions), "p50": pick(0.50), "p95": pick(0.95),
            "p99": pick(0.99), "max": done[-1] if done else None,
            "throughput": (len(e.transactions) / (e.sampled_cycles or 1)) if e.sampled_cycles else None,
            "outstanding": max((t.outstanding for t in e.transactions), default=0),
            "violations": len(e.violations),
        })

    report = ctx.check_report()
    for group in getattr(report, "groups", []) or []:
        by: dict[str, int] = {}
        for f in getattr(group, "findings", []) or []:
            by[str(getattr(f, "severity", "info"))] = by.get(str(getattr(f, "severity", "info")), 0) + 1
        for severity, n in by.items():
            run.findings.append((str(getattr(group, "name", group)), severity, n))

    cov = ctx.coverage_report() if hasattr(ctx, "coverage_report") else None
    code = getattr(cov, "code", None)
    if code is not None and code.total:
        run.coverage.append(("line", None, code.covered, code.total, code.score))
    for f in getattr(cov, "functional", []) or []:
        pts = [p for p in f.points if p.cells]
        if pts:
            covered, total = sum(p.covered for p in pts), sum(p.total for p in pts)
            run.coverage.append(("functional", f.iface, covered, total, covered / max(1, total)))


def _jsonable(v):
    """DuckDB hands back timestamps and decimals; JSON does not take them."""
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


@main.command()
@click.argument("sql", required=False)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None,
              help="The DuckDB file (default: regressions.duckdb at the project root).")
@click.option("--limit", default=40, type=int, help="Rows, when no SQL is given.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def history(sql, db_path, limit, as_json):
    """Query the regression database (§13.6).

    \b
      veritrace history
      veritrace history "SELECT commit_sha, p99_latency FROM txn_metrics
                         JOIN runs USING (run_id) WHERE iface='dma0'
                         ORDER BY ts DESC LIMIT 40"
    """
    from veritrace import regress

    path = _regress_db(db_path)
    if not Path(path).exists():
        raise click.ClickException(f"{path} does not exist yet — `veritrace record` writes it.")
    con = regress.connect(path)
    query = sql or (
        "SELECT run_id, ts, tag, commit_sha, seed, simulator, simulator_version "
        f"FROM runs ORDER BY run_id DESC LIMIT {limit}"
    )
    try:
        cols, rows = regress.query(con, query)
    except Exception as e:  # noqa: BLE001 - a user's SQL, reported as theirs
        raise click.ClickException(f"query failed: {e}") from e
    finally:
        con.close()

    if as_json:
        # P6, and the one place it matters most: §13.6's whole point is a trend
        # across runs, which is read by a script or a dashboard, not by eye.
        click.echo(
            json.dumps(
                {"columns": list(cols), "rows": [[_jsonable(v) for v in r] for r in rows]},
                indent=2,
            )
        )
        return
    if not rows:
        click.echo("no rows")
        return
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    click.echo("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    click.echo("  ".join("-" * w for w in widths))
    for r in rows:
        click.echo("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


@main.command()
@click.option("--run-id", type=int, default=None, help="Which run (default: the latest).")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None,
              help="The DuckDB file (default: regressions.duckdb at the project root).")
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def reproduce(run_id, db_path, rtl, top, as_json):
    """Rebuild the exact command a recorded run used (§13.6).

    \b
      veritrace reproduce --run-id 4821 --rtl rtl/

    If the RTL has changed since, this says so rather than pretending the
    reproduction is identical — the same honesty §5.7 applies to a stale trace.
    """
    from veritrace import regress

    path = _regress_db(db_path)
    if not Path(path).exists():
        raise click.ClickException(f"{path} does not exist yet — `veritrace record` writes it.")
    con = regress.connect(path)
    if run_id is None:
        row = regress.latest(con)
        if row is None:
            raise click.ClickException("the database has no runs in it")
        run_id = row["run_id"]

    files, *_ = _resolve_rtl(rtl, top)
    try:
        out = regress.plan(con, run_id, files or None)
    except KeyError as e:
        raise click.ClickException(str(e)) from e
    finally:
        con.close()

    if as_json:
        click.echo(json.dumps(out.to_dict(), indent=2))
        return
    click.echo(f"run {out.run_id}: {out.simulator} {out.simulator_version}, seed {out.seed}")
    if out.work_dir:
        click.echo(f"  cd {out.work_dir}")
    click.echo(f"  {out.command or '(no command was recorded)'}")
    if out.identical:
        click.echo(f"\nRTL hash: {out.rtl_then[:12]}… — identical to the original run.")
    else:
        click.echo(f"\n{out.caveat}")
        if out.rtl_now:
            click.echo(f"  then {out.rtl_then[:12]}…  now {out.rtl_now[:12]}…")


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@rtl_options
@click.option("--plan", "plan_path", type=click.Path(exists=True, path_type=Path), default=None,
              help="A testplan.toml, cross-referenced against what this run produced (§8.37).")
@click.option("--mutation", type=click.Path(exists=True, path_type=Path), default=None,
              help="`veritrace mutate --json` output.")
@click.option("--formal", "formal_json", type=click.Path(exists=True, path_type=Path), default=None,
              help="`veritrace formal --json` output.")
@click.option("--synth", "synth_json", type=click.Path(exists=True, path_type=Path), default=None,
              help="`veritrace synth-diff --json` output.")
@click.option("--fail-under", type=float, default=None,
              help="Exit non-zero if any category with a target misses it.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def scorecard(trace, rtl, top, plan_path, mutation, formal_json, synth_json, fail_under, as_json):
    """Every metric in one report (§8.36).

    \b
      veritrace scorecard dump.vcd --rtl rtl/
      veritrace scorecard --plan testplan.toml

    Pure aggregation — nothing here is computed that was not computed already.
    No row says "verified" without a qualifier, and the report never emits a
    binary verdict about the design, because that is not a claim a tool can make.
    """
    from veritrace import plan as plan_mod
    from veritrace.report import scorecard as card_mod

    ctx = _load(trace, rtl, top)
    coverage = ctx.coverage_report() if hasattr(ctx, "coverage_report") else None
    targets = {"line": 90.0, "functional": 80.0, "mutation": 70.0}
    if fail_under is not None:
        targets = {k: fail_under for k in targets}

    # §8.36 aggregates what other commands produced; they already emit JSON, so
    # a scorecard can be assembled in CI from four separate jobs.
    load = lambda p: card_mod.Loaded(json.loads(Path(p).read_text(encoding="utf-8"))) if p else None  # noqa: E731
    formal_reports = None
    if formal_json:
        raw = json.loads(Path(formal_json).read_text(encoding="utf-8"))
        formal_reports = [card_mod.Loaded(r) for r in (raw if isinstance(raw, list) else [raw])]

    card = card_mod.build(
        checks=ctx.check_report(),
        coverage=coverage,
        protocol=ctx.transactions(),
        mutation=load(mutation),
        formal=formal_reports,
        synth=load(synth_json),
        design=top or getattr(ctx.config, "top", "") or "",
        commit=__import__("veritrace.regress", fromlist=["git_commit"]).git_commit(
            getattr(ctx.config, "root", None) or Path.cwd()
        ),
        targets=targets,
    )

    plan_obj = None
    if plan_path:
        plan_obj = plan_mod.link(
            plan_mod.load(plan_path), coverage=coverage,
            formal=formal_reports, checks=ctx.check_report(),
        )

    if as_json:
        payload = card.to_dict()
        if plan_obj:
            payload["plan"] = plan_obj.to_dict()
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(_format_scorecard(card, plan_obj))

    if fail_under is not None and card.failing:
        raise SystemExit(1)


MARK = {"ok": "OK ", "warn": " ! ", "bad": "XX ", "absent": " - "}


def _format_scorecard(card, plan_obj) -> str:
    head = f"VERITRACE SCORECARD — {card.design or 'this design'}"
    if card.commit:
        head += f" @ {card.commit}"
    out = [head, ""]
    for row in card.rows:
        target = f"target {row.target}" if row.target else ""
        out.append(f"  {MARK[row.status]} {row.category:22} {row.value:>14}   {target}")
        out.append(f"        {row.detail}")
    out.append("")
    out.append(
        f"{len(card.covered)} of {len(card.rows)} categories measured in this run. "
        "No line here says a design is verified — that is not a claim this tool makes."
    )
    if plan_obj is not None:
        out += ["", _format_plan(plan_obj)]
    return "\n".join(out)


def _format_plan(plan_obj) -> str:
    score = plan_obj.score
    out = [
        f"VERIFICATION PLAN — {plan_obj.path.name if plan_obj.path else ''}"
        + (f"   {score:.0f}% of items covered" if score is not None else ""),
        "",
    ]
    for item in plan_obj.items:
        out.append(f"  {item.evidence:9} {item.id:10} {item.desc}")
        for l in item.links:
            out.append(f"              {l.state:8} {l.text}   {l.detail}")
        if item.status == "covered" and item.evidence != "covered":
            # A plan is a document and documents drift. Where the file claims
            # more than the run shows, the run wins and the gap is named.
            out.append("              the plan says covered; this run does not show it")
    for err in plan_obj.errors:
        out.append(f"  ! {err}")
    return "\n".join(out)


#: Where `run` leaves the command it used, and `export` looks for it.
COMMAND_FILE = "sim.command"


def _remember_command(dump: Path, command: str) -> None:
    if not command:
        return
    try:
        (Path(dump).parent / COMMAND_FILE).write_text(command + "\n", encoding="utf-8")
    except OSError:
        pass  # a read-only work directory must not fail the run


def _recall_command(trace: Path | None) -> str:
    """The simulation command, if this waveform came from `veritrace run`.

    Absent is normal — a dump from ModelSim or a colleague's machine has no
    such record — and the report says "(not recorded)" rather than inventing
    a plausible command line.
    """
    if trace is None:
        return ""
    for base in {Path(trace).parent, Path(str(trace).removesuffix(".vtx")).parent}:
        try:
            return (base / COMMAND_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def _project_root(trace: Path | None) -> Path:
    """The directory a team feature works relative to (§13.8).

    The project you are standing in, or the trace's own directory when the dump
    is kept outside one — the same rule `_load` follows, so `share` and `serve`
    never disagree about where `notes/` is.
    """
    from veritrace import config as cfg

    conf = cfg.load()
    if conf is not None:
        return Path(conf.root)
    return (trace.parent if trace else Path.cwd())


@main.command()
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Where to write it (default: <trace>.vtsession).")
@click.option("--query", default="", help="The question this session is about, e.g. 'why(top.x @ c120)'.")
def share(trace, output, query):
    """Bundle this screen into a `.vtsession` for a colleague (§13.8).

    \b
      veritrace share dump.vcd -o notes/dma_deadlock.vtsession
      veritrace restore notes/dma_deadlock.vtsession        # on their machine

    Layout, bookmarks, annotations and the current query travel; the dump does
    not — it is referenced by hash, because they already have it and what they
    lack is the certainty that it is the same one. No server is involved: this
    is a file you commit, review and grep like any other.
    """
    from veritrace import __version__, share as share_mod, store as store_mod
    from veritrace.api.sessions import LayoutFile

    trace = store_mod.ensure(_default_trace(trace, flag="a trace path"))
    root = _project_root(trace)
    bundle = share_mod.build(
        trace,
        LayoutFile.for_trace(trace).load(),
        root,
        query=query,
        version=__version__,
    )
    out = Path(output) if output else trace.with_name(trace.name + share_mod.SUFFIX)
    out.parent.mkdir(parents=True, exist_ok=True)
    share_mod.write(out, bundle)

    rows = len(bundle["layout"].get("signals") or [])
    click.echo(f"{out}")
    click.echo(f"  trace     {bundle['trace']['name']}  sha256 {bundle['trace']['sha256'][:12]}…")
    click.echo(f"  layout    {rows} row(s), {len(bundle['layout'].get('cursors') or [])} cursor(s)")
    click.echo(f"  notes     {len(bundle['notes'])} file(s)")
    if bundle["query"]:
        click.echo(f"  query     {bundle['query']}")


@main.command()
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("trace", type=click.Path(path_type=Path), required=False, default=None)
@click.option("--force", is_flag=True, help="Overwrite annotation files that differ locally.")
def restore(bundle, trace, force):
    """Open a colleague's `.vtsession` against your copy of the dump (§13.8).

    \b
      veritrace restore dma_deadlock.vtsession dump.vcd
      veritrace serve dump.vcd --rtl rtl/     # and there is their screen

    A dump whose hash does not match is still applied — the signal selection is
    the useful half either way — but it is called out, because bookmarks taken
    in another run point at times that do not exist in yours.
    """
    from veritrace import share as share_mod, store as store_mod

    data = share_mod.read(Path(bundle))
    trace = store_mod.ensure(_default_trace(trace, flag="a trace path"))
    applied = share_mod.apply(data, trace, _project_root(trace), force=force)

    click.echo(f"layout    -> {applied.layout_path}")
    for p in applied.notes:
        click.echo(f"notes     -> {p}")
    if applied.query:
        click.echo(f"query        {applied.query}")
    for w in applied.warnings:
        click.echo(f"  ! {w}", err=True)
    click.echo(f"\nveritrace serve {trace}")


@main.command("import-capture")
@click.argument("capture", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--format", "fmt", type=click.Choice(list(_CAPTURE_FORMATS)), default="vivado-ila",
              help="Which tool exported it.")
@click.option("--scope", default="", help="Prefix every probe with this scope, e.g. `top.dut`.")
@click.option("--timescale", default="1ns", help="Time unit to give one sample.")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Where to write the .vcd (default: beside the capture).")
def import_capture(capture, fmt, scope, timescale, output):
    """An ILA or SignalTap capture, as a trace (§8.11b).

    \b
      veritrace import-capture ila.csv --scope top.dut -o ila.vcd
      veritrace serve ila.vcd --rtl rtl/

    A capture is a *window*: shallow, narrow and triggered. Everything that
    reads a trace works on it, and a causal chain that reaches the first sample
    stops at `capture_boundary` with what to trigger on next time, rather than
    calling a value constant because the evidence ran out.
    """
    from veritrace.ingest import capture as cap_mod

    got = cap_mod.read(capture, fmt=fmt, scope=scope)
    out = Path(output) if output else Path(capture).with_suffix(".vcd")
    out.write_text(cap_mod.to_vcd(got, timescale), encoding="utf-8")

    click.echo(f"{out}")
    click.echo(f"  {len(got.names)} probe(s), {got.n_samples} sample(s), one per {timescale} tick")
    if got.trigger is not None:
        click.echo(f"  trigger at sample {got.trigger}")
    click.echo(
        "  a capture is a window: the chain stops at its first sample rather "
        "than claiming a cause it cannot see (§8.11b)"
    )


@main.command()
@click.argument("text")
@click.option("--signal", default=None, help="The signal this is about.")
@click.option("--at", "when", default=None, help="`c1247` or a raw timestamp. Needs --signal.")
@click.option("--loc", default=None, help="`rtl/dma.sv:42` — a source anchor instead of a signal.")
@click.option("-f", "--file", "path", type=click.Path(path_type=Path), default=None,
              help="Which .vtnotes to append to (default: notes/<top>.vtnotes).")
@click.option("--trace", type=click.Path(path_type=Path), default=None,
              help="Only needed to turn a `cN` cycle into a time.")
def note(text, signal, when, loc, path, trace):
    """Append an annotation to a versionable text file (§13.8).

    \b
      veritrace note "grant drops a cycle early" --signal top.u_dma.state --at c120
      veritrace note "this if() should test busy too" --loc rtl/dma.sv:42

    One note per line, in `notes/*.vtnotes`, so it is reviewable in the same
    pull request as the fix and findable with `grep`. No database, no cloud.
    """
    from veritrace import config as cfg
    from veritrace import notes as notes_mod

    at: int | None = None
    if when is not None:
        if not signal:
            raise click.ClickException("--at anchors a signal; pass --signal too.")
        # A cycle number only means something against a clock, so the trace is
        # loaded for that case and only that case.
        at = _at(_load(trace), when) if when.lower().startswith("c") else int(when)

    file_, line = "", None
    if loc:
        head, _, tail = loc.rpartition(":")
        if not tail.isdigit():
            raise click.ClickException(f"--loc wants file:line, got {loc!r}")
        file_, line = head, int(tail)

    conf = cfg.load()
    root = Path(conf.root) if conf is not None else Path.cwd()
    dest = Path(path) if path else root / "notes" / f"{(conf.top if conf else '') or 'veritrace'}.vtnotes"
    entry = notes_mod.Note(text=text, signal=signal or "", time=at, file=file_, line=line)
    notes_mod.append(dest, entry)
    click.echo(f"{dest}: {entry}")


def _write(text: str, output: Path | None, what: str) -> None:
    """To a file, or to stdout when there is none — §13's P6."""
    if output is None:
        click.echo(text)
        return
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(text, encoding="utf-8")
    click.echo(f"{what} -> {output}  ({len(text.encode()) // 1024 or 1} KB)", err=True)


if __name__ == "__main__":
    main()
