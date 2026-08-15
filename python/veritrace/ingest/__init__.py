"""Transactions that already exist — §8.34.

A team with a UVM or cocotb monitor has already written down what a transaction
is. Re-deriving it from wires is twice the work and can disagree with the
verification intent of whoever wrote the monitor, so it is read instead.

Ingested and derived transactions coexist, and the spec says which wins: *"daca
exista transaction DB, il preferi; daca nu, cazi pe extractia din semnale"* —
implemented by `merge`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veritrace.ingest import cocotb, model, uvm
from veritrace.protocol.model import Extraction

__all__ = ["cocotb", "uvm", "model", "read", "merge"]


def read(
    uvm_db: Path | str | None = None,
    cocotb_log: Path | str | None = None,
    patterns: dict[str, str] | None = None,
    timescale: str = "1ns",
) -> list[Extraction]:
    """Whatever was given, as extractions."""
    out: list[Extraction] = []
    if uvm_db:
        out += uvm.parse(Path(uvm_db).read_text(encoding="utf-8", errors="replace"))
    if cocotb_log:
        out += cocotb.parse(
            Path(cocotb_log).read_text(encoding="utf-8", errors="replace"), patterns, timescale
        )
    return out


def merge(derived: Any, ingested: list[Extraction]) -> Any:
    """Ingested streams into a `ProtocolAnalysis`, taking precedence by name.

    §8.34's rule, and it is the right way round: a monitor is a statement of
    what the team *means* by a transaction, and a pack is VeriTrace's guess at
    it. Where both describe the same interface, the statement wins — and the
    guess is not silently dropped, it is recorded as skipped so the reason is
    visible rather than mysterious.
    """
    if derived is None:
        from veritrace.protocol.engine import ProtocolAnalysis

        derived = ProtocolAnalysis()
    replaced = {e.interface.name for e in ingested}
    kept = []
    for e in derived.extractions:
        if e.interface.name in replaced:
            # Recorded, not silently dropped: an interface that disappears from
            # the tab with no explanation reads as a detection failure.
            derived.errors.append(
                f"{e.interface.name}: replaced by an ingested transaction stream "
                "(§8.34) — a monitor is what the team means by a transaction, "
                "a pack is a guess at it"
            )
            continue
        kept.append(e)
    derived.extractions = kept + ingested
    return derived
