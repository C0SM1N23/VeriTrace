"""The two flows of §8.27 and §8.35, assembled from the pieces around them.

`prove` turns a pack's rules into properties and asks whether any run breaks
them. `reach` turns coverage holes into `cover` statements and asks whether any
run reaches them — the same machinery, the opposite question, which is why they
share everything but a mode.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from veritrace import tools
from veritrace.export import sva
from veritrace.formal import harness, sby
from veritrace.formal.model import FormalReport, Reachability
from veritrace.protocol.model import Interface


def prove(
    elaboration: Any,
    iface: Interface,
    sources: list[Path],
    work: Path,
    mode: str = "bmc",
    depth: int = 20,
    engine: str = "smtbmc z3",
    timeout: float = 900.0,
    reset_active_low: bool = True,
) -> FormalReport:
    """§8.27 for one interface."""
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)

    dut = harness.instance_of(elaboration, iface.scope)
    if not dut.module:
        raise tools.ToolError(f"{iface.scope} is not an instance in the elaborated design")

    checker = sva.render(
        iface.pack,
        iface.name,
        target="formal",
        widths=harness.widths(elaboration, iface),
        clock=(iface.clock or "clk").rsplit(".", 1)[-1],
        reset=(iface.reset or "rst_n").rsplit(".", 1)[-1],
        reset_active_low=reset_active_low,
    )
    if not checker.emitted:
        raise tools.ToolError(
            f"{iface.pack.name} has no rule that can be checked formally.\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in checker.skipped.items())
        )

    chk = work / "checker.sv"
    chk.write_text(checker.code, encoding="utf-8")
    top = work / "harness.sv"
    top.write_text(
        harness.build(dut, iface, checker.module, _ports(checker.code), reset_active_low),
        encoding="utf-8",
    )

    # The design goes in as-is. Copying rather than referencing keeps the `.sby`
    # self-contained, which is the difference between "here is how to reproduce
    # it" and "it worked on my machine".
    staged = [_stage(s, work) for s in sources] + [chk, top]
    report = sby.run(
        staged, harness.TOP, work,
        {rid: _rule_text(iface, rid) for rid in checker.emitted},
        mode, depth, engine, timeout,
    )
    report.iface, report.pack = iface.name, iface.pack.name
    for p in report.properties:
        p.subject = _subject(iface, p.id)
    for rid, why in checker.skipped.items():
        report.properties.append(
            sby.Property(id=rid, text=_rule_text(iface, rid), depth=depth, reason=why)
        )
    return report


def _subject(iface: Interface, rid: str) -> str:
    """The signal a rule's *consequent* is about, as the harness names it.

    `awvalid && !awready |=> $stable(awaddr)` is a claim about `awaddr`; the
    antecedent only says when to make it. Asking why about the antecedent would
    answer a question nobody asked.
    """
    from veritrace.protocol import expr

    for rule in iface.pack.rules:
        if rule.id != rid:
            continue
        node = rule.node
        node = node.consequent if isinstance(node, expr.Implication) else node
        for name in expr.identifiers(node):
            if name in iface.signals:
                return f"{harness.TOP}.u_dut.{iface.signals[name].rsplit('.', 1)[-1]}"
    return ""


def reach(
    elaboration: Any,
    holes: list[Any],
    sources: list[Path],
    top: str,
    work: Path,
    depth: int = 30,
    engine: str = "smtbmc z3",
    timeout: float = 900.0,
) -> list[Reachability]:
    """§8.35 — is this coverage hole reachable at all, or is it dead code?

    A hole carries the conditions §8.12 derived for it. Each becomes a `cover`,
    and SBY answers the only question that matters before anyone writes a test:
    *can this ever happen.*
    """
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    dut = harness.instance_of(elaboration, top)
    if not dut.module:
        raise tools.ToolError(f"{top} is not an instance in the elaborated design")

    usable: list[tuple[str, Any, str]] = []
    out: list[Reachability] = []
    for i, hole in enumerate(holes):
        text = _condition(hole)
        r = Reachability(
            file=hole.file, line=hole.line, label=getattr(hole, "label", "") or "",
            condition=text or "", depth=depth,
        )
        if not text:
            # §8.12 could not say what it would take to reach this point, so
            # neither can a solver. Reported, not silently dropped: an unclassified
            # hole is still a hole.
            r.reason = getattr(hole, "note", "") or "no condition was derived for this point"
            out.append(r)
            continue
        usable.append((f"vt_reach_{i}", r, text))

    if not usable:
        return out

    # The file the DUT is declared in gets the covers; every other source is
    # staged unchanged.
    home = _declares(sources, dut.module)
    if home is None:
        raise tools.ToolError(f"no source in this set declares module {dut.module}")
    clock = next(
        (p.name for p in dut.ports if p.direction == "input" and p.name in CLOCK_NAMES), None
    )
    staged = [
        _with_covers(home, dut.module, clock, usable, work) if s == home else _stage(s, work)
        for s in sources
    ]
    top_file = work / "wrapper.sv"
    top_file.write_text(_wrapper(dut), encoding="utf-8")
    report = sby.run(
        [*staged, top_file], harness.TOP, work, {cid: text for cid, _, text in usable},
        "cover", depth, engine, timeout,
    )

    verdicts = {p.id.lower(): p for p in report.properties}
    for cid, r, _ in usable:
        p = verdicts.get(cid.lower())
        if p is None:
            r.reason = "sby did not report on this cover statement"
        elif p.verdict.value == "failed":
            # In cover mode "failed" is SBY reaching the statement, which is the
            # good news: the hole is real and a test can close it.
            r.status, r.step, r.trace = "reachable", p.step, p.trace
        elif p.verdict.value == "held":
            r.status = "unreachable"
        else:
            r.reason = p.reason
        out.append(r)
    return out


CLOCK_NAMES = ("clk", "clock", "aclk", "clk_i", "i_clk")


def _wrapper(dut: Any) -> str:
    """The design with every input free, and nothing else in it.

    The `cover` statements are deliberately not here — see `_with_covers`.
    """
    inputs = [p for p in dut.ports if p.direction == "input"]
    outputs = [p for p in dut.ports if p.direction == "output"]
    lines = [
        "// Generated by VeriTrace — §8.35. Every input is free, so a `cover`",
        "// the solver reaches is a statement some run reaches.",
        f"module {harness.TOP} (",
        ",\n".join(f"  {p.decl}" for p in inputs),
        ");",
        *(
            f"  wire [{p.width - 1}:0] {p.name};" if p.width > 1 else f"  wire {p.name};"
            for p in outputs
        ),
        "",
    ]
    if dut.parameters:
        lines += [
            f"  {dut.module} #(",
            ",\n".join(f"    .{n}({v})" for n, v in dut.parameters),
            "  ) u_dut (",
        ]
    else:
        lines.append(f"  {dut.module} u_dut (")
    lines += [
        ",\n".join(f"    .{p.name}({p.name})" for p in dut.ports),
        "  );",
        "endmodule",
        "",
    ]
    return "\n".join(lines)


def _with_covers(
    source: Path, module: str, clock: str | None, covers: list[tuple[str, Any, str]], work: Path
) -> Path:
    """A copy of `source` with one `cover` per hole, *inside* the module.

    They cannot go in the wrapper. A hole's condition is written in the scope of
    the module it came from — `retry > 3` — and Yosys does not resolve a
    hierarchical reference from outside it: `u_dut.retry` becomes an implicitly
    declared wire with no driver, and every cover then comes back unreachable.
    That is the worst failure available here, because the wrong answer looks
    exactly like the right one.

    The design is **copied**, never edited in place, using the same span-and-
    replace the mutation operators use — so every other byte, and therefore every
    line number in the report, is unchanged.
    """
    data = Path(source).read_bytes()
    at = _endmodule(data, module)
    if at is None:
        raise tools.ToolError(f"could not find the end of module {module} in {Path(source).name}")
    edge = f"posedge {clock}" if clock else "*"
    body = "\n".join(
        [
            "",
            "  // Inserted by VeriTrace — §8.35 reachability. Not part of the design.",
            f"  always @({edge}) begin",
            *(f"    {cid}: cover ({text});" for cid, _, text in covers),
            "  end",
            "",
        ]
    ).encode("utf-8")

    out = work / Path(source).name
    out.write_bytes(data[:at] + body + data[at:])
    return out


def _endmodule(data: bytes, module: str) -> int | None:
    """Byte offset of the `endmodule` that closes `module`."""
    from pyslang import syntax as S

    from veritrace.mutate.operators import span

    for node in _modules(data):
        if node.header.name.valueText != module:
            continue
        at = span(data, node.endmodule)
        return at[0] if at else None
    return None


def _declares(sources: list[Path], module: str) -> Path | None:
    """The source file that declares `module`.

    Matched on the parsed declaration rather than on the file name: a module
    called `fifo` is not reliably in `fifo.sv`, and guessing wrong would insert
    the covers into a file the design never reads — which reads as "unreachable"
    for every hole.
    """
    for source in sources:
        for node in _modules(Path(source).read_bytes()):
            if node.header.name.valueText == module:
                return Path(source)
    return None


def _modules(data: bytes) -> Any:
    """Every module declaration in `data`, or nothing if it will not parse."""
    from pyslang import syntax as S

    try:
        tree = S.SyntaxTree.fromText(data.decode("utf-8", errors="replace"), "<mem>")
    except Exception:  # noqa: BLE001 - slang raises several unrelated types
        return
    stack = [tree.root]
    while stack:
        node = stack.pop()
        if node.kind == S.SyntaxKind.ModuleDeclaration:
            yield node
        for child in node:
            if isinstance(child, S.SyntaxNode):
                stack.append(child)


def _condition(hole: Any) -> str:
    """A hole's conditions as one expression, in the design's own scope.

    §8.12 records each conjunct with the text it came from, so their conjunction
    is what would have to hold for the point to execute. Conjuncts the sampler
    could not evaluate against a trace are kept: a solver does not need one.
    """
    parts = [c.text.strip() for c in getattr(hole, "conditions", []) or [] if c.text.strip()]
    return " && ".join(f"({p})" for p in parts)


def _rule_text(iface: Interface, rid: str) -> str:
    for rule in iface.pack.rules:
        if rule.id == rid:
            return rule.msg or rule.check
    return rid


def _stage(source: Path, work: Path) -> Path:
    """`source`, copied next to the `.sby`.

    Copying rather than referencing keeps the run self-contained, which is the
    difference between "here is how to reproduce it" and "it worked on my
    machine": the whole directory can be handed to someone else.
    """
    dst = work / Path(source).name
    if Path(source).resolve() != dst.resolve():
        shutil.copyfile(source, dst)
    return dst


def _ports(code: str) -> list[Any]:
    """The checker's own port list, read back off the module it just emitted.

    Parsing what was generated a moment ago looks redundant, and is not: `render`
    decides which ports exist from which rules survived `_usable`, and the
    harness has to connect exactly those. Asking the artefact is more robust than
    recomputing the decision.
    """
    from veritrace.synth.model import Port

    out: list[Port] = []
    inside = False
    for line in code.splitlines():
        line = line.strip()
        if line.startswith("module "):
            inside = True
            continue
        if inside and line.startswith(");"):
            break
        if not inside or not line.startswith("input"):
            continue
        text = line.rstrip(",").replace("input wire", "").replace("input", "").strip()
        width = 1
        if text.startswith("["):
            span, _, text = text.partition("]")
            width = int(span[1:].split(":")[0]) + 1
            text = text.strip()
        out.append(Port(text, "input", width))
    return out
