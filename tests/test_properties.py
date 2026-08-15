"""Property-based tests — §14.2.

*"Property-based (hypothesis): genereaza expresii aleatoare + valori aleatoare,
verifica `eval()` fata de o implementare de referinta naiva. Aici gasesti
bug-urile pe care nu le-ai anticipat."*

The reference here is deliberately dumb: it evaluates over explicit 4-state
digit strings, one bit at a time, the way a person would on paper. It cannot
share a bug with the packed two-plane representation it is checking, which is
the whole point of having it.

The Rust side has its own property suite over the store (`crates/vt-trace/
tests/properties.rs`); this one covers the expression layer, which is what
guards, values and every `[guard: ...]` line in a causal chain go through.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from veritrace.graph.conditions import BV, evaluate
from veritrace.graph.model import Binary, Const, Ref, SignalId, Ternary, Unary

WIDTH = 8


# --- the naive reference ----------------------------------------------------


def ref_and(a: str, b: str) -> str:
    """Bitwise AND on 4-state digits, one column at a time.

    The controlling value wins over an unknown: `0 & x` is 0, because a zero
    forces the result whatever the other operand turns out to be.
    """
    return "".join(
        "0" if x == "0" or y == "0" else ("x" if x == "x" or y == "x" else "1")
        for x, y in zip(a, b)
    )


def ref_or(a: str, b: str) -> str:
    return "".join(
        "1" if x == "1" or y == "1" else ("x" if x == "x" or y == "x" else "0")
        for x, y in zip(a, b)
    )


def ref_xor(a: str, b: str) -> str:
    return "".join(
        "x" if x == "x" or y == "x" else str(int(x) ^ int(y)) for x, y in zip(a, b)
    )


def ref_not(a: str) -> str:
    return "".join("x" if c == "x" else ("1" if c == "0" else "0") for c in a)


def ref_truthy(a: str) -> str:
    """SystemVerilog truthiness: any 1 is true, all 0 is false, else unknown."""
    if "1" in a:
        return "1"
    return "x" if "x" in a else "0"


digits = st.text(alphabet="01x", min_size=WIDTH, max_size=WIDTH)


def bv(d: str) -> BV:
    v = int("".join(c if c in "01" else "0" for c in d), 2)
    x = int("".join("1" if c == "x" else "0" for c in d), 2)
    return BV(v & ~x, x, WIDTH)


def as_digits(b: BV) -> str:
    return "".join(
        "x" if (b.x >> i) & 1 else str((b.v >> i) & 1) for i in reversed(range(WIDTH))
    )


A = SignalId(("top",), "a")
B = SignalId(("top",), "b")


def env(a: str, b: str):
    table = {A.path(): bv(a), B.path(): bv(b)}
    return lambda s: table.get(s.path())


# --- the properties ---------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(a=digits, b=digits)
def test_bitwise_operators_match_the_naive_reference(a, b):
    read = env(a, b)
    ra, rb = Ref(A, WIDTH), Ref(B, WIDTH)
    assert as_digits(evaluate(Binary("&", ra, rb), read)) == ref_and(a, b)
    assert as_digits(evaluate(Binary("|", ra, rb), read)) == ref_or(a, b)
    assert as_digits(evaluate(Binary("^", ra, rb), read)) == ref_xor(a, b)
    assert as_digits(evaluate(Unary("~", ra), read)) == ref_not(a)


@settings(max_examples=200, deadline=None)
@given(a=digits, b=digits)
def test_logical_and_short_circuits_through_unknown(a, b):
    """`0 && x` is 0. Losing that is how an X spreads into a guard that should
    have been decidable, and a decidable guard is the difference between a
    causal chain and a shrug."""
    read = env(a, b)
    got = evaluate(Binary("&&", Ref(A, WIDTH), Ref(B, WIDTH)), read)
    ta, tb = ref_truthy(a), ref_truthy(b)
    if ta == "0" or tb == "0":
        want = "0"
    elif ta == "x" or tb == "x":
        want = "x"
    else:
        want = "1"
    assert ("x" if got.x else str(got.v)) == want


@settings(max_examples=200, deadline=None)
@given(a=digits, b=digits)
def test_equality_is_unknown_when_either_side_is(a, b):
    """§5.3 wants 4-state, and `x == x` is *not* true: two unknowns are not
    known to be equal. A tool that answers 1 here invents a fact."""
    got = evaluate(Binary("==", Ref(A, WIDTH), Ref(B, WIDTH)), env(a, b))
    if "x" in a or "x" in b:
        assert got.x, f"{a} == {b} answered {as_digits(got)} instead of x"
    else:
        assert not got.x and got.v == int(a == b)


@settings(max_examples=200, deadline=None)
@given(a=digits, b=digits, sel=st.sampled_from(["0", "1", "x"]))
def test_a_ternary_picks_a_branch_only_when_the_condition_is_known(a, b, sel):
    cond = Const(0 if sel == "0" else 1, 1, 1 if sel == "x" else 0)
    got = evaluate(Ternary(cond, Ref(A, WIDTH), Ref(B, WIDTH)), env(a, b))
    if sel == "x":
        assert got.x == (1 << WIDTH) - 1, "an unknown condition picked a branch"
    else:
        assert as_digits(got) == (b if sel == "0" else a)


@settings(max_examples=200, deadline=None)
@given(a=digits)
def test_an_unreadable_signal_is_unknown_not_zero(a):
    """P1: no invented values. A signal the trace does not have reads as X, and
    X has to survive every operator above it."""
    read = lambda s: bv(a) if s.path() == A.path() else None  # noqa: E731
    got = evaluate(Binary("|", Ref(A, WIDTH), Ref(B, WIDTH)), read)
    assert as_digits(got) == ref_or(a, "x" * WIDTH)


@settings(max_examples=100, deadline=None)
@given(a=digits, b=digits)
def test_evaluation_is_deterministic(a, b):
    """P1, stated as a property: the same inputs give the same answer, always."""
    e = Binary("&", Binary("|", Ref(A, WIDTH), Ref(B, WIDTH)), Unary("~", Ref(A, WIDTH)))
    first = as_digits(evaluate(e, env(a, b)))
    for _ in range(3):
        assert as_digits(evaluate(e, env(a, b))) == first


@settings(max_examples=150, deadline=None)
@given(a=digits, b=digits)
def test_de_morgan_holds_on_four_state_values(a, b):
    """`~(a & b) == (~a | ~b)` is true bit by bit in 4-state logic too. An
    algebraic identity is a check the reference and the implementation cannot
    both get wrong in the same way."""
    read = env(a, b)
    ra, rb = Ref(A, WIDTH), Ref(B, WIDTH)
    left = as_digits(evaluate(Unary("~", Binary("&", ra, rb)), read))
    right = as_digits(
        evaluate(Binary("|", Unary("~", ra), Unary("~", rb)), read)
    )
    assert left == right


@settings(max_examples=150, deadline=None)
@given(
    a=digits,
    hi=st.integers(min_value=0, max_value=WIDTH - 1),
    lo=st.integers(min_value=0, max_value=WIDTH - 1),
)
def test_a_slice_selects_the_same_digits_the_string_does(a, hi, lo):
    from veritrace.graph.model import Slice

    assume(hi >= lo)
    got = evaluate(Slice(Ref(A, WIDTH), Const(hi, 32), Const(lo, 32)), env(a, a))
    # Digit 0 is the rightmost character.
    want = a[WIDTH - 1 - hi : WIDTH - lo]
    assert "".join(
        "x" if (got.x >> i) & 1 else str((got.v >> i) & 1)
        for i in reversed(range(hi - lo + 1))
    ) == want


def test_the_reference_and_the_implementation_disagree_about_nothing_by_default():
    """A guard against the reference silently becoming a no-op."""
    assert ref_and("01x", "111") == "01x"
    assert ref_or("01x", "000") == "01x"
    assert ref_truthy("000") == "0" and ref_truthy("00x") == "x"
    with pytest.raises(AssertionError):
        assert ref_and("0", "x") == "x"  # 0 & x is 0, not x
