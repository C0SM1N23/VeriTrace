"""Elaboration, guard extraction and expression evaluation — §5.1 to §5.4."""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace.graph import conditions as C
from veritrace.graph.conditions import BV
from veritrace.graph.elaborate import elaborate
from veritrace.graph.model import (
    Binary,
    Concat,
    Const,
    DriverKind,
    Kind,
    Ref,
    Reduce,
    Role,
    SignalId,
    Slice,
    Ternary,
    Unary,
    refs,
    to_text,
)

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
FIFO = [DESIGNS / "fifo_async" / "tb_fifo_sync.sv", DESIGNS / "fifo_async" / "fifo_sync.sv"]
LANES = [DESIGNS / "lanes" / "tb_lanes.sv", DESIGNS / "lanes" / "lanes.sv"]


@pytest.fixture(scope="module")
def fifo():
    return elaborate(FIFO)


@pytest.fixture(scope="module")
def lanes():
    return elaborate(LANES)


# --- elaboration -----------------------------------------------------------


def test_elaborates_without_errors(fifo):
    assert fifo.graph.top == "tb_fifo_sync"
    # Diagnostics are structured, so severity is a field rather than a substring
    # of rendered text — which is what let this assertion pass vacuously before.
    assert not fifo.errors, [str(d) for d in fifo.errors]


def test_signal_kinds_and_widths(fifo):
    g = fifo.graph
    assert g.get("tb_fifo_sync.dut.wr_ptr").width == 5
    assert g.get("tb_fifo_sync.dut.clk").kind is Kind.PORT_IN
    assert g.get("tb_fifo_sync.dut.full").kind is Kind.PORT_OUT
    assert g.get("tb_fifo_sync.dut.WIDTH").kind is Kind.PARAM
    # A memory reports one element's width, not the flattened array.
    mem = g.get("tb_fifo_sync.dut.mem")
    assert mem.kind is Kind.MEM and mem.width == 8


def test_procedural_locals_are_not_design_signals(fifo):
    # `for (int i = ...)` inside an initial block is not a net or a register.
    assert fifo.graph.get("tb_fifo_sync.i") is None


def test_generate_and_instance_arrays_are_expanded(lanes):
    g = lanes.graph
    # for-generate: indices baked into the path by elaboration (§7.1).
    for i in range(4):
        assert g.get(f"tb_lanes.dut.g_lane[{i}].u_fifo.wr_ptr") is not None
        assert g.get(f"tb_lanes.dut.g_lane[{i}].busy") is not None
    # instance array.
    for i in range(4):
        assert g.get(f"tb_lanes.dut.u_cnt[{i}].q") is not None


def test_source_locations_are_recorded(fifo):
    sig = fifo.graph.get("tb_fifo_sync.dut.wr_ptr")
    assert sig.decl_loc.file == "fifo_sync.sv"
    assert sig.decl_loc.line > 0
    assert all(d.loc.line > 0 for d in sig.drivers)


# --- guards (§5.2) ---------------------------------------------------------


def test_guard_accumulates_and_negates_else(fifo):
    """The §5.2 example: two drivers, the second guarded by the negated first."""
    wr = fifo.graph.get("tb_fifo_sync.dut.wr_ptr")
    guards = sorted(to_text(d.guard) for d in wr.drivers)
    assert guards == ["!rst_n", "(rst_n && (wr_en && !full))"]
    # Reset branch assigns zero; the enabled branch increments.
    by_guard = {to_text(d.guard): to_text(d.value) for d in wr.drivers}
    assert by_guard["!rst_n"] == "5'd0"
    assert "wr_ptr" in by_guard["(rst_n && (wr_en && !full))"]


def test_sequential_drivers_carry_clock_and_reset(fifo):
    wr = fifo.graph.get("tb_fifo_sync.dut.wr_ptr")
    for d in wr.drivers:
        assert d.kind is DriverKind.ALWAYS_FF
        assert d.is_sequential
        assert str(d.clock) == "tb_fifo_sync.dut.clk"
        assert str(d.reset) == "tb_fifo_sync.dut.rst_n"


def test_continuous_assign_has_no_guard(fifo):
    empty = fifo.graph.get("tb_fifo_sync.dut.empty")
    (d,) = empty.drivers
    assert d.kind is DriverKind.CONT_ASSIGN
    assert to_text(d.guard) == "1"
    assert to_text(d.value) == "(wr_ptr == rd_ptr)"


def test_case_guards_include_default_negation():
    src = """
    module m(input logic clk, input logic [1:0] s, output logic [1:0] y);
      always_ff @(posedge clk) begin
        case (s)
          2'd0: y <= 2'd1;
          2'd1, 2'd2: y <= 2'd2;
          default: y <= 2'd3;
        endcase
      end
    endmodule
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.sv"
        p.write_text(src)
        g = elaborate([p]).graph
    y = g.get("m.y")
    guards = [to_text(dr.guard) for dr in y.drivers]
    assert len(guards) == 3
    # Two labels on one item become a disjunction.
    assert any("||" in gg for gg in guards)
    # The default is guarded by the negation of every label taken before it.
    default = [gg for gg in guards if gg.count("!") >= 2]
    assert default, guards


def test_edges_are_tagged_guard_versus_value(fifo):
    """§5.4: why-trace walks GUARD edges, so the distinction must survive."""
    edges = list(fifo.graph.edges_into("tb_fifo_sync.dut.wr_ptr"))
    roles = {e.role for e in edges}
    assert Role.GUARD in roles and Role.VALUE in roles
    assert Role.CLOCK in roles and Role.RESET in roles
    guard_srcs = {str(e.src) for e in edges if e.role is Role.GUARD}
    assert "tb_fifo_sync.dut.rst_n" in guard_srcs
    assert "tb_fifo_sync.dut.wr_en" in guard_srcs


def test_refs_walks_the_whole_expression():
    e = Binary("&&", Ref(SignalId((), "a")), Unary("!", Ref(SignalId((), "b"))))
    assert {str(s) for s in refs(e)} == {"a", "b"}


# --- four-state evaluation (§5.3) ------------------------------------------


def read_none(_):
    return None


def env(**vals):
    """Read values by signal name, sizing each to fit (so `3` stays 3 bits)."""

    def read(sid: SignalId):
        v = vals.get(sid.name)
        if isinstance(v, BV) or v is None:
            return v
        return BV.of(v, max(int(v).bit_length(), 1))

    return read


def test_evaluate_basic_logic():
    a, b = Ref(SignalId((), "a")), Ref(SignalId((), "b"))
    e = env(a=1, b=0)
    assert C.evaluate(Binary("&&", a, b), e).to_int() == 0
    assert C.evaluate(Binary("||", a, b), e).to_int() == 1
    assert C.evaluate(Unary("!", b), e).to_int() == 1
    assert C.evaluate(Binary("==", a, b), e).to_int() == 0


def test_x_propagates_instead_of_becoming_zero():
    """X is a feature, not a nuisance (§5.3) — `int` would lose this."""
    a = Ref(SignalId((), "a"))
    b = Ref(SignalId((), "b"))
    unknown = env(a=BV.unknown(), b=1)
    assert C.evaluate(Binary("&&", a, b), unknown).known is False
    assert C.evaluate(Binary("==", a, b), unknown).known is False
    # But a forcing operand still decides the result.
    assert C.evaluate(Binary("&&", a, b), env(a=BV.unknown(), b=0)).to_int() == 0
    assert C.evaluate(Binary("||", a, b), env(a=BV.unknown(), b=1)).to_int() == 1


def test_unknown_signal_reads_as_x_not_zero():
    e = Ref(SignalId((), "missing"))
    assert C.evaluate(e, read_none).known is False


def test_evaluate_ternary_and_concat():
    a = Ref(SignalId((), "a"))
    e = env(a=1)
    assert C.evaluate(Ternary(a, Const(7, 4), Const(2, 4)), e).to_int() == 7
    assert C.evaluate(Ternary(Const(0, 1), Const(7, 4), Const(2, 4)), e).to_int() == 2
    got = C.evaluate(Concat((Const(0b10, 2), Const(0b11, 2))), e)
    assert got.to_int() == 0b1011 and got.w == 4


def test_evaluate_slice_and_reduction():
    v = Ref(SignalId((), "v"))
    e = env(v=BV.of(0b1011_0000, 8))
    assert C.evaluate(Slice(v, Const(7, 4), Const(4, 4)), e).to_int() == 0b1011
    assert C.evaluate(Reduce("|", v), e).to_int() == 1
    assert C.evaluate(Reduce("&", v), e).to_int() == 0


def test_case_equality_is_defined_on_x():
    a = Ref(SignalId((), "a"))
    b = Ref(SignalId((), "b"))
    both_x = env(a=BV.unknown(), b=BV.unknown())
    # `==` is unknown when either side is X, `===` compares X literally.
    assert C.evaluate(Binary("==", a, b), both_x).known is False
    assert C.evaluate(Binary("===", a, b), both_x).to_int() == 1


def test_arithmetic_and_x_contamination():
    a, b = Ref(SignalId((), "a")), Ref(SignalId((), "b"))
    assert C.evaluate(Binary("+", a, b), env(a=3, b=4)).to_int() == 7
    assert C.evaluate(Binary("+", a, b), env(a=BV.unknown(), b=4)).known is False


def test_guard_algebra_stays_readable():
    from veritrace.graph.model import TRUE

    a = Ref(SignalId((), "a"))
    assert C.land(TRUE, a) is a
    assert C.land(a, TRUE) is a
    assert C.lnot(C.lnot(a)) is a
