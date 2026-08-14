"""The mutation table of §8.28, one row per operator.

Two decisions run through the whole file.

**Sites come from the syntax tree, edits are byte spans.** §8.28 asks for
mutations "aplicate pe AST-ul din pyslang, deci sintactic valide" — which the
*site* provides: an operator token that slang parsed as a comparison is a
comparison, where a regular expression would also match one inside a string or a
comment. Rewriting and re-printing the tree would additionally reformat the file,
so the edit stays a replacement and every other byte survives untouched.

**Every span is anchored on a token, in bytes, and verified before use.** Two
traps sit here. Slang's offsets are into the UTF-8 *buffer*, so one accented
character in a comment shifts every Python string index after it — all the
arithmetic below is therefore on `bytes`. And pyslang 11 reports token offsets
one position past where the token starts; rather than hard-code that correction,
`span()` confirms the bytes against the token's own `rawText`. The correction is
then free, and so is the safety net: a site that does not match is dropped, so
the worst case is a mutation that does not happen instead of a source file that
no longer parses.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from pyslang import syntax as S

from veritrace.mutate.model import Mutation

#: A replacement, before it knows which file it is in: byte span, and the text
#: that goes there.
Edit = tuple[int, int, str]
Operator = Callable[[object, bytes], Iterator[Edit]]

# --- span, the one primitive -------------------------------------------------


def span(data: bytes, tok: object) -> tuple[int, int] | None:
    """Where `tok` really is in `data`, or `None` if it is not there.

    The reported offset is trusted only as a hint; `rawText` is the authority.
    """
    raw = getattr(tok, "rawText", "")
    if not raw:
        return None
    want = raw.encode("utf-8")
    at = tok.range.start.offset
    for start in (at - 1, at, at + 1):
        if 0 <= start <= len(data) - len(want) and data[start : start + len(want)] == want:
            return start, start + len(want)
    return None


def node_span(data: bytes, node: object) -> tuple[int, int] | None:
    """A whole subtree's span, composed from its first and last token."""
    first = span(data, node.getFirstToken())
    last = span(data, node.getLastToken())
    return None if first is None or last is None else (first[0], last[1])


# --- the table ---------------------------------------------------------------

#: Relational and equality inversions. Keyed by *node kind*, never by token text:
#: `<=` is overwhelmingly the non-blocking assignment operator, and swapping one
#: of those for `<` produces a file that parses and means something else entirely.
COMPARE = {
    "LessThanExpression": "<=",
    "LessThanEqualExpression": "<",
    "GreaterThanExpression": ">=",
    "GreaterThanEqualExpression": ">",
    "EqualityExpression": "!=",
    "InequalityExpression": "==",
}
LOGICAL = {"LogicalAndExpression": "||", "LogicalOrExpression": "&&"}


def _binary(table: dict[str, str], operator: str) -> Operator:
    def op(node: object, data: bytes) -> Iterator[Edit]:
        now = table.get(node.kind.name)
        if now is None:
            return
        at = span(data, node.operatorToken)
        if at is not None:
            yield at[0], at[1], now

    op.__name__ = operator
    return op


def _condition(node: object, data: bytes) -> Iterator[Edit]:
    """`if (a)` -> `if (!(a))`.

    Anchored on the parentheses the statement already carries, so the predicate
    is whatever sits between them however it is written.
    """
    if node.kind != S.SyntaxKind.ConditionalStatement:
        return
    lhs, rhs = span(data, node.openParen), span(data, node.closeParen)
    if lhs is None or rhs is None or rhs[0] <= lhs[1]:
        return
    yield lhs[1], rhs[0], f"!({_text(data, lhs[1], rhs[0])})"


def _text(data: bytes, start: int, end: int) -> str:
    return data[start:end].decode("utf-8", errors="replace")


#: `4'd7`, `1'b0`, `32'h00ff`, `5'sd3` — size, base, digits.
_SIZED = re.compile(r"^(\d*)'([sS]?)([bBoOdDhH])([0-9a-fA-FxXzZ_?]+)$")
_BASE = {"b": 2, "o": 8, "d": 10, "h": 16}


def _perturb(raw: str) -> str | None:
    """One off from `raw`, in the notation `raw` was written in.

    `None` for anything whose value is not a number — `'x` and `'z` are already
    the interesting cases and nudging them says nothing.
    """
    if raw in ("'0", "'1"):
        return "'1" if raw == "'0" else "'0"
    m = _SIZED.match(raw)
    if m:
        size, signed, base, digits = m.groups()
        if any(c in digits for c in "xXzZ?"):
            return None
        value = int(digits.replace("_", ""), _BASE[base.lower()])
        width = int(size) if size else max(1, value.bit_length())
        # One up, wrapping inside the declared width so the literal stays legal
        # at its own size — a 1-bit constant mutates to its complement, which is
        # exactly §8.28's `1'b0 -> 1'b1`.
        nxt = (value + 1) % (1 << width) if width < 64 else value + 1
        digit = format(nxt, {2: "b", 8: "o", 10: "d", 16: "x"}[_BASE[base.lower()]])
        return f"{size}'{signed}{base}{digit}"
    if raw.isdigit():
        return str(int(raw) + 1)
    return None


#: Ancestors whose literals describe the *shape* of the design rather than its
#: behaviour: a parameter's default, a port's width, an array's bound. Perturbing
#: those is not the bug injection §8.28 is after — `DEPTH = 16 -> 17` builds a
#: different design, and a mutant that changes what the module *is* says nothing
#: about whether the tests would notice a bug in what it *does*. Left in, they
#: also swamp the sample: they are the most common literals in any RTL file.
SHAPE = frozenset(
    {
        "ParameterDeclaration",
        "TypeParameterDeclaration",
        "LocalVariableDeclaration",
        "VariableDimension",
        "PackedDimension",
        "RangeDimensionSpecifier",
        "ImplicitType",
        "PortDeclaration",
        "ImplicitAnsiPort",
        "ParameterDeclarationStatement",
        "ParameterValueAssignment",
    }
)


def _is_shape(node: object) -> bool:
    at = node
    for _ in range(8):  # a literal is never deep inside a declaration
        at = getattr(at, "parent", None)
        if at is None:
            return False
        if at.kind.name in SHAPE:
            return True
    return False


def _constant(node: object, data: bytes) -> Iterator[Edit]:
    if node.kind not in (
        S.SyntaxKind.IntegerLiteralExpression,
        S.SyntaxKind.IntegerVectorExpression,
        S.SyntaxKind.UnbasedUnsizedLiteralExpression,
    ) or _is_shape(node):
        return
    at = node_span(data, node)
    if at is None:
        return
    now = _perturb(_text(data, *at))
    if now is not None:
        yield at[0], at[1], now


def _stuck_at(node: object, data: bytes) -> Iterator[Edit]:
    """Replace a driver's right-hand side with a constant — §8.28's stuck-at.

    The wire itself cannot be forced without adding a declaration, and its only
    driver becoming a constant is the same thing from the design's point of view.
    """
    if node.kind not in (
        S.SyntaxKind.AssignmentExpression,
        S.SyntaxKind.NonblockingAssignmentExpression,
    ):
        return
    at = node_span(data, node.right)
    if at is None or _text(data, *at) in ("'0", "'1"):
        return
    yield at[0], at[1], "'0"
    yield at[0], at[1], "'1"


def _fsm_branch(node: object, data: bytes) -> Iterator[Edit]:
    """Delete one arm of a `case` — §8.28's "eliminare de tranzitie FSM"."""
    if node.kind != S.SyntaxKind.StandardCaseItem:
        return
    at = node_span(data, node)
    if at is not None:
        yield at[0], at[1], ""


def _index(node: object, data: bytes) -> Iterator[Edit]:
    """`[i]` -> `[i+1]`.

    `ElementSelect` is only the brackets; whether they hold an index or a range
    is the *selector's* kind. `[AW-1:0]` is a range, and `[(AW-1:0)+1]` is not
    Verilog — so the selector is what decides, not the brackets around it.
    """
    if node.kind != S.SyntaxKind.ElementSelect or _is_shape(node):
        return
    if getattr(node.selector, "kind", None) != S.SyntaxKind.BitSelect:
        return
    at = node_span(data, node)
    if at is None:
        return
    inner = _text(data, at[0] + 1, at[1] - 1)
    if not inner.strip():
        return
    yield at[0], at[1], f"[({inner})+1]"


#: §8.28's table, in its order. Adding an operator is one entry.
OPERATORS: dict[str, Operator] = {
    "relational": _binary(COMPARE, "relational"),
    "condition": _condition,
    "constant": _constant,
    "logical": _binary(LOGICAL, "logical"),
    "stuck-at": _stuck_at,
    "fsm-branch": _fsm_branch,
    "index": _index,
}


# --- walking -----------------------------------------------------------------


def _walk(node: object) -> Iterator[object]:
    yield node
    for child in node:
        if isinstance(child, S.SyntaxNode):
            yield from _walk(child)


def sites(path: Path, enabled: set[str] | None = None, validate: bool = True) -> list[Mutation]:
    """Every mutation `path` admits, in source order.

    A file slang cannot parse yields nothing rather than raising: one unparsable
    file in an RTL directory should cost its own mutations, not the whole run.

    `validate` re-parses each candidate, which is what makes §8.28's "syntactically
    valid by construction" true rather than aspirational — but it costs a parse
    per site, and a run that samples 200 out of 5000 would pay for 4800 it will
    never use. So the *runner* turns it off here and checks the sample instead
    (`run.one`), and the cost stays proportional to what is actually simulated.
    """
    data = Path(path).read_bytes()
    try:
        tree = S.SyntaxTree.fromText(data.decode("utf-8", errors="replace"), str(path))
    except Exception:  # noqa: BLE001 - slang raises several unrelated types
        return []

    lines = _line_index(data)
    out: list[Mutation] = []
    for node in _walk(tree.root):
        for name, operator in OPERATORS.items():
            if enabled is not None and name not in enabled:
                continue
            for start, end, now in operator(node, data):
                line = _line_of(lines, start)
                out.append(
                    Mutation(
                        operator=name,
                        file=Path(path),
                        line=line,
                        start=start,
                        end=end,
                        was=_text(data, start, end),
                        now=now,
                        context=_text(data, lines[line - 1], _eol(data, lines, line)).strip(),
                    )
                )
    # Source order, and stable: the sampler seeds off this list, so the same seed
    # has to mean the same mutants on the same input.
    out.sort(key=lambda m: (m.start, m.operator))
    return [m for m in out if not validate or _valid(m, data, str(path))]


def _valid(m: Mutation, data: bytes, name: str) -> bool:
    """Whether the mutant still parses.

    §8.28's claim is that an AST-derived mutation is syntactically valid, and for
    a single operator token it is — `<` for `<=` cannot break a file. A span
    composed from a subtree's first and last token is a different matter: slang
    reports offsets one position out, and for a token as short and as repeated as
    `]` the correction is ambiguous, so `mem[a[b:c]]` can lose its closing
    bracket. Rather than make the offset arithmetic cleverer, the claim is simply
    made true: a site that does not parse is not a site.

    Costs one parse per candidate, so `sites` only does it when asked; the runner
    checks the sampled mutants instead and books the failures as `invalid`, which
    is the same guarantee for a fraction of the work.
    """
    from pyslang import syntax as S

    try:
        tree = S.SyntaxTree.fromText(m.apply(data).decode("utf-8", errors="replace"), name)
    except Exception:  # noqa: BLE001 - slang raises several unrelated types
        return False
    return not any(d.isError for d in tree.diagnostics)


def _line_index(data: bytes) -> list[int]:
    """Byte offsets at which each line starts."""
    out, at = [0], data.find(b"\n")
    while at != -1:
        out.append(at + 1)
        at = data.find(b"\n", at + 1)
    return out


def _line_of(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def _eol(data: bytes, starts: list[int], line: int) -> int:
    return starts[line] - 1 if line < len(starts) else len(data)


__all__ = ["OPERATORS", "sites", "span", "node_span", "Mutation"]
