"""The §4.2 budgets that live above the Rust core.

`crates/vt-trace/examples/bench.rs` measures conversion, reopen, `value_at` and
the whole-trace constant scan — everything that is pure Rust. Three tier-A rows
of §4.2 are not Rust and were measured by nothing:

| §4.2 row                          | tier A | tier B |
|---|---|---|
| stuck detector, every signal      | 400 ms | 3 s    |
| transaction extraction, 1 iface   | 300 ms | —      |
| `why()` with 200 nodes            | 150 ms | 300 ms |

A budget nothing measures is a budget nothing keeps, which is how the stuck
detector came to sit three times over its number without anyone noticing. It
exits non-zero when a row is over, so the build fails the way §4.2 says it
should — and so a budget cannot quietly drift again.

    python bench/pybench.py                 # tier A: 5000 signals
    python bench/pybench.py --tier b        # tier B: 50000 signals
    python bench/pybench.py --json          # for CI

**Cold is what is reported.** Every one of these runs when a session opens, so
the warm number — which is twenty times better once the stream cache is full —
describes a state the budget is not about.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / "designs"

#: §4.2, by tier. `None` where the spec gives no number for that tier.
BUDGETS_MS = {
    "a": {"stuck": 400.0, "extract": 300.0, "why": 150.0},
    "b": {"stuck": 3000.0, "extract": None, "why": 300.0},
}

#: §4.2's own shapes: 5k/50k signals and 10^7/10^8 transitions.
#: `_generate` changes one tenth of the signals every other step, so 40k
#: steps reaches the named event count in both tiers.  A smaller default made
#: the CI job green on roughly four percent of the workload it claimed to gate.
SHAPE = {"a": (5_000, 40_000), "b": (50_000, 40_000)}

#: What §4.2 names for each tier: 10^7 transitions at A, 10^8 at B. The default
#: shape reaches these values; `--steps` may deliberately make a quicker local
#: smoke run, but that run is non-gating unless `--allow-smaller` is explicit.
NOMINAL_EVENTS = {"a": 10_000_000, "b": 100_000_000}

#: §4.2 asks about `why()` "cu 200 de noduri". The reference designs top out at
#: about 40, so the budget has never been measured at the size it names; the
#: generated design below is sized to cross it.
WHY_NODES = 200


@dataclass
class Row:
    name: str
    ms: float
    budget: float | None
    note: str = ""
    #: Almost everything here is a duration; the RAM row is not, and printing
    #: megabytes as milliseconds is the kind of mislabelling this file exists
    #: to stop doing.
    unit: str = "ms"

    @property
    def over(self) -> bool:
        return self.budget is not None and self.ms > self.budget


@dataclass
class Report:
    tier: str
    rows: list[Row] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            **self.context,
            "rows": [
                {"name": r.name, "value": round(r.ms, 1), "unit": r.unit,
                 "budget": r.budget, "over": r.over, "note": r.note}
                for r in self.rows
            ],
            "over_budget": [r.name for r in self.rows if r.over],
        }


def _ident(i: int) -> str:
    """VCD identifier codes, printable ASCII 33..126."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 94)
        out = chr(33 + r) + out
    return out


def _generate(path: Path, n_signals: int, n_steps: int) -> None:
    """A trace of `n_signals` in a four-deep hierarchy, a tenth moving per cycle.

    Mostly-frozen on purpose: that is the shape §8.4 exists for, and a design
    where everything moves would measure the branch the detector never takes.
    """
    rng = random.Random(7)
    with path.open("w", newline="\n") as f:
        f.write(
            "$timescale 1ns $end\n"
            "$scope module tb $end\n"
            "$scope module dut $end\n"
            "$scope module fabric $end\n"
            "$scope module bank $end\n"
        )
        f.write(f"$var reg 1 {_ident(0)} clk $end\n")
        for i in range(1, n_signals):
            f.write(f"$var reg 8 {_ident(i)} sig{i} [7:0] $end\n")
        f.write("$upscope $end\n" * 4)
        f.write("$enddefinitions $end\n")
        f.write(f"#0\n0{_ident(0)}\n")
        for i in range(1, n_signals):
            f.write(f"b0 {_ident(i)}\n")
        t = 0
        for step in range(n_steps):
            t += 5
            f.write(f"#{t}\n{step % 2}{_ident(0)}\n")
            if step % 2:
                continue
            for i in rng.sample(range(1, n_signals), max(1, n_signals // 10)):
                f.write(f"b{rng.randrange(256):b} {_ident(i)}\n")


def _timed(fn) -> tuple[float, object]:
    t = time.perf_counter()
    got = fn()
    return (time.perf_counter() - t) * 1e3, got


def _rss_mb() -> float | None:
    """Current resident set size without making the benchmark optional in CI."""
    try:
        import psutil
    except ImportError:
        # The benchmark jobs run on Linux. `/proc` reports current RSS (unlike
        # `resource.ru_maxrss`, which is a peak and makes before/after retention
        # meaningless), so Tier B's 1.5 GB gate remains real without adding a
        # runtime dependency to VeriTrace itself.
        try:
            fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
            return int(fields[1]) * int(__import__("os").sysconf("SC_PAGE_SIZE")) / 1e6
        except (OSError, ValueError, IndexError, AttributeError):
            return None
    return psutil.Process().memory_info().rss / 1e6


def measure_stuck(vtx: Path, budget: float | None) -> tuple[Row, Row | None]:
    """§8.4 over every signal, from a store nothing has touched yet.

    Reports RAM alongside, because the same pass answers §4.2's other unmeasured
    row: a whole-trace scan touches every signal once, so if anything retains
    what it decoded, this is where 1.5 GB would go.
    """
    from veritrace import TraceStore, clocks
    from veritrace.analysis import stuck
    from veritrace.config import Config

    before = _rss_mb()
    store = TraceStore(str(vtx))
    clock = clocks.resolve(store, None, Config.empty())
    # A threshold the run can actually reach, or the scan measures its early
    # return rather than its work.
    ms, found = _timed(
        lambda: list(stuck.scan(store, clock, None, Config.empty(), threshold_cycles=5))
    )
    after = _rss_mb()

    row = Row("stuck detector, every signal", ms, budget, f"{len(found)} findings")
    if before is None or after is None:
        return row, None
    disk = sum(f.stat().st_size for f in vtx.rglob("*") if f.is_file()) / 1e6
    return row, Row(
        "RAM after one whole-trace scan",
        after,
        1500.0,
        f"{after - before:.0f} MB retained over a {disk:.0f} MB store "
        f"({(after - before) / max(disk, 1):.1f}x)",
        unit="MB",
    )


def measure_extraction(budget: float | None) -> Row:
    """§8.14 step 1-4 on one interface, against the reference AXI design.

    Not the generated trace: extraction is about a protocol, and a synthetic
    dump has none. The budget is per interface, which is what this measures.
    """
    from veritrace import TraceStore, clocks
    from veritrace import store as store_mod
    from veritrace.config import Config
    from veritrace.protocol import engine

    path = store_mod.ensure(DESIGNS / "axi_lite" / "dump.vcd")
    store = TraceStore(str(path))
    clock = clocks.resolve(store, None, Config.empty())
    ms, analysis = _timed(
        lambda: engine.extract(store, path, clock, Config.empty(), use_cache=False)
    )
    n = len(analysis.extractions)
    per = ms / max(n, 1)
    return Row(
        "transaction extraction, 1 interface", per, budget,
        f"{n} interface(s), {len(analysis.transactions)} transactions, {ms:.0f} ms total",
    )


def _why_fixture(root: Path) -> tuple[Path, Path, str]:
    """A real elaborated 200-leaf causal question for §4.2's named shape."""
    count = WHY_NODES
    rtl = root / "why_bench.sv"
    vcd = root / "why_bench.vcd"
    names = [f"s{i}" for i in range(count)]
    rtl.write_text(
        "module why_bench(\n  input logic "
        + ", ".join(names)
        + ",\n  output logic out\n);\n"
        + "  assign out = "
        + " | ".join(names)
        + ";\nendmodule\n",
        encoding="utf-8",
    )
    lines = [
        "$timescale 1ns $end",
        "$scope module why_bench $end",
    ]
    for i, name in enumerate([*names, "out"]):
        lines.append(f"$var wire 1 {_ident(i)} {name} $end")
    lines += ["$upscope $end", "$enddefinitions $end", "#0"]
    lines += [f"0{_ident(i)}" for i in range(count + 1)]
    vcd.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rtl, vcd, "why_bench.out"


def measure_why(root: Path, budget: float | None) -> Row:
    """§8.1 over at least 200 nodes through parser, Slang and correlation."""
    from veritrace import TraceStore, clocks
    from veritrace import store as store_mod
    from veritrace.analysis.whytrace import WhyTracer
    from veritrace.config import Config
    from veritrace.correlate.resolver import correlate
    from veritrace.graph.elaborate import elaborate

    rtl, vcd, target = _why_fixture(root)
    path = store_mod.ensure(vcd)
    store = TraceStore(str(path))
    el = elaborate([rtl], top="why_bench", analyse=False)
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clocks.resolve(store, el.graph, Config.empty())

    ms, result = _timed(lambda: WhyTracer(el.graph, store).why(target, 0))
    n = result.nodes
    if n < WHY_NODES:
        raise RuntimeError(
            f"the why benchmark built {n} nodes, below §4.2's {WHY_NODES}; "
            "the timing would not prove the named workload"
        )
    return Row(f"why() ({n} nodes)", ms, budget, "real VCD + Slang + correlated graph")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=["a", "b"], default="a")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    ap.add_argument("--keep", type=Path, default=None,
                    help="Where to build the synthetic trace (default: a temp dir).")
    ap.add_argument("--steps", type=int, default=None,
                    help="Clock steps to generate. Raise it to reach §4.2's event count.")
    ap.add_argument(
        "--allow-smaller",
        action="store_true",
        help="Allow --steps below the specified workload for a local smoke run.",
    )
    args = ap.parse_args()

    budgets = BUDGETS_MS[args.tier]
    n_signals, n_steps = SHAPE[args.tier]
    n_steps = args.steps or n_steps
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="vt-pybench-"))
    work.mkdir(parents=True, exist_ok=True)
    src, vtx = work / "bench.vcd", work / "bench.vtx"

    try:
        if not vtx.is_dir():
            if not src.exists():
                print(f"generating {n_signals} signals x {n_steps} steps...", file=sys.stderr)
                _generate(src, n_signals, n_steps)
            from veritrace import convert

            convert(str(src), str(vtx))

        from veritrace import TraceStore

        s = TraceStore(str(vtx))
        report = Report(
            tier=args.tier,
            context={
                "n_signals": s.n_signals,
                "n_events": s.n_events,
                "source_mb": round(src.stat().st_size / 1e6, 1) if src.exists() else None,
                "nominal_events": NOMINAL_EVENTS[args.tier],
                "fraction_of_spec": s.n_events / NOMINAL_EVENTS[args.tier],
                "short_of_spec": s.n_events < NOMINAL_EVENTS[args.tier],
            },
        )
        del s

        stuck_row, ram_row = measure_stuck(vtx, budgets["stuck"])
        report.rows.append(stuck_row)
        if ram_row is not None:
            # §4.2 budgets RAM at tier B only, but the number is worth seeing at
            # both: a ratio measured on a small store is what predicts the big
            # one, and `psutil` is optional so this line may simply be absent.
            report.rows.append(
                ram_row
                if args.tier == "b"
                else Row(ram_row.name, ram_row.ms, None, ram_row.note, ram_row.unit)
            )
        if budgets["extract"] is not None:
            report.rows.append(measure_extraction(budgets["extract"]))
        report.rows.append(measure_why(work, budgets["why"]))
    finally:
        if args.keep is None:
            shutil.rmtree(work, ignore_errors=True)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        c = report.context
        print(f"\n  tier {args.tier.upper()}: {c['n_signals']} signals, {c['n_events']} events")
        if c.get("short_of_spec"):
            print(
                f"  NOTE: §4.2 tier {args.tier.upper()} describes "
                f"{NOMINAL_EVENTS[args.tier]:,} transitions and this trace has "
                f"{c['fraction_of_spec']:.0%} of them — the numbers below are a "
                "non-gating smoke shape, not the budget. Remove the smaller "
                "--steps override for the real gate."
            )
        print(f"  {'operation':<38} {'measured':>10} {'budget':>9}")
        print("  " + "-" * 60)
        for r in report.rows:
            budget = f"{r.budget:.0f} {r.unit}" if r.budget is not None else "-"
            mark = "  OVER" if r.over else ""
            print(f"  {r.name:<38} {r.ms:>7.0f} {r.unit:<3}{budget:>9}{mark}")
            if r.note:
                print(f"    {r.note}")
        print()

    over = [r.name for r in report.rows if r.over]
    if report.context.get("short_of_spec") and not args.allow_smaller:
        over.append("generated workload is smaller than §4.2")
    if over:
        # Fatal, which is what §4.2 asks for: *"praguri verificate in CI la
        # fiecare commit, cu build care pica daca sunt depasite"*. This returned
        # 0 while printing the warning, on the reasoning that a red CI nobody
        # can fix teaches people to ignore CI — but the thing nobody could fix
        # has been fixed, and a gate that never closes is not a gate. `bench.rs`
        # has always exited 1 here; now both halves of §4.2 agree.
        print(f"OVER BUDGET: {', '.join(over)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
