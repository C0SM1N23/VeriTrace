"""Checkers generated from a pack's rules — §8.22.

A `[[rule]]` is already an assertion in abstract form, so emitting one is
transcription rather than invention:

    check = "awvalid && !awready |=> $stable(awaddr)"

§8.22's point is that **you cannot rely on SVA at run time across the free
stack** — Verilator supports a subset, ModelSim's free edition varies by
version, Icarus effectively has none — so the same rules are emitted two ways:

* `--target verilator` — SVA, restricted to what `--assert` actually accepts:
  `|->`, `|=>`, `$stable`, `$past`, `$rose`, `$fell`.
* `--target portable` — **plain Verilog**. Every temporal operator becomes a
  registered copy and an `if`, which runs in Icarus, ModelSim free, xsim, and
  anything else that can simulate at all.

Both come from the same parsed AST, so the two files check the same thing. That
is the only property that matters here: a portable checker that quietly says
something weaker than the SVA one would be worse than not shipping it, because
it would pass.

The output is a module plus a `bind`, so it attaches to a design without
touching it — the same reasoning as §13.4b's generated `$dumpvars` module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from veritrace import licensing
from veritrace.protocol import expr
from veritrace.protocol.pack import Pack, Rule

#: `formal` is `portable`'s lowering with `assert` in place of `$error` — see
#: `_verilog`. It exists because §8.27 needs the *same* rules as a property for
#: SymbiYosys, and a second transcription of them would be a second chance to
#: say something subtly different.
TARGETS = ("verilator", "portable", "formal")

#: Functions §8.22 says Verilator's `--assert` accepts. Anything else in a rule
#: is refused for that target rather than emitted and hoped for.
SVA_FUNCTIONS = frozenset({"$stable", "$past", "$rose", "$fell", "$changed", "$onehot", "$countones"})


class SvaError(ValueError):
    """A rule that cannot be emitted for the requested target, and why."""


@dataclass(slots=True)
class Port:
    name: str
    width: int = 1

    @property
    def decl(self) -> str:
        return f"input wire {'' if self.width <= 1 else f'[{self.width - 1}:0] '}{self.name}"


@dataclass(slots=True)
class Checker:
    module: str
    target: str
    code: str
    iface: str
    #: Rules that made it into the file.
    emitted: list[str] = field(default_factory=list)
    #: Rules left out, with the reason — never silently dropped.
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "target": self.target,
            "iface": self.iface,
            "code": self.code,
            "emitted": self.emitted,
            "skipped": self.skipped,
        }


# --- reading a rule ---------------------------------------------------------


def _substitute(node: expr.Node, params: dict[str, int]) -> expr.Node:
    """Replace `param.max` with its value — the checker has no parameters."""
    match node:
        case expr.Name(name=n) if n.startswith("param."):
            key = n.split(".", 1)[1]
            if key not in params:
                raise SvaError(f"`{n}` has no value in the pack")
            return expr.Const(params[key])
        case expr.Unary(op=op, arg=a):
            return expr.Unary(op, _substitute(a, params))
        case expr.Binary(op=op, lhs=l, rhs=r):
            return expr.Binary(op, _substitute(l, params), _substitute(r, params))
        case expr.Ternary(cond=c, then=t, other=o):
            return expr.Ternary(
                _substitute(c, params), _substitute(t, params), _substitute(o, params)
            )
        case expr.Call(fn=fn, args=args):
            return expr.Call(fn, tuple(_substitute(a, params) for a in args))
        case expr.Implication(op=op, antecedent=a, consequent=c):
            return expr.Implication(op, _substitute(a, params), _substitute(c, params))
        case expr.Index(arg=a, index=i):
            return expr.Index(_substitute(a, params), _substitute(i, params))
        case expr.Slice(arg=a, hi=h, lo=lo):
            return expr.Slice(_substitute(a, params), _substitute(h, params), _substitute(lo, params))
        case _:
            return node


def _text(node: expr.Node) -> str:
    """The expression as SystemVerilog. `$past`, `$stable` and friends survive."""
    match node:
        case expr.Const(value=v):
            return str(v) if isinstance(v, int) else f'"{v}"'
        case expr.Name(name=n):
            return n
        case expr.Unary(op=op, arg=a):
            return f"{op}{_text(a)}"
        case expr.Binary(op=op, lhs=l, rhs=r):
            return f"({_text(l)} {op} {_text(r)})"
        case expr.Ternary(cond=c, then=t, other=o):
            return f"({_text(c)} ? {_text(t)} : {_text(o)})"
        case expr.Call(fn=fn, args=args):
            return f"{fn}({', '.join(_text(a) for a in args)})"
        case expr.Index(arg=a, index=i):
            return f"{_text(a)}[{_text(i)}]"
        case expr.Slice(arg=a, hi=h, lo=lo):
            return f"{_text(a)}[{_text(h)}:{_text(lo)}]"
        case expr.Implication(op=op, antecedent=a, consequent=c):
            return f"{_text(a)} {op} {_text(c)}"
        case _:
            raise SvaError(f"cannot write {node!r} as SystemVerilog")


def _lower(node: expr.Node, past: dict[str, None]) -> str:
    """The same expression in **plain Verilog**, recording what needs a register.

    Each temporal function becomes a comparison against a registered copy, and
    the name of that copy goes into `past` so the caller can declare it. That is
    the whole of the portable translation — §8.22 calls it mechanical, and it is.
    """
    match node:
        case expr.Call(fn="$stable", args=(a,)):
            name = _past_name(a, past)
            return f"({_lower(a, past)} == {name})"
        case expr.Call(fn="$changed", args=(a,)):
            name = _past_name(a, past)
            return f"({_lower(a, past)} != {name})"
        case expr.Call(fn="$past", args=(a, *_rest)):
            return _past_name(a, past)
        case expr.Call(fn="$rose", args=(a,)):
            name = _past_name(a, past)
            return f"({_lower(a, past)} && !{name})"
        case expr.Call(fn="$fell", args=(a,)):
            name = _past_name(a, past)
            return f"(!{_lower(a, past)} && {name})"
        case expr.Call(fn="$onehot", args=(a,)):
            inner = _lower(a, past)
            return f"(({inner} != 0) && (({inner} & ({inner} - 1)) == 0))"
        case expr.Call(fn=fn):
            raise SvaError(f"`{fn}` has no plain-Verilog form")
        case expr.Unary(op=op, arg=a):
            return f"{op}{_lower(a, past)}"
        case expr.Binary(op=op, lhs=l, rhs=r):
            return f"({_lower(l, past)} {op} {_lower(r, past)})"
        case expr.Ternary(cond=c, then=t, other=o):
            return f"({_lower(c, past)} ? {_lower(t, past)} : {_lower(o, past)})"
        case expr.Index(arg=a, index=i):
            return f"{_lower(a, past)}[{_lower(i, past)}]"
        case expr.Slice(arg=a, hi=h, lo=lo):
            return f"{_lower(a, past)}[{_lower(h, past)}:{_lower(lo, past)}]"
        case expr.Implication():
            raise SvaError("an implication cannot appear inside an expression")
        case _:
            return _text(node)


def _past_name(node: expr.Node, past: dict[str, None]) -> str:
    if not isinstance(node, expr.Name):
        raise SvaError(f"only a plain signal can be sampled in the past, not `{_text(node)}`")
    past[node.name] = None
    return f"{node.name}_q"


# --- emitting ---------------------------------------------------------------


def _signals(rule: Rule) -> set[str]:
    return {n for n in expr.identifiers(rule.node) if not n.startswith("param.")}


def _usable(rule: Rule, known: set[str]) -> str | None:
    """Why this rule cannot be emitted, or `None` if it can.

    Two reasons, and the second is the one that matters.

    A rule with no temporal operator is evaluated by the engine **per
    transaction, at close** (§8.14 step 5) — `bresp == 0` means "the response of
    this transaction was OKAY", not "bresp is zero on every cycle of the run".
    A cycle-level checker cannot say that, and one that checked it every cycle
    would fire on every idle cycle between transactions and be switched off
    within a day. So it is skipped, and the message says where that rule *is*
    checked: post-mortem, which §8.22 argues is the more valuable half anyway.

    A pack that means a genuine per-cycle invariant writes it as an implication —
    `read || write |-> !(read && write)` — which says the same thing and is
    emittable.
    """
    missing = sorted(_signals(rule) - known)
    if missing:
        return f"the interface has no {', '.join(missing)}"
    if not rule.is_temporal:
        return (
            "evaluated per transaction rather than per cycle; VeriTrace checks it "
            "from the trace (§8.14)"
        )
    return None


def render(
    pack: Pack,
    iface: str,
    target: str = "verilator",
    widths: dict[str, int] | None = None,
    clock: str = "clk",
    reset: str = "rst_n",
    reset_active_low: bool = True,
    module: str | None = None,
) -> Checker:
    """A checker module plus its `bind`, for every rule in `pack` (§8.22)."""
    if target not in TARGETS:
        raise SvaError(f"unknown target {target!r}; try one of {', '.join(TARGETS)}")
    if not pack.rules:
        raise SvaError(f"{pack.name} declares no [[rule]], so there is nothing to check")

    widths = widths or {}
    name = module or f"vt_{_ident(pack.name)}_checker"
    known = set(widths) or {s for r in pack.rules for s in _signals(r)}

    ports = [Port(clock), Port(reset)]
    used: set[str] = set()
    for rule in pack.rules:
        if _usable(rule, known) is None:
            used |= _signals(rule)
    ports += [Port(s, widths.get(s, 1)) for s in sorted(used) if s not in (clock, reset)]

    body, emitted, skipped, past = (
        _sva(pack.rules, known, clock, reset, reset_active_low)
        if target == "verilator"
        else _verilog(pack.rules, known, clock, reset, reset_active_low, target == "formal")
    )

    HEADING = {
        "verilator": "// SVA, restricted to the subset Verilator's --assert accepts.",
        "portable": "// Plain Verilog: no SVA at all, so this runs in Icarus, ModelSim's\n"
        "// free edition and xsim as well as in Verilator.",
        "formal": "// Immediate assertions in a clocked block — what `read -formal` takes.\n"
        "// Proved by SymbiYosys up to a bounded depth, never further (§8.27).",
    }
    out = [
        f"// Generated by VeriTrace from {pack.name} {pack.version} — §8.22.",
        *licensing.banner(),
        "//",
        HEADING[target],
        f"// Bound to {iface}; the design is not modified.",
        "",
        f"module {name} (",
        ",\n".join(f"  {p.decl}" for p in ports),
        ");",
    ]
    if past:
        out.append("")
        out.append("  // Registered copies, one per signal a rule looks back at.")
        for signal in past:
            w = widths.get(signal, 1)
            vec = "" if w <= 1 else f"[{w - 1}:0] "
            out.append(f"  reg {vec}{signal}_q;")
        out.append(f"  always @(posedge {clock}) begin")
        for signal in past:
            out.append(f"    {signal}_q <= {signal};")
        out.append("  end")
    out.append("")
    out.extend(body)
    out.append("endmodule")
    out.append("")
    out.extend(_attach(name, iface, ports, target))

    return Checker(
        module=name,
        target=target,
        iface=iface,
        code="\n".join(out) + "\n",
        emitted=emitted,
        skipped=skipped,
    )


def _sva(
    rules: Iterable[Rule], known: set[str], clock: str, reset: str, low: bool
) -> tuple[list[str], list[str], dict[str, str], dict[str, None]]:
    out: list[str] = []
    emitted: list[str] = []
    skipped: dict[str, str] = {}
    guard = f"disable iff ({'!' if low else ''}{reset})"

    for rule in rules:
        why = _usable(rule, known)
        if why:
            skipped[rule.id] = why
            continue
        try:
            node = _substitute(rule.node, rule.params)
            bad = [
                c.fn
                for c in _calls(node)
                if c.fn not in SVA_FUNCTIONS
            ]
            if bad:
                raise SvaError(
                    f"`{bad[0]}` is outside the subset Verilator's --assert accepts"
                )
            text = _text(node) if isinstance(node, expr.Implication) else f"({_text(node)})"
        except SvaError as e:
            skipped[rule.id] = str(e)
            continue
        emitted.append(rule.id)
        out.append(f"  // {rule.msg or rule.id}")
        out.append(f"  property p_{rule.id};")
        out.append(f"    @(posedge {clock}) {guard}")
        out.append(f"    {text};")
        out.append("  endproperty")
        severity = "$error" if rule.severity == "error" else "$warning"
        out.append(
            f'  a_{rule.id}: assert property (p_{rule.id}) else {severity}("{_escape(rule)}");'
        )
        out.append("")
    return out, emitted, skipped, {}


def _verilog(
    rules: Iterable[Rule], known: set[str], clock: str, reset: str, low: bool,
    formal: bool = False,
) -> tuple[list[str], list[str], dict[str, str], dict[str, None]]:
    """The same rules with no SVA at all — §8.22's `--target portable`.

    `a |=> b` is "if `a` held last cycle, `b` must hold now", which is a
    registered copy of `a` and an `if`. `a |-> b` needs no register. Everything
    else is a combinational check under the same reset guard.

    `formal` swaps the report for an `assert`, which is all §8.27 needs: an
    immediate assertion inside a clocked block is what `read -formal` consumes,
    so nothing here depends on how much of concurrent SVA Yosys supports this
    month. The lowering is shared deliberately — a formal property that said
    something slightly different from the simulation checker would make the two
    results incomparable, which is the one thing §8.27 is for.
    """
    out: list[str] = []
    emitted: list[str] = []
    skipped: dict[str, str] = {}
    past: dict[str, None] = {}
    running = f"{'' if low else '!'}{reset}"

    for rule in rules:
        why = _usable(rule, known)
        if why:
            skipped[rule.id] = why
            continue
        local: dict[str, None] = {}
        try:
            node = _substitute(rule.node, rule.params)
            if isinstance(node, expr.Implication):
                consequent = _lower(node.consequent, local)
                if node.op == "|=>":
                    # The antecedent is about the *previous* cycle, so it is
                    # registered whole rather than signal by signal: a rule like
                    # `a && !b |=> c` must fire on what a and b were together.
                    antecedent = f"ant_{rule.id}_q"
                    local_ante = _lower(node.antecedent, local)
                    out.append(f"  reg ant_{rule.id}_q;")
                    out.append(f"  always @(posedge {clock}) begin")
                    out.append(f"    ant_{rule.id}_q <= {running} && ({local_ante});")
                    out.append("  end")
                else:
                    antecedent = _lower(node.antecedent, local)
                condition = f"!({antecedent}) || ({consequent})"
            else:
                condition = _lower(node, local)
        except SvaError as e:
            skipped[rule.id] = str(e)
            continue

        past.update(local)
        emitted.append(rule.id)
        task = "$error" if rule.severity == "error" else "$display"
        prefix = "" if rule.severity == "error" else "WARNING: "
        out.append(f"  // {rule.msg or rule.id}")
        out.append(f"  always @(posedge {clock}) begin")
        out.append(f"    if ({running}) begin")
        if formal:
            out.append(f"      {rule.id}: assert ({condition});")
        else:
            out.append(f"      if (!({condition}))")
            out.append(f'        {task}("{prefix}{_escape(rule)}");')
        out.append("    end")
        out.append("  end")
        out.append("")
    return out, emitted, skipped, past


def _calls(node: expr.Node) -> list[expr.Call]:
    out: list[expr.Call] = []
    stack: list[expr.Node] = [node]
    while stack:
        n = stack.pop()
        match n:
            case expr.Call(args=args):
                out.append(n)
                stack.extend(args)
            case expr.Unary(arg=a) | expr.Index(arg=a):
                stack.append(a)
            case expr.Binary(lhs=l, rhs=r) | expr.Implication(antecedent=l, consequent=r):
                stack += [l, r]
            case expr.Ternary(cond=c, then=t, other=o):
                stack += [c, t, o]
            case expr.Slice(arg=a, hi=h, lo=lo):
                stack += [a, h, lo]
    return out


def _escape(rule: Rule) -> str:
    text = (rule.msg or rule.id).replace("\\", "\\\\").replace('"', '\\"')
    return f"{rule.id}: {text}"


def _ident(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text).strip("_").lower()
    return out or "pack"


def _attach(module: str, iface: str, ports: list[Port], target: str) -> list[str]:
    """How the checker gets connected to the design, without editing it.

    Two ways, because they are not equally portable and §8.22's whole point is
    that portability is the constraint:

    * `bind` for the Verilator target. It is the right construct and Verilator
      supports it.
    * a **top-level module with hierarchical references** for the portable one.
      Icarus does not implement `bind` at all, so a file that used it would fail
      to compile in exactly the simulator this target exists for — the claim
      "runs in Icarus" would have been false in the first line of the file. This
      is the same device `veritrace run` already uses to inject `$dumpvars`
      without touching a testbench, and it works everywhere.
    """
    if target == "formal":
        # Nothing here. A formal run has no testbench to attach to — the design's
        # inputs must be left *free* so the solver can drive them — so the
        # checker is instantiated by the harness `formal/harness.py` builds,
        # alongside the design, on the same nets.
        return []
    if target == "verilator":
        return [
            "// Attached with `bind`, so the design is not modified.",
            f"bind {iface} {module} vt_check (",
            ",\n".join(f"  .{p.name}({p.name})" for p in ports),
            ");",
        ]
    return [
        "// Attached from the outside with hierarchical references, because Icarus",
        "// has no `bind`. Nothing in the design changes; this is an extra root.",
        "//",
        "// If your build names its top explicitly (`iverilog -s tb_top`), name this",
        f"// one too: `-s {module}_top`.",
        f"module {module}_top;",
        f"  {module} vt_check (",
        ",\n".join(f"    .{p.name}({iface}.{p.name})" for p in ports),
        "  );",
        "endmodule",
    ]
