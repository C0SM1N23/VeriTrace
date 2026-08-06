"""Guard extraction and evaluation — §5.2, §5.3.

The guard of a driver is built by accumulating conditions along the path from
the root of a procedural block down to the assignment, negating the `else`
branches already consumed (§5.2). For

    if (!rst_n)           state <= IDLE;
    else if (en && !hold) state <= next_state;

that yields two drivers, guarded by `!rst_n` and `rst_n && en && !hold`.

Evaluation is four-state: every value carries a mask of unknown bits, so X
propagates instead of silently becoming 0. `int` is not good enough (§5.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from pyslang import ast as A

from veritrace.graph.model import (
    Binary,
    Concat,
    Const,
    Expr,
    Ref,
    Reduce,
    Slice,
    SignalId,
    SourceLoc,
    Ternary,
    Unary,
    Call,
    TRUE,
)

BIN_OPS = {
    A.BinaryOperator.Add: "+",
    A.BinaryOperator.Subtract: "-",
    A.BinaryOperator.Multiply: "*",
    A.BinaryOperator.Divide: "/",
    A.BinaryOperator.Mod: "%",
    A.BinaryOperator.BinaryAnd: "&",
    A.BinaryOperator.BinaryOr: "|",
    A.BinaryOperator.BinaryXor: "^",
    A.BinaryOperator.BinaryXnor: "~^",
    A.BinaryOperator.Equality: "==",
    A.BinaryOperator.Inequality: "!=",
    A.BinaryOperator.CaseEquality: "===",
    A.BinaryOperator.CaseInequality: "!==",
    A.BinaryOperator.WildcardEquality: "==?",
    A.BinaryOperator.WildcardInequality: "!=?",
    A.BinaryOperator.GreaterThan: ">",
    A.BinaryOperator.GreaterThanEqual: ">=",
    A.BinaryOperator.LessThan: "<",
    A.BinaryOperator.LessThanEqual: "<=",
    A.BinaryOperator.LogicalAnd: "&&",
    A.BinaryOperator.LogicalOr: "||",
    A.BinaryOperator.LogicalShiftLeft: "<<",
    A.BinaryOperator.LogicalShiftRight: ">>",
    A.BinaryOperator.ArithmeticShiftLeft: "<<<",
    A.BinaryOperator.ArithmeticShiftRight: ">>>",
    A.BinaryOperator.Power: "**",
    A.BinaryOperator.LogicalImplication: "->",
    A.BinaryOperator.LogicalEquivalence: "<->",
}

UN_OPS = {
    A.UnaryOperator.LogicalNot: "!",
    A.UnaryOperator.BitwiseNot: "~",
    A.UnaryOperator.Minus: "-",
    A.UnaryOperator.Plus: "+",
}

REDUCE_OPS = {
    A.UnaryOperator.BitwiseAnd: "&",
    A.UnaryOperator.BitwiseOr: "|",
    A.UnaryOperator.BitwiseXor: "^",
    A.UnaryOperator.BitwiseNand: "~&",
    A.UnaryOperator.BitwiseNor: "~|",
    A.UnaryOperator.BitwiseXnor: "~^",
}

#: Maps a pyslang symbol to its SignalId; supplied by the elaborator.
SymbolMap = Callable[[object], SignalId | None]


# --- guard algebra ---------------------------------------------------------


def land(a: Expr, b: Expr) -> Expr:
    """`a && b`, dropping the constant-true operand so guards stay readable."""
    if a is TRUE or a == TRUE:
        return b
    if b is TRUE or b == TRUE:
        return a
    return Binary("&&", a, b)


def lnot(e: Expr) -> Expr:
    """`!e`, collapsing double negation."""
    if isinstance(e, Unary) and e.op == "!":
        return e.arg
    if isinstance(e, Const):
        return Const(0 if e.value else 1, 1)
    return Unary("!", e)


# --- pyslang -> Expr -------------------------------------------------------


def convert(e, sym_of: SymbolMap) -> Expr:
    """Translate a pyslang expression into the tree of §5.3.

    Anything not modelled becomes a `Call` node naming its kind rather than
    being silently dropped — evaluation then yields X, which is honest.
    """
    if e is None:
        return TRUE

    # Let pyslang fold whatever it can; parameters resolve to literals here.
    folded = _const(getattr(e, "constant", None))
    if folded is not None:
        return folded

    kind = str(e.kind).rsplit(".", 1)[-1]

    if kind.endswith("Literal"):
        # Literals do not always carry a folded `constant`; read them directly.
        lit = _from_svint(getattr(e, "value", None))
        return lit if lit is not None else Const(0, 1, 1)
    if kind in ("NamedValue", "HierarchicalValue", "MemberAccess"):
        ref = e.getSymbolReference()
        # An enum member is a named constant, not a signal. Treating `WRITE_A`
        # in `state == WRITE_A` as a reference put a signal in the graph that no
        # simulator dumps, so every FSM comparison grew a `not_traced` branch —
        # noise in the causal tree, and one more thing depressing the match rate.
        if str(getattr(ref, "kind", "")).rsplit(".", 1)[-1] == "EnumValue":
            folded = _const(getattr(ref, "value", None))
            if folded is not None:
                return folded
        sid = sym_of(ref)
        return Ref(sid, _width(e)) if sid else Call(kind, ())
    if kind == "Conversion":
        return convert(e.operand, sym_of)
    if kind == "UnaryOp":
        inner = convert(e.operand, sym_of)
        if e.op in UN_OPS:
            return Unary(UN_OPS[e.op], inner)
        if e.op in REDUCE_OPS:
            return Reduce(REDUCE_OPS[e.op], inner)
        return Call(str(e.op), (inner,))
    if kind == "BinaryOp":
        return Binary(BIN_OPS.get(e.op, "?"), convert(e.left, sym_of), convert(e.right, sym_of))
    if kind == "ConditionalOp":
        cond = e.conditions[0].expr if e.conditions else None
        return Ternary(convert(cond, sym_of), convert(e.left, sym_of), convert(e.right, sym_of))
    if kind == "Concatenation":
        return Concat(tuple(convert(o, sym_of) for o in e.operands))
    if kind == "Replication":
        return Concat((convert(e.concat, sym_of),))
    if kind in ("ElementSelect",):
        idx = convert(e.selector, sym_of)
        return Slice(convert(e.value, sym_of), idx, idx)
    if kind == "RangeSelect":
        return Slice(convert(e.value, sym_of), convert(e.left, sym_of), convert(e.right, sym_of))
    if kind == "Call":
        args = tuple(convert(a, sym_of) for a in getattr(e, "arguments", ()))
        return Call(getattr(e, "subroutineName", "call"), args)
    if kind == "Assignment":
        return convert(e.right, sym_of)
    return Call(kind, ())


def _width(e) -> int:
    t = getattr(e, "type", None)
    return int(getattr(t, "bitWidth", 1) or 1)


def _const(c) -> Const | None:
    """ConstantValue -> Const, preserving X/Z bits.

    Returns None when the value is not an integer constant (unset, real,
    string, aggregate), so the caller keeps walking the expression instead of
    inventing a number.
    """
    return _from_svint(getattr(c, "value", None)) if c is not None else None


def _from_svint(sv) -> Const | None:
    """SVInt (or a 1-bit logic value) -> Const."""
    if sv is None:
        return None
    try:
        w = int(getattr(sv, "bitWidth", 1) or 1)
        if w <= 0:
            return None
        if callable(getattr(sv, "hasUnknown", None)) and sv.hasUnknown():
            # Which bits are unknown is not exposed; mark the whole value.
            return Const(0, w, (1 << w) - 1)
        raw = sv.convertToInt() if hasattr(sv, "convertToInt") else int(sv)
        return Const(int(raw) & ((1 << w) - 1), w)
    except Exception:
        return None


# --- statement walk (§5.2) -------------------------------------------------


@dataclass(slots=True)
class Assignment:
    target: SignalId
    guard: Expr
    value: Expr
    loc: SourceLoc
    non_blocking: bool


def assignments(
    stmt,
    sym_of: SymbolMap,
    loc_of: Callable[[object], SourceLoc],
    guard: Expr = TRUE,
) -> Iterator[Assignment]:
    """Yield every assignment under `stmt`, each with its accumulated guard."""
    if stmt is None:
        return
    kind = str(stmt.kind).rsplit(".", 1)[-1]

    if kind == "Timed":
        yield from assignments(stmt.stmt, sym_of, loc_of, guard)
    elif kind == "Block":
        yield from assignments(stmt.body, sym_of, loc_of, guard)
    elif kind == "List":
        for s in stmt.list:
            yield from assignments(s, sym_of, loc_of, guard)
    elif kind == "Conditional":
        cond: Expr = TRUE
        for c in stmt.conditions:
            cond = land(cond, convert(c.expr, sym_of))
        yield from assignments(stmt.ifTrue, sym_of, loc_of, land(guard, cond))
        # The else branch is reached only when the condition failed.
        yield from assignments(stmt.ifFalse, sym_of, loc_of, land(guard, lnot(cond)))
    elif kind in ("Case", "PatternCase"):
        subject = convert(stmt.expr, sym_of)
        taken: list[Expr] = []
        for item in stmt.items:
            match: Expr | None = None
            for label in item.expressions:
                eq = Binary("==", subject, convert(label, sym_of))
                match = eq if match is None else Binary("||", match, eq)
            if match is None:
                continue
            taken.append(match)
            yield from assignments(item.stmt, sym_of, loc_of, land(guard, match))
        if stmt.defaultCase is not None:
            # default is reached only when no label matched.
            g = guard
            for m in taken:
                g = land(g, lnot(m))
            yield from assignments(stmt.defaultCase, sym_of, loc_of, g)
    elif kind == "ExpressionStatement":
        expr = stmt.expr
        if str(expr.kind).endswith("Assignment"):
            sid = sym_of(expr.left.getSymbolReference())
            if sid is not None:
                yield Assignment(
                    target=sid,
                    guard=guard,
                    value=convert(expr.right, sym_of),
                    loc=loc_of(expr),
                    non_blocking=bool(getattr(expr, "isNonBlocking", False)),
                )
    else:
        # Loops and the rest: descend without adding a condition. Unrolling is
        # elaboration's job, and a loop body is reached under the same guard.
        for attr in ("body", "stmt"):
            inner = getattr(stmt, attr, None)
            if inner is not None and inner is not stmt:
                yield from assignments(inner, sym_of, loc_of, guard)
                break


# --- four-state evaluation (§5.3) ------------------------------------------


@dataclass(frozen=True, slots=True)
class BV:
    """Four-state bit vector: `v` is the value plane, `x` marks unknown bits."""

    v: int
    x: int
    w: int

    @staticmethod
    def unknown(w: int = 1) -> BV:
        return BV(0, (1 << w) - 1, w)

    @staticmethod
    def of(value: int, w: int = 1) -> BV:
        return BV(value & ((1 << w) - 1), 0, w)

    @property
    def known(self) -> bool:
        return self.x == 0

    def truthy(self) -> BV:
        """Collapse to a 1-bit logical value, X-preserving."""
        if self.v & ~self.x:
            return BV.of(1)
        return BV.unknown() if self.x else BV.of(0)

    def to_int(self) -> int | None:
        return None if self.x else self.v

    def __str__(self) -> str:
        if self.x == (1 << self.w) - 1:
            return "x"
        return format(self.v, f"0{self.w}b") if self.w > 1 else str(self.v)


def _mask(w: int) -> int:
    return (1 << w) - 1


def evaluate(e: Expr, read: Callable[[SignalId], BV | None]) -> BV:
    """Evaluate an expression given a way to read signal values.

    `read` returns `None` for a signal that cannot be observed; the result is
    then X rather than a guess (P1 — no invented values).
    """
    match e:
        case Const(value=v, width=w, x=x):
            return BV(v & _mask(w), x & _mask(w), w)
        case Ref(signal=s, width=w):
            got = read(s)
            return got if got is not None else BV.unknown(w)
        case Unary(op=op, arg=a):
            r = evaluate(a, read)
            if op == "!":
                t = r.truthy()
                return BV.unknown() if t.x else BV.of(0 if t.v else 1)
            if op == "~":
                return BV(~r.v & _mask(r.w), r.x, r.w)
            if op == "-":
                return BV.unknown(r.w) if r.x else BV.of(-r.v, r.w)
            return r
        case Reduce(op=op, arg=a):
            r = evaluate(a, read)
            if r.x:
                return BV.unknown()
            bits = [(r.v >> i) & 1 for i in range(r.w)]
            val = {
                "&": int(all(bits)),
                "|": int(any(bits)),
                "^": sum(bits) & 1,
                "~&": int(not all(bits)),
                "~|": int(not any(bits)),
                "~^": (sum(bits) & 1) ^ 1,
            }.get(op, 0)
            return BV.of(val)
        case Binary(op=op, lhs=l, rhs=r):
            return _binary(op, evaluate(l, read), evaluate(r, read))
        case Ternary(cond=c, then=t, other=o):
            cv = evaluate(c, read).truthy()
            if cv.x:
                return BV.unknown(max(evaluate(t, read).w, evaluate(o, read).w))
            return evaluate(t if cv.v else o, read)
        case Concat(parts=ps):
            v = x = 0
            w = 0
            for p in reversed(ps):
                r = evaluate(p, read)
                v |= r.v << w
                x |= r.x << w
                w += r.w
            return BV(v, x, max(w, 1))
        case Slice(arg=a, msb=m, lsb=l):
            r = evaluate(a, read)
            hi, lo = evaluate(m, read).to_int(), evaluate(l, read).to_int()
            if hi is None or lo is None:
                return BV.unknown(r.w)
            hi, lo = max(hi, lo), min(hi, lo)
            w = hi - lo + 1
            return BV((r.v >> lo) & _mask(w), (r.x >> lo) & _mask(w), w)
        case _:
            # Unmodelled construct: X beats a wrong answer.
            return BV.unknown()


def _binary(op: str, a: BV, b: BV) -> BV:
    w = max(a.w, b.w)
    if op in ("&", "|", "^", "~^"):
        if op == "&":
            forced0 = (~a.v & ~a.x) | (~b.v & ~b.x)
            x = (a.x | b.x) & ~forced0 & _mask(w)
            return BV(a.v & b.v & ~x & _mask(w), x, w)
        if op == "|":
            forced1 = (a.v & ~a.x) | (b.v & ~b.x)
            x = (a.x | b.x) & ~forced1 & _mask(w)
            return BV((a.v | b.v) & ~x & _mask(w), x, w)
        x = (a.x | b.x) & _mask(w)
        v = (a.v ^ b.v) & _mask(w)
        return BV(v & ~x, x, w) if op == "^" else BV(~v & ~x & _mask(w), x, w)

    if op in ("&&", "||"):
        ta, tb = a.truthy(), b.truthy()
        # Short-circuit through X: 0 && x is 0, 1 || x is 1.
        if op == "&&":
            if (not ta.x and ta.v == 0) or (not tb.x and tb.v == 0):
                return BV.of(0)
            return BV.unknown() if (ta.x or tb.x) else BV.of(1)
        if (not ta.x and ta.v) or (not tb.x and tb.v):
            return BV.of(1)
        return BV.unknown() if (ta.x or tb.x) else BV.of(0)

    if op in ("==", "!=", "<", "<=", ">", ">="):
        if a.x or b.x:
            return BV.unknown()
        r = {
            "==": a.v == b.v,
            "!=": a.v != b.v,
            "<": a.v < b.v,
            "<=": a.v <= b.v,
            ">": a.v > b.v,
            ">=": a.v >= b.v,
        }[op]
        return BV.of(int(r))

    if op in ("===", "!==", "==?", "!=?"):
        # Case equality compares X bits literally, so it is always known.
        same = a.v == b.v and a.x == b.x
        return BV.of(int(same if op in ("===", "==?") else not same))

    if op in ("+", "-", "*", "/", "%", "<<", ">>", "<<<", ">>>"):
        if a.x or b.x:
            return BV.unknown(w)
        try:
            r = {
                "+": lambda: a.v + b.v,
                "-": lambda: a.v - b.v,
                "*": lambda: a.v * b.v,
                "/": lambda: a.v // b.v if b.v else 0,
                "%": lambda: a.v % b.v if b.v else 0,
                "<<": lambda: a.v << b.v,
                "<<<": lambda: a.v << b.v,
                ">>": lambda: a.v >> b.v,
                ">>>": lambda: a.v >> b.v,
            }[op]()
        except (ValueError, OverflowError):
            return BV.unknown(w)
        return BV.of(r, w)

    return BV.unknown(w)
