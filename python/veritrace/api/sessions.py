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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veritrace._native import TraceStore

#: Bumped when the on-disk layout schema changes incompatibly.
LAYOUT_VERSION = 1


def session_id_for(trace_path: Path) -> str:
    """Stable id derived from the trace location.

    Deterministic on purpose (P1): reopening the same dump — in a later process,
    after a reboot — yields the same id, so a client that remembers a session
    finds its layout again instead of starting blank.
    """
    resolved = str(trace_path.resolve()).replace("\\", "/").lower()
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
        # §11.4: a suppressed finding, and the mandatory reason for it. Without
        # the reason the list becomes a graveyard nobody dares to empty.
        "suppressions": {},
    }


class LayoutFile:
    """The JSON sidecar holding one session's user state."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_trace(cls, trace_path: Path) -> LayoutFile:
        # dump.vtx -> dump.vtx.session.json, a sibling of the store directory.
        return cls(trace_path.with_name(trace_path.name + ".session.json"))

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_layout()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt sidecar must not make the trace unopenable (P7).
            return default_layout()
        if not isinstance(data, dict):
            return default_layout()
        merged = default_layout()
        merged.update(data)
        merged["version"] = LAYOUT_VERSION
        return merged

    def save(self, layout: dict[str, Any]) -> dict[str, Any]:
        merged = default_layout()
        # Keys the UI does not own must survive a layout PUT: provenance is the
        # RTL hash §5.7's check depends on, and suppressions are set through
        # their own endpoint. A wave-layout save must not wipe either.
        existing = self.load() if self.path.exists() else {}
        for key in ("provenance", "suppressions"):
            if key in existing:
                merged[key] = existing[key]
        merged.update(layout)
        merged["version"] = LAYOUT_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted save cannot truncate the file.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
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

    @classmethod
    def open(
        cls,
        trace_path: str | Path,
        rtl_paths: list[str] | None = None,
        top: str | None = None,
    ) -> Session:
        from veritrace import config as cfg

        path = Path(trace_path)
        if not path.exists():
            raise FileNotFoundError(f"no such trace: {path}")
        store = TraceStore(str(path))
        session = cls(
            session_id=session_id_for(path),
            trace_path=path,
            store=store,
            layout_file=LayoutFile.for_trace(path),
            rtl_paths=[str(p) for p in (rtl_paths or [])],
            top=top,
            config=cfg.load_or_empty(path.parent),
        )
        if rtl_paths:
            session._load_rtl([Path(p) for p in rtl_paths], top)
        session.analyse()
        return session

    # --- automatic analysis (§1.4, §13.4) --------------------------------

    def analyse(self) -> Any:
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
        from veritrace.analysis import checks

        self.clock = clocks.resolve(self.store, self.graph, self.config)
        self._extract()
        self._measure()
        self._memory()
        self.report = checks.run_all(
            self.store,
            self.graph,
            self.elaboration,
            self.clock,
            self.config,
            self.protocol,
            self.performance.liveness if self.performance is not None else None,
            self.memory,
        )
        return self.report

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
            )
        except Exception as e:  # noqa: BLE001 - memory analysis must not close the trace
            self.memory = None
            self.memory_error = str(e)

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
        layout = self.layout_file.load()
        layout.setdefault("suppressions", {})[finding_id] = reason
        self.layout_file.save(layout)
        return layout["suppressions"]

    def unsuppress(self, finding_id: str) -> dict[str, str]:
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
            self.elaboration = elaborate(expanded, top=top)
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
            "clock": self.clock.path if self.clock else None,
            "clock_method": self.clock.method if self.clock else None,
            "n_cycles": self.clock.n_cycles if self.clock else 0,
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
            "source_sha256": self.store.source_sha256,
            "has_rtl": self.graph is not None,
            "rtl_files": self.rtl_paths,
            "rtl_sha256": self.rtl_sha256,
            "rtl_changed": self.rtl_changed,
            "n_rtl_signals": len(self.graph) if self.graph else 0,
            "rtl_error": self.rtl_error,
        }

    def load_layout(self) -> dict[str, Any]:
        return self.layout_file.load()

    def save_layout(self, layout: dict[str, Any]) -> dict[str, Any]:
        return self.layout_file.save(layout)


class SessionRegistry:
    """Open sessions, keyed by their deterministic id."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def open(
        self,
        trace_path: str | Path,
        rtl_paths: list[str] | None = None,
        top: str | None = None,
    ) -> Session:
        sid = session_id_for(Path(trace_path))
        existing = self._sessions.get(sid)
        if existing is not None:
            # Re-opening with RTL when the session was opened without it should
            # upgrade the session rather than silently ignore the sources.
            if rtl_paths and existing.graph is None:
                existing.rtl_paths = [str(p) for p in rtl_paths]
                existing._load_rtl([Path(p) for p in rtl_paths], top or existing.top)
                # The graph unlocks the checks that need it, so re-run them.
                existing.analyse()
            return existing
        session = Session.open(trace_path, rtl_paths, top)
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)
