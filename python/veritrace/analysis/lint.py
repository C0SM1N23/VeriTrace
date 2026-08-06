"""Static checks focused on sim/synth mismatch — §8.11, Group A.

The reframing in §8.11 is the whole design of this module. Generic lint is done
better by Verilator and slang, and competing with them would be wasted work.
The subset that matters is the class *"works in simulation, fails on the
board"* — the most expensive kind of pain on an FPGA, and nobody's priority,
because catching it needs **both the graph and the trace**.

So the module splits three ways:

* **Aggregated.** Inferred latches and multi-driver conflicts come from slang's
  own dataflow analysis (`SLANG_CHECKS`). A real flow analysis beats any
  pattern match over the statement tree, and §8.11 says so outright: *"do not
  compete, aggregate"*.
* **Structural.** Blocking in `always_ff`, non-blocking in `always_comb`, an
  incomplete sensitivity list, an unsynchronised clock crossing, an async reset
  with no synchronous deassert — all derivable from the graph alone.
* **Correlated with the trace.** Dependence on an initial value, X-optimism,
  and the sampled cycles that turn a structural CDC warning into evidence.
  This is the part no static tool can produce, and the reason the module
  exists at all.

The honesty note of §8.11 about CDC is attached to every CDC finding, not
buried in documentation: the check is structural, not formal sign-off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from veritrace.analysis.findings import Finding, Group, Severity
from veritrace.clocks import Clock
from veritrace.graph.model import (
    Const,
    DesignGraph,
    Driver,
    DriverKind,
    Kind,
    Ref,
    Signal,
    SignalId,
    refs,
    to_text,
)

#: §8.11's own words, kept verbatim on every CDC finding. A structural check
#: that lets itself be read as sign-off is worse than no check.
CDC_NOTE = (
    "Structural check: it finds a crossing with no two-flop synchroniser on the "
    "path. It is not formal CDC sign-off — no reconvergence analysis, no "
    "handshake-protocol proof, no glitch analysis. For a production design "
    "those need a dedicated CDC tool."
)

#: slang diagnostics worth surfacing as findings, and what to call them.
#: Everything not listed here is dropped, so slang's unused-variable chatter
#: never reaches the Checks tab.
SLANG_CHECKS: dict[str, tuple[str, Severity, str]] = {
    "InferredLatch": (
        "inferred_latch",
        Severity.ERROR,
        "synthesis infers a latch; simulation does not show one",
    ),
    "CaseDefault": (
        "case_no_default",
        Severity.WARN,
        "synthesis may choose differently from simulation for the uncovered values",
    ),
    "CaseIncomplete": (
        "case_no_default",
        Severity.WARN,
        "synthesis may choose differently from simulation for the uncovered values",
    ),
    "MultipleAlwaysAssigns": (
        "multi_driver",
        Severity.ERROR,
        "more than one procedural block drives this",
    ),
    "MultipleContAssigns": (
        "multi_driver",
        Severity.ERROR,
        "more than one continuous assignment drives this",
    ),
    "MultipleUWireDrivers": (
        "multi_driver",
        Severity.ERROR,
        "a `uwire` with more than one driver",
    ),
    "UndrivenNet": ("undriven", Severity.WARN, "read but never assigned"),
    "UndrivenPort": ("undriven", Severity.WARN, "read but never assigned"),
}

#: Every check this module can produce, for `--fail-on` and `checks.disable`.
CHECKS: dict[str, str] = {
    "inferred_latch": "latch inferred in combinational logic",
    "case_no_default": "case without a default label",
    "initial_value_dependency": "logic depends on a declared initial value",
    "x_optimism": "mux selected with an X selector",
    "blocking_in_always_ff": "blocking assignment inside always_ff",
    "nonblocking_in_always_comb": "non-blocking assignment inside always_comb",
    "incomplete_sensitivity": "sensitivity list misses a signal the block reads",
    "async_reset_no_sync": "asynchronous reset with no synchronous deassert",
    "cdc_no_sync": "clock-domain crossing with no synchroniser",
    "multi_driver": "more than one driver",
    "undriven": "read but never driven",
}


# --- aggregation from slang ------------------------------------------------


def signal_at(graph: DesignGraph, file: str, line: int) -> str | None:
    """The signal written by the procedural block covering `file:line`.

    A front-end diagnostic points at the construct it dislikes — a `case`
    keyword, an `always_comb` header — not at a signal. The blocks carry their
    exact line span, so the mapping is containment rather than nearest-match.
    The tightest enclosing block wins, which matters for nested blocks.
    """
    best: tuple[int, str] | None = None
    for sig in graph:
        for d in sig.drivers:
            if d.block_lines is None or d.loc.file != file:
                continue
            lo, hi = d.block_lines
            if lo <= line <= hi and (best is None or hi - lo < best[0]):
                best = (hi - lo, sig.path)
    return best[1] if best else None


def from_diagnostics(diagnostics: Iterable[Any], graph: DesignGraph) -> Iterator[Finding]:
    """Map the curated slang diagnostics onto findings."""
    for d in diagnostics:
        mapped = SLANG_CHECKS.get(d.code)
        if mapped is None:
            continue
        check, severity, detail = mapped
        path = d.named_signal()
        # slang names a signal relative to its scope; only keep it if it lands
        # on something the graph actually knows, so `[why]` cannot dangle.
        signal = path if path and graph.get(path) is not None else None
        if signal is None:
            signal = signal_at(graph, d.loc.file, d.loc.line)
        yield Finding(
            group=Group.LINT,
            severity=severity,
            check=check,
            title=d.message,
            signal=signal,
            loc=d.loc,
            detail=detail,
            why=f"why({signal})" if signal else None,
        )


# --- structural checks over the graph --------------------------------------


def _assignment_style(graph: DesignGraph) -> Iterator[Finding]:
    """§8.11: blocking in `always_ff`, non-blocking in `always_comb`.

    Both change evaluation order between simulation and synthesis, and both are
    a single bit already recorded on the driver.
    """
    for sig in graph:
        for d in sig.drivers:
            if d.kind is DriverKind.ALWAYS_FF and not d.non_blocking:
                yield Finding(
                    group=Group.LINT,
                    severity=Severity.ERROR,
                    check="blocking_in_always_ff",
                    title=f"blocking assignment to '{sig.id.name}' inside always_ff",
                    signal=sig.path,
                    loc=d.loc,
                    detail="synthesis and simulation can order this differently; use <=",
                    why=f"why({sig.path})",
                )
            elif d.kind is DriverKind.ALWAYS_COMB and d.non_blocking:
                yield Finding(
                    group=Group.LINT,
                    severity=Severity.WARN,
                    check="nonblocking_in_always_comb",
                    title=f"non-blocking assignment to '{sig.id.name}' inside always_comb",
                    signal=sig.path,
                    loc=d.loc,
                    detail="the block will not re-evaluate as synthesis expects; use =",
                    why=f"why({sig.path})",
                )


def _sensitivity(graph: DesignGraph) -> Iterator[Finding]:
    """§8.11: an `always @(...)` list that misses something the block reads.

    Synthesis ignores the list and builds the logic from the reads; simulation
    obeys it. The two then disagree, which is the definition of the class.
    `always_comb` cannot be wrong this way, so it carries no list to check.
    """
    seen: set[tuple[str, int]] = set()
    for sig in graph:
        for d in sig.drivers:
            if d.sensitivity is None:
                continue
            listed = {s.path() for s in d.sensitivity}
            read = {s.path() for s in refs(d.guard)} | {s.path() for s in refs(d.value)}
            missing = sorted(read - listed)
            if not missing:
                continue
            key = (d.loc.file, d.loc.line)
            if key in seen:
                continue
            seen.add(key)
            names = ", ".join(m.rsplit(".", 1)[-1] for m in missing)
            yield Finding(
                group=Group.LINT,
                severity=Severity.ERROR,
                check="incomplete_sensitivity",
                title=f"sensitivity list misses {names}",
                signal=sig.path,
                loc=d.loc,
                detail="synthesis builds this from the reads and ignores the list; use always_comb",
                why=f"why({sig.path})",
                related=tuple(missing),
            )


# --- clock domains ---------------------------------------------------------


@dataclass(slots=True)
class _Domains:
    """Which clock each signal ultimately comes from.

    A flop belongs to the clock it is written on. Anything combinational
    belongs to whatever feeds it, so the walk continues through logic until it
    reaches a flop or a design input. The result is a set, because a signal
    genuinely can be fed from two domains — and that is itself the crossing.

    Clocks are identified by **net**, not by path. One clock reaching four
    instances appears as `top.a.clk`, `top.b.clk`, `top.c.clk` and `top.clk`,
    and comparing those as strings makes every crossing between two instances
    look like a CDC. A checker that cries wolf on a single-clock design is worse
    than no checker, because it is the reason people stop reading the output.
    """

    graph: DesignGraph
    _memo: dict[str, frozenset[str]] = None  # type: ignore[assignment]
    _canon: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._memo = {}
        self._canon = {}

    def canonical(self, path: str) -> str:
        """The topmost name of the net `path` is connected to.

        Follows pass-through drivers — an instance port connection, or a plain
        `assign a = b;` — up to the source that actually generates the signal.
        """
        seen: set[str] = set()
        cur = path
        while cur not in seen:
            if cur in self._canon:
                cur = self._canon[cur]
                break
            seen.add(cur)
            sig = self.graph.get(cur)
            if sig is None or len(sig.drivers) != 1:
                break
            d = sig.drivers[0]
            if d.is_sequential or not isinstance(d.value, Ref):
                break
            cur = d.value.signal.path()
        for p in seen:
            self._canon[p] = cur
        return cur

    def of(self, path: str, _stack: frozenset[str] = frozenset()) -> frozenset[str]:
        if path in self._memo:
            return self._memo[path]
        if path in _stack:
            return frozenset()  # feedback: contributes no new domain
        sig = self.graph.get(path)
        if sig is None or not sig.drivers:
            return frozenset()
        seq = [d for d in sig.drivers if d.is_sequential and d.clock is not None]
        if seq:
            out = frozenset(self.canonical(d.clock.path()) for d in seq)
        else:
            inner = _stack | {path}
            out = frozenset(
                c
                for d in sig.drivers
                for r in refs(d.value)
                for c in self.of(r.path(), inner)
            )
        # Only memoise results that did not depend on where the walk started.
        if not _stack:
            self._memo[path] = out
        return out


def _is_flop_of(graph: DesignGraph, path: str, source: str, clock: str) -> bool:
    """True when `path` is a flop on `clock` whose data is exactly `source`.

    "Exactly" matters: a synchroniser stage is a bare register. As soon as
    there is logic on the path the crossing is no longer protected, which is
    the bug this check is about.
    """
    sig = graph.get(path)
    if sig is None:
        return False
    for d in sig.drivers:
        if not d.is_sequential or d.clock is None or d.clock.path() != clock:
            continue
        # Reset branches assign a constant; the data branch is the one to test.
        if isinstance(d.value, Const):
            continue
        if isinstance(d.value, Ref) and d.value.signal.path() == source:
            return True
    return False


def _has_two_flop_sync(graph: DesignGraph, first: str, source: str, clock: str) -> bool:
    """`source -> first -> second`, both flops on `clock`, no logic between."""
    if not _is_flop_of(graph, first, source, clock):
        return False
    return any(_is_flop_of(graph, nxt, first, clock) for nxt in graph.fanout(first))


def _posedges(store: Any, graph: DesignGraph, clock_path: str, cache: dict[str, set[int]]) -> set[int]:
    """Rising edges of an arbitrary clock, memoised for the whole scan."""
    if clock_path in cache:
        return cache[clock_path]
    sig = graph.get(clock_path)
    out: set[int] = set()
    if sig is not None and sig.trace_handle is not None:
        t0, t1 = store.time_range
        out = {
            t
            for t, v in store.transitions(sig.trace_handle, t0, t1 + 1)
            if v.to_int() == 1
        }
    cache[clock_path] = out
    return out


def _sampled_in_risky_window(
    store: Any,
    graph: DesignGraph,
    source: str,
    dst_clock: str,
    numbering: Clock | None,
    cache: dict[str, set[int]],
    limit: int = 4,
) -> list[int]:
    """Cycles where the source changed on the very edge that captured it.

    The edges are the *destination* clock's — that is the one doing the
    sampling. Cycle numbers are still the primary clock's, because that is the
    only numbering §5.5 lets the rest of the tool speak in.

    In a zero-delay RTL simulation the observable danger is a source transition
    landing on the same simulation time as the destination clock edge: the
    order the simulator picks then decides the sampled value, and hardware has
    no such rule. That is the honest trace-side evidence a dump can support —
    not a setup/hold analysis, which would need timing data it has not got.
    """
    sig = graph.get(source)
    if sig is None or sig.trace_handle is None:
        return []
    t0, t1 = store.time_range
    changes = {t for t, _ in store.transitions(sig.trace_handle, t0, t1 + 1)}
    hits = sorted(changes & _posedges(store, graph, dst_clock, cache))
    if numbering is None:
        return hits[:limit]
    return [numbering.cycle_of(t) for t in hits[:limit]]


def _cdc(graph: DesignGraph, store: Any, clock: Clock | None) -> Iterator[Finding]:
    """§8.11: a crossing between clock domains with no two-flop synchroniser."""
    domains = _Domains(graph)
    reported: set[tuple[str, str]] = set()
    edge_cache: dict[str, set[int]] = {}
    for sig in graph:
        for d in sig.drivers:
            if not d.is_sequential or d.clock is None:
                continue
            dst_clock = d.clock.path()
            # Two names, on purpose: the *path* is what the trace is indexed by,
            # the *net* is what a domain comparison must use. One clock reaching
            # four instances has four paths and one net.
            dst_domain = domains.canonical(dst_clock)
            for r in refs(d.value):
                src = r.path()
                foreign = domains.of(src) - {dst_domain}
                if not foreign:
                    continue
                if _has_two_flop_sync(graph, sig.path, src, dst_clock):
                    continue
                key = (src, sig.path)
                if key in reported:
                    continue
                reported.add(key)
                src_clock = sorted(foreign)[0]
                cycles = (
                    _sampled_in_risky_window(
                        store, graph, src, dst_clock, clock, edge_cache
                    )
                    if store is not None
                    else []
                )
                notes = [CDC_NOTE]
                if cycles:
                    # The line §8.11 asks for: what a static tool cannot say.
                    notes.insert(
                        0,
                        f"sampled in the risky window {len(cycles)}x in this trace, at "
                        + ", ".join(f"c{c}" for c in cycles),
                    )
                yield Finding(
                    group=Group.LINT,
                    severity=Severity.ERROR,
                    check="cdc_no_sync",
                    title=(
                        f"{src} ({src_clock.rsplit('.', 1)[-1]}) crosses into "
                        f"{sig.path} ({dst_clock.rsplit('.', 1)[-1]}) unsynchronised"
                    ),
                    signal=sig.path,
                    loc=d.loc,
                    detail="no two-flop synchroniser on the path",
                    why=f"why({sig.path})",
                    notes=tuple(notes),
                    related=(src,),
                )


def _async_reset(graph: DesignGraph) -> Iterator[Finding]:
    """§8.11: an async reset whose release is not synchronised to the clock.

    Asserting asynchronously is fine and intended. Releasing asynchronously is
    what causes metastability, invisibly in simulation, because the deassert
    can land arbitrarily close to a clock edge.
    """
    reported: set[tuple[str, str]] = set()
    for sig in graph:
        for d in sig.drivers:
            if not d.async_reset or d.reset is None or d.clock is None:
                continue
            reset, clk = d.reset.path(), d.clock.path()
            if (reset, clk) in reported:
                continue
            # Synchronised when the reset is itself registered on this clock —
            # the deassert then only ever moves on an edge.
            rsig = graph.get(reset)
            if rsig is not None and any(
                dd.is_sequential and dd.clock is not None and dd.clock.path() == clk
                for dd in rsig.drivers
            ):
                continue
            reported.add((reset, clk))
            yield Finding(
                group=Group.LINT,
                severity=Severity.WARN,
                check="async_reset_no_sync",
                title=f"{reset} is async on {clk.rsplit('.', 1)[-1]} with no synchronised release",
                signal=sig.path,
                loc=d.loc,
                detail="assert async, release on a clock edge: register the deassert",
                why=f"why({reset})",
                related=(reset,),
            )


# --- checks that need the trace --------------------------------------------


def _initial_value(graph: DesignGraph, store: Any, clock: Clock | None) -> Iterator[Finding]:
    """§8.11: `reg x = 0;` read before anything writes it.

    Works in simulation, is X on an ASIC and non-deterministic on an FPGA
    without an explicit initial-value guarantee. The trace decides it: the
    signal still holds its declared value at the first clock edge, and
    something reads it.
    """
    if clock is None or not clock.edges:
        return
    first_edge = clock.edges[0]
    for sig in graph:
        if not sig.has_initializer or sig.trace_handle is None:
            continue
        if not graph.fanout(sig.path):
            continue  # nothing reads it; the initial value cannot matter
        if store.last_change_before(sig.trace_handle, first_edge + 1) is not None:
            continue  # written before anything could sample it
        value = store.value_at(sig.trace_handle, first_edge)
        yield Finding(
            group=Group.LINT,
            severity=Severity.WARN,
            check="initial_value_dependency",
            title=f"'{sig.id.name}' is read at its declared initial value {value}",
            signal=sig.path,
            loc=sig.decl_loc,
            time=first_edge,
            detail="X on an ASIC, non-deterministic on an FPGA without an init guarantee",
            why=f"why({sig.path} @ {first_edge})",
        )


def _x_optimism(graph: DesignGraph, store: Any) -> Iterator[Finding]:
    """§8.11: a mux branch taken while its selector is X in the trace.

    The simulator picks a branch and propagates a clean value; the hardware has
    a real selector and may take the other one. The result looks correct in
    simulation for reasons that will not hold on the board.
    """
    x_at = {h: t for h, t in store.first_x_all()}
    if not x_at:
        return
    reported: set[str] = set()
    for sig in graph:
        if sig.trace_handle is None:
            continue
        for d in sig.drivers:
            if d.is_sequential or isinstance(d.guard, Const):
                continue
            for r in refs(d.guard):
                selector = graph.get(r.path())
                if selector is None or selector.trace_handle is None:
                    continue
                t = x_at.get(selector.trace_handle)
                if t is None:
                    continue
                target = store.value_at(sig.trace_handle, t)
                if target is None or not target.is_two_state():
                    continue  # the X propagated honestly; nothing optimistic here
                if sig.path in reported:
                    continue
                reported.add(sig.path)
                yield Finding(
                    group=Group.LINT,
                    severity=Severity.WARN,
                    check="x_optimism",
                    title=f"'{sig.id.name}' resolves to {target} while {r.path()} is X",
                    signal=sig.path,
                    loc=d.loc,
                    time=t,
                    detail=f"selector: {to_text(d.guard)}",
                    why=f"why({r.path()} @ {t})",
                    related=(r.path(),),
                )
                break


# --- entry point -----------------------------------------------------------


def scan(
    graph: DesignGraph,
    diagnostics: Iterable[Any] = (),
    store: Any = None,
    clock: Clock | None = None,
    config: Any = None,
) -> Iterator[Finding]:
    """Every Group A check, plus the aggregated slang findings."""
    sources: list[Iterator[Finding]] = [
        from_diagnostics(diagnostics, graph),
        _assignment_style(graph),
        _sensitivity(graph),
        _cdc(graph, store, clock),
        _async_reset(graph),
    ]
    if store is not None:
        sources += [_initial_value(graph, store, clock), _x_optimism(graph, store)]

    for it in sources:
        for finding in it:
            if config is not None and finding.signal and config.is_ignored(finding.signal):
                continue
            yield finding
