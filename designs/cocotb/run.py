"""Run the monitor without `make` — cocotb's own Python runner.

cocotb ships a Makefile flow and a runner API. The Makefile is what most
projects use; on Windows it hands MSYS-style paths to `CreateProcess`, which
cannot resolve them, so the runner is used here instead. Same simulator, same
monitor, and no build system to get wrong.

    cd designs/cocotb && python run.py       # writes sim.log next to this file
"""

from pathlib import Path

from cocotb_tools.runner import get_runner

HERE = Path(__file__).parent


def main() -> None:
    runner = get_runner("icarus")
    # `waves=True` here generates cocotb's dumper module and compiles it in;
    # `waves=False` at test time is what leaves `-fst` off the vvp command, so
    # Icarus writes a VCD. cocotb suppresses any dumper but its own, so this
    # split is the only way to get one in a format the store reads.
    runner.build(
        sources=[HERE / "axil_slave.sv"],
        hdl_toplevel="axil_slave",
        build_dir=HERE / "build",
        always=True,
        waves=True,
    )
    runner.test(
        hdl_toplevel="axil_slave",
        test_module="axi_mon",
        build_dir=HERE / "build",
        # Forward slashes: the path travels through a Verilog string literal,
        # and a backslash in one is an escape.
        plusargs=["+dumpfile_path=" + (HERE / "wave.vcd").as_posix()],
        # A seed makes the run reproducible, which is the same requirement
        # §13.6 puts on every recorded regression.
        seed=1234,
    )


if __name__ == "__main__":
    main()
