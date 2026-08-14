"""Coverage-directed generation from a pack's own field domains — §8.32.

Two things decide everything here.

**The pack already knows the domains.** A channel's payload fields have widths in
the design, and a `[[cover]]` names the bins that matter. Nothing is invented: a
field is drawn from its width, biased towards the bin boundaries the pack cared
enough to name.

**A hole is closed by the same predicate that scores it.** §8.21 decides a corner
is hit by evaluating `Cover.node` against a transaction; this evaluates *the same
node* against a candidate and keeps the candidate when it passes. Writing a
second, generator-side notion of "what an unaligned address is" would let the
generator and the scorer drift apart, and the whole feature is the claim that
they do not — run, see the hole, generate, run, hole gone.
"""

from __future__ import annotations

import random
from typing import Any

from veritrace.protocol import expr
from veritrace.protocol.model import Interface
from veritrace.protocol.pack import Cover, Pack, Transaction
from veritrace.stim.model import Item, Plan, Target

#: How many candidates to draw before giving up on a corner. A corner like
#: "the address is not aligned" is satisfied by three quarters of all draws; one
#: that needs a 32-bit equality is not reachable by sampling at all, and saying
#: so beats looping.
TRIES = 400


class _Env:
    """A candidate's field values, in the shape `expr.evaluate` reads.

    Deliberately strict: an unknown name raises rather than reading as zero, so a
    corner this generator cannot evaluate is reported as untargetable instead of
    silently counted as hit.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def lookup(self, name: str) -> Any:
        if name not in self.values:
            raise expr.ExprError(f"{name} is not a field this generator drives")
        return self.values[name]

    def call(self, fn: str, args: Any, ev: Any) -> Any:
        raise expr.ExprError(f"{fn}() cannot be evaluated before the transaction exists")


def drivable(pack: Pack, txn: Transaction) -> list[str]:
    """The payload fields an initiator of `txn` chooses.

    The channels it *starts* and the ones it sends a body on; the closing channel
    belongs to the responder, so its payload is not ours to pick. Read off the
    transaction rather than hard-coded, which is what keeps this working for a
    pack nobody has written yet.
    """
    names = [txn.start] + ([txn.body.channel] if txn.body else [])
    out: list[str] = []
    for ch in pack.channels:
        if ch.name in names:
            out += [f for f in ch.payload]
    return out


def widths_of(iface: Interface, graph: Any, fields: list[str]) -> dict[str, int]:
    """Bit width per field, from the design. Absent means one bit."""
    out: dict[str, int] = {}
    for name in fields:
        path = iface.signals.get(name)
        sig = graph.get(path) if (graph is not None and path) else None
        out[name] = sig.width if sig is not None else 1
    return out


def _interesting(width: int) -> list[int]:
    """Values worth drawing more often than chance would.

    Zero, all-ones and the powers of two around them: the boundaries where an
    off-by-one lives. Random alone reaches them with probability 2^-width, which
    on a 32-bit address is never.
    """
    top = (1 << width) - 1
    out = {0, top, 1, top >> 1, (top >> 1) + 1}
    return sorted(v for v in out if 0 <= v <= top)


def _draw(rng: random.Random, width: int) -> int:
    top = (1 << width) - 1
    if rng.random() < 0.3:
        return rng.choice(_interesting(width))
    return rng.randrange(0, top + 1)


def _candidate(rng: random.Random, txn: Transaction, widths: dict[str, int]) -> dict[str, int]:
    return {f: _draw(rng, w) for f, w in widths.items()}


def _env_for(
    pack: Pack, txn: Transaction, values: dict[str, int], stride: int
) -> _Env:
    """A candidate as `[[cover]]` expressions see it.

    The transaction's `key` is what gives a corner the right to say `addr` when
    the wire is called `awaddr` — the pack declares that mapping, so it is applied
    here rather than guessed.
    """
    env = dict(values)
    env["kind"] = txn.name
    env["stride"] = stride
    for alias, source in (txn.key or {}).items():
        field = source.split(".")[-1]
        if field in values:
            env[alias] = values[field]
    return _Env(env)


def _hits(spec: Cover, env: _Env) -> bool:
    try:
        return bool(expr.evaluate(spec.node, env))
    except expr.ExprError:
        return False


def _covers_by_name(pack: Pack) -> dict[str, Cover]:
    return {c.name: c for c in pack.cover}


def generate(
    pack: Pack,
    iface: Interface,
    graph: Any = None,
    n: int = 100,
    seed: int = 0,
    holes: list[tuple[str, str]] | None = None,
    stride: int = 4,
) -> Plan:
    """`n` legal transactions, with `holes` targeted first.

    `holes` is `[(point, bin)]` — exactly what `veritrace coverage --json` lists
    as never hit. Everything that can be aimed at is aimed at once, and the rest
    of the budget is filled randomly so the run still exercises the interface
    rather than only its corners.
    """
    rng = random.Random(seed)
    plan = Plan(pack=pack.name, iface=iface.name, seed=seed)
    if not pack.transactions:
        return plan

    fields: dict[str, dict[str, int]] = {}
    for txn in pack.transactions:
        fields[txn.name] = widths_of(iface, graph, drivable(pack, txn))
        plan.widths.update(fields[txn.name])

    specs = _covers_by_name(pack)
    for point, want in holes or []:
        item, why = _target(rng, pack, iface, specs.get(point), point, want, fields, stride)
        plan.targets.append(Target(point=point, bin=want, status="targeted" if item else "skipped", reason=why))
        if item:
            plan.items.extend(item)

    while len(plan.items) < n:
        txn = rng.choice(pack.transactions)
        plan.items.append(Item(kind=txn.name, values=_candidate(rng, txn, fields[txn.name])))
    return plan


def _target(
    rng: random.Random,
    pack: Pack,
    iface: Interface,
    spec: Cover | None,
    point: str,
    want: str,
    fields: dict[str, dict[str, int]],
    stride: int,
) -> tuple[list[Item], str]:
    """Items that close one hole, or nothing and the reason why.

    Three shapes, and they need three different answers:

    * a **corner** is a predicate, so candidates are drawn until one satisfies it;
    * a **sequence** is an ordering, so it is emitted as consecutive items;
    * a **field bin** is a value, so it is only closable when the field is one an
      initiator drives — `bresp` is the slave's answer, and no stimulus can make
      it `DECERR`. Saying that plainly is worth more than generating traffic that
      will not close it.
    """
    if spec is not None and spec.corner:
        for txn in pack.transactions:
            for _ in range(TRIES):
                values = _candidate(rng, txn, fields[txn.name])
                if _hits(spec, _env_for(pack, txn, values, stride)):
                    return [Item(txn.name, values, targets=f"{point}/{want}")], ""
        return [], f"no candidate satisfied `{spec.when}` in {TRIES} draws"

    if spec is not None and spec.sequence:
        kinds = [k.strip() for k in want.replace("x", " ").split() if k.strip()]
        known = {t.name: t for t in pack.transactions}
        if not kinds or any(k not in known for k in kinds):
            return [], f"{want} does not name transactions this pack has"
        out = [
            Item(k, _candidate(rng, known[k], fields[k]), targets=f"{point}/{want}") for k in kinds
        ]
        return out, ""

    # A field bin. Closable only if we drive the field.
    for txn in pack.transactions:
        if point not in fields[txn.name]:
            continue
        lo, hi = (spec.bins.get(want, (None, None)) if spec is not None else (None, None))
        if lo is None:
            return [], f"the pack declares no bin called {want} for {point}"
        values = _candidate(rng, txn, fields[txn.name])
        values[point] = rng.randint(lo, hi)
        return [Item(txn.name, values, targets=f"{point}/{want}")], ""
    return [], f"{point} is driven by the responder, so no stimulus can reach {want}"


def holes_from(report: dict[str, Any], iface: str | None = None) -> list[tuple[str, str]]:
    """Every never-hit bin in a `veritrace coverage --json` report.

    Reading the report rather than recomputing coverage keeps §8.32 a step in the
    §8.21 pipeline instead of a second opinion about what a hole is.
    """
    out: list[tuple[str, str]] = []
    for cov in report.get("functional", []):
        if iface and cov.get("iface") != iface:
            continue
        for point in cov.get("points", []):
            labels = [a for axis in point.get("labels", []) for a in axis]
            hit = {tuple(c["key"]) for c in point.get("cells", []) if c.get("hits")}
            for cell in point.get("cells", []):
                if cell.get("hits"):
                    continue
                out.append((point["name"], " x ".join(cell["key"])))
            if not point.get("cells") and labels:
                out += [(point["name"], label) for label in labels]
    return out
