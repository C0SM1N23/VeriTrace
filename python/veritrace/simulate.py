"""Run a simulation, so that pointing VeriTrace at a folder is enough.

§4.0 is blunt that nobody reads documentation for simulator flags and that the
flags are not optional. `make sim-<tool>` answers that for a project that has a
Makefile; this answers it for the first five minutes, when someone has a folder
of SystemVerilog and a question.

Two things here are not obvious and are the reason the folder-in case works at
all:

* **A testbench with no `$dumpfile` produces no waveform**, and most do not have
  one until someone needs it. Rather than editing the user's source, a tiny
  extra module is compiled alongside it, which is legal Verilog and leaves the
  tree untouched.
* **Everything lands in a work directory**, so a run leaves `sim.vvp` and a
  dump somewhere obvious and deletable instead of scattering them next to the
  RTL — but the simulation still *runs* where the user's own flow runs it, so
  that `$readmemh("program.hex")` and `+incdir+../../hdl` resolve to the same
  files they always did. Those two are separate directories on purpose, and
  conflating them is what breaks every real project's testbench.

Icarus is the only simulator driven from here on purpose. It is §4.0's primary
target, it needs no flags to keep the hierarchy, and it is the one that installs
in a minute. The other three keep their `make sim-<tool>` recipes, which is
where their flags are already right — reimplementing those in Python would be
three more places for them to drift.
"""

from __future__ import annotations

import re
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from veritrace import tools

#: The two calls are deliberately tracked separately.  A testbench can name a
#: dump file and still forget to select anything with ``$dumpvars``; treating
#: the former as proof of the latter produces an empty waveform and a very
#: confusing parser error after a simulation that appeared to pass.
_DUMPFILE_CALL = re.compile(r"\$dumpfile\s*\(", re.I)
_DUMPVARS_CALL = re.compile(r"\$dumpvars\s*\(", re.I)

#: Literal dump paths can be followed after the run without changing the
#: user's source.  Dynamic expressions cannot be predicted safely; those are
#: still discovered when they land in the simulator cwd or work directory.
_LITERAL_DUMPFILE = re.compile(
    r'(?P<head>\$dumpfile\s*\(\s*)"(?P<path>(?:\\.|[^"\\])*)"(?P<tail>\s*\))',
    re.I,
)

#: Name of the module compiled in to produce a waveform when nothing else does.
#: Leading underscore so it sorts away from the design and cannot collide with
#: anything a person would write.
DUMPER_MODULE = "_veritrace_dump"


class SimulationError(RuntimeError):
    """The simulator refused, or the run produced nothing usable."""


@dataclass(slots=True)
class Result:
    dump: Path
    log: Path
    top: str
    #: True when a generated module supplied ``$dumpvars`` rather than the
    #: testbench. Worth stating: it means the dump has everything, which is
    #: right for a first look and large for a long run.
    injected: bool
    seconds: float = 0.0
    #: What the simulation printed. Carried so `triage` can be run on it
    #: without a second pass over the file.
    output: str = ""
    warnings: list[str] = field(default_factory=list)
    #: The exact commands that produced this waveform, one per line. §12 wants
    #: the simulation command in the bug report's header, and §13.6 wants it in
    #: the regression database — neither could have it while only this function
    #: knew what it ran.
    command: str = ""
    #: Directory both commands ran in.  Re-running the same argv in another
    #: directory is not a reproduction: filelists, `$readmemh`, and relative
    #: dump paths all change meaning.
    run_dir: Path | None = None
    #: Simulator identity is captured at the point of execution.  Asking the
    #: user to type it back into ``record`` later creates provenance that can
    #: disagree with the process that actually produced the waveform.
    simulator: str = "icarus"
    simulator_version: str = ""
    #: `$error`/`$fatal` can coexist with a usable waveform.  The orchestration
    #: command consumes that waveform to print the root cause, then exits
    #: non-zero; lower-level callers reject it immediately by default.
    simulation_failed: bool = False
    failure_output: str = ""


def find_iverilog() -> tuple[str, str] | None:
    """`(iverilog, vvp)`, or `None` when Icarus is not installed.

    Windows installs Icarus outside `PATH` often enough that the usual location
    is worth one `is_file()` before giving up — the alternative is telling
    somebody to install a program they already have.
    """
    iverilog = shutil.which("iverilog")
    vvp = shutil.which("vvp")
    if iverilog and vvp:
        return iverilog, vvp
    for base in (Path("C:/iverilog/bin"), Path("/usr/local/bin"), Path("/opt/iverilog/bin")):
        a, b = base / "iverilog.exe", base / "vvp.exe"
        if a.is_file() and b.is_file():
            return str(a), str(b)
        a, b = base / "iverilog", base / "vvp"
        if a.is_file() and b.is_file():
            return str(a), str(b)
    return None


def write_dumper(
    work: Path,
    top: str,
    dump_name: str = "dump.vcd",
    *,
    set_dumpfile: bool = True,
    memory_elements: list[str] | None = None,
    dump_scope: bool = True,
) -> Path:
    """The module that makes an ordinary testbench produce a waveform.

    `$dumpvars(0, <top>)` from a separate module reaches the whole design
    because `top` is a top-level instance name, so this needs no change to the
    sources and no knowledge of the hierarchy below it.
    """
    path = work / f"{DUMPER_MODULE}.sv"
    body = [
        "// Generated by `veritrace run`. Not part of your design: it exists",
        "// to capture the hierarchy or explicitly selected memory words.",
        f"module {DUMPER_MODULE};",
        "  initial begin",
    ]
    if set_dumpfile:
        body.append(f'    $dumpfile("{dump_name}");')
    else:
        # Let the testbench's time-zero `$dumpfile` run first.  Without the #0,
        # root-module scheduling order decides whether Icarus opens its default
        # `dump.vcd` before it sees the user's filename.
        body.append("    #0;")
    if dump_scope:
        body.append(f"    $dumpvars(0, {top});")
    body += [f"    $dumpvars(1, {element});" for element in memory_elements or []]
    body += ["  end", "endmodule", ""]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        # `tools.run_capture`, not `subprocess.run`: the timeout below has to
        # end the simulation, and stopping only the process Python started
        # leaves `vvp` writing a waveform nobody is waiting for any more.
        return tools.run_capture(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SimulationError(
            f"`{Path(cmd[0]).name}` did not finish within {timeout:g}s. A testbench with no "
            "$finish runs forever; add one, or raise --timeout."
        ) from e
    except OSError as e:
        raise SimulationError(f"could not run `{cmd[0]}`: {e}") from e


def icarus(
    sources: list[Path],
    top: str,
    work: Path,
    defines: list[str] | None = None,
    incdirs: list[str] | None = None,
    timeout: float = 120.0,
    run_dir: Path | None = None,
    allow_testbench_failure: bool = False,
    memory_elements: list[str] | None = None,
) -> Result:
    """Compile and run with Icarus, and return where the waveform landed.

    `run_dir` is where both tools are invoked — the directory the project's own
    flow runs from. It defaults to where the first source lives. Everything a
    testbench reads at run time is relative to it: `$readmemh("program.hex")`,
    a `+incdir+../../hdl` inside a `.f` filelist, an `$fopen` of a log. Running
    somewhere else would break all three, so the *outputs* go to `work` and the
    *run* happens here.
    """
    tools = find_iverilog()
    if tools is None:
        raise SimulationError(
            "Icarus Verilog is not installed, or not on PATH.\n"
            "  Get it from https://bleyer.org/icarus (Windows) or your package "
            "manager (`apt install iverilog`, `brew install icarus-verilog`).\n"
            "  Already have a waveform? `veritrace check dump.vcd --rtl <dir>` "
            "needs no simulator."
        )
    iverilog, vvp = tools
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    sources = [Path(s).resolve() for s in sources]
    here = (run_dir or (sources[0].parent if sources else Path.cwd())).resolve()

    # A `.f` file is a list of arguments, not Verilog: it is handed to the
    # compiler with `-f` and its own relative paths resolve against `run_dir`,
    # which is the whole reason that directory is a parameter. Nothing here
    # parses it — Icarus already does, and so does every other simulator, which
    # is what makes a filelist the portable way to describe a build.
    filelists = [s for s in sources if s.suffix.lower() == ".f"]
    verilog = [s for s in sources if s.suffix.lower() != ".f"]

    # A filelist is compiler input rather than HDL, but the files it names are
    # still where `$dumpfile`/`$dumpvars` normally live.  Inspect them for this
    # one decision so a filelist-based project follows the same runtime path as
    # the equivalent explicit source list.
    inspected = list(verilog)
    for filelist in filelists:
        inspected.extend(read_filelist(filelist)[0])
    inspected = list(dict.fromkeys(p.resolve() for p in inspected))
    bodies = [_without_comments(_read(f)) for f in inspected]
    has_dumpfile = any(_DUMPFILE_CALL.search(text) for text in bodies)
    has_dumpvars = any(_DUMPVARS_CALL.search(text) for text in bodies)
    injected = not has_dumpvars
    dump = work / "dump.vcd"

    started = time.perf_counter()
    vvp_path = work / "sim.vvp"
    # Never allow a compiler wrapper that exits zero without producing output
    # to hand the previous run's executable to vvp.
    try:
        vvp_path.unlink(missing_ok=True)
    except OSError as e:
        raise SimulationError(f"could not replace {vvp_path}: {e}") from e
    build = [iverilog, "-g2012", "-o", str(vvp_path), "-s", top]
    if injected or memory_elements:
        # Both roots have to be elaborated: the design and the dumper. Naming
        # them explicitly also pins the top when the sources contain more than
        # one candidate, which is exactly when a guess would be wrong.
        build += ["-s", DUMPER_MODULE]
    # The run directory and every directory a source came from, as include
    # paths. A `.vh` next to the file that includes it is the overwhelmingly
    # common case, and `+incdir+.` is what every hand-written flow adds anyway.
    for d in include_path(here, verilog, incdirs or []):
        build += ["-I", str(d)]
    for d in defines or []:
        build += [f"-D{d}"]
    # Preserve the caller's order exactly.  Packages, global macros and legacy
    # Verilog compilation units can make source order semantic; collecting all
    # filelists first silently changed a build such as ``pre.sv build.f post.sv``.
    if filelists and verilog:
        # Windows Icarus parses ``-f`` as a filename once a positional source
        # has appeared.  A generated command file is the only way to preserve
        # an interleaved caller order without moving an option across sources.
        ordered = work / "veritrace-inputs.f"
        ordered.write_text(
            "\n".join(
                # Icarus on Windows cannot open a nested absolute ``-f`` from
                # another command file.  Inline its original text instead:
                # the same parser remains authoritative, unknown flags survive,
                # and relative paths still resolve against the simulator cwd.
                _read(source)
                if source.suffix.lower() == ".f"
                else _filelist_quote(source)
                for source in sources
            ) + "\n",
            encoding="utf-8",
        )
        build += ["-f", str(ordered)]
    else:
        for source in sources:
            build += ["-f", str(source)] if source.suffix.lower() == ".f" else [str(source)]
    if injected or memory_elements:
        build.append(
            str(
                write_dumper(
                    work,
                    top,
                    str(dump).replace("\\", "/"),
                    # If the testbench already chooses a file at time zero, a
                    # second `$dumpfile` is unnecessary.  The generated
                    # `$dumpvars` is delayed one delta so the user's call wins.
                    set_dumpfile=not has_dumpfile,
                    memory_elements=memory_elements,
                    dump_scope=injected,
                )
            )
        )

    got = _run(build, here, timeout)
    if got.returncode != 0:
        raise SimulationError(_compile_error((got.stdout or "") + (got.stderr or "")))
    if not vvp_path.is_file():
        raise SimulationError(
            "Icarus reported successful compilation but did not create "
            f"{vvp_path}. Refusing to run a stale executable."
        )
    compile_output = (got.stdout or "") + (got.stderr or "")
    warnings = [ln for ln in compile_output.splitlines() if "warning" in ln.lower()]

    # Snapshot every dump we could consume immediately before simulation.  A
    # conditional `$dumpfile` may not execute; in that case an old dump beside
    # the sources must not turn a no-output run into a fake success.
    literal_dumps = _literal_dumps(bodies, here)
    before = _dump_snapshot(work, here, literal_dumps)

    run_cmd = [vvp, str(vvp_path)]
    ran = _run(run_cmd, here, timeout)
    seconds = time.perf_counter() - started
    output = (ran.stdout or "") + (ran.stderr or "")
    log = work / "sim.log"
    log.write_text(output, encoding="utf-8")
    if ran.returncode != 0:
        raise SimulationError(f"the simulation exited {ran.returncode}:\n{_tail(output)}")
    simulation_failed = _icarus_reported_failure(output)
    if simulation_failed and not allow_testbench_failure:
        # Icarus deliberately leaves the process status at zero for `$error`
        # (and for some `$fatal` forms).  Treating only the status as truth is a
        # particularly convincing fake success: a dump exists and all later
        # analysis runs, even though the testbench explicitly failed.
        raise SimulationError(
            "the simulation reported a testbench failure despite exiting 0:\n"
            + _tail(output)
        )

    fresh = _fresh_dumps(before, work, here, literal_dumps)
    found = dump if dump in fresh else (max(fresh, key=lambda p: p.stat().st_mtime_ns) if fresh else None)
    if found is None:
        raise SimulationError(
            "the simulation ran but wrote no new waveform. A dump left by an "
            "older run was ignored.\n"
            "  Make sure `$dumpvars` executes on this path, or remove the "
            "conditional `$dumpfile` and let VeriTrace inject one."
        )
    if found.stat().st_size == 0:
        raise SimulationError(f"the simulation created {found}, but it is empty")

    # Downstream conversion must consume this run's artifact from the work
    # directory even when a user-authored `$dumpfile("dump.vcd")` wrote beside
    # the sources.  The source is left untouched; copying only the generated
    # output keeps all VeriTrace-owned artifacts together as §13.4b promises.
    if found.parent.resolve() != work:
        landed = work / found.name
        try:
            shutil.copy2(found, landed)
        except OSError as e:
            raise SimulationError(f"could not copy waveform {found} to {landed}: {e}") from e
        found = landed
    return Result(
        dump=found,
        log=log,
        top=top,
        injected=injected,
        seconds=seconds,
        output=output,
        warnings=warnings,
        # This is both displayed and executed by `veritrace reproduce`.
        # Newlines are *not* command separators for `cmd.exe /c` (the second
        # line is silently ignored), while `&&` has the required fail-fast
        # semantics on both cmd.exe and POSIX shells.
        command=_shell(build) + " && " + _shell(run_cmd),
        run_dir=here,
        simulator_version=_tool_version(iverilog),
        simulation_failed=simulation_failed,
        failure_output=_tail(output) if simulation_failed else "",
    )


#: A filelist line that is an option rather than a file.
_INCDIR = re.compile(r"^\+incdir\+(.*)$")
_DEFINE = re.compile(r"^\+define\+(.*)$")


def read_filelist(path: Path, seen: set[Path] | None = None) -> tuple[list[Path], list[Path], list[str]]:
    """The files, include directories and defines a `.f` names.

    The *compiler* is still handed `-f` and remains the authority on the build.
    This reads the same file for a different consumer: pyslang, which builds the
    design graph and has no notion of a command file. Two readers of one file is
    a smell; a second *opinion* would be the bug, and there is none — if this
    disagrees with Icarus about which files exist, the graph is short and the
    correlation rate says so out loud.

    Deliberately small: paths, `+incdir+`, `+define+`, comments, and nested
    `-f`. Anything else is a compiler flag and not this reader's business.
    """
    path = Path(path).resolve()
    seen = seen if seen is not None else set()
    if path in seen or not path.is_file():
        return [], [], []
    seen.add(path)

    root = path.parent
    files: list[Path] = []
    incdirs: list[Path] = []
    defines: list[str] = []
    tokens = _filelist_tokens(_read(path))

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if m := _INCDIR.match(tok):
            incdirs += [(root / d).resolve() for d in m.group(1).split("+") if d]
        elif m := _DEFINE.match(tok):
            defines += [d for d in m.group(1).split("+") if d]
        elif tok == "-I" and i < len(tokens):
            incdirs.append((root / tokens[i]).resolve())
            i += 1
        elif tok.startswith("-I") and len(tok) > 2:
            incdirs.append((root / tok[2:]).resolve())
        elif tok == "-D" and i < len(tokens):
            defines.append(tokens[i])
            i += 1
        elif tok.startswith("-D") and len(tok) > 2:
            defines.append(tok[2:])
        elif tok in ("-f", "-F"):
            if i < len(tokens):
                nested = read_filelist((root / tokens[i]).resolve(), seen)
                i += 1
                files += nested[0]
                incdirs += nested[1]
                defines += nested[2]
        elif tok.startswith(("-", "+")):
            continue  # a compiler flag; Icarus reads the file itself
        else:
            files.append((root / tok).resolve())
    return files, incdirs, defines


def _filelist_tokens(text: str) -> list[str]:
    """Tokenise an Icarus-style command file without losing quoted paths.

    ``str.split`` made ``"rtl with spaces/dut.sv"`` become three imaginary
    sources.  Python's POSIX ``shlex`` fixes that case but interprets every
    backslash as an escape, corrupting native Windows paths.  This deliberately
    small scanner implements the common command-file contract: whitespace,
    single/double quotes, escaped quotes/whitespace, ``#``/``//`` comments and
    a trailing-backslash line continuation.  The compiler still parses the
    original file; this is only the graph-side view of the same inputs.
    """
    out: list[str] = []
    token: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and nxt in (quote, "\\"):
                token.append(nxt)
                i += 1
            else:
                token.append(c)
        elif c in ("'", '"'):
            quote = c
        elif c == "\\" and nxt in ("\r", "\n"):
            if nxt == "\r" and i + 2 < len(text) and text[i + 2] == "\n":
                i += 1
            i += 1
        elif c == "\\" and nxt and (nxt.isspace() or nxt in ("'", '"', "#", "\\")):
            token.append(nxt)
            i += 1
        elif c == "#" or (c == "/" and nxt == "/"):
            if token:
                out.append("".join(token))
                token = []
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        elif c.isspace():
            if token:
                out.append("".join(token))
                token = []
        else:
            token.append(c)
        i += 1
    if token:
        out.append("".join(token))
    return out


def _filelist_quote(path: Path) -> str:
    """One absolute path as an Icarus command-file line.

    Icarus command files are line-oriented: spaces inside a filename are
    accepted as-is, while surrounding quotes are retained as literal filename
    characters by the Windows build. Quoting therefore turns a valid path into
    ``\"C:/...\"`` and compilation fails with ``Invalid argument``. Forward
    slashes keep drive-letter paths portable without involving a shell.
    """
    value = str(Path(path).resolve()).replace("\\", "/")
    if "\n" in value or "\r" in value:
        raise SimulationError(f"a source path cannot contain a newline: {path}")
    return value


def include_path(here: Path, sources: list[Path], extra: list[str]) -> list[Path]:
    """Where to look for ``include`, nearest first and without duplicates.

    Public because §8.3's generated testbench is compiled against the same
    sources by the same rules; two answers to "where are the headers" is how a
    repro fails to build on a design that simulates fine.
    """
    # Icarus resolves a relative ``-I`` against its cwd.  Resolve it by the
    # same rule before turning it into an absolute argv element; ``Path.resolve``
    # alone used VeriTrace's launch directory and changed the compiler's input.
    out: list[Path] = [
        (Path(d) if Path(d).is_absolute() else here / d).resolve() for d in extra
    ]
    for d in [here] + [s.parent for s in sources]:
        if d not in out:
            out.append(d)
    return out


#: Icarus says `Include file foo.vh not found`. Turning that into the flag that
#: fixes it is the difference between a wall of output and one thing to do.
_MISSING_INCLUDE = re.compile(r"Include file (\S+) not found")


def _shell(cmd: list[str]) -> str:
    """A command line someone can paste back into a terminal."""
    # This string is persisted for `veritrace reproduce`, so merely quoting
    # spaces is insufficient (`&`, parentheses, quotes and trailing slashes all
    # have shell meaning).  Use the platform's own argv quoting rules.
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def _compile_error(text: str) -> str:
    missing = sorted(set(_MISSING_INCLUDE.findall(text or "")))
    out = f"compilation failed:\n{_tail(text)}"
    if missing:
        out += (
            f"\n\n  {', '.join(missing)} is included but not on the include path.\n"
            "  Add the directory holding it:  --incdir <dir>\n"
            "  If your project already has a `.f` filelist with `+incdir+` in it, "
            "point at that instead:  veritrace run path/to/rtl.f path/to/tb.f --top <top>"
        )
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _without_comments(text: str) -> str:
    """Remove SV comments while preserving strings and line positions.

    A commented-out `$dumpvars` is not a waveform implementation.  Regex-only
    detection made exactly that ghost disable the generated dumper.
    """
    out: list[str] = []
    i = 0
    state = "code"
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if c == '"':
                state = "string"
                out.append(c)
            elif c == "/" and nxt == "/":
                state = "line"
                out.extend("  ")
                i += 1
            elif c == "/" and nxt == "*":
                state = "block"
                out.extend("  ")
                i += 1
            else:
                out.append(c)
        elif state == "string":
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 1
            elif c == '"':
                state = "code"
        elif state == "line":
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = "code"
        else:
            out.append("\n" if c == "\n" else " ")
            if c == "*" and nxt == "/":
                out.append(" ")
                i += 1
                state = "code"
        i += 1
    return "".join(out)


def _literal_dumps(bodies: list[str], here: Path) -> set[Path]:
    """Literal `$dumpfile` targets, resolved with the simulator's cwd."""
    out: set[Path] = set()
    for body in bodies:
        for match in _LITERAL_DUMPFILE.finditer(body):
            # Verilog string escapes are deliberately not interpreted here.
            # Ordinary slash/backslash paths cover the actionable case; a
            # dynamic/escaped expression is still discovered if it lands
            # directly in `here` or `work`.
            raw = match.group("path").replace("\\\\", "\\")
            path = Path(raw)
            out.add((path if path.is_absolute() else here / path).resolve())
    return out


def _dump_candidates(work: Path, here: Path, literals: set[Path]) -> set[Path]:
    out = set(literals)
    for root in {work.resolve(), here.resolve()}:
        out.update(p.resolve() for p in root.glob("*.vcd") if p.is_file())
        out.update(p.resolve() for p in root.glob("*.fst") if p.is_file())
    return out


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except OSError:
        return None


def _dump_snapshot(work: Path, here: Path, literals: set[Path]) -> dict[Path, tuple[int, int, int] | None]:
    return {path: _signature(path) for path in _dump_candidates(work, here, literals)}


def _fresh_dumps(
    before: dict[Path, tuple[int, int, int] | None],
    work: Path,
    here: Path,
    literals: set[Path],
) -> list[Path]:
    after = _dump_candidates(work, here, literals)
    return [p for p in after if p.is_file() and _signature(p) != before.get(p)]


def _tail(text: str, lines: int = 20) -> str:
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join("    " + ln for ln in kept)


_ICARUS_FAILURE = re.compile(
    r"(?im)^\s*(?:>+\s*)?(?:\[[^\]\r\n]*\]\s*)?"
    r"(?:FATAL|ERROR|ERR|EROARE|FAIL(?:ED)?)(?=[:!\s]|$)"
    r"|^\s*\[(?:FAIL(?:ED)?|EROARE)\]"
    r"|\bTEST(?:\s+\d+)?\s+FAILED\b"
)


def _icarus_reported_failure(output: str) -> bool:
    """Explicit failure markers from vvp or a self-checking testbench.

    `$display` + `$finish` also exit zero. Match error labels, not any mention
    of errors: a summary such as '38 PASS, 0 FAIL' is not a failing test.
    """
    return bool(_ICARUS_FAILURE.search(output or ""))


def _tool_version(executable: str) -> str:
    """The first useful version line from the executable that actually ran."""
    try:
        got = tools.run_capture([executable, "-V"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = (got.stdout or "") + "\n" + (got.stderr or "")
    for line in text.splitlines():
        line = line.strip()
        if line and "version" in line.lower():
            return line
    return "unknown"
