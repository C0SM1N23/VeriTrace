"""Design graph data model — §5.1 through §5.4.

Deliberately plain: frozen dataclasses and dicts. The graph is small enough
(one node per declared signal) that an adjacency dict beats a graph library,
and keeping the types dumb makes them trivial to serialise to the API later.

The expression tree of §5.3 lives here too; evaluating it against a trace is
`conditions.evaluate`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator


class Kind(Enum):
    WIRE = "wire"
    REG = "reg"
    PORT_IN = "port_in"
    PORT_OUT = "port_out"
    PARAM = "param"
    MEM = "mem"


class DriverKind(Enum):
    CONT_ASSIGN = "cont_assign"
    ALWAYS_FF = "always_ff"
    ALWAYS_COMB = "always_comb"
    INST_PORT = "inst_port"


class Role(Enum):
    """Why an edge exists. GUARD vs VALUE is the distinction why-trace turns on
    (§5.4): asked why `ready == 0`, the interesting path is through the guard."""

    GUARD = "guard"
    VALUE = "value"
    CLOCK = "clock"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class SourceLoc:
    file: str
    line: int
    col: int = 0

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True, slots=True)
class SignalId:
    hier: tuple[str, ...]
    name: str

    def path(self) -> str:
        return ".".join((*self.hier, self.name))

    @staticmethod
    def parse(path: str) -> SignalId:
        *hier, name = path.split(".")
        return SignalId(tuple(hier), name)

    def __str__(self) -> str:
        return self.path()


# --- expressions (§5.3) ----------------------------------------------------
#
# One frozen node type per production. `Expr` is the union; every node carries
# only what evaluation needs.


@dataclass(frozen=True, slots=True)
class Const:
    """Literal. `x` marks bits that are X/Z, so `'x` survives constant folding."""

    value: int
    width: int
    x: int = 0


@dataclass(frozen=True, slots=True)
class Ref:
    signal: SignalId
    width: int = 1


@dataclass(frozen=True, slots=True)
class Unary:
    op: str
    arg: "Expr"


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    lhs: "Expr"
    rhs: "Expr"


@dataclass(frozen=True, slots=True)
class Ternary:
    cond: "Expr"
    then: "Expr"
    other: "Expr"


@dataclass(frozen=True, slots=True)
class Concat:
    parts: tuple["Expr", ...]


@dataclass(frozen=True, slots=True)
class Slice:
    arg: "Expr"
    msb: "Expr"
    lsb: "Expr"


@dataclass(frozen=True, slots=True)
class Reduce:
    op: str
    arg: "Expr"


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple["Expr", ...]


Expr = Const | Ref | Unary | Binary | Ternary | Concat | Slice | Reduce | Call

#: Guard of a driver that is always active.
TRUE = Const(1, 1)


def refs(e: Expr | None) -> Iterator[SignalId]:
    """Every signal an expression reads."""
    match e:
        case None | Const():
            return
        case Ref(signal=s):
            yield s
        case Unary(arg=a) | Reduce(arg=a):
            yield from refs(a)
        case Binary(lhs=l, rhs=r):
            yield from refs(l)
            yield from refs(r)
        case Ternary(cond=c, then=t, other=o):
            yield from refs(c)
            yield from refs(t)
            yield from refs(o)
        case Concat(parts=ps) | Call(args=ps):
            for p in ps:
                yield from refs(p)
        case Slice(arg=a, msb=m, lsb=l):
            yield from refs(a)
            yield from refs(m)
            yield from refs(l)


def to_text(e: Expr | None) -> str:
    """Readable form, for reports and causal cards."""
    match e:
        case None:
            return ""
        case Const(value=v, width=w, x=x):
            return f"{w}'x" if x else (f"{w}'d{v}" if w > 1 else str(v))
        case Ref(signal=s):
            return s.name
        case Unary(op=op, arg=a):
            return f"{op}{to_text(a)}"
        case Binary(op=op, lhs=l, rhs=r):
            return f"({to_text(l)} {op} {to_text(r)})"
        case Ternary(cond=c, then=t, other=o):
            return f"({to_text(c)} ? {to_text(t)} : {to_text(o)})"
        case Concat(parts=ps):
            return "{" + ", ".join(to_text(p) for p in ps) + "}"
        case Slice(arg=a, msb=m, lsb=l):
            return f"{to_text(a)}[{to_text(m)}:{to_text(l)}]"
        case Reduce(op=op, arg=a):
            return f"{op}{to_text(a)}"
        case Call(name=n, args=ps):
            return f"{n}(" + ", ".join(to_text(p) for p in ps) + ")"
    return "?"


# --- graph (§5.1, §5.2, §5.4) ----------------------------------------------


@dataclass(slots=True)
class Driver:
    target: SignalId
    kind: DriverKind
    guard: Expr
    value: Expr
    loc: SourceLoc
    clock: SignalId | None = None
    reset: SignalId | None = None
    #: `<=` rather than `=`. The pairing of this with `kind` is the whole of the
    #: blocking/non-blocking sim-synth mismatch checks in §8.11 Group A.
    non_blocking: bool = False
    #: Signals named in an explicit `always @(...)` list, or `None` for
    #: `always_comb`/`always_ff` where the list is implicit and by definition
    #: complete. Feeds the incomplete-sensitivity-list check (§8.11).
    sensitivity: tuple[SignalId, ...] | None = None
    #: True when the reset edge came from the event list — an async reset,
    #: which §8.11 wants checked for a synchronous deassert.
    async_reset: bool = False
    #: First and last line of the enclosing procedural block. A front-end
    #: diagnostic reports the line of the construct it dislikes, not the signal;
    #: this is what maps that line back onto a signal exactly rather than by
    #: proximity (§8.11).
    block_lines: tuple[int, int] | None = None

    @property
    def is_sequential(self) -> bool:
        return self.kind is DriverKind.ALWAYS_FF


@dataclass(slots=True)
class Signal:
    id: SignalId
    width: int
    signed: bool
    kind: Kind
    decl_loc: SourceLoc
    trace_handle: int | None = None
    drivers: list[Driver] = field(default_factory=list)
    #: For a memory: element index -> trace handle. Simulators dump `mem[0]`,
    #: `mem[1]` and so on, never the array as a whole (§5.6).
    elements: dict[int, int] = field(default_factory=dict)
    #: Declared element count of a memory, 0 for anything else. The *only*
    #: honest bound on an element index (§5.6): `width` describes one element
    #: and `elements` describes what the simulator chose to dump, so neither
    #: says how many entries the RTL declared.
    depth: int = 0
    #: `logic [3:0] x = 4'd7;` — the declaration initialiser. Present means the
    #: signal has a power-on value that synthesis may not honour (§8.11).
    has_initializer: bool = False
    #: True for an instance port left unconnected in the AST — one of the X
    #: terminals of §8.5.
    unconnected: bool = False
    #: Written only by `initial`/`final` blocks, i.e. by testbench stimulus.
    #: Such a signal has no design driver but is not floating either: it comes
    #: from outside the design, which is §8.1's `PRIMARY_INPUT`.
    stimulus_only: bool = False
    #: Literal bounds for a one-dimensional fixed unpacked array.
    array_left: int | None = None
    array_right: int | None = None

    @property
    def path(self) -> str:
        return self.id.path()

    @property
    def is_traced(self) -> bool:
        return self.trace_handle is not None or bool(self.elements)

    @property
    def is_reconstructible(self) -> bool:
        """Undumped combinational value that can be evaluated at one instant.

        A sequential register or memory has history. Evaluating its assignment
        expression from the inputs visible *now* is not reconstruction of that
        history; it is a plausible-looking fabrication. Section 7.3 only
        permits the combinational ``tmp = a & b`` case, while §5.6 explicitly
        requires an undumped array to be reported as unavailable.
        """
        return (
            self.trace_handle is None
            and self.kind is not Kind.MEM
            and bool(self.drivers)
            and all(not driver.is_sequential for driver in self.drivers)
        )

    @property
    def is_tie_off(self) -> bool:
        """The RTL says this cannot change: a parameter, or `assign x = 1'b0`.

        The distinction that matters for both the stuck detector (§8.4) and the
        causal terminals (§8.1) is between a signal that *is* constant and one
        that merely *stayed* constant. A tie-off is an answer; a guarded
        register that never fired is a question.
        """
        if self.kind is Kind.PARAM:
            return True
        return bool(self.drivers) and all(
            isinstance(d.value, Const) and isinstance(d.guard, Const) for d in self.drivers
        )


@dataclass(frozen=True, slots=True)
class Edge:
    src: SignalId
    dst: SignalId
    role: Role
    driver: Driver


@dataclass(slots=True)
class DesignGraph:
    signals: dict[str, Signal] = field(default_factory=dict)
    #: Instance path -> module name, for BLACKBOX_IP reporting (§7.4b).
    blackboxes: dict[str, str] = field(default_factory=dict)
    #: Signals of the *enclosing* scope that a black box drives. Without this a
    #: wire coming out of encrypted IP reads as undriven, which is what a
    #: floating net reads as — two very different statements about a design.
    blackbox_driven: set[str] = field(default_factory=set)
    top: str = ""
    #: src path -> dst paths. Built on first `fanout` call and dropped by `add`,
    #: so it can never answer from a stale graph.
    _fanout: dict[str, set[str]] | None = field(default=None, repr=False)

    def add(self, s: Signal) -> Signal:
        self._fanout = None
        return self.signals.setdefault(s.path, s)

    def get(self, path: str | SignalId) -> Signal | None:
        return self.signals.get(str(path))

    def __len__(self) -> int:
        return len(self.signals)

    def __iter__(self) -> Iterator[Signal]:
        return iter(self.signals.values())

    def edges_into(self, path: str | SignalId) -> Iterable[Edge]:
        """Everything that feeds `path`, tagged by role."""
        sig = self.get(path)
        if sig is None:
            return
        for d in sig.drivers:
            for role, expr in ((Role.GUARD, d.guard), (Role.VALUE, d.value)):
                for src in refs(expr):
                    yield Edge(src, sig.id, role, d)
            for role, sid in ((Role.CLOCK, d.clock), (Role.RESET, d.reset)):
                if sid is not None:
                    yield Edge(sid, sig.id, role, d)

    def fanout(self, path: str | SignalId) -> set[str]:
        """Signals directly driven by `path`.

        Backed by a reverse index built once, because the forward BFS of §8.6
        asks this per visited node: scanning every signal's drivers per call
        would make a cone quadratic in the size of the design.
        """
        if self._fanout is None:
            index: dict[str, set[str]] = {}
            for s in self:
                for e in self.edges_into(s.path):
                    index.setdefault(str(e.src), set()).add(s.path)
            self._fanout = index
        return self._fanout.get(str(path), set())

    def fanin(self, path: str | SignalId) -> set[str]:
        """Signals feeding `path`, in any role."""
        return {str(e.src) for e in self.edges_into(path)}

    def invalidate(self) -> None:
        """Drop derived indexes after mutating drivers in place."""
        self._fanout = None
