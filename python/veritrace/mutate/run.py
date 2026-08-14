"""Sampling, running and scoring the mutants — §8.28 steps 1-5.

Three decisions worth stating.

**Sandboxes, not in-place edits.** A mutant is a changed source file, and running
several at once means several trees. One copy *per worker* rather than per mutant
keeps that at `jobs` copies instead of `sample` of them, and a worker restores its
file before taking the next job.

**Threads, not processes.** Every second of this run is spent inside a simulator
subprocess, so the GIL is released the whole time; `ProcessPoolExecutor` would add
pickling and a second copy of the interpreter to buy nothing.

**A mutant that does not compile is not a kill.** §8.28 scores whether the tests
*observe* a change. A mutation that breaks the build tested the compiler, and
counting it would inflate the score with mutants no testbench could have caught
(P1). It is checked for syntactically before it is ever run, and the built-in
Icarus suite separates its build step from its run step so the distinction stays
exact rather than inferred from an exit code.
"""

from __future__ import annotations

import queue
import random
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from pyslang import syntax as S

from veritrace.mutate.model import Mutation, MutationReport, Survivor
from veritrace.mutate.operators import sites

#: Directories and artefacts a sandbox has no use for. Copying a `.git` or a
#: previous `.veritrace` per worker is most of the wall time on a real project.
SKIP = shutil.ignore_patterns(
    ".git", ".veritrace", "work", "__pycache__", "*.vvp", "*.vcd", "*.vtx",
    "*.vtx.session.json", "*.wlf", "transcript",
)

KILLED, SURVIVED, INVALID = "killed", "survived", "invalid"

#: Icarus reports `$fatal` and `$error` on stdout and still exits 0 — `vvp` only
#: fails the process for its own errors, not the design's. Reading the exit code
#: alone would score every mutant a survivor and report 0%, so the severity
#: markers it prints are honoured as well. Both are Icarus' own wording.
ICARUS_FAILED = ("FATAL:", "ERROR:")


@dataclass(frozen=True, slots=True)
class Suite:
    """How one mutant is judged.

    `command` is the user's own suite (§8.28's `--run "make sim-verilator"`): a
    black box, so a non-zero exit is a kill and nothing finer can be said. When
    it is absent the sources are built and run with Icarus directly, which can
    tell a build failure from a failing test.
    """

    command: str = ""
    sources: tuple[str, ...] = ()
    top: str = ""
    timeout: float = 300.0

    def verdict(self, sandbox: Path) -> tuple[str, str]:
        if self.command:
            out = _shell(self.command, sandbox, self.timeout)
            return (KILLED if out.returncode else SURVIVED), _why(out)
        return self._icarus(sandbox)

    def _icarus(self, sandbox: Path) -> tuple[str, str]:
        from veritrace.simulate import find_iverilog

        tools = find_iverilog()
        if tools is None:
            raise RuntimeError("Icarus is not installed; pass --run with your own suite")
        iverilog, vvp = tools
        # Every flag before the first source: iverilog stops taking options once
        # it has seen a file, and reads `-s` as a filename.
        args = [iverilog, "-g2012", *(["-s", self.top] if self.top else []), "-o", "mutant.vvp"]
        build = _exec([*args, *self.sources], sandbox, self.timeout)
        if build.returncode:
            return INVALID, _why(build)
        run = _exec([vvp, "mutant.vvp"], sandbox, self.timeout)
        text = run.stdout + run.stderr
        failed = run.returncode != 0 or any(m in text for m in ICARUS_FAILED)
        return (KILLED if failed else SURVIVED), _why(run)


def _exec(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except subprocess.TimeoutExpired:
        # A mutant that hangs is one the suite noticed — an infinite loop is a
        # failure, and waiting for it forever is not an option at 200 of them.
        return subprocess.CompletedProcess(args, 124, "", f"no result within {timeout:.0f}s")


def _shell(command: str, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, cwd=cwd, shell=True, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"no result within {timeout:.0f}s")


def _why(out: subprocess.CompletedProcess[str]) -> str:
    text = (out.stderr or out.stdout or "").strip()
    return text.splitlines()[-1][:200] if text else ""


def _parses(text: str, name: str) -> bool:
    """Whether the mutant is still valid SystemVerilog.

    Cheap, and it keeps a broken mutant out of the score without needing a
    compiler to tell us the same thing several seconds later.
    """
    try:
        tree = S.SyntaxTree.fromText(text, name)
    except Exception:  # noqa: BLE001 - slang raises several unrelated types
        return False
    return not any(d.isError for d in tree.diagnostics)


def run(
    files: Iterable[Path],
    suite: Suite,
    work: Path,
    root: Path,
    sample: int = 200,
    seed: int = 0,
    operators: set[str] | None = None,
    jobs: int = 4,
    on_result: Callable[[Mutation, str], None] | None = None,
) -> MutationReport:
    """§8.28 end to end: sample, mutate, run, score."""
    started = time.perf_counter()
    files = [Path(f) for f in files]
    root = Path(root).resolve()

    every = [m for f in files for m in sites(f, operators)]
    chosen = _sample(every, sample, seed)

    report = MutationReport(
        seed=seed, sampled=len(chosen), total_sites=len(every),
        command=suite.command or f"iverilog + vvp (top {suite.top or '?'})",
    )
    if not chosen:
        report.elapsed_s = time.perf_counter() - started
        return report

    jobs = max(1, min(jobs, len(chosen)))
    pool: queue.Queue[Path] = queue.Queue()
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    for i in range(jobs):
        box = work / f"sandbox{i}"
        if box.exists():
            shutil.rmtree(box, ignore_errors=True)
        shutil.copytree(root, box, ignore=SKIP, dirs_exist_ok=True)
        pool.put(box)

    def one(m: Mutation) -> tuple[Mutation, str, str]:
        box = pool.get()
        try:
            target = box / m.file.resolve().relative_to(root)
            original = target.read_bytes()
            mutated = m.apply(original)
            if not _parses(mutated.decode("utf-8", errors="replace"), target.name):
                return m, INVALID, "the mutant is not valid SystemVerilog"
            target.write_bytes(mutated)
            try:
                status, note = suite.verdict(box)
            finally:
                target.write_bytes(original)
            return m, status, note
        finally:
            pool.put(box)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for m, status, note in ex.map(one, chosen):
            if on_result:
                on_result(m, status)
            if status == KILLED:
                report.killed += 1
            elif status == INVALID:
                report.invalid.append(m)
            else:
                report.survivors.append(Survivor(m, note))

    report.elapsed_s = time.perf_counter() - started
    return report


def _sample(every: list[Mutation], n: int, seed: int) -> list[Mutation]:
    """`n` mutants, reproducibly, spread across operators.

    A flat sample is dominated by whichever operator has the most sites —
    constants, always — and a run that is 80% constant perturbation says little
    about the rest. Drawing round-robin from each operator's own shuffled pool
    keeps the mix representative without needing weights to tune.
    """
    if n <= 0 or n >= len(every):
        return list(every)
    rng = random.Random(seed)
    pools: dict[str, list[Mutation]] = {}
    for m in every:
        pools.setdefault(m.operator, []).append(m)
    for group in pools.values():
        rng.shuffle(group)

    out: list[Mutation] = []
    order = sorted(pools)
    while len(out) < n and any(pools[k] for k in order):
        for k in order:
            if pools[k] and len(out) < n:
                out.append(pools[k].pop())
    out.sort(key=lambda m: (str(m.file), m.start, m.operator))
    return out
