"""The §8.14 extraction engine, all six steps in order.

    1. detect interfaces in scope        -> detect.py
    2. channel events, one scan each     -> channels.py   (parallel, in Rust)
    3. assemble with a state machine     -> assemble.py
    4. metrics per transaction           -> assemble.py
    5. rules, incrementally              -> assemble.py
    6. persist to txn/<iface>.parquet    -> persist.py

*Written once, this engine gives you every protocol.* Nothing below mentions a
protocol by name; everything protocol-specific arrives as TOML.

Two things this layer owns that no single step could:

* **Each interface is numbered against its own clock.** A design with an AXI
  master on `aclk` and a memory controller on `mem_clk` has two cycle counts,
  and reporting a transaction on one against the edges of the other is a silent
  off-by-a-lot. `clocks.clock_at` resolves per interface and falls back to the
  session clock only when the pack found nothing.
* **One broken interface does not lose the others.** Extraction runs per
  interface behind a guard, and a failure is recorded on that interface rather
  than raised — the same rule the checks of §8 already follow (P7).
"""

from __future__ import annotations

import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from veritrace import clocks
from veritrace.clocks import Clock
from veritrace.perf import stalls
from veritrace.protocol import assemble, channels, detect, pack as packs_mod, persist
from veritrace.protocol.model import Extraction, Interface, Transaction
from veritrace.protocol.pack import Pack, PackError


@dataclass(slots=True)
class ProtocolAnalysis:
    """Every interface in a session, with what was extracted from it."""

    extractions: list[Extraction] = field(default_factory=list)
    packs: list[Pack] = field(default_factory=list)
    #: Pack files that could not be loaded, and interfaces that failed. Shown,
    #: never swallowed: an empty Transactions tab has to say why it is empty.
    errors: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def __iter__(self):
        return iter(self.extractions)

    def __len__(self) -> int:
        return len(self.extractions)

    @property
    def interfaces(self) -> list[Interface]:
        return [e.interface for e in self.extractions]

    @property
    def transactions(self) -> list[Transaction]:
        return [t for e in self.extractions for t in e.transactions]

    def get(self, name: str) -> Extraction | None:
        """An interface by name, by alias, or by unambiguous suffix.

        Typing `txn(dma0)` when the interface is called `dut.dma0` should work:
        the long form exists to stay unique, not to be typed.
        """
        for e in self.extractions:
            if e.interface.name == name or name in e.interface.aliases:
                return e
        hits = [e for e in self.extractions if e.interface.name.endswith("." + name)]
        return hits[0] if len(hits) == 1 else None

    def transaction(self, ref: str) -> Transaction | None:
        """`dma0.WRITE[128]` -> the transaction, or `None`."""
        iface, _, rest = ref.rpartition(".")
        kind, _, idx = rest.partition("[")
        ex = self.get(iface)
        if ex is None or not idx.rstrip("]").isdigit():
            return None
        n = int(idx.rstrip("]"))
        return next(
            (t for t in ex.transactions if t.kind == kind and t.index == n), None
        )

    def summary(self) -> str:
        """The line printed after `init` and `serve` when interfaces were found."""
        if not self.extractions:
            return "no protocol interfaces detected"
        n_txn = len(self.transactions)
        n_open = sum(len(e.open_transactions) for e in self.extractions)
        parts = [f"{len(self.extractions)} interface(s)", f"{n_txn} transactions"]
        if n_open:
            parts.append(f"{n_open} never completed")
        n_viol = sum(len(e.violations) for e in self.extractions)
        if n_viol:
            parts.append(f"{n_viol} protocol violations")
        return " - ".join(parts)

    def to_dict(self, with_transactions: bool = False) -> dict[str, Any]:
        return {
            "interfaces": [e.to_dict(with_transactions) for e in self.extractions],
            "packs": [p.to_dict() for p in self.packs],
            "errors": list(self.errors),
            "ms": round(self.elapsed_ms, 2),
        }


def extract(
    store: Any,
    trace_path: Path | str | None = None,
    clock: Clock | None = None,
    config: Any = None,
    project_root: Path | str | None = None,
    pack_names: Iterable[str] | None = None,
    use_cache: bool = True,
) -> ProtocolAnalysis:
    """Run the whole pipeline over one trace."""
    started = _time.perf_counter()
    out = ProtocolAnalysis()

    names = list(pack_names) if pack_names is not None else list(
        getattr(config, "protocol_packs", ()) or ()
    )
    try:
        out.packs = packs_mod.resolve(names, project_root, out.errors)
    except PackError as e:
        out.errors.append(str(e))
        out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
        return out
    for p in out.packs:
        out.errors += [f"{p.slug}: {w}" for w in p.warnings]

    # Step 1. Memory packs (§8.20) decode a command bus, not valid/ready
    # channels — `veritrace.memory` extracts those separately, on the same
    # detected-interfaces list, so this pipeline is not asked to assemble
    # transactions out of a pack that declares none.
    interfaces = [i for i in detect.detect(store, out.packs, config) if not i.pack.is_memory]
    if not interfaces:
        out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
        return out

    path = Path(trace_path) if trace_path is not None else None

    def one(iface: Interface) -> Extraction:
        try:
            return _extract_one(store, path, iface, clock, config, use_cache)
        except Exception as e:  # noqa: BLE001 - one interface must not lose the rest
            out.errors.append(f"{iface.name}: {e}")
            return Extraction(interface=iface, skipped={"extraction": str(e)})

    # The expensive part is `sample_before`, which runs in Rust with the
    # interpreter detached, so interfaces genuinely overlap rather than taking
    # turns.
    if len(interfaces) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(interfaces))) as pool:
            out.extractions = list(pool.map(one, interfaces))
    else:
        out.extractions = [one(interfaces[0])]

    out.extractions.sort(key=lambda e: e.interface.name)
    out.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return out


def _extract_one(
    store: Any,
    trace_path: Path | None,
    iface: Interface,
    session_clock: Clock | None,
    config: Any,
    use_cache: bool,
) -> Extraction:
    started = _time.perf_counter()

    if use_cache and trace_path is not None:
        cached = persist.restore(trace_path, store, iface)
        if cached is not None:
            cached.elapsed_ms = (_time.perf_counter() - started) * 1000.0
            return cached

    clock = clocks.clock_at(store, iface.clock) if iface.clock else None
    clock = clock or session_clock
    if clock is None or not clock.edges:
        return Extraction(
            interface=iface,
            skipped={"clock": "no usable clock for this interface"},
            elapsed_ms=(_time.perf_counter() - started) * 1000.0,
        )

    sampler = channels.Sampler(store, clock.edges)

    # Step 2.
    in_reset = channels.reset_mask(iface, sampler, config)
    ex = Extraction(interface=iface, sampled_cycles=len(clock.edges))
    if all(in_reset):
        # Either the design is held in reset for the whole run, or the polarity
        # was read backwards. Both would silently extract nothing, so say what
        # happened and scan anyway rather than report an idle bus.
        ex.skipped["reset"] = (
            f"`{iface.reset}` looks asserted for the whole run; ignoring it"
        )
        in_reset = [False] * len(clock.edges)

    scans = channels.scan(iface, sampler, in_reset)
    for name, scan in scans.items():
        if scan.skipped:
            ex.skipped[f"channel {name}"] = scan.skipped

    # Steps 3-5.
    asm = assemble.assemble(iface, scans, clock)
    ex.transactions = asm.transactions
    ex.violations = list(asm.violations)
    ex.n_events = asm.n_events
    ex.n_matched = asm.n_matched

    # §8.17, while the sampled matrix is still in hand.
    try:
        ex.perf = stalls.attribute(iface, sampler, in_reset, scans, ex.transactions)
    except Exception as e:  # noqa: BLE001 - performance is an extra, not a gate
        ex.skipped["stalls"] = str(e)

    temporal, notes = assemble.check_temporal_rules(iface, sampler, in_reset, clock)
    ex.violations.extend(temporal)
    ex.skipped.update({f"rule {k}": v for k, v in notes.items()})
    ex.violations.sort(key=lambda v: (v.time, v.rule))

    # Step 6.
    if trace_path is not None:
        try:
            ex.parquet = str(persist.write(Path(trace_path), store, ex, clock))
        except Exception as e:  # noqa: BLE001 - a read-only tree must not fail analysis
            ex.skipped["persist"] = str(e)

    ex.elapsed_ms = (_time.perf_counter() - started) * 1000.0
    return ex
