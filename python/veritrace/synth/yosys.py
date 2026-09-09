"""Synthesis, and the one piece of glue it needs — §8.29 steps 1 and 3.

Yosys emits a netlist as plain Verilog operators and `always` blocks, so Icarus
simulates it directly with no cell library. What it does *not* emit is
parameters: synthesis elaborates them away, and the netlist module has a fixed
port list. §8.29 insists the netlist is simulated "cu acelasi testbench", and
that testbench instantiates the DUT with overrides — so a bare netlist does not
even elaborate.

The fix is a **shim**: the netlist top is renamed, and a module with the original
name, the original parameter declarations and the original ports is generated to
wrap it. The parameters are accepted and ignored, which is correct, because the
netlist was synthesised at exactly the values the testbench passes — read off the
same elaboration the rest of VeriTrace uses, not guessed from defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from veritrace import tools
from veritrace.graph.model import Kind
from veritrace.synth.model import Instance, Port

INSTALL = "apt-get install -y yosys"
#: Suffix for the synthesised module, so the shim can take its name back.
GATE = "__vt_gate"


@dataclass(frozen=True, slots=True)
class Netlist:
    path: Path
    top: str
    synthesiser: str
    #: Yosys' own log, kept because its warnings are the interesting half — an
    #: inferred latch is announced here long before it shows up as a divergence.
    log: str = ""


def dut_of(elaboration: object, top: str, name: str | None = None) -> Instance:
    """The instance under `top` that synth-diff should synthesise.

    With no `name`, the design's single child of the testbench. A testbench with
    several children has to say which, because "the DUT" is not something the
    elaboration knows.
    """
    instances: dict[str, str] = dict(getattr(elaboration, "instances", {}) or {})
    children = sorted(
        p for p in instances if p.count(".") == 1 and p.startswith(top + ".")
    )
    if name:
        want = name if name.startswith(top + ".") else f"{top}.{name}"
        if want not in instances:
            raise tools.ToolError(
                f"{want} is not an instance under {top}; try one of "
                + ", ".join(c.split(".")[-1] for c in children)
            )
        path = want
    elif len(children) == 1:
        path = children[0]
    else:
        raise tools.ToolError(
            f"{top} instantiates {len(children)} modules, so --dut has to say which: "
            + ", ".join(c.split(".")[-1] for c in children)
        )

    graph = getattr(elaboration, "graph", None)
    ports = sorted(
        (
            Port(s.id.name, "input" if s.kind is Kind.PORT_IN else "output", s.width)
            for s in (graph or ())
            if ".".join(s.id.hier) == path and s.kind in (Kind.PORT_IN, Kind.PORT_OUT)
        ),
        key=lambda p: p.name,
    )

    params = getattr(elaboration, "parameters", {}) or {}
    values = tuple(
        (p.name, p.value)
        for p in sorted(params.get(path, {}).values(), key=lambda p: p.name)
        # A localparam is derived inside the module and must not be overridden;
        # passing one to `chparam` is an error, and passing one to the shim is a
        # redeclaration.
        if not p.local
    )
    return Instance(path=path, module=instances[path], ports=tuple(ports), parameters=values)


#: `4'd7`, `32'sd-1`, `8` — what an elaborated parameter value looks like coming
#: out of slang. Yosys' `chparam` wants a plain integer or a sized constant, and
#: anything else (a string, a real, a struct) is left to Yosys' own default.
_INT = re.compile(r"^-?\d+$")
_SIZED = re.compile(r"^\d+'[sS]?[bBoOdDhH][0-9a-fA-F_]+$")


def synthesise(
    sources: list[Path],
    dut: Instance,
    work: Path,
    incdirs: list[str] | None = None,
    defines: list[str] | None = None,
    timeout: float = 600.0,
) -> Netlist:
    """§8.29 step 1, plus the shim that makes step 3 possible."""
    yosys = tools.require("yosys", INSTALL)
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out = work / "netlist.v"

    read = ["read_verilog -sv"]
    read += [f"-I{yosys.path(d)}" for d in (incdirs or [])]
    read += [f"-D{d}" for d in (defines or [])]
    read += [yosys.path(Path(s).resolve()) for s in sources]

    script = [" ".join(read)]
    for name, value in dut.parameters:
        if _INT.match(value) or _SIZED.match(value):
            script.append(f"chparam -set {name} {value} {dut.module}")
    script += [
        f"synth -top {dut.module}",
        # The shim below takes the original name, so the netlist has to give it
        # up or the two modules collide at elaboration.
        f"rename {dut.module} {dut.module}{GATE}",
        # `-noattr` keeps the file readable; without `-noexpr` Yosys renders its
        # internal cells as Verilog operators, which is what lets Icarus simulate
        # this with no cell library at all.
        f"write_verilog -noattr {yosys.path(out)}",
    ]
    run = yosys.run(["-p", "; ".join(script)], cwd=work, timeout=timeout)
    if run.returncode != 0 or not out.exists():
        raise tools.ToolError(
            "yosys could not synthesise the design:\n"
            + (run.stderr or run.stdout).strip()[-2000:]
        )

    out.write_text(out.read_text(encoding="utf-8", errors="replace") + shim(dut), encoding="utf-8")
    return Netlist(path=out, top=dut.module, synthesiser=f"yosys ({yosys.where})", log=run.stdout)


def shim(dut: Instance) -> str:
    """A module with the original name, parameters and ports, wrapping the netlist.

    The parameters are declared and unused. That is not an oversight: the netlist
    was synthesised *at* the values this instance elaborated to, so honouring an
    override here would be a lie — and refusing to accept one would stop the
    testbench compiling. Declared-and-ignored is the honest shape, and it is
    stated in the generated file.
    """
    params = ",\n".join(f"    parameter {n} = {v}" for n, v in dut.parameters)
    ports = ",\n".join(f"    {p.decl}" for p in dut.ports)
    conns = ",\n".join(f"    .{p.name}({p.name})" for p in dut.ports)
    return "\n".join(
        [
            "",
            "// Generated by VeriTrace — §8.29. Lets the *unmodified* testbench drive",
            f"// the synthesised {dut.module}.",
            "//",
            "// The parameters below are accepted and ignored. Synthesis elaborated them",
            "// away, and the netlist above was built at the values this instance already",
            "// had, so there is nothing left for an override to change. Refusing them",
            "// instead would stop the testbench from compiling at all.",
            f"module {dut.module} #(" if params else f"module {dut.module} (",
            *([params, ") ("] if params else []),
            ports,
            ");",
            f"  {dut.module}{GATE} u_gate (",
            conns,
            "  );",
            "endmodule",
            "",
        ]
    )
