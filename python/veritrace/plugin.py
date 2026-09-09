"""Analysis and exporter plugins — §13.7's second and third extension points.

§13.7's promise is short and testable: **an analysis you add does not touch the
core, and does not touch the UI.** Findings a plugin yields appear in the Checks
tab like any other; tabular results appear as a tab of their own. §16.2 sets the
bar at *under 45 minutes to write a new one from the documentation alone*.

    from veritrace.plugin import Analysis, register

    @register
    class ClockGatingEfficiency(Analysis):
        name  = "clock_gating"
        needs = ["trace"]

        def run(self, ctx):
            for clk in ctx.signals(kind="clock"):
                gated = ctx.count_where(clk, 0)
                yield ctx.finding(
                    f"{clk.path}: gated {gated / ctx.total_cycles:.0%} of the time",
                    severity="info", signal=clk.path)

Three decisions worth stating, because each is a way this could have been more
powerful and less useful:

* **`needs` is a contract, not a hint.** A plugin that asks for `transactions`
  is not run at all on a design with no bus, and the reason is recorded next to
  the other skipped checks. Letting it run and find nothing would be
  indistinguishable from a design with nothing wrong (P7).
* **A plugin that raises is a recorded skip, never a broken Checks tab.** The
  same rule the built-in detectors follow — one bad analysis must not hide the
  other nine.
* **Discovery is files, no entry points, no install step.** §13.8's rule for
  team features is "no feature may require a server"; the same reasoning applies
  here. Drop a `.py` in `plugins/` next to the RTL and it is versioned with the
  design, reviewable, and greppable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from veritrace.analysis.findings import Finding, Group, Severity

#: What a plugin may ask for. Each maps to something the session already built,
#: so asking costs nothing beyond what the tool was going to do anyway.
CAPABILITIES = ("trace", "graph", "transactions", "coverage", "memory", "performance")

#: Where plugins are looked for, nearest last so a project overrides the user.
USER_DIR = Path.home() / ".veritrace" / "plugins"
PROJECT_DIR = "plugins"

#: Registries filled by ``@register`` while a plugin module is imported.
#:
#: A process-wide mutable list looks harmless in a command-line program, but
#: the API analyses and exports several projects concurrently.  In that case a
#: discovery for project B could clear/replace the classes project A was about
#: to use.  Context-local immutable snapshots isolate threads and async tasks;
#: mutating a copied list in :func:`register` also prevents inherited contexts
#: from sharing list storage.
_REGISTERED: ContextVar[tuple[type["Analysis"], ...]] = ContextVar(
    "veritrace_plugin_analyses", default=()
)
_EXPORTERS: ContextVar[tuple[type["Exporter"], ...]] = ContextVar(
    "veritrace_plugin_exporters", default=()
)
_IMPORT_LOCK = threading.RLock()


class PluginError(RuntimeError):
    """A plugin that could not be loaded, with the file it came from."""


@dataclass(slots=True)
class SignalView:
    """One signal, as a plugin sees it."""

    path: str
    handle: int
    width: int
    n_events: int

    @property
    def name(self) -> str:
        return self.path.rsplit(".", 1)[-1]


@dataclass(slots=True)
class Result:
    """A table a plugin produced. §13.7: it becomes a tab without touching the UI."""

    title: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "columns": self.columns, "rows": self.rows}


class Context:
    """What a plugin is handed. Everything else is off limits on purpose.

    Narrow by design: a plugin that reaches into the trace store directly is a
    plugin that breaks when the store changes, and §13.7's promise is that an
    analysis survives the core moving under it.
    """

    def __init__(
        self,
        store: Any = None,
        graph: Any = None,
        elaboration: Any = None,
        clock: Any = None,
        config: Any = None,
        protocol: Any = None,
        coverage: Any = None,
        memory: Any = None,
        performance: Any = None,
        plugin: str = "",
        query: str | None = None,
        causal: Any = None,
        findings: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.graph = graph
        #: The elaborated design, when RTL was loaded. Separate from `graph`
        #: because the graph alone does not carry parameter *values* — an
        #: analysis that resolves `S_IDLE` to a number needs this, and the first
        #: plugin written against `graph` alone silently found nothing.
        self.elaboration = elaboration
        self.clock = clock
        self.config = config
        self.protocol = protocol
        self.coverage = coverage
        self.memory = memory
        self.performance = performance
        #: Exporters receive the same narrow, stable view as analyses plus the
        #: optional question/result that led to the report.  Analysis plugins
        #: simply see these as ``None``.
        self.query = query
        self.causal = causal
        self.findings = findings
        self.metadata = dict(metadata or {})
        self._plugin = plugin

    # -- the trace -------------------------------------------------------

    @property
    def total_cycles(self) -> int:
        """Clock cycles in the run, or the raw time span when there is no clock."""
        if self.clock is not None:
            return self.clock.n_cycles
        lo, hi = self.store.time_range
        return max(1, hi - lo)

    def signals(self, kind: str | None = None, match: str | None = None) -> list[SignalView]:
        """Every signal, optionally narrowed.

        `kind="clock"` is the primary clock plus anything the design graph
        drives sequential logic from — the question a plugin asks is "which
        wires are clocks", and answering it from the graph is better than
        matching names, which is what a plugin author would otherwise do.
        """
        import fnmatch

        rows = [
            SignalView(path=s.path, handle=s.handle, width=s.width, n_events=s.n_events)
            for s in self.store.signals()
        ]
        if kind == "clock":
            wanted = self._clock_paths()
            rows = [r for r in rows if r.path in wanted]
        elif kind is not None:
            rows = [r for r in rows if self._kind_of(r.path) == kind]
        if match:
            rows = [r for r in rows if fnmatch.fnmatch(r.path, match)]
        return rows

    def _clock_paths(self) -> set[str]:
        out: set[str] = set()
        if self.clock is not None:
            out.add(self.clock.path)
        if self.graph is not None:
            for sig in self.graph:
                for d in sig.drivers:
                    if d.clock is not None:
                        out.add(d.clock.path())
        return out

    def _kind_of(self, path: str) -> str | None:
        sig = self.graph.get(path) if self.graph is not None else None
        return sig.kind.value if sig is not None else None

    def value_at(self, signal: str | SignalView, t: int) -> str | None:
        """Raw four-state digits at a time, or `None` if the signal is not dumped."""
        handle = signal.handle if isinstance(signal, SignalView) else self.store.find(signal)
        if handle is None:
            return None
        got = self.store.value_at(handle, t)
        return got.bits if got is not None else None

    def transitions(self, signal: str | SignalView) -> list[tuple[int, str]]:
        handle = signal.handle if isinstance(signal, SignalView) else self.store.find(signal)
        if handle is None:
            return []
        lo, hi = self.store.time_range
        return [(t, v.bits) for t, v in self.store.transitions(handle, lo, hi + 1)]

    def count_where(self, signal: str | SignalView, value: int) -> int:
        """Clock cycles in which the signal held `value`.

        Counted on clock edges when there is a clock, so the answer is in the
        unit an engineer reports it in (§5.5) rather than in raw timestamps.
        """
        handle = signal.handle if isinstance(signal, SignalView) else self.store.find(signal)
        if handle is None:
            return 0
        edges = self.clock.edges if self.clock is not None else None
        if not edges:
            lo, hi = self.store.time_range
            return sum(
                1 for _t, v in self.store.transitions(handle, lo, hi + 1) if v.to_int() == value
            )
        rows = self.store.sample_before([handle], list(edges))
        return sum(1 for cell in rows[0] if _as_int(cell) == value)

    # -- the transaction layer -------------------------------------------

    @property
    def interfaces(self) -> list[Any]:
        return [e.interface for e in getattr(self.protocol, "extractions", [])]

    def transactions(self, iface: str | None = None) -> list[Any]:
        out: list[Any] = []
        for ex in getattr(self.protocol, "extractions", []):
            if iface is None or ex.interface.name == iface:
                out.extend(ex.transactions)
        return out

    # -- what a plugin produces ------------------------------------------

    def finding(
        self,
        title: str,
        severity: str = "info",
        signal: str | None = None,
        detail: str = "",
        loc: Any = None,
        why: str | None = None,
    ) -> Finding:
        """A finding, which appears in the Checks tab like any other (§13.7).

        The `check` name is the plugin's own, prefixed, so `--fail-on` and
        `checks.disable` address it exactly as they address a built-in — and so
        two plugins cannot collide over a name.
        """
        return Finding(
            group=Group.PLUGIN,
            severity=_SEVERITY.get(severity.lower(), Severity.INFO),
            check=f"plugin.{self._plugin}",
            title=title,
            signal=signal,
            detail=detail,
            loc=loc,
            why=why,
        )

    def table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> Result:
        """A tabular result, which appears as a tab of its own (§13.7)."""
        return Result(title=title, columns=list(columns), rows=[list(r) for r in rows])


_SEVERITY = {"info": Severity.INFO, "warn": Severity.WARN, "warning": Severity.WARN,
             "error": Severity.ERROR}


def _as_int(cell: Any) -> int | None:
    """A sampled cell as an integer, or `None` when it held X or Z.

    The store hands back `Value` objects, not numbers — which is what
    `count_where` was reading when it silently counted nothing on a signal that
    was tied low for an entire run. A helper the documentation promises has to
    work on what the store actually returns, not on what it looks like it
    returns.
    """
    if cell is None:
        return None
    if isinstance(cell, int):
        return cell
    to_int = getattr(cell, "to_int", None)
    if callable(to_int):
        return to_int()
    if isinstance(cell, str):
        try:
            return int(cell, 2)
        except ValueError:
            return None
    return None


class Analysis:
    """Base class for a plugin. Subclass it, set `name` and `needs`, write `run`."""

    #: Stable identifier. Used in `--fail-on plugin.<name>` and as the tab title.
    name: str = ""
    #: What this analysis cannot run without — any of `CAPABILITIES`.
    needs: Sequence[str] = ()
    #: One line, shown beside the results.
    description: str = ""

    def run(self, ctx: Context) -> Iterator[Finding | Result]:
        """Yield `ctx.finding(...)` and `ctx.table(...)`. Both are optional."""
        raise NotImplementedError


class Exporter:
    """Base class for a custom report format (§13.7).

    Subclass it, set :attr:`name` and optional :attr:`needs`, then implement
    :meth:`render`.  The returned text/bytes are written atomically by the real
    ``veritrace export`` command; a plugin never has to know CLI paths.
    """

    #: Stable value accepted by ``veritrace export --format``.
    name: str = ""
    #: The same capability contract analysis plugins use.
    needs: Sequence[str] = ()
    #: One line shown by ``veritrace plugins``.
    description: str = ""
    #: Suggested suffix, for documentation/discovery.  The caller's explicit
    #: output path remains authoritative.
    extension: str = ""

    def render(self, ctx: Context) -> str | bytes:
        """Return the complete standalone artifact."""
        raise NotImplementedError


def register(cls):
    """Make an :class:`Analysis` or :class:`Exporter` visible.

    The one decorator is the "same mechanism" promised by §13.7.  Discovery
    imports user plugins first and project plugins second, so replacement by
    stable name makes the nearer project definition win deterministically.
    """
    if not getattr(cls, "name", ""):
        raise PluginError(f"{cls.__name__} has no `name`")
    if not issubclass(cls, (Analysis, Exporter)):
        raise PluginError(f"{cls.__name__} must inherit Analysis or Exporter")
    bad = [n for n in cls.needs if n not in CAPABILITIES]
    if bad:
        raise PluginError(
            f"{cls.name} asks for {', '.join(bad)}; try one of {', '.join(CAPABILITIES)}"
        )
    variable = _REGISTERED if issubclass(cls, Analysis) else _EXPORTERS
    registry = list(variable.get())
    # Discovery imports the user directory first and the project directory
    # second.  Replacement by stable name is what makes the documented
    # "project overrides user" rule real.
    for i, old in enumerate(registry):
        if old.name == cls.name:
            registry[i] = cls
            break
    else:
        registry.append(cls)
    variable.set(tuple(registry))
    return cls


def registered() -> list[type[Analysis]]:
    return list(_REGISTERED.get())


def exporters() -> list[type[Exporter]]:
    """Every discovered exporter, in deterministic discovery order."""
    return list(_EXPORTERS.get())


def clear() -> None:
    """Forget plugins in this execution context.

    Other request threads/tasks keep their own immutable snapshots.  This is
    intentionally not a process-wide reset: one session reloading its project
    must not invalidate an export already running for another session.
    """
    _REGISTERED.set(())
    _EXPORTERS.set(())


# --- discovery --------------------------------------------------------------


def search_path(project_root: Path | str | None = None) -> list[Path]:
    """Where plugins are looked for, in the order §13.7 lists them."""
    out = [USER_DIR]
    if project_root is not None:
        out.append(Path(project_root) / PROJECT_DIR)
    return [p for p in out if p.is_dir()]


def discover(project_root: Path | str | None = None) -> tuple[list[type[Analysis]], dict[str, str]]:
    """Import every plugin on the search path. Returns `(plugins, errors)`.

    Errors are returned rather than raised: a project with one broken plugin
    still gets the other three, and the broken one is named. A discovery step
    that aborts on the first bad file is one people stop using.
    """
    # Discovery describes exactly one user/project search path.  Retaining the
    # preceding project's registry made a plugin appear installed merely
    # because another session happened to have loaded it earlier.
    clear()
    errors: dict[str, str] = {}
    for directory in search_path(project_root):
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            analyses_before = _REGISTERED.get()
            exporters_before = _EXPORTERS.get()
            try:
                _import(path)
            except Exception as e:  # noqa: BLE001 - a bad plugin is data, not a crash
                # A file may register one class and then fail.  Treat the file
                # atomically so a reported-broken plugin cannot still execute.
                _REGISTERED.set(analyses_before)
                _EXPORTERS.set(exporters_before)
                errors[path.name] = f"{type(e).__name__}: {e}"
    return registered(), errors


def _import(path: Path) -> None:
    """Import a file as a module, without putting its directory on `sys.path`.

    A plugin directory next to somebody's RTL should not be able to shadow
    `os` or `json` for the rest of the process, which is what appending it to
    `sys.path` would allow.
    """
    # A basename-only module key collided when two projects both contained
    # ``plugins/checks.py``. Include the canonical path, while keeping the name
    # stable so class introspection/pickling can still resolve its module.
    identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    name = f"veritrace_plugin_{path.stem}_{identity}"
    # ``exec_module`` itself is not protected by Python's normal import lock.
    # Serialising this small discovery boundary prevents two requests loading
    # the same file from replacing its sys.modules entry during execution.
    with _IMPORT_LOCK:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot import {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if sys.modules.get(name) is module:
                sys.modules.pop(name, None)
            raise


# --- running ----------------------------------------------------------------


@dataclass(slots=True)
class PluginRun:
    findings: list[Finding] = field(default_factory=list)
    tables: list[Result] = field(default_factory=list)
    #: Plugin name -> why it did not run.
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "tables": [t.to_dict() for t in self.tables],
            "skipped": self.skipped,
        }


def _available(ctx: Context) -> dict[str, bool]:
    return {
        "trace": ctx.store is not None,
        "graph": ctx.graph is not None,
        "transactions": bool(getattr(ctx.protocol, "extractions", None)),
        "coverage": ctx.coverage is not None,
        "memory": bool(ctx.memory),
        "performance": ctx.performance is not None,
    }


def run_all(
    plugins: Sequence[type[Analysis]],
    store: Any = None,
    graph: Any = None,
    elaboration: Any = None,
    clock: Any = None,
    config: Any = None,
    protocol: Any = None,
    coverage: Any = None,
    memory: Any = None,
    performance: Any = None,
) -> PluginRun:
    """Run every plugin whose `needs` are satisfied, and record why the rest did not."""
    out = PluginRun()
    disabled = set(getattr(config, "disabled_checks", ()) or ())
    for cls in plugins:
        ctx = Context(
            store=store,
            graph=graph,
            elaboration=elaboration,
            clock=clock,
            config=config,
            protocol=protocol,
            coverage=coverage,
            memory=memory,
            performance=performance,
            plugin=cls.name,
        )
        if f"plugin.{cls.name}" in disabled or cls.name in disabled:
            out.skipped[cls.name] = "disabled in .veritrace.toml"
            continue
        have = _available(ctx)
        missing = [n for n in cls.needs if not have.get(n, False)]
        if missing:
            out.skipped[cls.name] = f"needs {', '.join(missing)}, which this session has not got"
            continue
        try:
            for item in cls().run(ctx) or ():
                if isinstance(item, Finding):
                    out.findings.append(item)
                elif isinstance(item, Result):
                    out.tables.append(item)
                else:
                    raise PluginError(
                        f"yielded {type(item).__name__}; expected ctx.finding(...) or ctx.table(...)"
                    )
        except Exception as e:  # noqa: BLE001 - one plugin must not hide the rest
            out.skipped[cls.name] = f"failed: {type(e).__name__}: {e}"
            if getattr(config, "debug", False):
                traceback.print_exc()
    return out


def run_exporter(
    exporter: type[Exporter],
    *,
    store: Any = None,
    graph: Any = None,
    elaboration: Any = None,
    clock: Any = None,
    config: Any = None,
    protocol: Any = None,
    coverage: Any = None,
    memory: Any = None,
    performance: Any = None,
    query: str | None = None,
    causal: Any = None,
    findings: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> str | bytes:
    """Render one exporter against production session data.

    Unlike analysis plugins, an exporter failure is not a skippable finding:
    it is the operation the user explicitly requested.  Raise a named
    :class:`PluginError` so the CLI/API cannot claim an artifact was produced.
    """
    ctx = Context(
        store=store,
        graph=graph,
        elaboration=elaboration,
        clock=clock,
        config=config,
        protocol=protocol,
        coverage=coverage,
        memory=memory,
        performance=performance,
        query=query,
        causal=causal,
        findings=findings,
        metadata=metadata,
        plugin=exporter.name,
    )
    missing = [name for name in exporter.needs if not _available(ctx).get(name, False)]
    if missing:
        raise PluginError(
            f"exporter {exporter.name!r} needs {', '.join(missing)}, "
            "which this session has not got"
        )
    try:
        artifact = exporter().render(ctx)
    except Exception as exc:
        raise PluginError(
            f"exporter {exporter.name!r} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(artifact, (str, bytes)):
        raise PluginError(
            f"exporter {exporter.name!r} returned {type(artifact).__name__}; "
            "expected str or bytes"
        )
    return artifact
