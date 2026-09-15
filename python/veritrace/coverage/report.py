"""One call that produces the whole of TAB 7 — §8.21, §8.12.

The same seam as `perf.report.build` and `memory.report.build`: the CLI, the
API and the tests go through it, so what the tab shows and what CI checks are
the same computation.
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

from veritrace.coverage import code as code_mod, functional, holes
from veritrace.coverage.code import CoverageError
from veritrace.coverage.model import CoverageReport


def build(
    analysis: Any = None,
    store: Any = None,
    clock: Any = None,
    graph: Any = None,
    coverage_path: Any = None,
    project_root: Any = None,
    source: str | None = None,
    elaboration: Any = None,
) -> CoverageReport:
    """§8.21 from the transactions, §8.12 from whatever coverage database exists.

    Neither half is required. A design with no bus still gets code coverage; a
    project that never ran `--coverage` still gets the functional matrix. Which
    of the two ran is on the report, so an empty section is never mistaken for a
    full one (P1).
    """
    started = _time.perf_counter()
    out = CoverageReport()

    extractions = list(getattr(analysis, "extractions", []) or [])
    if extractions:
        for ex in extractions:
            if ex.interface.pack.is_memory:
                continue
            out.functional.append(functional.measure(store, ex, clock))
    else:
        out.skipped["functional"] = "no protocol interface was extracted"

    # Taken as given. A path typed on the command line is relative to where it
    # was typed, and one read from `.veritrace.toml` is resolved against the
    # configuration by `Config.coverage_file()` — the same rule `design.rtl`
    # follows. Re-rooting here applied the config rule to both, so
    # `--coverage cov.csv` was validated against the process and then read from
    # the project directory.
    path = Path(coverage_path) if coverage_path else (
        code_mod.discover(project_root) if project_root else None
    )
    if path is None:
        out.skipped["code"] = (
            "no coverage database found; run verilator --coverage or xcrg, "
            "or pass --coverage <file>"
        )
    else:
        try:
            out.code = code_mod.read(path, source)
        except CoverageError as e:
            out.skipped["code"] = str(e)
            # A database that exists and will not parse is a different situation
            # from having none, and only the reader can be told so — the CLI
            # prints `skipped` either way.
            out.code_error = str(e)

    if out.code is not None and not out.code.error:
        out.holes, note = holes.derive(out.code, graph, store, clock, project_root)
        if note:
            out.skipped["holes"] = note

    out.holes.extend(holes.fsm_holes(graph, store, clock, elaboration))

    out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return out
