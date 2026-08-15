"""Time-to-root-cause, the VeriTrace half — §15.

§15 compares two runs per bug:

* **A (baseline)** — GTKWave, an editor and grep. A person, with a stopwatch.
  Nothing here can measure that, and a script pretending to would be the exact
  kind of invented number §15's anti-bias rules exist to prevent.
* **B (VeriTrace)** — one command from the symptom to the sentence about the
  root cause. That *is* measurable, deterministically, and this is it.

So this script produces the B column and the evidence behind it: for every
reference design that declares an expectation, it asks the question a user would
ask and records how long the answer took and whether it landed on the root cause
in `expected.toml`. The A column stays a human column, and the README says so.

    python bench/ttrc.py            # table
    python bench/ttrc.py --json     # for a spreadsheet
"""

from __future__ import annotations

import argparse
import json
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / "designs"


def cases() -> list[tuple[str, dict]]:
    out = []
    for spec in sorted(DESIGNS.glob("*/expected.toml")):
        data = tomllib.loads(spec.read_text(encoding="utf-8"))
        if data.get("why"):
            out.append((spec.parent.name, data))
    return out


def measure(name: str, spec: dict) -> list[dict]:
    from veritrace import TraceStore, convert
    from veritrace.analysis import vtq
    from veritrace.analysis.whytrace import WhyTracer, root_cause
    from veritrace.correlate.resolver import correlate
    from veritrace.graph.elaborate import discover, elaborate

    design = DESIGNS / name
    vtx = design / "dump.vcd.vtx"
    if not vtx.is_dir():
        convert(str(design / "dump.vcd"), str(vtx))

    # The clock starts where a user's does: the dump is on disk and the RTL has
    # not been read yet. Elaboration is part of the cost of the first answer.
    t0 = time.perf_counter()
    el = elaborate(discover(design))
    store = TraceStore(str(vtx))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    setup_ms = (time.perf_counter() - t0) * 1000

    rows = []
    for case in spec["why"]:
        q = vtq.parse(case["question"])
        at = q.time if q.time is not None else store.time_range[1]
        t1 = time.perf_counter()
        result = WhyTracer(el.graph, store).why(q.signal, at)
        why_ms = (time.perf_counter() - t1) * 1000

        cause = root_cause(result.root)
        want = case.get("root_cause") or case.get("reaches") or ""
        reached = bool(want) and (
            (cause is not None and cause.signal.path() == want)
            or any(n.signal.path() == want for n in result.root.walk())
        )
        rows.append(
            {
                "design": name,
                "question": case["question"],
                "expected": want,
                "found": cause.signal.path() if cause else "",
                "reached_expected": reached,
                "nodes": result.nodes,
                "setup_ms": round(setup_ms, 1),
                "why_ms": round(why_ms, 1),
                "total_ms": round(setup_ms + why_ms, 1),
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    rows = [r for name, spec in cases() for r in measure(name, spec)]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"{'design':<12} {'nodes':>6} {'setup':>9} {'why':>9} {'total':>9}  root cause")
    print("-" * 78)
    for r in rows:
        mark = "ok " if r["reached_expected"] else "MISS"
        print(
            f"{r['design']:<12} {r['nodes']:>6} {r['setup_ms']:>8.1f}ms "
            f"{r['why_ms']:>8.1f}ms {r['total_ms']:>8.1f}ms  {mark} {r['expected']}"
        )
    missed = [r for r in rows if not r["reached_expected"]]
    print()
    print(
        f"{len(rows) - len(missed)}/{len(rows)} questions reached the root cause "
        "written in the design's own expected.toml."
    )
    print(
        "This is column B of §15 only. Column A is a person with GTKWave and a "
        "stopwatch, and no script can stand in for it."
    )
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
