"""§8.32 — coverage-directed stimulus generation.

The acceptance criterion is a loop, not a function: run, see a hole, generate,
run again, watch the hole close. That is the last test here, and it needs Icarus.
Everything before it checks the parts that decide whether the loop can work at
all — that the generator only claims holes it can actually reach, and that it
uses the scorer's own predicate to reach them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import design_store

from veritrace.formal import harness
from veritrace.graph.elaborate import discover, elaborate
from veritrace.protocol import pack as pack_mod
from veritrace.stim import generate, holes_from, render
from veritrace.stim.emit import TARGETS
from veritrace.stim.generate import drivable
from veritrace.simulate import find_iverilog

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
AXI = DESIGNS / "axi_lite"


@pytest.fixture(scope="module")
def slave():
    """The AXI4-Lite slave, as something to drive."""
    el = elaborate(discover(AXI), [], [], "axil_slave")
    pack = pack_mod.resolve(["axi4lite"])[0]
    iface = harness.interfaces(el, [pack])[0]
    return el, pack, iface, harness.instance_of(el, "axil_slave")


@pytest.fixture(scope="module")
def holes(tmp_path_factory):
    """The real coverage report for the reference run — 47%, with named gaps."""
    from veritrace import TraceStore, clocks
    from veritrace.coverage import report as cov
    from veritrace.protocol import engine

    store = TraceStore(str(design_store("axi_lite")))
    clock = clocks.resolve(store, None, None)
    protocol = engine.extract(store, AXI / "dump.vtx", clock, None, project_root=AXI)
    report = cov.build(protocol, store, clock, project_root=AXI)
    return holes_from(report.to_dict())


def test_the_holes_come_from_the_coverage_report(holes):
    """§8.32 reads §8.21's output; it does not have its own idea of a hole."""
    assert ("unaligned", "hit") in holes
    assert ("partial_write", "hit") in holes
    assert ("kind", "READ x READ") in holes


def test_only_initiator_driven_fields_are_offered(slave):
    """A response field is the DUT's answer, not our stimulus."""
    _, pack, _, _ = slave
    write = next(t for t in pack.transactions if t.name == "WRITE")
    fields = drivable(pack, write)
    assert {"awaddr", "awprot", "wdata", "wstrb"} <= set(fields)
    assert "bresp" not in fields, "bresp is the slave's answer"


def test_a_hole_no_stimulus_can_close_is_reported_not_attempted(slave, holes):
    """Saying so beats generating traffic that will never work."""
    el, pack, iface, _ = slave
    plan = generate(pack, iface, el.graph, n=20, seed=1, holes=holes)
    by = {(t.point, t.bin): t for t in plan.targets}

    assert by[("bresp", "DECERR")].status == "skipped"
    assert "responder" in by[("bresp", "DECERR")].reason
    assert by[("unaligned", "hit")].status == "targeted"
    assert by[("partial_write", "hit")].status == "targeted"


def test_a_targeted_corner_really_satisfies_the_packs_own_predicate(slave, holes):
    """The generator and the scorer must not have two ideas of "unaligned".

    This evaluates the `[[cover]]` node — the same object `coverage/functional.py`
    evaluates — against the item that was generated to hit it.
    """
    from veritrace.protocol import expr
    from veritrace.stim.generate import _env_for

    el, pack, iface, _ = slave
    plan = generate(pack, iface, el.graph, n=12, seed=5, holes=holes)
    specs = {c.name: c for c in pack.cover}

    for item in plan.items:
        if not item.targets:
            continue
        point, _, _bin = item.targets.partition("/")
        spec = specs.get(point)
        if spec is None or not spec.corner:
            continue
        txn = next(t for t in pack.transactions if t.name == item.kind)
        assert expr.evaluate(spec.node, _env_for(pack, txn, item.values, 4)), item


def test_generation_is_reproducible(slave, holes):
    el, pack, iface, _ = slave
    a = generate(pack, iface, el.graph, n=30, seed=9, holes=holes)
    b = generate(pack, iface, el.graph, n=30, seed=9, holes=holes)
    assert [i.to_dict() for i in a.items] == [i.to_dict() for i in b.items]
    assert generate(pack, iface, el.graph, n=30, seed=10, holes=holes).items != a.items


def test_fields_are_drawn_inside_their_declared_width(slave):
    el, pack, iface, _ = slave
    plan = generate(pack, iface, el.graph, n=200, seed=3)
    assert plan.widths["awaddr"] == 32 and plan.widths["wstrb"] == 4
    for item in plan.items:
        for name, value in item.values.items():
            assert 0 <= value < (1 << plan.widths[name]), f"{name}={value}"


@pytest.mark.parametrize("target", TARGETS)
def test_both_targets_emit_something_their_language_accepts(slave, target, tmp_path):
    el, pack, iface, dut = slave
    plan = generate(pack, iface, el.graph, n=6, seed=2)
    text = render(plan, pack, iface, dut, target)
    if target == "cocotb":
        # Not run — cocotb is not a dependency of this project — but it must at
        # least be Python, or the file is not worth shipping.
        compile(text, "stim.py", "exec")
        assert "async def stimulus(dut)" in text
    else:
        assert text.startswith("`timescale") and "endmodule" in text
        assert f"{dut.module} #(" in text or f"{dut.module} dut (" in text


@pytest.mark.skipif(find_iverilog() is None, reason="needs Icarus")
def test_the_generated_stimulus_closes_the_holes(slave, holes, tmp_path):
    """§8.32's acceptance criterion, run rather than argued.

    Generate against the real coverage report, simulate, measure again: the four
    holes stimgen said it would target are closed, and the four it said no
    stimulus could reach are still open. Being right about the second half is
    what makes the first half trustworthy.
    """
    from veritrace import TraceStore, clocks, store as store_mod
    from veritrace.coverage import report as cov
    from veritrace.protocol import engine

    el, pack, iface, dut = slave
    plan = generate(pack, iface, el.graph, n=40, seed=42, holes=holes)
    shutil.copyfile(AXI / "axil_slave.sv", tmp_path / "axil_slave.sv")
    (tmp_path / "vt_stim_tb.sv").write_text(render(plan, pack, iface, dut, "sv"), encoding="utf-8")

    iverilog, vvp = find_iverilog()
    build = subprocess.run(
        [iverilog, "-g2012", "-s", "vt_stim_tb", "-o", "stim.vvp",
         "axil_slave.sv", "vt_stim_tb.sv"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([vvp, "stim.vvp"], cwd=tmp_path, capture_output=True,
                         text=True, timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "transaction(s) driven" in run.stdout, run.stdout

    dump = store_mod.ensure(tmp_path / "dump.vcd")
    after_store = TraceStore(str(dump))
    clock = clocks.resolve(after_store, None, None)
    protocol = engine.extract(after_store, dump, clock, None, project_root=tmp_path)
    after = holes_from(cov.build(protocol, after_store, clock, project_root=tmp_path).to_dict())

    closed = set(holes) - set(after)
    assert ("unaligned", "hit") in closed
    assert ("partial_write", "hit") in closed
    assert ("kind", "READ x READ") in closed and ("kind", "WRITE x WRITE") in closed
    # And the ones it refused to promise are still open, which is the honest half.
    assert ("bresp", "DECERR") in set(after)
