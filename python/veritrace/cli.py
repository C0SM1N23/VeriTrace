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
from veritrace.export import viewers

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


@click.group()
@click.version_option(version=__version__, prog_name="veritrace")
def main() -> None:
    """VeriTrace command-line interface."""


def _resolve_rtl(rtl: tuple[Path, ...], top: str | None) -> tuple[list[Path], list[str], list[str], str | None]:
    """RTL sources from the command line, falling back to `.veritrace.toml`."""
    from veritrace import config as cfg
    from veritrace.graph.elaborate import discover

    files: list[Path] = []
    for r in rtl:
        files.extend(discover(r))
    incdirs: list[str] = []
    defines: list[str] = []

    conf = cfg.load()
    if conf is not None:
        if not files:
            files = conf.rtl_files()
        incdirs = [str(conf.root / d) for d in conf.incdirs]
        defines = conf.defines
        top = top or conf.top
    return files, incdirs, defines, top


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


def _load(
    trace: Path,
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

    # §13's examples pass raw dumps, so accept one and convert transparently.
    trace = _ensure_store(trace)
    ctx = Context(
        store=TraceStore(str(trace)),
        config=cfg.load_or_empty(trace.parent),
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
    return ctx


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
def correlate(trace: Path | None, rtl: tuple[Path, ...], top: str | None, limit: int) -> None:
    """Match RTL signals against a trace and report the correlation rate.

    §7 makes this a first-class number rather than something buried: a low rate
    means every later answer is built on sand, and it is nearly always a
    missing simulator flag (§4.0).
    """
    from veritrace.correlate.resolver import format_report

    # §7.4: no RTL is a supported mode, but this command has nothing to do
    # without it.
    ctx = _load(_default_trace(trace, flag="a trace path"), rtl, top, need_rtl=True)
    el = ctx.elaboration

    click.echo(f"top = {el.graph.top or '?'}")
    click.echo(f"{len(el.graph)} RTL signals, {ctx.store.n_signals} trace signals")
    click.echo(format_report(ctx.correlation, limit=limit))
    if el.graph.blackboxes:
        click.echo(f"  {len(el.graph.blackboxes)} black-box instance(s) without source")
    if el.errors:
        click.echo(f"  {len(el.errors)} elaboration error(s); first: {el.errors[0]}")


@main.command()
@click.argument(
    "trace", type=click.Path(exists=True, path_type=Path), required=False, default=None
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
    trace: Path | None, host: str, port: int, browser: bool, rtl: tuple[Path, ...], top: str | None
) -> None:
    """Serve a trace over HTTP and WebSocket.

    TRACE may be a `.vtx` store or a raw `.vcd`/`.fst`, which is converted on
    the way in. With no argument it uses `trace.default` from
    `.veritrace.toml`, so §13.4's `veritrace init && veritrace serve` works
    with nothing typed in between.
    """
    import uvicorn

    from veritrace.api import create_app

    trace = _default_trace(trace, flag="a trace path")
    files, _incdirs, _defines, top = _resolve_rtl(rtl, top)
    app = create_app(default_trace=trace, rtl=[str(f) for f in files], top=top)
    session_id = app.state.default_session_id
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
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
@click.argument("query")
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
    from veritrace.analysis import vtq
    from veritrace.analysis.whytrace import WhyTracer, root_cause

    ctx = _load(trace, rtl, top, need_rtl=True)
    try:
        parsed = vtq.parse(query)
    except vtq.QueryError as e:
        raise click.ClickException(str(e)) from e

    headline = ""
    if parsed.txn is not None:
        signal, at, headline = _txn_question(ctx, parsed.txn)
    else:
        signal = parsed.signal
        if ctx.graph.get(signal) is None:
            raise click.ClickException(f"unknown signal: {signal}")
        at = _at(
            ctx,
            None if parsed.time is None else f"c{parsed.time}" if parsed.is_cycle else str(parsed.time),
        )
        ctx.transactions()

    result = WhyTracer(ctx.graph, ctx.store, txn_index=ctx.txn_index).why(signal, at)

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

    if as_json:
        payload = {"query": query, "time": at, **result.to_dict()}
        if headline:
            payload["headline"] = headline
        click.echo(json.dumps(payload, indent=2))
        return
    if headline:
        click.echo(headline + "\n")
    _print_chain(result.root, ctx.clock)
    click.echo(f"\n{result.nodes} nodes in {result.elapsed_ms:.1f} ms")


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
        click.echo(
            f"{marker}{node.signal} = {node.value}   [{node.reason.value}] at {at}"
            + ("   (as above)" if repeat else where)
        )
        if node.detail and not repeat:
            click.echo("  " * (depth + 1) + f"   {node.detail}")
    if repeat:
        return
    for child in node.children:
        _print_chain(child, clock, depth + 1, seen)


@main.command()
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
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
@viewer_options
def cone(trace, signal, rtl, top, depth, direction, active_only, t_from, t_to, output, **flags):
    """Signals within N edges of SIGNAL. From 4000 signals you keep 8 (§8.6)."""
    from veritrace.analysis import cone as cone_mod

    ctx = _load(trace, rtl, top, need_rtl=True)
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

    if _emit_selection(ctx, result.paths(), f"Cone of {signal}", flags, output):
        return

    click.echo(f"{direction} cone of {signal}, depth {depth}: {len(result.nodes)} signal(s)")
    if result.n_inactive:
        click.echo(f"  {result.n_inactive} dropped as inactive in the window")
    for node in result.nodes:
        role = f" [{node.role}]" if node.role else ""
        click.echo(f"  {'  ' * node.depth}{node.path}{role}")


@main.command()
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
@rtl_options
@click.option("--cycles", default=None, type=int, help="Threshold, in clock cycles.")
@viewer_options
def stuck(trace, rtl, top, cycles, output, **flags):
    """Signals frozen for longer than the threshold (§8.4)."""
    from veritrace.analysis import stuck as stuck_mod

    ctx = _load(trace, rtl, top)
    if ctx.clock is None:
        raise click.ClickException("No clock could be identified, so cycles have no meaning.")
    found = sorted(
        stuck_mod.scan(ctx.store, ctx.clock, ctx.graph, ctx.config, cycles),
        key=lambda f: f.sort_key,
    )
    if _emit_selection(ctx, [f.signal for f in found if f.signal], "Stuck", flags, output):
        return
    if not found:
        click.echo("nothing has been frozen for longer than the threshold")
        return
    click.echo(f"{len(found)} frozen signal(s), clock {ctx.clock.path}")
    for f in found:
        click.echo(f"  {f.signal}   {f.title}   ({f.detail})   {f.loc or ''}")


@main.command()
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
@rtl_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--fail-on",
    default="",
    help="Exit non-zero if any of these fire. Names or groups: stuck,x,lint,cdc,protocol,deadlock.",
)
def check(trace, rtl, top, as_json, fail_on):
    """Every automatic finding: stuck, X sources, lint, parameters (§13, §13.9).

    With `--fail-on` this is the CI gate: a failing build carries the cause in
    its log rather than a dump nobody will open.
    """
    from veritrace.analysis import checks as checks_mod

    ctx = _load(trace, rtl, top)
    report = checks_mod.run_all(
        ctx.store,
        ctx.graph,
        ctx.elaboration,
        ctx.clock,
        ctx.config,
        ctx.transactions(),
        ctx.measure().liveness,
        ctx.memory_reports(),
    )

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        click.echo(_format_report(report, ctx.clock))

    gate = checks_mod.expand_checks(fail_on.split(",")) if fail_on else set()
    hits = [f for f in report if f.check in gate]
    if hits:
        raise SystemExit(1)


@main.command()
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
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
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
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
@click.argument("trace", type=click.Path(exists=True, path_type=Path))
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


def _format_report(report, clock) -> str:
    from veritrace import clocks as _clocks

    if not len(report):
        out = ["no automatic findings"]
    else:
        out = [f"{len(report)} finding(s) in {report.elapsed_ms:.0f} ms"]
        for group, findings in report.by_group().items():
            out.append("")
            out.append(f"{group.label} ({len(findings)})")
            for f in findings:
                at = f"  at {_clocks.format_time(f.time, clock)}" if f.time is not None else ""
                out.append(f"  {f.signal or '-'}   {f.title}{at}   {f.loc or ''}")
                for note in f.notes:
                    out.append(f"      {note}")
    # P7: a check that could not run says so; silence would read as a pass.
    for name, why_not in report.skipped.items():
        out.append(f"  not run: {name} - {why_not}")
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
    try:
        result = cone_mod.cone(graph, signal, depth=depth)
    except KeyError:
        raise click.ClickException(f"unknown signal: {signal}") from None

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
    """A `.vtx` store for `path`, converting a raw dump if that is what it is.

    §13's examples pass dumps directly (`veritrace serve dump.fst`), so asking
    the user to convert first would be a step that exists only because the tool
    wanted it. An existing store is reused unless the dump is newer, which is
    the case that matters: re-running the simulation must not silently serve
    yesterday's data.
    """
    if path.is_dir():
        return path
    if path.suffix.lower() not in (".vcd", ".fst"):
        return path

    from veritrace import _native

    out = path.with_name(path.name + ".vtx")
    fresh = (
        out.is_dir()
        and (out / "index.bin").exists()
        and out.stat().st_mtime >= path.stat().st_mtime
    )
    if not fresh:
        click.echo(f"converting {path} -> {out}", err=True)
        _native.convert(str(path), str(out))
    return out


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

    n_events = _native.convert(str(source), str(out))
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


if __name__ == "__main__":
    main()
