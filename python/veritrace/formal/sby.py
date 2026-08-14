"""Driving SymbiYosys, and reading its answer back — §8.27 steps 2-4.

SBY's own output is the source of truth for the verdict. It prints one line per
assertion in `cover`/`bmc` mode and ends with a status line; the failing trace
lands in `<task>/engine_0/trace.vcd`. Nothing here re-derives what the solver
decided — the translation is from SBY's wording into §8.27's, which insists that
a pass is reported with the depth that produced it.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from veritrace import tools
from veritrace.formal.model import FormalReport, Property, Verdict

INSTALL = "apt-get install -y yosys && git clone https://github.com/YosysHQ/sby && make -C sby install"

MODES = ("bmc", "prove", "cover")

#: SBY's summary names each assertion it broke and the step it broke at:
#:
#:     failed assertion vt_formal_top.u_chk.AXI_AWSTABLE at checker.sv:38 step 1
#:     reached cover statement vt_formal_top.u_dut.vt_reach_1 at f.sv:91 step 1
#:     Unreached cover statement at vt_formal_top.u_dut: vt_reach_0
#:
#: The label is the last dotted component, which is the `id` the pack gave the
#: rule — that is the whole reason `_verilog` emits `NAME: assert (...)` rather
#: than a bare assertion.
#:
#: `(?<!un)` on the reached pattern is not decoration: "unreached cover
#: statement" contains "reached cover statement", so without it every dead
#: branch is reported as reachable — the answer that costs someone a day writing
#: a test for something that cannot happen.
_FAIL = re.compile(r"failed assertion \S*?(\w+) at \S+ step (\d+)", re.I)
_REACHED = re.compile(r"(?<!un)reached cover statement \S*?(\w+) at \S+ step (\d+)", re.I)
_UNREACHED = re.compile(r"unreached cover statement (?:at \S+?:\s*)?(\w+)", re.I)


def write(
    work: Path,
    files: list[Path],
    top: str,
    mode: str = "bmc",
    depth: int = 20,
    engine: str = "smtbmc z3",
    tool: tools.Tool | None = None,
) -> Path:
    """The `.sby` of §8.27 step 2."""
    if mode not in MODES:
        raise tools.ToolError(f"unknown mode {mode!r}; try one of {', '.join(MODES)}")
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    here = tool.path if tool else str

    reads = "\n".join(f"read -formal {Path(f).name}" for f in files)
    sby = work / "check.sby"
    sby.write_text(
        "\n".join(
            [
                "[options]",
                f"mode {mode}",
                f"depth {depth}",
                # §8.27's honesty requirement starts here: `prove` attempts
                # k-induction and can return an unbounded result, `bmc` cannot.
                # Which one ran is recorded and reported either way.
                "",
                "[engines]",
                engine,
                "",
                "[script]",
                reads,
                f"prep -top {top}",
                "",
                "[files]",
                *(here(Path(f).resolve()) for f in files),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return sby


def run(
    files: list[Path],
    top: str,
    work: Path,
    properties: dict[str, str],
    mode: str = "bmc",
    depth: int = 20,
    engine: str = "smtbmc z3",
    timeout: float = 900.0,
) -> FormalReport:
    """§8.27 steps 2-4: write the `.sby`, run it, translate the outcome.

    `properties` is `id -> text`, so the report can name a rule the way the pack
    wrote it rather than the way Yosys mangled it.
    """
    started = time.perf_counter()
    sby_tool = tools.require("sby", INSTALL)
    work = Path(work).resolve()

    # SBY resolves `[files]` against its own directory and copies them in, so the
    # script's `read` lines use bare names — see `write`.
    path = write(work, files, top, mode, depth, engine, sby_tool)
    out = sby_tool.run(["-f", sby_tool.path(path)], cwd=work, timeout=timeout)
    log = out.stdout + out.stderr

    report = FormalReport(mode=mode, depth=depth, engine=engine, work=work, log=log)
    report.properties = _translate(properties, log, mode, depth, work / "check")
    report.elapsed_s = time.perf_counter() - started
    if not report.properties:
        raise tools.ToolError(
            "sby produced no verdict. Its output was:\n" + log.strip()[-2000:]
        )
    return report


def _translate(
    properties: dict[str, str], log: str, mode: str, depth: int, task: Path
) -> list[Property]:
    """SBY's wording, in §8.27's terms.

    The unit of a verdict is the *property*, not the run: in `bmc` mode SBY stops
    at the first assertion it breaks, so everything it did not name is reported
    as having held to the depth searched — which is exactly what happened, and
    which is why the depth is part of the sentence.
    """
    trace = _trace_of(task)
    hit = {m.group(1).lower(): int(m.group(2)) for m in _FAIL.finditer(log)}
    reached = {m.group(1).lower(): int(m.group(2)) for m in _REACHED.finditer(log)}
    unreached = {m.group(1).lower() for m in _UNREACHED.finditer(log)}
    # `DONE (PASS)` for bmc/prove, `DONE (PASS, rc=0)` for cover with everything
    # reached — either way it is the line that says the run finished cleanly.
    finished = "DONE (PASS" in log
    ran = finished or "DONE (FAIL" in log

    # A bounded model check stops at the first step it can break something. Every
    # other property was therefore only examined up to *that* step, not to the
    # requested depth, and reporting them as held to `depth` would be a stronger
    # claim than the run supports — the exact overstatement §8.27 is written to
    # prevent, just one level further in.
    reached_depth = min(hit.values()) if hit else depth

    out: list[Property] = []
    for pid, text in properties.items():
        p = Property(id=pid, text=text, depth=depth)
        low = pid.lower()
        if mode == "cover":
            if low in reached:
                p.verdict, p.step, p.trace = Verdict.FAILED, reached[low], trace
            elif low in unreached or finished:
                p.verdict = Verdict.HELD
            else:
                p.reason = "sby did not report on this cover statement"
        elif low in hit:
            p.verdict, p.step, p.trace = Verdict.FAILED, hit[low], trace
        elif ran:
            # Not named by a run that did finish: no counterexample exists for it
            # as far as the search got. That is the strongest thing bounded model
            # checking can say, and `Property.__str__` says it with the depth
            # attached.
            p.verdict, p.depth = Verdict.HELD, reached_depth
        else:
            # Neither a verdict nor a completed run — a timeout, or a tool error.
            # Saying HELD here would be the unqualified claim §8.27 forbids, in
            # its most misleading form.
            p.reason = "the run did not finish; no claim can be made"
        out.append(p)
    return out


def _trace_of(task: Path) -> Path | None:
    """The counterexample SBY wrote, whichever engine directory it landed in."""
    for vcd in sorted(task.glob("engine_*/trace*.vcd")):
        return vcd
    return None
