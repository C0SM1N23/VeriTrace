"""§8.28 — mutation testing.

The operators are tested without a simulator: a mutation is a span and a
replacement, so "did it produce the right file" is answerable on its own. The
one test that needs Icarus says so and skips.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from veritrace.mutate.model import Mutation
from veritrace.mutate.operators import OPERATORS, _perturb, sites
from veritrace.mutate.run import Suite, _parses, _sample, run

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
FIFO = DESIGNS / "mutation" / "fifo.sv"


def test_every_operator_fires_on_real_rtl():
    """All seven rows of §8.28's table, on the reference designs."""
    found = set()
    for design in ("mutation/fifo.sv", "fsm/fsm_dut.sv", "axi_lite/axil_slave.sv"):
        found |= {m.operator for m in sites(DESIGNS / design)}
    assert found == set(OPERATORS), f"never fired: {sorted(set(OPERATORS) - found)}"


def test_every_mutant_of_the_reference_design_still_parses():
    """A mutation is derived from the AST, so it cannot produce nonsense.

    This is §8.28's reason for going through pyslang rather than a regular
    expression, and it is worth an assertion: a mutation that does not parse
    wastes a whole simulator run to prove the compiler works.
    """
    data = FIFO.read_bytes()
    for m in sites(FIFO):
        assert _parses(m.apply(data).decode(), FIFO.name), f"{m} broke the file"


def test_a_mutation_changes_exactly_its_own_span():
    data = FIFO.read_bytes()
    m = next(m for m in sites(FIFO) if m.operator == "relational")
    out = m.apply(data)
    assert out[: m.start] == data[: m.start]
    assert out[m.start : m.start + len(m.now)] == m.now.encode()
    assert out[m.start + len(m.now) :] == data[m.end :]


def test_offsets_are_bytes_not_characters():
    """A non-ASCII comment must not move the spans after it.

    Slang counts bytes and Python counts characters; mixing them silently
    corrupts every mutation past the first accented character in a file, which
    is why `designs/fsm/fsm_dut.sv` once produced no sites at all.
    """
    path = DESIGNS / "fsm" / "fsm_dut.sv"
    data = path.read_bytes()
    assert len(data) > len(data.decode()), "fixture no longer has multi-byte characters"
    found = sites(path)
    assert found
    for m in found:
        assert data[m.start : m.end].decode() == m.was


def test_declaration_literals_are_left_alone():
    """§8.28 perturbs constants in logic, not the shape of the design.

    `DEPTH = 16 -> 17` builds a different module; it says nothing about whether
    the tests would notice a bug in this one.
    """
    for m in sites(FIFO):
        assert "parameter" not in m.context or m.operator != "constant"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("1'b0", "1'b1"),
        ("1'b1", "1'b0"),
        ("4'd7", "4'd8"),
        ("8'hff", "8'h0"),
        ("'0", "'1"),
        ("3", "4"),
        ("4'bxx", None),
        ("2'bz0", None),
    ],
)
def test_constant_perturbation(raw, want):
    assert _perturb(raw) == want


def test_sampling_is_reproducible_and_spread_across_operators():
    every = sites(DESIGNS / "axi_lite" / "axil_slave.sv")
    a = _sample(every, 20, seed=3)
    b = _sample(every, 20, seed=3)
    assert [m.id for m in a] == [m.id for m in b]
    assert _sample(every, 20, seed=4) != a
    # A flat sample is dominated by whichever operator has the most sites.
    assert len({m.operator for m in a}) >= 4


def test_sample_of_zero_means_everything():
    every = sites(FIFO)
    assert _sample(every, 0, seed=1) == list(every)


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="needs Icarus")
def test_the_reference_fixture_scores_and_names_its_survivors(tmp_path):
    """§8.28's acceptance criterion, on the design built for it.

    `designs/mutation` is a *correct* FIFO with a testbench that never fills it,
    so every survivor should be on the line that decides when it is full.
    """
    root = DESIGNS / "mutation"
    suite = Suite(sources=("fifo.sv", "tb_fifo.sv"), top="tb_fifo", timeout=60)
    report = run([root / "fifo.sv"], suite, tmp_path, root, sample=0, seed=7, jobs=4)

    assert report.score is not None and report.killed > 0
    assert report.survivors, "a testbench that never fills the FIFO must leave survivors"
    lines = {s.mutation.line for s in report.survivors}
    assert lines == {25}, f"survivors outside the `full` expression: {lines}"


def test_a_mutant_that_does_not_build_is_not_scored():
    """§8.28 scores observation, not compilation (P1).

    A broken mutant tests the compiler; counting it as killed inflates the score
    with mutations no testbench could have been expected to catch.
    """
    from veritrace.mutate.model import MutationReport

    assert not _parses("module m; assign x = ; endmodule", "m.sv")

    report = MutationReport(killed=3)
    report.invalid.append(Mutation("index", FIFO, 1, 0, 1, "a", "b"))
    assert report.scored == 3, "an invalid mutant is in neither half of the ratio"
    assert report.score == 1.0
