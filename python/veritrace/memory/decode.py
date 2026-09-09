"""Step 1 of §8.20: raw signals to a decoded command stream.

*Extract commands from the command bus.* One scan over the clock edges,
evaluating each `[[command]]` row's `encode` in priority order at every
cycle — the identical cascade shape §8.17's stall attribution already uses
(`expr` is "one engine, every call site", stated as much in its own
docstring). The first row that matches wins; a cycle that matches none is not
a command at all, because a real command bus has no NOP encoding of its own —
deselected (`cs_n` high, in the usual polarity) is silence, not a value.

No transaction assembly here, unlike §8.14: a command bus has no
valid/ready and no multi-beat body, so there is nothing to assemble — the
command stream *is* the extracted data, and the bank tracker (`banks.py`)
reads it directly.
"""

from __future__ import annotations

from typing import Any

from veritrace.memory.model import CmdEvent
from veritrace.protocol import expr
from veritrace.protocol.assemble import CycleEnv
from veritrace.protocol.channels import Sampler, reset_mask
from veritrace.protocol.model import Interface


def decode(
    iface: Interface, sampler: Sampler, config: Any = None
) -> tuple[list[CmdEvent], dict[str, str]]:
    """Every command issued, at every clock edge that matched one row.

    Returns the stream and a `{name: reason}` map for any rung (an `encode`
    or an `args` expression) that could not be evaluated — dropped rather than
    raised, the same rule §8.17's cascade follows, so one bad row does not
    lose every other command in the run.

    Cycles where the controller is in reset are skipped, through the same
    `reset_mask` the channel scan uses: an SDRAM has no reset pin of its own,
    but a controller held in reset drives whatever its flops power up to, and
    decoding that as a burst of ACTIVATEs at time zero would put phantom
    commands ahead of every real one. `reset_mask` reads the asserted level
    out of the trace rather than trusting the name, so a mis-polarised guess
    cannot blank the whole run either.
    """
    pack = iface.pack
    edges = sampler.edges
    sampler.prefetch(list(iface.signals.values()))
    columns = {p: c for p in set(iface.signals.values()) if (c := sampler.column(p)) is not None}

    in_reset = reset_mask(iface, sampler, config)
    if all(in_reset):
        return [], {"reset": f"`{iface.reset}` is asserted or unknown for the whole run; no commands sampled"}

    rungs = list(pack.commands)
    notes: dict[str, str] = {}
    out: list[CmdEvent] = []

    i = 0
    while i < len(edges):
        if in_reset[i]:
            i += 1
            continue
        env = CycleEnv(iface, columns, i, {})
        matched: Any = None
        failed: tuple[Any, str] | None = None
        for cmd in rungs:
            try:
                if expr.evaluate(cmd.node, env):
                    matched = cmd
                    break
            except expr.ExprError as e:
                failed = (cmd, str(e))
                break
        if failed is not None:
            cmd, why = failed
            notes[cmd.name] = why
            rungs = [c for c in rungs if c is not cmd]
            continue  # redo this cycle against the smaller cascade

        if matched is not None:
            fields: dict[str, Any] = {}
            for arg_name, src in matched.args.items():
                try:
                    fields[arg_name] = expr.evaluate(expr.parse(src), env)
                except expr.ExprError as e:
                    notes[f"{matched.name}.{arg_name}"] = str(e)
                    fields[arg_name] = None
            out.append(CmdEvent(time=edges[i], name=matched.name, fields=fields, cycle=i))
        i += 1

    return out, notes
