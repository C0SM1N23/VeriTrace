"""The little expression language a protocol pack is written in — §8.14.

Every place a pack says something conditional uses this one grammar:

    [[transaction]] body   = { count = "AW.awlen + 1" }
    [[transaction]] end    = { match_on = "bid == AW.awid" }
    [metrics]       latency = "end.time - start.time"
    [[rule]]        check  = "awvalid && !awready |=> awvalid"
    [[stall_reason]] when  = "valid && !ready"          (§8.17)

One engine, five call sites, and the ones §8.17 and §8.20 add later come for
free. That is the whole economics of §8.14: *the extraction engine is generic
and knows nothing about AXI* — which is only true if the conditional parts of a
pack are data too.

Three decisions worth stating:

* **Parsed, not `eval`'d.** Rewriting `&&` to `and` and handing the result to
  `eval` would be five lines and a remote-code-execution hole in a file format
  meant to be shared between colleagues. A Pratt parser over an explicit token
  list is ~200 lines and can only ever produce a number.
* **Unknown is a value, not an error.** A payload sampled while it carried X has
  no integer meaning, so it evaluates to `None` and propagates. §8.14's rules
  then treat "could not be decided" as *no verdict* rather than as a violation
  — the SVA reading, and the only one that does not invent failures out of
  reset-time X (P1).
* **`|=>` is not evaluable here.** A temporal implication spans two cycles, so
  it parses to a node the rule engine walks per cycle. Calling `evaluate` on one
  raises rather than quietly answering about a single instant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

__all__ = [
    "ExprError",
    "Node",
    "Implication",
    "parse",
    "evaluate",
    "identifiers",
    "is_temporal",
    "lookback",
    "Env",
    "MappingEnv",
]


class ExprError(ValueError):
    """A pack expression that cannot be parsed or evaluated.

    Always carries the offending text: a pack is edited by hand, and "unexpected
    `]` in `AW.awlen + 1]`" is the difference between a thirty-second fix and a
    bug report.
    """


# --- tokens -----------------------------------------------------------------

#: Longest-first, so `|=>` wins over `||` and `<=` over `<`.
_OPERATORS = (
    "|=>", "|->", "<<<", ">>>", "&&", "||", "==", "!=", "<=", ">=", "<<", ">>",
    "+", "-", "*", "/", "%", "<", ">", "!", "&", "|", "^", "?", ":", "(", ")",
    "[", "]", ",",
    # Tokenised only so `unary` can reject it with an explanation rather than
    # "unexpected character".
    "~",
)

_NAME = re.compile(r"[A-Za-z_$][A-Za-z_0-9$]*(?:\.[A-Za-z_$][A-Za-z_0-9$]*)*")
#: SystemVerilog sized literals (`4'hF`, `3'b010`) as well as `0x40`, `0b1`, `42`.
_SIZED = re.compile(r"(?:(\d+)\s*)?'\s*([sS]?)([bBoOdDhH])([0-9a-fA-FxXzZ_?]+)")
_NUMBER = re.compile(r"0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*")
_STRING = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')

_RADIX = {"b": 2, "o": 8, "d": 10, "h": 16}


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str  # "num" | "str" | "name" | "op"
    text: str
    pos: int
    value: Any = None


def _tokenize(src: str) -> list[_Tok]:
    out: list[_Tok] = []
    i, n = 0, len(src)
    while i < n:
        if src[i].isspace():
            i += 1
            continue
        if src.startswith("#", i):  # a trailing comment inside an expression
            break
        m = _STRING.match(src, i)
        if m:
            out.append(_Tok("str", m.group(0), i, m.group(1)))
            i = m.end()
            continue
        m = _SIZED.match(src, i)
        if m:
            digits = m.group(4).replace("_", "")
            if any(c in "xXzZ?" for c in digits):
                # A literal with unknown bits compares equal to nothing, which
                # is exactly what `None` already means here.
                out.append(_Tok("num", m.group(0), i, None))
            else:
                out.append(_Tok("num", m.group(0), i, int(digits, _RADIX[m.group(3).lower()])))
            i = m.end()
            continue
        m = _NUMBER.match(src, i)
        if m:
            out.append(_Tok("num", m.group(0), i, int(m.group(0).replace("_", ""), 0)))
            i = m.end()
            continue
        m = _NAME.match(src, i)
        if m:
            out.append(_Tok("name", m.group(0), i))
            i = m.end()
            continue
        for op in _OPERATORS:
            if src.startswith(op, i):
                out.append(_Tok("op", op, i))
                i += len(op)
                break
        else:
            raise ExprError(f"unexpected character {src[i]!r} at {i} in {src!r}")
    return out


# --- syntax tree ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Const:
    value: Any


@dataclass(frozen=True, slots=True)
class Name:
    name: str


@dataclass(frozen=True, slots=True)
class Unary:
    op: str
    arg: "Node"


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    lhs: "Node"
    rhs: "Node"


@dataclass(frozen=True, slots=True)
class Ternary:
    cond: "Node"
    then: "Node"
    other: "Node"


@dataclass(frozen=True, slots=True)
class Call:
    fn: str
    args: tuple["Node", ...]


@dataclass(frozen=True, slots=True)
class Index:
    arg: "Node"
    index: "Node"


@dataclass(frozen=True, slots=True)
class Slice:
    """`a[9:0]` — a SystemVerilog bit range, added for §8.20's command decode
    (`args = { col = "a[9:0]", ap = "a[10]" }`).

    Deliberately narrow: it only slices a plain integer. A bus sampled while it
    carried X has no integer meaning (§5.5), and reconstructing a sub-range from
    the raw four-state digit string would need the signal's full declared width
    — information an expression has no access to — so it evaluates to unknown
    rather than guessing at a padding.
    """

    arg: "Node"
    hi: "Node"
    lo: "Node"


@dataclass(frozen=True, slots=True)
class Implication:
    """`a |=> b` (next cycle) or `a |-> b` (same cycle) — §8.14 rules.

    Not a value: it is a statement about two instants, so only the rule engine,
    which owns the cycle loop, can decide it.
    """

    op: str
    antecedent: "Node"
    consequent: "Node"


Node = Const | Name | Unary | Binary | Ternary | Call | Index | Slice | Implication


# --- parser -----------------------------------------------------------------

#: Binding power per binary operator, loosest first. Mirrors SystemVerilog so a
#: pack author's reflexes are right.
_BINDING: dict[str, int] = {
    "||": 1, "|": 1,
    "&&": 2, "&": 2, "^": 2,
    "==": 3, "!=": 3,
    "<": 4, "<=": 4, ">": 4, ">=": 4,
    "<<": 5, ">>": 5, "<<<": 5, ">>>": 5,
    "+": 6, "-": 6,
    "*": 7, "/": 7, "%": 7,
}


class _Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0

    def peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> _Tok:
        if self.i >= len(self.toks):
            raise ExprError(f"expression ends early: {self.src!r}")
        self.i += 1
        return self.toks[self.i - 1]

    def eat(self, text: str) -> bool:
        t = self.peek()
        if t is not None and t.kind == "op" and t.text == text:
            self.i += 1
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.eat(text):
            got = self.peek()
            where = f"{got.text!r}" if got else "end of expression"
            raise ExprError(f"expected {text!r} but found {where} in {self.src!r}")

    def parse(self) -> Node:
        node = self.implication()
        if self.peek() is not None:
            raise ExprError(f"trailing {self.peek().text!r} in {self.src!r}")
        return node

    def implication(self) -> Node:
        lhs = self.ternary()
        for op in ("|=>", "|->"):
            if self.eat(op):
                return Implication(op, lhs, self.ternary())
        return lhs

    def ternary(self) -> Node:
        cond = self.binary(0)
        if self.eat("?"):
            then = self.ternary()
            self.expect(":")
            return Ternary(cond, then, self.ternary())
        return cond

    def binary(self, min_bp: int) -> Node:
        lhs = self.unary()
        while True:
            t = self.peek()
            if t is None or t.kind != "op":
                return lhs
            bp = _BINDING.get(t.text)
            if bp is None or bp < min_bp:
                return lhs
            self.take()
            lhs = Binary(t.text, lhs, self.binary(bp + 1))

    def unary(self) -> Node:
        t = self.peek()
        if t is not None and t.kind == "op" and t.text in ("!", "-", "+"):
            self.take()
            return self.unary() if t.text == "+" else Unary(t.text, self.unary())
        if t is not None and t.kind == "op" and t.text == "~":
            raise ExprError(
                "`~` needs a bit width and a pack expression does not carry one; "
                "use `!` for logical negation"
            )
        return self.postfix()

    def postfix(self) -> Node:
        node = self.primary()
        while self.eat("["):
            first = self.ternary()
            if self.eat(":"):
                node = Slice(node, first, self.ternary())
            else:
                node = Index(node, first)
            self.expect("]")
        return node

    def primary(self) -> Node:
        t = self.take()
        if t.kind in ("num", "str"):
            return Const(t.value)
        if t.kind == "name":
            if self.eat("("):
                args: list[Node] = []
                if not self.eat(")"):
                    args.append(self.ternary())
                    while self.eat(","):
                        args.append(self.ternary())
                    self.expect(")")
                return Call(t.text, tuple(args))
            return Name(t.text)
        if t.text == "(":
            node = self.ternary()
            self.expect(")")
            return node
        raise ExprError(f"unexpected {t.text!r} at {t.pos} in {self.src!r}")


_CACHE: dict[str, Node] = {}


def parse(src: str) -> Node:
    """Parse a pack expression. Cached: a rule is re-parsed once per pack load
    but evaluated once per cycle, and the AST is immutable."""
    node = _CACHE.get(src)
    if node is None:
        node = _CACHE[src] = _Parser(src).parse()
    return node


def is_temporal(node: Node) -> bool:
    """True for `a |=> b` / `a |-> b`, which the rule engine evaluates per cycle
    rather than per transaction."""
    return isinstance(node, Implication)


def lookback(node: Node) -> int:
    """How many cycles *before* the current one this expression reads.

    §8.17 evaluates its cascade at every clock edge, which is millions of
    evaluations on a real trace. The cascade's answer depends only on the values
    it reads, so memoising on those values collapses that to the handful of
    distinct bus states a design actually visits — but only if the key covers
    every cycle the expression can see. This is what says how far back to look.
    """
    match node:
        case Call(fn=fn, args=args):
            own = 0
            if fn in ("$stable", "$changed", "$rose", "$fell"):
                own = 1
            elif fn == "$past":
                # `$past(x)` is one cycle; `$past(x, n)` is n, when n is a
                # literal. A computed n cannot be bounded here, so the caller is
                # told to give up memoising rather than be handed a wrong key.
                own = 1
                if len(args) > 1:
                    own = args[1].value if isinstance(args[1], Const) else -1
                    if not isinstance(own, int):
                        own = -1
            inner = [lookback(a) for a in args]
            return -1 if own < 0 or -1 in inner else max([own, *inner])
        case Unary(arg=a):
            return lookback(a)
        case Binary(lhs=l, rhs=r):
            return _worst(lookback(l), lookback(r))
        case Ternary(cond=c, then=t, other=o):
            return _worst(lookback(c), _worst(lookback(t), lookback(o)))
        case Index(arg=a, index=i):
            return _worst(lookback(a), lookback(i))
        case Slice(arg=a, hi=h, lo=lo):
            return _worst(lookback(a), _worst(lookback(h), lookback(lo)))
        case Implication(antecedent=a, consequent=c):
            return _worst(lookback(a), lookback(c))
    return 0


def _worst(a: int, b: int) -> int:
    return -1 if -1 in (a, b) else max(a, b)


def identifiers(node: Node) -> set[str]:
    """Every name the expression reads.

    Used to decide which signals a temporal rule needs sampled at every clock
    edge — sampling only what is referenced is what keeps a pack with thirty
    rules as cheap as one with three.
    """
    out: set[str] = set()
    _names(node, out)
    return out


def _names(n: Node, out: set[str]) -> None:
    match n:
        case Name(name=name):
            out.add(name)
        case Unary(arg=a):
            _names(a, out)
        case Binary(lhs=l, rhs=r):
            _names(l, out)
            _names(r, out)
        case Ternary(cond=c, then=t, other=o):
            for x in (c, t, o):
                _names(x, out)
        case Call(args=args):
            for a in args:
                _names(a, out)
        case Index(arg=a, index=i):
            _names(a, out)
            _names(i, out)
        case Slice(arg=a, hi=h, lo=lo):
            _names(a, out)
            _names(h, out)
            _names(lo, out)
        case Implication(antecedent=a, consequent=c):
            _names(a, out)
            _names(c, out)


# --- evaluation -------------------------------------------------------------


class Env(Protocol):
    """What an expression can see. Implemented per call site — a transaction
    exposes its channels and metrics, a cycle exposes sampled signals."""

    def lookup(self, name: str) -> Any:
        """Value of `name`, or `None` when it is unknown at this instant.

        Raise `ExprError` for a name the pack should not have used at all: an
        unknown *value* is normal, an unknown *identifier* is a typo in the pack
        and must be reported as one.
        """

    def call(self, fn: str, args: Sequence[Node], ev: Callable[[Node], Any]) -> Any:
        """Evaluate a function. Receives unevaluated arguments so aggregates
        (`sum(stall)`) can bind their own per-item scope."""


class MappingEnv:
    """The common case: a flat namespace plus the built-in functions."""

    def __init__(self, values: dict[str, Any], functions: dict[str, Callable[..., Any]] | None = None) -> None:
        self.values = values
        self.functions = functions or {}

    def lookup(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        raise ExprError(f"unknown name `{name}` in this context")

    def call(self, fn: str, args: Sequence[Node], ev: Callable[[Node], Any]) -> Any:
        f = self.functions.get(fn) or _BUILTINS.get(fn)
        if f is None:
            raise ExprError(f"unknown function `{fn}`")
        return f(*[ev(a) for a in args])


def _abs(v: Any) -> Any:
    return None if v is None else abs(v)


def _min(*vs: Any) -> Any:
    known = [v for v in vs if v is not None]
    return min(known) if known else None


def _max(*vs: Any) -> Any:
    known = [v for v in vs if v is not None]
    return max(known) if known else None


#: Pure functions available everywhere. Aggregates and `$`-sampled functions
#: need context and are provided by the environment instead.
_BUILTINS: dict[str, Callable[..., Any]] = {"abs": _abs, "min": _min, "max": _max}


def _truth(v: Any) -> bool | None:
    """Three-valued truthiness: `None` stays `None`."""
    if v is None:
        return None
    if isinstance(v, str):
        return bool(v)
    return bool(v)


def evaluate(node: Node, env: Env) -> Any:
    """Value of `node` in `env`; `None` when any input it actually depends on is
    unknown."""
    match node:
        case Const(value=v):
            return v
        case Name(name=name):
            return env.lookup(name)
        case Call(fn=fn, args=args):
            return env.call(fn, args, lambda a: evaluate(a, env))
        case Index(arg=a, index=i):
            base, idx = evaluate(a, env), evaluate(i, env)
            if base is None or idx is None:
                return None
            if isinstance(base, int):
                # `a[10]` on an integer is SystemVerilog bit-select — a slice of
                # width 1 (§8.20: `ap = "a[10]"`), not a Python subscript.
                return (base >> idx) & 1 if idx >= 0 else None
            try:
                return base[idx]
            except (TypeError, KeyError, IndexError):
                return None
        case Slice(arg=a, hi=h, lo=lo):
            base, hi_v, lo_v = evaluate(a, env), evaluate(h, env), evaluate(lo, env)
            if (
                not isinstance(base, int)
                or not isinstance(hi_v, int)
                or not isinstance(lo_v, int)
                or hi_v < lo_v
                or lo_v < 0
            ):
                return None
            return (base >> lo_v) & ((1 << (hi_v - lo_v + 1)) - 1)
        case Unary(op=op, arg=a):
            v = evaluate(a, env)
            if op == "!":
                t = _truth(v)
                return None if t is None else int(not t)
            return None if v is None else -v
        case Ternary(cond=c, then=t, other=o):
            cv = _truth(evaluate(c, env))
            if cv is None:
                return None
            return evaluate(t if cv else o, env)
        case Binary(op=op, lhs=l, rhs=r):
            return _binary(op, l, r, env)
        case Implication():
            raise ExprError(
                "a temporal implication (`|=>`) spans two cycles and cannot be "
                "evaluated at a single instant"
            )
    raise ExprError(f"cannot evaluate {node!r}")


def _binary(op: str, l: Node, r: Node, env: Env) -> Any:
    # Short-circuit first: `0 && x` is 0 even when x is unknown, and the whole
    # point of a controlling value is that the other operand stops mattering.
    if op in ("&&", "||"):
        lv = _truth(evaluate(l, env))
        controlling = False if op == "&&" else True
        if lv is controlling:
            return int(controlling)
        rv = _truth(evaluate(r, env))
        if rv is controlling:
            return int(controlling)
        if lv is None or rv is None:
            return None
        return int(lv and rv) if op == "&&" else int(lv or rv)

    a, b = evaluate(l, env), evaluate(r, env)
    if op in ("==", "!="):
        # Unknown compares equal to nothing, not even to itself: reporting a
        # match on two X's would be exactly the fabricated answer P1 forbids.
        if a is None or b is None:
            return None
        return int((a == b) if op == "==" else (a != b))
    if a is None or b is None:
        return None
    if isinstance(a, str) or isinstance(b, str):
        raise ExprError(f"operator `{op}` does not apply to text")
    try:
        match op:
            case "+":
                return a + b
            case "-":
                return a - b
            case "*":
                return a * b
            case "/":
                return None if b == 0 else a // b
            case "%":
                return None if b == 0 else a % b
            case "<":
                return int(a < b)
            case "<=":
                return int(a <= b)
            case ">":
                return int(a > b)
            case ">=":
                return int(a >= b)
            case "&":
                return a & b
            case "|":
                return a | b
            case "^":
                return a ^ b
            case "<<" | "<<<":
                return a << b
            case ">>" | ">>>":
                return a >> b
    except (ValueError, OverflowError) as e:  # e.g. a shift count out of range
        raise ExprError(f"`{op}` failed on {a!r} and {b!r}: {e}") from e
    raise ExprError(f"unknown operator `{op}`")
