"""§8.29 end to end: synthesise, simulate both, diff the ports.

The diff is §8.7's, unchanged — that is the whole argument of §8.29. Two runs of
one testbench against two versions of one design is exactly the shape the
first-divergence engine already handles, so this module is the flow around it and
not a second comparison.
"""

from __future__ import annotations

import time
from pathlib import Path

from veritrace import diff as diff_mod
from veritrace import simulate
from veritrace.graph.elaborate import declares
from veritrace.synth.model import Instance, SynthDiff
from veritrace.synth.yosys import Netlist, synthesise


def run(
    sources: list[Path],
    testbench: list[Path],
    top: str,
    dut: Instance,
    work: Path,
    incdirs: list[str] | None = None,
    defines: list[str] | None = None,
    rtl_trace: Path | None = None,
    limit: int = 12,
    timeout: float = 300.0,
    run_dir: Path | None = None,
) -> SynthDiff:
    """Steps 1-4. Step 5 — why-trace on both — is the caller's, and needs a graph."""
    started = time.perf_counter()
    work = Path(work).resolve()
    out = SynthDiff(ports=tuple(p.name for p in dut.ports))

    netlist = synthesise(sources, dut, work / "synth", incdirs, defines)
    out.synthesiser, out.netlist = netlist.synthesiser, netlist.path

    # Step 2. An RTL waveform the caller already has is reused: the point of
    # comparison is the netlist, and re-running a simulation that produced the
    # trace on disk buys nothing.
    if rtl_trace is None:
        rtl = simulate.icarus(
            [*sources, *testbench], top, work / "rtl", defines, incdirs,
            timeout=timeout, run_dir=run_dir,
        )
        rtl_trace = rtl.dump
    out.rtl_trace = Path(rtl_trace)

    # Step 3. The netlist replaces **only the file that declared the DUT**; the
    # rest of the design stays as it was and the testbench is byte-identical.
    # Replacing everything breaks any testbench that instantiates more than the
    # one module being synthesised, which is most of them.
    home = declares(sources, dut.module)
    rest = [s for s in sources if home is None or Path(s).resolve() != home.resolve()]
    gate = simulate.icarus(
        [netlist.path, *rest, *testbench], top, work / "gate", defines, incdirs,
        timeout=timeout, run_dir=run_dir,
    )
    out.gate_trace = gate.dump

    # Step 4. §8.29 says top-level ports, and `dut` knows which those are.
    scope = dut.path.split(".", 1)[1] if "." in dut.path else dut.path
    out.report = _compare(out.rtl_trace, out.gate_trace, dut, scope, limit)
    out.elapsed_s = time.perf_counter() - started
    return out


def _compare(rtl: Path, gate: Path, dut: Instance, scope: str, limit: int) -> object:
    from veritrace import TraceStore, clocks, store

    a = TraceStore(str(store.ensure(rtl)))
    b = TraceStore(str(store.ensure(gate)))
    side_a = diff_mod.side(rtl.name, a, clocks.resolve(a, None, None))
    side_b = diff_mod.side(gate.name, b, clocks.resolve(b, None, None))
    alignment = diff_mod.align(side_a, side_b, "cycle")
    # Normalised paths drop the top scope (§8.7 step 1), so a port of the DUT
    # reads as `dut.clk` on both sides however the two testbench tops are named.
    ports = [f"{scope}.{p.name}" for p in dut.ports]
    return diff_mod.compare(alignment, limit=limit, only=ports)


__all__ = ["run", "Netlist", "synthesise"]
