"""Sessions and their on-disk state.

Principle P5 — *nothing is lost on reload*. Layout, groups, radix and bookmarks
live in a plain JSON file next to the dump, so they survive a server restart, a
machine reboot, and are diffable and committable like any other project file.

The file deliberately sits *beside* the `.vtx` directory rather than inside it:
conversion rewrites the store from scratch (`write_vtx` clears the target), and
user state must not be collateral damage of re-running a simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from veritrace._native import TraceStore

#: Bumped when the on-disk layout schema changes incompatibly.
LAYOUT_VERSION = 1

#: How many causal trees a session keeps. Small: the questions that matter are
#: the one on screen and the ones Replay and the report ask about it.
WHY_CACHE = 8


def session_id_for(trace_path: Path) -> str:
    """Stable id derived from the trace location.

    Deterministic on purpose (P1): reopening the same dump — in a later process,
    after a reboot — yields the same id, so a client that remembers a session
    finds its layout again instead of starting blank.
    """
    resolved = os.path.normcase(str(trace_path.resolve())).replace("\\", "/")
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def default_layout() -> dict[str, Any]:
    return {
        "version": LAYOUT_VERSION,
        "signals": [],
        "groups": [],
        "radix": {},
        "bookmarks": [],
        "cursors": [],
        "zoom": None,
        # The causal question on screen. Part of the layout because P5 lists the
        # query history as state that survives a reload, and because §13.8's
        # `.vtsession` is mostly this one string — the tree under it is derived.
        "query": "",
        "queryHistory": [],
        "savedQueries": {},
        # §11.4: a suppressed finding, and the mandatory reason for it. Without
        # the reason the list becomes a graveyard nobody dares to empty.
        "suppressions": {},
    }


class LayoutFile:
    """The JSON sidecar holding one session's user state."""

    _locks_guard = threading.Lock()
    _path_locks: dict[Path, Any] = {}

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_error = ""
        self.backup_path: Path | None = None
        # Browsers persist several slices of the store in quick succession and
        # FastAPI serves those PUTs on different worker threads.  They must not
        # race through the same write-then-rename temporary path.
        with self._locks_guard:
            self._lock = self._path_locks.setdefault(path.resolve(), threading.RLock())

    @classmethod
    def for_trace(cls, trace_path: Path) -> LayoutFile:
        # dump.vtx -> dump.vtx.session.json, a sibling of the store directory.
        return cls(trace_path.with_name(trace_path.name + ".session.json"))

    def load(self) -> dict[str, Any]:
        with self._lock:
            self.last_error = ""
            if not self.path.exists():
                return default_layout()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                # A corrupt sidecar must not make the trace unopenable (P7).
                self.last_error = f"could not read saved layout {self.path.name}: {e}"
                return default_layout()
            if not isinstance(data, dict):
                self.last_error = f"saved layout {self.path.name} must contain a JSON object"
                return default_layout()
            merged = default_layout()
            merged.update(data)
            merged["version"] = LAYOUT_VERSION
            return merged

    def save(self, layout: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = default_layout()
            # Keys the UI does not own must survive a layout PUT: provenance is the
            # RTL hash §5.7's check depends on, and suppressions are set through
            # their own endpoint. A wave-layout save must not wipe either.
            existing = self.load() if self.path.exists() else {}
            if self.last_error and self.path.exists():
                # An explicit save may recover from a malformed sidecar, but the
                # bytes that failed to parse remain available for manual repair.
                backup = self.path.with_name(self.path.name + ".corrupt")
                n = 1
                while backup.exists():
                    backup = self.path.with_name(self.path.name + f".corrupt.{n}")
                    n += 1
                shutil.copy2(self.path, backup)
                self.backup_path = backup
            for key in ("provenance", "suppressions", "memoryTiming"):
                if key in existing:
                    merged[key] = existing[key]
            merged.update(layout)
            merged["version"] = LAYOUT_VERSION
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so an interrupted save cannot truncate the file.
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            self.last_error = ""
            return merged


def sha256_files(paths: list[Path]) -> str:
    """One hash over a set of RTL files, content and names.

    §5.7: analysing a fresh dump against edited RTL is the most insidious way
    this tool could lie, so provenance is recorded and checked.
    """
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x).lower()):
        h.update(p.name.encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


@dataclass
class Session:
    """An open trace plus the user state attached to it."""

    session_id: str
    trace_path: Path
    store: TraceStore
    layout_file: LayoutFile
    rtl_paths: list[str] = field(default_factory=list)
    top: str | None = None
    #: Populated when RTL was supplied; None means "waveform viewer only" (§7.4).
    graph: Any = None
    correlation: Any = None
    elaboration: Any = None
    rtl_sha256: str | None = None
    #: True when the RTL changed after the trace was made (§5.7).
    rtl_changed: bool = False
    #: Why RTL could not be loaded, if it could not.
    rtl_error: str = ""
    #: Project configuration (§4.3), resolved from the trace's directory.
    config: Any = None
    #: Primary clock, for cycle numbering (§5.5).
    clock: Any = None
    #: Automatic findings, computed on open (§1.4, §13.4).
    report: Any = None
    #: Extracted transactions per interface (§8.13-8.14), computed on open.
    protocol: Any = None
    #: (signal, time) -> transaction, for §8.16's causal hop.
    txn_index: Any = None
    #: Why extraction could not run at all, if it could not.
    protocol_error: str = ""
    #: §8.17's per-interface metrics and §8.18's wait-for graph, computed on
    #: open — §8.18 is explicit that all three liveness scans run then, like the
    #: stuck detector.
    performance: Any = None
    wait_for: list = field(default_factory=list)
    performance_error: str = ""
    #: §8.20's per-interface reports (bank timeline, timing violations,
    #: efficiency), computed on open — a fourth scan alongside extraction,
    #: performance and liveness, run for the same reason: §11.4b needs to know
    #: whether a memory interface exists before it can choose a default tab.
    memory: Any = None
    memory_error: str = ""
    #: §8.19's scoreboard and path comparison, and §8.21+§8.12's coverage. Both
    #: are passes over the transaction table extraction already produced, so
    #: they cost no trace access and run on open like everything else.
    integrity: Any = None
    integrity_error: str = ""
    coverage: Any = None
    coverage_error: str = ""
    #: §8.37's verification plan, when the project has a `testplan.toml`.
    plan: Any = None
    plan_error: str = ""
    #: Causal answers, keyed by `(signal, time)`. §8.2's minimisation, §8.3's
    #: repro and §12's report all re-ask the question the Causal tab has just
    #: answered; without this, pressing Replay rebuilds a tree that is already
    #: on screen. Bounded, because a long session should not hold every tree it
    #: ever built.
    _why: dict[tuple[str, int], Any] = field(default_factory=dict, repr=False)
    _why_not: dict[tuple[str, int, str], Any] = field(default_factory=dict, repr=False)

    def why(self, signal: str, t: int, on_node: Any = None) -> Any:
        """`WhyResult` for this question, computed once (§8.1).

        ``on_node`` is the progressive WebSocket observer of §10.2.  A cached
        answer is replayed in post-order (root last); an uncached one emits as
        it is built by :class:`WhyTracer`.  REST and CLI callers pay nothing.
        """
        from veritrace.analysis.whytrace import WhyTracer

        key = (signal, t)
        got = self._why.get(key)
        if got is None:
            if len(self._why) >= WHY_CACHE:
                self._why.clear()
            start = (
                self.store.time_range[0]
                if getattr(self.config, "capture", False)
                else None
            )
            got = WhyTracer(
                self.graph,
                self.store,
                txn_index=self.txn_index,
                capture_start=start,
                on_node=on_node,
            ).why(signal, t)
            self._why[key] = got
        elif on_node is not None:
            def replay(node: Any) -> None:
                for child in node.children:
                    replay(child)
                on_node(node)

            replay(got.root)
        return got

    def why_not(self, signal: str, t: int, desired: Any, on_node: Any = None) -> Any:
        """Cached §11.4 counterfactual analysis over this production session."""
        from veritrace.analysis.whytrace import WhyTracer

        key = (signal, t, str(desired))
        got = self._why_not.get(key)
        if got is None:
            if len(self._why_not) >= WHY_CACHE:
                self._why_not.clear()
            start = (
                self.store.time_range[0]
                if getattr(self.config, "capture", False)
                else None
            )
            got = WhyTracer(
                self.graph,
                self.store,
                txn_index=self.txn_index,
                capture_start=start,
                on_node=on_node,
            ).why_not(signal, t, desired)
            self._why_not[key] = got
        elif on_node is not None:
            def replay(node: Any) -> None:
                for child in node.children:
                    replay(child)
                on_node(node)

            replay(got.root)
        return got

    def rtl_files(self) -> list[Path]:
        """Every RTL file behind this session, expanded as the graph saw them."""
        from veritrace.graph.elaborate import discover

        out: list[Path] = []
        for p in self.rtl_paths:
            out.extend(discover(p))
        return out

    @classmethod
    def open(
        cls,
        trace_path: str | Path,
        rtl_paths: list[str] | None = None,
        top: str | None = None,
        progress: Callable[[str, float], None] | None = None,
    ) -> Session:
        from veritrace import config as cfg

        from veritrace import store as store_mod

        path = Path(trace_path)
        if not path.exists():
            raise FileNotFoundError(f"no such trace: {path}")
        # A replacement store can have an immutable generation filename, while
        # the user's URL and saved layout still belong to the requested dump.
        identity_path = store_mod.identity(path)
        emit = progress or (lambda _phase, _pct: None)
        emit("converting", 0.05)
        # §13: every entry point accepts a raw dump and converts it on the way
        # in. Without this the REST API was the one door that did not, and it
        # reported a real `.vcd` as a missing file.
        path = store_mod.ensure(path)
        emit("indexing", 0.25)
        store = TraceStore(str(path))
        rtl_root = Path(rtl_paths[0]) if rtl_paths else None
        rtl_config = cfg.load(rtl_root if rtl_root.is_dir() else rtl_root.parent) if rtl_root else None
        from veritrace import build
        from veritrace.graph.elaborate import discover

        config = rtl_config or cfg.load() or cfg.load_or_empty(path.parent)
        requested = ([p for root in rtl_paths for p in discover(Path(root))]
                     if rtl_paths is not None else None)
        restored = build.config_for(path, store, config, requested)
        if restored is not config:
            rtl_paths = [str(p) for p in restored.rtl_files()]
        session = cls(
            session_id=session_id_for(identity_path),
            trace_path=path,
            store=store,
            layout_file=LayoutFile.for_trace(identity_path),
            rtl_paths=[str(Path(p).resolve()) for p in (rtl_paths or [])],
            top=top,
            # The project the server was started in decides its own settings;
            # the trace's directory is only the fallback for a dump kept outside
            # it. `cli._load` resolves it the same way, so the CLI and the
            # interface never disagree about which config is in force.
            config=restored,
        )
        # Capture identity travels beside the generated VCD/store. Without this
        # durable marker, `import-capture` followed by `serve` quietly turns an
        # ILA window back into a simulation and why-trace invents history before
        # sample zero.
        from veritrace.ingest import capture as capture_mod

        if capture_mod.marker_path(path).is_file() or capture_mod.marker_path(identity_path).is_file():
            session.config.capture = True
        # Omission means use the project's configured build; an explicit []
        # still requests waveform-only mode. Reading config without consuming
        # its RTL list made a fresh REST/installed session lose all causality.
        chosen_rtl = session.config.rtl_files() if rtl_paths is None else [Path(p) for p in rtl_paths]
        session.rtl_paths = [str(p.resolve()) for p in chosen_rtl]
        if chosen_rtl:
            emit("elaborating", 0.35)
            session._load_rtl(chosen_rtl, top)
        session.analyse(progress=emit)
        return session

    def _reset_analysis(self) -> None:
        """Discard every value derived from RTL/session inputs before reload."""
        self.graph = None
        self.correlation = None
        self.elaboration = None
        self.rtl_sha256 = None
        self.rtl_changed = False
        self.rtl_error = ""
        self.clock = None
        self.report = None
        self.protocol = None
        self.txn_index = None
        self.protocol_error = ""
        self.performance = None
        self.wait_for = []
        self.performance_error = ""
        self.memory = None
        self.memory_error = ""
        self.integrity = None
        self.integrity_error = ""
        self.coverage = None
        self.coverage_error = ""
        self.plan = None
        self.plan_error = ""
        self._why.clear()
        self._why_not.clear()

    def reconfigure(self, rtl_paths: list[str], top: str | None) -> None:
        """Reload one path-backed session against a new RTL/top selection.

        Session ids intentionally depend only on the dump path, so changing
        the design inputs must replace all dependent state in-place rather
        than return a session whose public fields describe one design and
        whose cached why trees describe another.
        """
        self._reset_analysis()
        self.rtl_paths = [str(Path(p).resolve()) for p in rtl_paths]
        self.top = top
        if self.rtl_paths:
            self._load_rtl([Path(p) for p in self.rtl_paths], top)
        self.analyse()

    # --- automatic analysis (§1.4, §13.4) --------------------------------

    def analyse(self, progress: Callable[[str, float], None] | None = None) -> Any:
        """Resolve the clock, extract transactions, and run every check.

        §1.4 makes this unconditional: the findings have to be there when the
        session opens, not when someone asks. It is cheap — one parallel scan
        in Rust plus a graph walk — and it is the entire reason the Checks tab
        is worth opening on.

        Extraction comes before the checks because they consume it: protocol
        violations are findings like any other (§11.4), and §11.4b needs to know
        how many interfaces there are before it can choose a default tab.
        """
        from veritrace import clocks

        emit = progress or (lambda _phase, _pct: None)
        emit("clock", 0.48)
        self.clock = clocks.resolve(self.store, self.graph, self.config)
        emit("transactions", 0.55)
        self._extract()
        emit("performance", 0.64)
        self._measure()
        emit("memory", 0.71)
        self._memory()
        emit("integrity", 0.78)
        self._integrity()
        emit("coverage", 0.84)
        self._coverage()
        emit("checks", 0.90)
        self.report = self._checks(self.memory)
        # After the checks, because §8.37 items cite them (`check:stuck`).
        emit("verification-plan", 0.97)
        self._plan()
        return self.report

    def _checks(self, memory: Any) -> Any:
        from veritrace.analysis import checks

        return checks.run_all(
            self.store,
            self.graph,
            self.elaboration,
            self.clock,
            self.config,
            self.protocol,
            self.performance.liveness if self.performance is not None else None,
            memory,
            self.integrity,
            project_root=(
                getattr(self.config, "root", None) or self.trace_path.parent
            ),
            coverage_report=self.coverage,
            performance_report=self.performance,
        )

    def _extract(self) -> None:
        """§8.13-8.14, on session open. Failure degrades to "no transactions"."""
        from veritrace.protocol import engine
        from veritrace.protocol.link import TxnIndex

        root = self.config.root if self.config is not None else self.trace_path.parent
        try:
            self.protocol = engine.extract(
                self.store,
                self.trace_path,
                self.clock,
                self.config,
                project_root=root,
            )
            self.txn_index = TxnIndex.build(self.protocol, self.store)
        except Exception as e:  # noqa: BLE001 - a bad pack must not close the trace
            self.protocol = None
            self.txn_index = None
            self.protocol_error = str(e)

    def _measure(self) -> None:
        """§8.17 and §8.18, on session open. Failure degrades to "no numbers"."""
        from veritrace.perf import report as perf_report

        if self.protocol is None:
            return
        try:
            self.performance, self.wait_for = perf_report.build(
                self.store,
                self.protocol.extractions,
                self.graph,
                self.clock,
                self.config,
            )
        except Exception as e:  # noqa: BLE001 - metrics must not close the trace
            self.performance = None
            self.performance_error = str(e)

    def _memory(self) -> None:
        """§8.20, on session open. Failure degrades to "no memory tab", not a
        closed trace — the same rule every other automatic scan follows."""
        from veritrace.memory import report as mem_report

        if self.protocol is None:
            return
        root = self.config.root if self.config is not None else self.trace_path.parent
        try:
            self.memory = mem_report.build_all(
                self.store,
                self.protocol.packs,
                self.clock,
                self.config,
                project_root=root,
                choices=self.layout_file.load().get("memoryTiming"),
            )
        except Exception as e:  # noqa: BLE001 - memory analysis must not close the trace
            self.memory = None
            self.memory_error = str(e)

    def set_memory_timing(self, iface_name: str, choice: dict[str, str]) -> None:
        """Apply, verify and persist a timing choice; every consumer gets the same result."""
        from veritrace.memory import report as mem_report, timing

        root = getattr(self.config, "root", None) or self.trace_path.parent
        chip = timing.resolve_choice(choice, root)
        if self.protocol is None or not self.memory:
            raise timing.TimingError("no memory interface is available")
        iface = next((i for i in mem_report.detect_interfaces(
            self.store, self.protocol.packs, self.config
        ) if i.name == iface_name), None)
        if iface is None:
            raise timing.TimingError(f"no memory interface named {iface_name!r}")
        # Same lock as layout PUTs; concurrent choices must not lose each other.
        with self.layout_file._lock:
            replacement = mem_report.build(self.store, iface, self.clock, chip, root, self.config)
            reports = [replacement if r.iface == iface_name else r for r in self.memory]
            findings = self._checks(reports)
            layout = self.layout_file.load()
            layout.setdefault("memoryTiming", {})[iface_name] = dict(choice)
            self.layout_file.save(layout)  # Failure must not advertise a saved selection.
            self.memory = reports
            self.report = findings
            self._plan()

    def _integrity(self) -> None:
        """§8.19, on session open. Degrades the same way everything else does."""
        from veritrace.integrity import report as int_report

        if self.protocol is None:
            return
        try:
            self.integrity = int_report.build(self.protocol, self.store, self.clock)
        except Exception as e:  # noqa: BLE001 - one scan must not close the trace
            self.integrity = None
            self.integrity_error = str(e)

    def _plan(self) -> None:
        """§8.37 — the verification plan, cross-referenced against this run.

        Found by convention next to the config, because a plan nobody has to
        wire up is one that gets kept current. Absent is normal and silent.
        """
        from veritrace import plan as plan_mod

        root = getattr(self.config, "root", None)
        path = Path(root or ".") / "testplan.toml"
        if not path.exists():
            return
        try:
            self.plan = plan_mod.link(
                plan_mod.load(path), coverage=self.coverage, checks=self.report
            )
        except Exception as e:  # noqa: BLE001 - a plan is a bonus, never a blocker (P7)
            self.plan_error = str(e)

    def _coverage(self) -> None:
        """§8.21 and §8.12, on session open.

        The code half looks for a coverage database next to the project and
        imports it if one is there. Not asking for it is deliberate: §8.12's
        value is closing the loop, and a loop with a mandatory argument in it
        does not close by itself.
        """
        from veritrace.coverage import report as cov_report

        root = self.config.root if self.config is not None else self.trace_path.parent
        try:
            self.coverage = cov_report.build(
                self.protocol,
                self.store,
                self.clock,
                self.graph,
                # Resolved against the configuration, not this process's cwd.
                coverage_path=getattr(self.config, "coverage_file", lambda: None)(),
                project_root=root,
                elaboration=self.elaboration,
            )
        except Exception as e:  # noqa: BLE001 - one scan must not close the trace
            self.coverage = None
            self.coverage_error = str(e)

    def suppressions(self) -> dict[str, str]:
        return dict(self.layout_file.load().get("suppressions") or {})

    def suppress(self, finding_id: str, reason: str) -> dict[str, str]:
        """Hide a finding, permanently and with a reason (§11.4).

        The reason is mandatory at the API boundary, not just in the UI: a
        suppression without one is how a findings list turns into a graveyard.
        """
        reason = reason.strip()
        if not reason:
            raise ValueError("a suppression needs a reason")
        with self.layout_file._lock:
            layout = self.layout_file.load()
            layout.setdefault("suppressions", {})[finding_id] = reason
            self.layout_file.save(layout)
            return layout["suppressions"]

    def unsuppress(self, finding_id: str) -> dict[str, str]:
        with self.layout_file._lock:
            layout = self.layout_file.load()
            (layout.get("suppressions") or {}).pop(finding_id, None)
            self.layout_file.save(layout)
            return layout.get("suppressions") or {}

    def findings(self) -> Any:
        """The report with suppressed findings removed."""
        from veritrace.analysis.findings import Report

        if self.report is None:
            return Report()
        return self.report.without(self.suppressions())

    def _load_rtl(self, files: list[Path], top: str | None) -> None:
        """Elaborate and correlate. Failure degrades to viewer mode, not a crash."""
        from veritrace.correlate.resolver import correlate
        from veritrace.graph.elaborate import discover, elaborate

        expanded: list[Path] = []
        for f in files:
            expanded.extend(discover(f))
        if not expanded:
            return
        try:
            # `serve` used to resolve these options and then drop them at the
            # API boundary.  The browser therefore elaborated a different
            # design from every CLI analysis whenever the project used an
            # include directory or a define (§4.3).  Resolve relative include
            # paths against the config that supplied them and pass the exact
            # option set into the one elaborator.
            config_root = Path(getattr(self.config, "root", self.trace_path.parent))
            incdirs = [
                str(Path(d) if Path(d).is_absolute() else config_root / d)
                for d in (getattr(self.config, "incdirs", None) or [])
            ]
            defines = list(getattr(self.config, "defines", None) or [])
            chosen_top = top or getattr(self.config, "top", None)
            self.top = chosen_top
            self.elaboration = elaborate(expanded, incdirs, defines, chosen_top)
            self.graph = self.elaboration.graph
            self.correlation = correlate(
                self.graph,
                {s.path: s.handle for s in self.store.signals()},
                self.elaboration.aliases,
            )
        except Exception as e:  # noqa: BLE001 - RTL problems must not close the trace
            self.elaboration = None
            self.graph = None
            self.rtl_error = str(e)
            return
        self.rtl_sha256 = sha256_files(expanded)
        self._check_provenance()

    def _check_provenance(self) -> None:
        """Compare against the hashes recorded when this trace was first opened.

        A different trace means a new simulation, so the record is refreshed.
        The same trace with different RTL means the sources moved on without a
        re-run — that is the case worth a banner.
        """
        stored = self.layout_file.load().get("provenance") or {}
        if self.layout_file.last_error:
            # Opening a trace is read-only with respect to damaged user state.
            # The warning is surfaced in status; an explicit layout PUT can
            # replace it and first preserves the original as ``.corrupt``.
            return
        same_trace = stored.get("trace_sha256") == self.store.source_sha256
        if stored and same_trace and stored.get("rtl_sha256") != self.rtl_sha256:
            self.rtl_changed = True
            return
        if not stored or not same_trace:
            layout = self.layout_file.load()
            layout["provenance"] = {
                "trace_sha256": self.store.source_sha256,
                "rtl_sha256": self.rtl_sha256,
            }
            self.layout_file.save(layout)

    def status(self) -> dict[str, Any]:
        from veritrace import clocks
        from veritrace.analysis.checks import default_tab

        t0, t1 = self.store.time_range
        report = self.findings()
        n_ifaces = len(self.protocol.extractions) if self.protocol else 0
        n_mem_ifaces = len(self.memory) if self.memory else 0
        return {
            # §11.4b: the tab to open on, decided when the session is created.
            "default_tab": default_tab(
                report,
                n_interfaces=n_ifaces,
                configured=getattr(self.config, "default_tab", None),
                n_memory_interfaces=n_mem_ifaces,
            ),
            "n_findings": len(report),
            "n_interfaces": n_ifaces,
            "n_memory_interfaces": n_mem_ifaces,
            "n_transactions": len(self.protocol.transactions) if self.protocol else 0,
            "protocol_error": self.protocol_error,
            "memory_error": self.memory_error,
            # TAB 7 is enabled when either half of it has anything to show, so
            # the tab strip is not offering an empty page (§13.4).
            "has_coverage": bool(
                self.coverage
                and (self.coverage.functional or self.coverage.code is not None)
            ),
            "clock": self.clock.path if self.clock else None,
            "clock_method": self.clock.method if self.clock else None,
            "n_cycles": self.clock.n_cycles if self.clock else 0,
            # §5.5, problem 3: `c1247` is ambiguous with two clocks, so the
            # Inspector shows a signal's own domain beside the primary count.
            "clock_domains": clocks.domains(
                self.store,
                self.graph,
                self.config,
                self.clock,
                getattr(self.elaboration, "aliases", None),
            ),
            "phase": "ready",
            "progress": 1.0,
            # Without RTL there is nothing to correlate; reporting null beats a
            # fabricated 0% or 100% (P7).
            "correlation_rate": self.correlation.percent if self.correlation else None,
            "n_signals": self.store.n_signals,
            "n_events": self.store.n_events,
            "t0": t0,
            "t1": t1,
            "timescale": self.store.timescale,
            "capture": bool(getattr(self.config, "capture", False)),
            "trace": str(self.trace_path),
            "trace_name": self.trace_path.name.removesuffix(".vtx"),
            "top": getattr(self.graph, "top", "") or self.top,
            "source_sha256": self.store.source_sha256,
            "has_rtl": self.graph is not None,
            "rtl_files": self.rtl_paths,
            "rtl_sha256": self.rtl_sha256,
            "rtl_changed": self.rtl_changed,
            "n_rtl_signals": len(self.graph) if self.graph else 0,
            "rtl_error": self.rtl_error,
            "layout_error": self.layout_file.last_error,
            "layout_backup": (
                str(self.layout_file.backup_path)
                if self.layout_file.backup_path is not None
                else None
            ),
            # §4.3's UI options are real inputs, not parser-only fields.  The
            # layout endpoint applies them as defaults; exposing them here also
            # makes the active configuration inspectable by any client.
            "ui": {
                "row_height": getattr(self.config, "row_height", "compact"),
                "radix": dict(getattr(self.config, "radix_globs", {}) or {}),
            },
        }

    def load_layout(self) -> dict[str, Any]:
        layout = self.layout_file.load()
        # Saved choices win; config globs fill only signals the user has never
        # assigned a radix to.  This is the adoption feature §4.3 promises — a
        # fresh session opens with addresses in hex and counters in decimal,
        # while a hand-picked binary row survives every reload (P5).
        valid_radices = {"hex", "dec", "bin", "ascii", "enum"}
        configured = {
            s.path: radix
            for s in self.store.signals()
            if (radix := getattr(self.config, "radix_for", lambda _p: None)(s.path))
            in valid_radices
        }
        configured.update(
            {
                str(path): str(radix)
                for path, radix in dict(layout.get("radix") or {}).items()
                if radix in valid_radices
            }
        )
        layout["radix"] = configured
        if "rowH" not in layout:
            layout["rowH"] = (
                28
                if getattr(self.config, "row_height", "compact") == "comfortable"
                else 20
            )
        return layout

    def save_layout(self, layout: dict[str, Any]) -> dict[str, Any]:
        return self.layout_file.save(layout)


@dataclass(slots=True)
class SessionJob:
    """Observable setup state for a session being opened in the background."""

    session_id: str
    trace_path: Path
    phase: str = "queued"
    progress: float = 0.0
    error: str = ""

    def status(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "progress": self.progress,
            "correlation_rate": None,
            "trace": str(self.trace_path),
            "error": self.error,
        }


class SessionRegistry:
    """Open sessions, keyed by their deterministic id."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._jobs: dict[str, SessionJob] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _requested_id(trace_path: Path) -> str:
        # Raw dumps have a deterministic canonical cache name even before the
        # converter has run. That lets POST return a stable id immediately.
        from veritrace.store import identity

        return session_id_for(identity(trace_path))

    @staticmethod
    def _reopen_inputs(existing: Session, store_path: Path, store: TraceStore,
                       rtl_paths: list[str] | None, top: str | None) -> tuple[list[str] | None, str | None]:
        from veritrace.build import MANIFEST

        chosen_rtl = existing.rtl_paths if rtl_paths is None else rtl_paths
        chosen_top = existing.top if top is None else top
        # Inherit a viewer's choices for the same trace. A new recorded build
        # has its own sources/top, not yesterday's; explicit requests and an
        # existing waveform-only selection still take precedence.
        if store.source_sha256 != existing.store.source_sha256 and (store_path / MANIFEST).is_file():
            if rtl_paths is None and existing.rtl_paths:
                chosen_rtl = None
            if top is None:
                chosen_top = None
        return chosen_rtl, chosen_top

    def start(
        self,
        trace_path: str | Path,
        rtl_paths: list[str] | None = None,
        top: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Start an observable session setup and return without blocking."""
        path = Path(trace_path)
        if not path.exists():
            raise FileNotFoundError(f"no such trace: {path}")
        sid = self._requested_id(path)
        with self._lock:
            existing = self._sessions.get(sid)
            pending = self._jobs.get(sid)
            if pending is not None and pending.phase != "error":
                return sid, pending.status()
            job = SessionJob(sid, path.resolve())
            self._jobs[sid] = job
            initial = job.status()

        def update(phase: str, pct: float) -> None:
            with self._lock:
                job.phase = phase
                job.progress = max(job.progress, min(0.99, float(pct)))

        def worker() -> None:
            try:
                # Every POST means reopen the requested inputs.  Returning an
                # already-open object here bypassed rerun/top/RTL changes even
                # though the synchronous registry path supported them.
                chosen_rtl, chosen_top = rtl_paths, top
                if existing is not None:
                    from veritrace import store as store_mod

                    update("converting", 0.05)
                    current = store_mod.ensure(path)
                    chosen_rtl, chosen_top = self._reopen_inputs(
                        existing, current, TraceStore(str(current)), rtl_paths, top,
                    )
                session = Session.open(path, chosen_rtl, chosen_top, progress=update)
            except Exception as exc:  # noqa: BLE001 - surfaced through /status
                with self._lock:
                    job.phase = "error"
                    job.error = f"{type(exc).__name__}: {exc}"
                return
            with self._lock:
                # The public id belongs to the request, not to an immutable
                # cache generation selected midway through conversion.
                session.session_id = sid
                self._sessions[sid] = session
                self._jobs.pop(sid, None)

        threading.Thread(
            target=worker,
            name=f"veritrace-session-{sid}",
            daemon=True,
        ).start()
        return sid, initial

    def open(
        self,
        trace_path: str | Path,
        rtl_paths: list[str] | None = None,
        top: str | None = None,
    ) -> Session:
        from veritrace import store as store_mod

        # Keyed on the store, not on what was typed: `dump.vcd` and the
        # `dump.vcd.vtx` it converts to are one session, or opening the same
        # trace by its two names would build the graph twice.
        requested_path = Path(trace_path)
        store_path = store_mod.ensure(requested_path)
        sid = self._requested_id(requested_path)
        existing = self._sessions.get(sid)
        if existing is not None:
            # A simulator may atomically replace a store at the same path.
            # Path-only identity is useful for persistent URLs, but it cannot
            # make the old mmap authoritative after a rerun.
            probe = TraceStore(str(store_path))

            def fingerprint(store: TraceStore) -> tuple[Any, ...]:
                return (
                    store.source_sha256,
                    getattr(store, "source_bytes", None),
                    store.n_signals,
                    store.n_events,
                    store.time_range,
                    store.timescale,
                )

            if fingerprint(probe) != fingerprint(existing.store):
                chosen_rtl, chosen_top = self._reopen_inputs(existing, store_path, probe, rtl_paths, top)
                replacement = Session.open(requested_path, chosen_rtl, chosen_top)
                self._sessions[sid] = replacement
                return replacement

            # ``None`` means the caller did not express an RTL preference;
            # an explicit empty list means waveform-only mode.  Any changed
            # design/top selection invalidates why trees and every automatic
            # analysis that consumed the graph.
            if rtl_paths is not None or top is not None:
                requested = existing.rtl_paths if rtl_paths is None else [str(Path(p).resolve()) for p in rtl_paths]
                top_changed = top is not None and top != existing.top
                if requested != existing.rtl_paths or top_changed:
                    existing.reconfigure(requested, top)
            return existing
        session = Session.open(trace_path, rtl_paths, top)
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            if session_id in self._jobs:
                return None
            return self._sessions.get(session_id)

    def status(self, session_id: str) -> dict[str, Any] | None:
        """Ready session status or the live setup/error state for its job."""
        with self._lock:
            job = self._jobs.get(session_id)
            if job is not None:
                return job.status()
            session = self._sessions.get(session_id)
            if session is not None:
                return session.status()
            return None

    def pending(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._jobs

    def all(self) -> list[Session]:
        """Every open session, in the order they were opened (§11.4's TAB 5)."""
        with self._lock:
            return list(self._sessions.values())

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return session_id in self._sessions or session_id in self._jobs

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
