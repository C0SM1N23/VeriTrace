"""RTL -> design graph, via pyslang's *elaborated* AST.

Elaboration rather than parsing is the whole point (§4.1): parameters are
resolved, generate blocks are expanded and instance arrays are unrolled, so
`hierarchicalPath` already reads `top.g_lane[2].fifo.wr_ptr` — the exact shape
the trace uses. That is what makes the hard cases in §7.1 tractable instead of
requiring path reconstruction here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pyslang
from pyslang import driver as D

from veritrace.config import WORK_DIR
from veritrace.graph import conditions as C
from veritrace.graph.model import (
    DesignGraph,
    Driver,
    DriverKind,
    Kind,
    Ref,
    Signal,
    SignalId,
    SourceLoc,
    TRUE,
)

RTL_SUFFIXES = (".sv", ".v", ".svh", ".vh")

#: slang names the offending signal in the message but attaches the enclosing
#: scope as the symbol; pulling the quoted name out is what lets a diagnostic
#: become a finding with a working `[why]` (§11.4).
QUOTED_NAME_RE = re.compile(r"'([\w.\[\]$]+)'")


@dataclass(frozen=True, slots=True)
class Diag:
    """One slang diagnostic, kept structured rather than pre-rendered.

    §8.11 is explicit that VeriTrace should aggregate an existing front end
    rather than compete with it, so these are first-class inputs to the lint
    layer, not just console noise.
    """

    code: str
    severity: str  # "error" | "warning" | "note"
    message: str
    loc: SourceLoc
    #: Hierarchical path of the symbol slang blamed, when it attached one.
    symbol: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def downgraded(self) -> "Diag":
        """The same diagnostic as a note, with why it stopped being an error.

        §7.4b's rule: a module with no source is a boundary the tool is honest
        about, not a failure. Keeping it an error would make a design that uses
        encrypted IP — which is most designs that touch DDR or PCIe — open with
        a wall of red about something nobody can fix.
        """
        return Diag(
            code=self.code,
            severity="note",
            message=f"{self.message} — treated as a BLACKBOX_IP boundary (§7.4b)",
            loc=self.loc,
            symbol=self.symbol,
        )

    def named_signal(self) -> str | None:
        """The signal this diagnostic is about, as a hierarchical path.

        slang blames the enclosing scope but quotes the signal in the text, so
        the two are recombined here.
        """
        m = QUOTED_NAME_RE.search(self.message)
        if m is None:
            return self.symbol
        name = m.group(1)
        if "." in name or not self.symbol:
            return name
        # The symbol is sometimes the signal itself and sometimes its scope.
        if self.symbol == name or self.symbol.endswith("." + name):
            return self.symbol
        return f"{self.symbol}.{name}"

    def __str__(self) -> str:
        return f"{self.loc}: {self.severity} {self.code}: {self.message}"


@dataclass(slots=True)
class Elaboration:
    graph: DesignGraph
    #: Physical files read by slang, including headers reached via `include.
    sources: list[Path] = field(default_factory=list)
    #: Diagnostics from slang's parser and its dataflow analysis pass. Errors do
    #: not abort: a partially elaborated design is still worth analysing (P7).
    diagnostics: list[Diag] = field(default_factory=list)
    #: Port-connection equivalence classes, for §7.2 step (c).
    aliases: list[tuple[str, str]] = field(default_factory=list)
    #: Instance path -> {parameter name: value}, for the inspector of §8.11c.
    parameters: dict[str, dict[str, "ParamValue"]] = field(default_factory=dict)
    #: Instance path -> module name, so the parameter tree can name each level.
    instances: dict[str, str] = field(default_factory=dict)
    #: Sources withheld because they carry an encrypted P1735 block (§7.4b).
    #: Reported, never silently dropped: "the tool could not see inside this" is
    #: a different statement from "there was nothing there".
    protected: list[Path] = field(default_factory=list)

    @property
    def errors(self) -> list[Diag]:
        return [d for d in self.diagnostics if d.is_error]


@dataclass(frozen=True, slots=True)
class ParamValue:
    """An elaborated parameter, with whether this instance overrode it (§8.11c)."""

    name: str
    value: str
    overridden: bool
    local: bool
    loc: SourceLoc


def _kind_of(sym, direction: str | None) -> Kind:
    if direction == "In":
        return Kind.PORT_IN
    if direction in ("Out", "InOut"):
        return Kind.PORT_OUT
    k = str(sym.kind).rsplit(".", 1)[-1]
    if k == "Parameter":
        return Kind.PARAM
    if k == "Net":
        return Kind.WIRE
    t = getattr(sym, "type", None)
    if t is not None and getattr(t, "isUnpackedArray", False):
        return Kind.MEM
    return Kind.REG


def _width(sym) -> tuple[int, bool]:
    t = getattr(sym, "type", None)
    if t is None:
        return 1, False
    signed = bool(getattr(t, "isSigned", False))
    # For a memory, the useful width is one element, not the flattened array.
    if getattr(t, "isUnpackedArray", False):
        elem = getattr(t, "elementType", None)
        if elem is not None:
            return int(getattr(elem, "bitWidth", 1) or 1), signed
    return int(getattr(t, "bitWidth", 1) or 1), signed


def _depth(sym) -> int:
    """Declared element count of an unpacked array; 0 when it is not one.

    Needed because an *element index* is bounded by this and by nothing else
    (§5.6). Deriving the bound from the element width instead calls `mem[8]`
    out of range on a 64-entry memory of bytes, and deriving it from the
    elements that reached the trace does the same as soon as a simulator
    truncates the dump (Verilator's `--trace-max-array` defaults to 32).
    """
    t = getattr(sym, "type", None)
    if t is None or not getattr(t, "isUnpackedArray", False):
        return 0
    # slang reports the flattened bit count and the element type; their ratio
    # is the element count, and it survives multi-dimensional arrays where a
    # range would only describe the outermost dimension.
    elem = getattr(t, "elementType", None)
    total = int(getattr(t, "bitstreamWidth", 0) or 0)
    per = int(getattr(elem, "bitWidth", 0) or 0) if elem is not None else 0
    if total and per:
        return total // per
    rng = getattr(t, "range", None)
    return int(getattr(rng, "width", 0) or 0) if rng is not None else 0


class _SourceNames:
    """Keep short, stable names only when they identify exactly one source."""

    def __init__(self, sm) -> None:
        self.sm = sm
        self.paths = sorted({Path(sm.getFullPath(b)).resolve() for b in sm.getAllBuffers()
                             if sm.getFullPath(b)})
        self.duplicates = {p.name for p in self.paths
                           if sum(q.name == p.name for q in self.paths) > 1}

    def name(self, loc) -> str:
        name = Path(self.sm.getFileName(loc)).name
        if name in self.duplicates:
            return Path(self.sm.getFullPath(loc.buffer)).resolve().as_posix()
        return name


class _Elaborator:
    def __init__(self, sm, sources: _SourceNames) -> None:
        self.sm = sm
        self.sources = sources
        self.graph = DesignGraph()
        self.aliases: list[tuple[str, str]] = []
        self.parameters: dict[str, dict[str, ParamValue]] = {}
        self.instances: dict[str, str] = {}
        #: (direction, inner, outer, loc), applied by `finish` once every
        #: declaration in the design has been seen.
        self._connections: list[tuple[str, SignalId, SignalId, SourceLoc]] = []
        #: Paths written only from `initial`/`final` blocks.
        self._stimulus: set[str] = set()

    # -- helpers ---------------------------------------------------------

    def loc(self, obj) -> SourceLoc:
        """SourceLocation -> (file, line). Needs the SourceManager."""
        try:
            sr = getattr(obj, "sourceRange", None)
            loc = sr.start if sr is not None else obj.location
            return SourceLoc(
                self.sources.name(loc),
                int(self.sm.getLineNumber(loc)),
                int(self.sm.getColumnNumber(loc)),
            )
        except Exception:
            return SourceLoc("?", 0)

    def span(self, sym) -> tuple[int, int] | None:
        """First and last line of a symbol's syntax, from the source manager."""
        syn = getattr(sym, "syntax", None)
        sr = getattr(syn, "sourceRange", None)
        if sr is None:
            return None
        try:
            return int(self.sm.getLineNumber(sr.start)), int(self.sm.getLineNumber(sr.end))
        except Exception:
            return None

    def sym_of(self, sym) -> SignalId | None:
        """pyslang symbol -> SignalId, using its elaborated hierarchical path."""
        if sym is None:
            return None
        try:
            hp = sym.hierarchicalPath
        except Exception:
            hp = None
        if not hp:
            name = getattr(sym, "name", "")
            return SignalId((), name) if name else None
        return SignalId.parse(hp)

    def add_signal(self, sym, direction: str | None = None) -> Signal | None:
        sid = self.sym_of(sym)
        if sid is None or not sid.name:
            return None
        w, signed = _width(sym)
        return self.graph.add(
            Signal(
                id=sid,
                width=w,
                signed=signed,
                kind=_kind_of(sym, direction),
                decl_loc=self.loc(sym),
                has_initializer=getattr(sym, "initializer", None) is not None,
                depth=_depth(sym),
            )
        )

    def add_driver(self, target: SignalId, drv: Driver) -> None:
        sig = self.graph.get(target)
        if sig is None:
            sig = self.graph.add(
                Signal(target, 1, False, Kind.WIRE, drv.loc)
            )
        sig.drivers.append(drv)
        self.graph.invalidate()

    def add_parameter(self, sym) -> None:
        sid = self.sym_of(sym)
        if sid is None:
            return
        scope = ".".join(sid.hier) or self.graph.top
        self.parameters.setdefault(scope, {})[sid.name] = ParamValue(
            name=sid.name,
            value=str(getattr(sym, "value", "")),
            # pyslang exposes this directly, so "left at its default" is a fact
            # rather than an inference (§8.11c).
            overridden=bool(getattr(sym, "isOverridden", False)),
            local=bool(getattr(sym, "isLocalParam", False)),
            loc=self.loc(sym),
        )

    # -- traversal -------------------------------------------------------

    def visit_scope(self, scope) -> None:
        for m in scope:
            self.visit(m)

    def visit(self, sym) -> None:
        kind = str(sym.kind).rsplit(".", 1)[-1]

        if kind == "Port":
            inner = getattr(sym, "internalSymbol", None)
            self.add_signal(inner if inner is not None else sym, str(sym.direction).rsplit(".", 1)[-1])
            return
        if kind in ("Variable", "Net", "Parameter", "TypeParameter"):
            self.add_signal(sym)
            if kind == "Parameter":
                self.add_parameter(sym)
            elif kind == "Net":
                self.net_initializer(sym)
            return
        if kind == "ContinuousAssign":
            self.continuous(sym)
            return
        if kind == "ProceduralBlock":
            self.procedural(sym)
            return
        if kind == "Instance":
            self.instance(sym)
            return
        if kind == "StatementBlock":
            # Locals of a procedural block (loop counters, temporaries) are not
            # design signals — §5.1 has no Kind for them and no simulator dumps
            # them as nets. Counting them would only depress the match rate.
            return
        # Generate blocks, instance arrays, unnamed blocks: descend. pyslang has
        # already expanded them, so their members carry indexed paths.
        if getattr(sym, "isScope", False):
            self.visit_scope(sym)

    def net_initializer(self, sym) -> None:
        """`wire ready = a && !b;` — a continuous assignment in declaration form.

        The LRM makes this exactly equivalent to a separate `assign`, and RTL
        uses it constantly for one-line combinational terms. Missing it left
        those nets looking *undriven*, which is the worst possible answer: the
        causal walk stopped at a wire whose whole definition was one line above
        it, and reported the stop as a root cause.
        """
        init = getattr(sym, "initializer", None)
        target = self.sym_of(sym)
        if init is None or target is None:
            return
        self.add_driver(
            target,
            Driver(
                target=target,
                kind=DriverKind.CONT_ASSIGN,
                guard=TRUE,
                value=C.convert(init, self.sym_of),
                loc=self.loc(sym),
            ),
        )

    def continuous(self, sym) -> None:
        a = sym.assignment
        target = self.sym_of(a.left.getSymbolReference())
        if target is None:
            return
        self.add_driver(
            target,
            Driver(
                target=target,
                kind=DriverKind.CONT_ASSIGN,
                guard=TRUE,
                value=C.convert(a.right, self.sym_of),
                loc=self.loc(a),
            ),
        )

    def procedural(self, sym) -> None:
        pk = str(getattr(sym, "procedureKind", "")).rsplit(".", 1)[-1]
        if pk in ("Initial", "Final"):
            # Stimulus, not a driver of design state — but worth recording, so
            # a walk that reaches one can say "this comes from the testbench"
            # instead of "nothing drives this" (§8.1).
            # Deferred like port connections: the declaration may come later.
            self._stimulus.update(
                asg.target.path() for asg in C.assignments(sym.body, self.sym_of, self.loc)
            )
            return
        block_lines = self.span(sym)
        edges, levels = self.timing(sym.body)
        clock = edges[0] if edges else None
        reset = edges[1] if len(edges) > 1 else None
        seq = pk == "AlwaysFF" or (pk == "Always" and clock is not None)
        kind = DriverKind.ALWAYS_FF if seq else DriverKind.ALWAYS_COMB
        # An explicit level-sensitive list only exists on a bare `always @(...)`.
        # `always_comb` and `always_ff` have implicit or edge lists, which cannot
        # be incomplete, so `None` says "nothing to check" (§8.11).
        sensitivity = tuple(levels) if (levels and not seq) else None
        for asg in C.assignments(sym.body, self.sym_of, self.loc):
            self.add_driver(
                asg.target,
                Driver(
                    target=asg.target,
                    kind=kind,
                    guard=asg.guard,
                    value=asg.value,
                    loc=asg.loc,
                    clock=clock if seq else None,
                    reset=reset if seq else None,
                    non_blocking=asg.non_blocking,
                    sensitivity=sensitivity,
                    # A reset named in the event list is asynchronous by
                    # construction — that is what putting it there means.
                    async_reset=seq and reset is not None,
                    block_lines=block_lines,
                ),
            )

    def timing(self, body) -> tuple[list[SignalId], list[SignalId]]:
        """Split an `@(...)` header into edge-triggered and level-sensitive names.

        The first edge is the clock and any further edge an async reset, the
        convention every synthesisable design follows. Level-sensitive entries
        are a plain `always @(a or b)` sensitivity list.
        """
        if body is None or not str(body.kind).endswith("Timed"):
            return [], []
        edges: list[SignalId] = []
        levels: list[SignalId] = []
        for ev in _signal_events(getattr(body, "timing", None)):
            expr = getattr(ev, "expr", None)
            if expr is None or not hasattr(expr, "getSymbolReference"):
                continue
            sid = self.sym_of(expr.getSymbolReference())
            if sid is None:
                continue
            edged = str(getattr(ev, "edge", "")).rsplit(".", 1)[-1] not in ("None_", "None", "")
            (edges if edged else levels).append(sid)
        return edges, levels

    def instance(self, sym) -> None:
        body = getattr(sym, "body", None)
        path = str(self.sym_of(sym) or sym.name)
        module = _definition_name(sym)
        self.instances[path] = module
        if body is None:
            self.graph.blackboxes[path] = module
            return
        # Port connections tie two names to one signal (§7.1); record them as
        # equivalences so correlation can substitute one for the other.
        for conn in getattr(sym, "portConnections", ()) or ():
            port = getattr(conn, "port", None)
            expr = getattr(conn, "expression", None)
            if expr is None:
                # `.a()` — left dangling. An input port with nothing on it reads
                # as X for the whole run, which is the UNCONNECTED_PORT terminal
                # of §8.5.
                inner = self.sym_of(getattr(port, "internalSymbol", None) or port)
                target = self.graph.get(inner) if inner is not None else None
                if target is not None:
                    target.unconnected = True
                continue
            outer_expr, element = _connected_expr(expr)
            if outer_expr is None:
                continue
            inner = self.sym_of(getattr(port, "internalSymbol", None) or port)
            outer = self.sym_of(outer_expr.getSymbolReference())
            if inner is None or outer is None or inner == outer:
                continue
            if element is not None:
                # `.dout(lane_dout[i])` — one *element* of an array, and the
                # index is a constant because the generate loop is elaborated.
                # Recorded as `lane_dout[2] == g_lane[2].u_fifo.dout` so the
                # correlator can find the word without tying the whole array to
                # one lane (§7.1).
                self.aliases.append((inner.path(), f"{outer.path()}[{element}]"))
                continue
            self.aliases.append((inner.path(), outer.path()))
            # Deferred: the module body has not been visited yet, so the inner
            # port symbol may not be in the graph. Adding a driver now would
            # invent a placeholder signal with a guessed width and kind that the
            # real declaration could no longer replace.
            direction = str(getattr(port, "direction", "")).rsplit(".", 1)[-1]
            self._connections.append((direction, inner, outer, self.loc(expr)))
        self.visit_scope(body)

    def finish(self) -> None:
        """Apply everything that had to wait for the whole design to be visited.

        Turns every recorded port connection into an edge, and marks the
        signals that only testbench stimulus writes.

        Without these edges the graph stops at every module boundary: asking
        why a top-level output is X terminates on the port instead of walking
        into the instance that drove it, and a fan-in cone never leaves the
        module it started in. §5.4 treats a connection as an equivalence, and
        the directed form of an equivalence is a driver.
        """
        for direction, inner, outer, loc in self._connections:
            # In: the outer expression feeds the port. Out: the port feeds the
            # outer signal. InOut is genuinely both; §8.1's cycle guard copes.
            pairs: list[tuple[SignalId, SignalId]] = []
            if direction in ("In", "InOut"):
                pairs.append((inner, outer))
            if direction in ("Out", "InOut"):
                pairs.append((outer, inner))
            for target, source in pairs:
                width = getattr(self.graph.get(source), "width", 1) or 1
                self.add_driver(
                    target,
                    Driver(
                        target=target,
                        kind=DriverKind.INST_PORT,
                        guard=TRUE,
                        value=Ref(source, width),
                        loc=loc,
                    ),
                )
        # After the connections, so a signal that a port drives is never
        # mistaken for stimulus.
        for path in self._stimulus:
            sig = self.graph.get(path)
            if sig is not None and not sig.drivers:
                sig.stimulus_only = True
        self.graph.invalidate()


def _to_diag(d, sm, engine, sources: _SourceNames) -> Diag:
    """slang `Diagnostic` -> `Diag`, keeping code, severity, text and symbol.

    The engine renders the message, which is worth having verbatim: "latch
    inferred for 'o' because it is not assigned on all control paths" is a
    better finding than any paraphrase, and it names the signal.
    """
    code = str(getattr(d, "code", d)).replace("DiagCode(", "").rstrip(")")
    loc = SourceLoc("?", 0)
    severity, message, symbol = "warning", code, None
    try:
        loc = SourceLoc(
            sources.name(d.location),
            int(sm.getLineNumber(d.location)),
            int(sm.getColumnNumber(d.location)),
        )
    except Exception:
        pass
    try:
        severity = str(engine.getSeverity(d.code, d.location)).rsplit(".", 1)[-1].lower()
        message = engine.formatMessage(d)
    except Exception:
        pass
    sym = getattr(d, "symbol", None)
    if sym is not None:
        symbol = getattr(sym, "hierarchicalPath", None) or getattr(sym, "name", None)
    return Diag(code=code, severity=severity, message=message, loc=loc, symbol=symbol)


def _connected_expr(expr) -> tuple[object | None, int | None]:
    """`(outer signal, element index)` of a port connection.

    An *output* connection arrives as an `Assignment` writing the port into the
    outer signal, so the name is on its left; an input connection is the name
    itself. Missing the wrapper makes every output port invisible to both the
    alias table and the graph.

    `.dout(arr[i])` connects one *element*, and the index comes back with it.
    Treating that as an equivalence for the whole array would map every lane
    onto lane 0 (§7.1); dropping it entirely — which is what happened before —
    left the array uncorrelated instead.
    """
    if expr is None:
        return None, None
    if str(expr.kind).rsplit(".", 1)[-1] == "Assignment":
        expr = getattr(expr, "left", None)
        if expr is None:
            return None, None

    element: int | None = None
    if str(expr.kind).rsplit(".", 1)[-1] == "ElementSelect":
        selector = getattr(expr, "selector", None)
        value = getattr(selector, "constant", None) if selector is not None else None
        # Only a constant index names one element. A dynamic one selects a
        # different word every cycle and is not an equivalence at all.
        if value is None:
            return None, None
        try:
            element = int(str(value))
        except ValueError:
            return None, None
        expr = getattr(expr, "value", None)
        if expr is None:
            return None, None

    kind = str(expr.kind).rsplit(".", 1)[-1]
    if kind not in ("NamedValue", "HierarchicalValue"):
        return None, None
    return expr, element


def _definition_name(sym) -> str:
    """Module name behind an instance — `fifo_sync` for an instance named `dut`."""
    definition = getattr(sym, "definition", None)
    return getattr(definition, "name", None) or getattr(sym, "name", "?")


def _signal_events(timing) -> Iterable[object]:
    """Flatten a timing control into its `SignalEvent` nodes.

    The nodes rather than their expressions, because `edge` is what separates a
    clock from a sensitivity-list entry.
    """
    if timing is None:
        return ()
    kind = str(timing.kind).rsplit(".", 1)[-1]
    if kind == "SignalEvent":
        return (timing,)
    if kind == "EventList":
        out: list[object] = []
        for e in getattr(timing, "events", ()) or ():
            out.extend(_signal_events(e))
        return out
    return ()


#: IEEE P1735. Every vendor writes the same directive: Xilinx, Intel, Synopsys
#: and Cadence all emit `` `pragma protect begin_protected ``.
PROTECT = re.compile(rb"`pragma\s+protect\s+begin_protected", re.I)
#: Read only the head of a file — the directive is in the header, and an
#: encrypted IP can be megabytes of base64 nobody needs to scan.
PROTECT_SCAN = 64 * 1024


def is_protected(path: Path) -> bool:
    """Whether `path` carries an encrypted P1735 block."""
    try:
        with open(path, "rb") as f:
            return PROTECT.search(f.read(PROTECT_SCAN)) is not None
    except OSError:
        return False


class TopNotFound(ValueError):
    """The requested top module is not among the given sources.

    Its own type so the CLI can turn it into a one-line message rather than a
    traceback: it is the commonest way to point a command at the wrong design,
    and the answer is a sentence, not a stack.
    """


def elaborate(
    files: Sequence[str | Path],
    incdirs: Sequence[str | Path] = (),
    defines: Sequence[str] = (),
    top: str | None = None,
    analyse: bool = True,
) -> Elaboration:
    """Elaborate `files` and build the design graph.

    `analyse` runs slang's dataflow pass as well as its parser, which is what
    supplies the inferred-latch and multi-driver findings of §8.11. It costs a
    second pass over the design; turn it off where only the graph is wanted.
    """
    paths = [Path(f) for f in files]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"no such RTL file(s): {', '.join(missing)}")

    # §7.4b: encrypted IP is licensed content, and slang neither can nor should
    # parse it. A protected file is withheld from the compilation, which leaves
    # its modules unresolved — and an unresolved instance is already a
    # BLACKBOX_IP terminal, so the boundary needs no second mechanism. The rest
    # of the design stays completely analysable.
    protected = [p for p in paths if is_protected(p)]
    paths = [p for p in paths if p not in protected]

    # slang's own Driver already knows how to take include paths, defines and a
    # top module as command-line options, and how to load and parse sources.
    # Rebuilding that from option bags would be a lot of code for no gain.
    drv = D.Driver()
    drv.addStandardArgs()
    args = ["veritrace"]
    args += [f'"{p}"' for p in paths]
    args += [f'-I"{Path(d)}"' for d in incdirs]
    # Always defined, so a file can hide from elaboration what only a simulator
    # should see. The concrete need: `$dumpvars(1, dut.mem[0])` is how Icarus is
    # told to dump unpacked-array elements (§5.6), and it is a vendor extension
    # the LRM does not allow, so slang rejects it. Guarding it with
    # `ifndef VERITRACE` keeps both tools happy without changing what is dumped.
    args += ["-DVERITRACE=1"]
    # Icarus applies the last -D for a name; slang otherwise retains the first.
    # Config defaults precede command-line overrides, so handing both copies
    # to slang elaborates a different design from the waveform's compiler.
    effective_defines = {d.partition("=")[0]: d for d in defines}
    args += [f"-D{d}" for d in effective_defines.values()]
    if top:
        args += ["--top", top]
    if not drv.parseCommandLine(" ".join(args)):
        raise ValueError("slang rejected the RTL options")
    drv.processOptions()
    drv.parseAllSources()

    comp = drv.createCompilation()
    sm = drv.sourceManager
    engine = pyslang.DiagnosticEngine(sm)
    raw = list(comp.getAllDiagnostics())
    analysis_error = ""
    if analyse:
        # slang's dataflow pass is where `InferredLatch` and the multi-driver
        # findings come from. §8.11 says to aggregate an existing front end
        # rather than reimplement it, and a real flow analysis is strictly
        # better than any pattern match over the statement tree would be.
        try:
            raw += list(drv.runAnalysis(comp).getDiagnostics())
        except Exception as e:  # noqa: BLE001 - lint is a bonus, never a blocker (P7)
            # Degrading is fine; degrading in silence is not. Without this line
            # the inferred-latch and multi-driver checks simply produce nothing
            # and the Checks tab looks like a clean design.
            analysis_error = f"the dataflow analysis did not run: {e}"
    sources = _SourceNames(sm)
    diags = [_to_diag(d, sm, engine, sources) for d in raw]
    if analysis_error:
        diags.append(
            Diag(
                code="AnalysisUnavailable",
                severity="note",
                message=analysis_error + " — inferred latches and multi-driver "
                "conflicts are not reported for this design",
                loc=SourceLoc("", 0, 0),
            )
        )

    el = _Elaborator(sm, sources)
    root = comp.getRoot()
    tops = list(root.topInstances)
    if top and not tops:
        # Asking for a top module that is not in these sources gives slang no
        # top instance, and every later step reads that as "a design with no
        # signals" rather than as a question that cannot be answered. Refusing
        # here is what turns a confident wrong answer — `fsm` finding no state
        # machines, `why` rejecting a signal it then suggests — into a sentence
        # naming the mistake.
        names = sorted({d.name for d in comp.getDefinitions() if getattr(d, "name", "")})
        near = ", ".join(names[:8]) or "none"
        raise TopNotFound(
            f"top module {top!r} is not in these sources; found: {near}"
            + (" …" if len(names) > 8 else "")
        )
    el.graph.top = tops[0].name if tops else ""
    for inst in tops:
        el.instances[inst.name] = _definition_name(inst)
        el.visit_scope(inst.body)
    el.finish()

    # §7.4b: an instantiation of a module with no source is a *boundary*, not an
    # error. Recovered from the syntax tree, and the diagnostic is downgraded to
    # match — a design that legitimately uses encrypted IP must not open with a
    # wall of red.
    unknown = _unknown_modules(diags)
    if unknown:
        _blackbox_boundary(paths, unknown, el.graph, el.graph.top)
        diags = [
            d.downgraded() if _UNKNOWN.search(d.message) else d for d in diags
        ]

    return Elaboration(
        graph=el.graph,
        sources=sources.paths,
        diagnostics=diags,
        aliases=el.aliases,
        parameters=el.parameters,
        instances=el.instances,
        protected=protected,
    )


def discover(root: str | Path) -> list[Path]:
    """Every RTL file under `root`, for the `--rtl <dir>` convenience form.

    `WORK_DIR` is skipped. Everything the tool generates lands there — the
    `$dumpvars` module of §13.4b, the repro testbench of §8.3 — and picking those
    back up as design source means elaborating a module twice and failing to
    compile against a directory that simulated fine ten seconds earlier.
    """
    r = Path(root)
    if r.is_file():
        return [r]
    return sorted(
        p
        for p in r.rglob("*")
        if p.suffix in RTL_SUFFIXES and p.is_file() and WORK_DIR not in p.parts
    )


# --- §7.4b: modules with no source ------------------------------------------
#
# slang reports an instantiation of a module it cannot find as an *error* and
# builds no symbol for it, so the elaborated AST has nothing to attach to. The
# **syntax** tree still parses fine, and that is where the instance name and its
# port connections are — so the boundary is recovered from there.
#
# What this buys is the difference between two very different statements. A wire
# driven by an IP nobody can see reads as `never assigned` without this, which is
# what a floating net reads as; with it, why-trace stops at the boundary and says
# whose boundary it is.

#: `secret_phy u_phy (.clk(clk), .ready(phy_ready));`
_UNKNOWN = re.compile(r"unknown module '([^']+)'")


def _unknown_modules(diags: list["Diag"]) -> set[str]:
    return {m.group(1) for d in diags if (m := _UNKNOWN.search(d.message))}


def _blackbox_boundary(
    paths: list[Path], unknown: set[str], graph: DesignGraph, top: str
) -> None:
    """Register instances of `unknown` modules, and what they drive.

    Walks the syntax tree rather than the elaborated one, because for these
    modules there is no elaborated one. Only the *names* are taken — the type,
    the instance and the signals connected to it — which is all §7.4b claims to
    know about licensed content.
    """
    from pyslang import syntax as S

    for path in paths:
        try:
            tree = S.SyntaxTree.fromText(
                path.read_text(encoding="utf-8", errors="replace"), str(path)
            )
        except Exception:  # noqa: BLE001 - slang raises several unrelated types
            continue
        stack = [tree.root]
        while stack:
            node = stack.pop()
            for child in node:
                if isinstance(child, S.SyntaxNode):
                    stack.append(child)
            if node.kind != S.SyntaxKind.HierarchyInstantiation:
                continue
            module = str(node.type.valueText if hasattr(node.type, "valueText") else node.type)
            if module not in unknown:
                continue
            for inst in node.instances:
                name = getattr(getattr(inst, "decl", None), "name", None)
                if name is None:
                    continue
                scope = f"{top}.{name.valueText}" if top else str(name.valueText)
                graph.blackboxes[scope] = module
                for signal in _connected(inst, top, graph):
                    graph.blackbox_driven.add(signal)


def _connected(inst: object, top: str, graph: DesignGraph) -> list[str]:
    """Signals of the enclosing scope wired to this instance.

    A named connection carries an expression; only a plain identifier is taken.
    An expression like `.a(x & y)` connects several signals and none of them is
    *the* driver, so claiming one would be worse than claiming none.

    Direction cannot be read off an encrypted module, so it is inferred from the
    design instead: a wire the black box drives is one **nothing else drives**.
    A clock and a reset go *into* the IP and are driven from outside it, and
    marking those would stop why-trace at the clock — which explains nothing.
    """
    out: list[str] = []
    conns = getattr(inst, "connections", None) or ()
    for conn in conns:
        expr = getattr(conn, "expr", None) or getattr(conn, "expression", None)
        text = str(expr).strip() if expr is not None else ""
        if not text or not text.replace("_", "").isalnum():
            continue
        path = f"{top}.{text}" if top else text
        sig = graph.get(path)
        if sig is None or sig.drivers or sig.kind is Kind.PORT_IN:
            continue
        out.append(path)
    return out


def declares(sources: "Sequence[str | Path]", module: str) -> Path | None:
    """The source file that declares `module`, or `None`.

    Matched on the parsed declaration rather than on the file name: a module
    called `fifo` is not reliably in `fifo.sv`. Two callers need this — §8.35
    inserts cover statements into that file, and §8.29 replaces it with a
    netlist — and both would get the wrong file by guessing.
    """
    from pyslang import syntax as S

    for source in sources:
        path = Path(source)
        try:
            tree = S.SyntaxTree.fromText(
                path.read_text(encoding="utf-8", errors="replace"), str(path)
            )
        except Exception:  # noqa: BLE001 - slang raises several unrelated types
            continue
        stack = [tree.root]
        while stack:
            node = stack.pop()
            if node.kind == S.SyntaxKind.ModuleDeclaration and node.header.name.valueText == module:
                return path
            for child in node:
                if isinstance(child, S.SyntaxNode):
                    stack.append(child)
    return None
