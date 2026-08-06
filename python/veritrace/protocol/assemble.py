"""Steps 3-5 of §8.14: events to transactions, metrics, rules.

*A state machine per `[[transaction]]`, with a table of open transactions keyed
on `id` (which is what supports outstanding and out-of-order). Metrics evaluated
per transaction at close. Rules evaluated incrementally; violations become
findings carrying (rule, transaction, cycle, signal).*

The engine still knows nothing about AXI. What it knows is:

* a transaction **opens** on one channel, optionally accumulates **beats** on
  another, and **closes** on a third;
* which open transaction an event belongs to is decided by the pack's
  `match_on` when there is one, and by age when there is not — which is exactly
  AXI's write-data ordering rule, expressed once here rather than in a pack;
* a transaction that never closes is **kept**, not discarded. It is the most
  valuable row in the table: §8.18's wait-for graph is built out of these, and
  `txn(iface) | open()` is how a deadlock is found before anyone knows there is
  one.

Rules split in two by shape, not by configuration. `a |=> b` spans two cycles,
so it is evaluated against the sampled signal matrix; everything else is a
statement about one finished transaction and is evaluated at close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from veritrace.clocks import Clock
from veritrace.protocol import expr
from veritrace.protocol.channels import ChannelScan, Sampler
from veritrace.protocol.model import ChannelEvent, FieldValue, Interface, Transaction, Violation
from veritrace.protocol.pack import Pack, Phase

#: Violations listed per rule before the rest are summarised. A protocol broken
#: on every cycle is one problem, and four thousand rows hide the other nine.
MAX_VIOLATIONS_PER_RULE = 20


# --- the environment a pack expression sees ---------------------------------


class TxnEnv:
    """Names visible to `[metrics]`, `match_on` and per-transaction rules.

    Resolution order, most specific first:

    1. `CH.field` / `CH.accept_time` / `CH.assert_time` / `CH.stall` - a named
       channel's first beat, which is the only beat for an address or response
       channel and the first for a data burst.
    2. `start.time` / `end.time` / `start.cycle` / `end.cycle`.
    3. `param.x` from the rule being evaluated.
    4. a metric already computed for this transaction, so `[metrics]` can build
       on itself instead of repeating subexpressions.
    5. a bare payload field, looked up on the start event first and then on any
       beat - this is what lets `bid == AW.awid` be written the short way.

    A field the pack declares but the design does not carry resolves to *unknown*
    rather than raising: an AXI4 design without `awcache` is still AXI4. A name
    no channel declares at all does raise, because that is a typo in the pack
    and silence would turn it into a rule that never fires.
    """

    def __init__(
        self,
        txn: Transaction,
        pack: Pack,
        clock: Clock | None,
        params: dict[str, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.txn = txn
        self.pack = pack
        self.clock = clock
        self.params = params or {}
        self.extra = extra or {}
        self._declared = {name for ch in pack.channels for name in ch.payload}
        self._first: dict[str, ChannelEvent] = {}
        for e in txn.events:
            self._first.setdefault(e.channel, e)

    def _cycle(self, t: int | None) -> int | None:
        if t is None or self.clock is None:
            return t
        return self.clock.cycle_of(t)

    def lookup(self, name: str) -> Any:
        if name in self.extra:
            return self.extra[name]
        head, _, tail = name.partition(".")
        if tail:
            if head == "param":
                return self.params.get(tail)
            if head in ("start", "end"):
                t = self.txn.start_time if head == "start" else self.txn.end_time
                if tail == "time":
                    return t
                if tail == "cycle":
                    return self._cycle(t)
            ev = self._first.get(head)
            if ev is not None or any(c.name == head for c in self.pack.channels):
                return self._event_field(ev, tail)
        if name == "outstanding":
            return self.txn.outstanding
        if name in self.txn.metrics:
            return self.txn.metrics[name]
        if name in self.txn.fields:
            return self.txn.fields[name]
        for e in self.txn.events:
            if name in e.fields:
                return e.fields[name]
        if name in self._declared:
            return None  # declared by the pack, absent from this design
        raise expr.ExprError(f"unknown name `{name}` in transaction {self.txn.ref}")

    def _event_field(self, ev: ChannelEvent | None, field_name: str) -> Any:
        if ev is None:
            return None
        match field_name:
            case "time" | "accept_time":
                return ev.time
            case "assert_time":
                return ev.assert_time
            case "cycle":
                return self._cycle(ev.time)
            case "stall":
                return ev.stall
        return ev.fields.get(field_name)

    def call(self, fn: str, args: Sequence[expr.Node], ev: Callable[[expr.Node], Any]) -> Any:
        match fn:
            case "count":
                if len(args) != 1:
                    raise expr.ExprError("count() takes one channel name")
                name = args[0].name if isinstance(args[0], expr.Name) else None
                if name is None:
                    raise expr.ExprError("count() takes a channel name, e.g. count(W)")
                return len(self.txn.beats(name))
            case "sum" | "avg" | "any" | "all":
                vals = [v for v in self._per_event(args, fn) if v is not None]
                if fn == "sum":
                    return sum(vals)
                if fn == "avg":
                    return (sum(vals) // len(vals)) if vals else None
                if fn == "any":
                    return int(any(vals))
                return int(all(vals)) if vals else None
        return expr.MappingEnv({}, {}).call(fn, args, ev)

    def _per_event(self, args: Sequence[expr.Node], fn: str) -> list[Any]:
        """Evaluate `args[0]` once per beat of this transaction.

        `sum(stall)` is the §8.14 example's `sum(valid && !ready)` written in
        terms this engine can actually decide: at the accepting edge both are
        high by definition, so the count of waiting cycles lives on the event,
        where the scan already recorded it.
        """
        if len(args) != 1:
            raise expr.ExprError(f"{fn}() takes one expression")
        out = []
        for e in self.txn.events:
            out.append(expr.evaluate(args[0], _EventEnv(self, e)))
        return out


class _EventEnv:
    """One beat's scope, for the aggregate functions."""

    def __init__(self, parent: TxnEnv, ev: ChannelEvent) -> None:
        self.parent = parent
        self.ev = ev

    def lookup(self, name: str) -> Any:
        match name:
            case "stall":
                return self.ev.stall
            case "time" | "accept_time":
                return self.ev.time
            case "assert_time":
                return self.ev.assert_time
            case "channel":
                return self.ev.channel
        if name in self.ev.fields:
            return self.ev.fields[name]
        return self.parent.lookup(name)

    def call(self, fn: str, args: Sequence[expr.Node], ev: Callable[[expr.Node], Any]) -> Any:
        if fn in ("sum", "avg", "count", "any", "all"):
            raise expr.ExprError(f"{fn}() cannot be nested inside another aggregate")
        return self.parent.call(fn, args, ev)


# --- step 3: the state machine ----------------------------------------------


@dataclass(slots=True)
class _Open:
    txn: Transaction
    #: Beats still expected on the body/end channel; `None` when the pack says
    #: "until the terminator".
    remaining: int | None = None
    body_done: bool = False


@dataclass(slots=True)
class Assembly:
    transactions: list[Transaction] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    n_events: int = 0
    n_matched: int = 0
    skipped: dict[str, str] = field(default_factory=dict)


def _ordered_events(pack: Pack, scans: dict[str, ChannelScan]) -> list[ChannelEvent]:
    """Every channel event in one chronological stream.

    Ties are broken by the channel's position in the pack, so a pack author
    controls what happens when an address and its response land on the same
    edge — and two runs of the same dump always agree (P1).
    """
    order = {c.name: i for i, c in enumerate(pack.channels)}
    events = [e for s in scans.values() for e in s.events]
    events.sort(key=lambda e: (e.time, order.get(e.channel, 99)))
    return events


def assemble(
    iface: Interface,
    scans: dict[str, ChannelScan],
    clock: Clock | None,
) -> Assembly:
    """Steps 3-5 for one interface: assemble, measure, check.

    Metrics and per-transaction rules run here rather than in a later pass
    because both are questions about a single finished transaction, and keeping
    them together is what makes "add a metric" a one-line change to a TOML file.
    """
    pack = iface.pack
    out = Assembly()
    events = _ordered_events(pack, scans)
    out.n_events = len(events)

    # One machine per transaction type. They are independent: a pack whose types
    # start on different channels (AW vs AR) never sees the other's events.
    machines = [_Machine(iface, spec, clock) for spec in pack.transactions]
    for ev in events:
        for m in machines:
            m.feed(ev)

    for m in machines:
        m.finish()
        out.transactions.extend(m.done)

    for txn in out.transactions:
        apply_metrics(txn, pack, clock)
        txn.violations = check_transaction_rules(txn, pack, clock)
        out.violations.extend(txn.violations)

    # §8.14's acceptance criterion is that the correlation between signals and
    # transactions is reported explicitly, so it is measured on the outcome —
    # beats that actually ended up inside a transaction — rather than on what
    # the machines claimed on the way past.
    placed = {id(e) for t in out.transactions for e in t.events}
    out.n_matched = sum(1 for e in events if id(e) in placed)
    out.transactions.sort(key=lambda t: (t.start_time, t.kind, t.index))
    return out


class _Machine:
    def __init__(self, iface: Interface, spec: Any, clock: Clock | None) -> None:
        self.iface = iface
        self.spec = spec
        self.clock = clock
        self.open: list[_Open] = []
        self.done: list[Transaction] = []
        self._n = 0
        #: Beats that arrived before the transaction they belong to. AXI
        #: explicitly permits write data ahead of its address, and dropping
        #: those beats would silently under-count every such burst.
        self._early: list[tuple[ChannelEvent, Phase, bool]] = []

    # -- dispatch ---------------------------------------------------------

    def feed(self, ev: ChannelEvent) -> None:
        """Consume `ev` if it belongs to this transaction type."""
        if ev.channel == self.spec.start:
            self._start(ev)
        body = self.spec.body
        if body is not None and ev.channel == body.channel:
            self._deliver(ev, body, closes=False)
        end = self.spec.end
        if end is not None and ev.channel == end.channel:
            self._deliver(ev, end, closes=True)

    def _deliver(self, ev: ChannelEvent, phase: Phase, closes: bool) -> None:
        if not self._beat(ev, phase, closes):
            self._early.append((ev, phase, closes))

    def _drain(self) -> None:
        """Retry beats that had no transaction to belong to yet."""
        if not self._early:
            return
        pending, self._early = self._early, []
        for ev, phase, closes in pending:
            if not self._beat(ev, phase, closes):
                self._early.append((ev, phase, closes))

    # -- opening ----------------------------------------------------------

    def _start(self, ev: ChannelEvent) -> None:
        txn = Transaction(
            iface=self.iface.name,
            kind=self.spec.name,
            index=self._n,
            # When it was *issued*, not when it was accepted. §8.13 prints
            # "issued c1200 - addr_acc c1203 - resp c1222" and calls the
            # latency 22 cycles, so `start.time` is the edge valid went high.
            # The accepted edge stays available as `<CH>.accept_time`.
            start_time=ev.assert_time,
            id=ev.fields.get(self.spec.id) if self.spec.id else None,
            fields=dict(ev.fields),
            events=[ev],
            outstanding=len(self.open),
        )
        self._n += 1
        entry = _Open(txn=txn, remaining=self._count_for(txn, self.spec.body or self.spec.end))
        # `key = "addr = AW.awaddr"` lifts named values onto the transaction,
        # which is what `txn(iface, addr=0x4000)` then filters on.
        env = TxnEnv(txn, self.iface.pack, self.clock)
        for name, source in self.spec.key.items():
            try:
                txn.fields[name] = expr.evaluate(expr.parse(source), env)
            except expr.ExprError:
                txn.fields[name] = None

        if self.spec.body is None and self.spec.end is None:
            # A single-beat protocol: the start *is* the transaction.
            txn.end_time = ev.time
            self.done.append(txn)
            return
        self.open.append(entry)
        self._drain()

    def _count_for(self, txn: Transaction, phase: Phase | None) -> int | None:
        if phase is None or phase.count is None:
            return None
        try:
            v = expr.evaluate(expr.parse(phase.count), TxnEnv(txn, self.iface.pack, self.clock))
        except expr.ExprError:
            return None
        return int(v) if isinstance(v, int) else None

    # -- beats and closing -------------------------------------------------

    def _beat(self, ev: ChannelEvent, phase: Phase, closes: bool) -> bool:
        entry = self._match(ev, phase, closes)
        if entry is None:
            return False
        entry.txn.events.append(ev)

        if phase.counts_beats:
            if entry.remaining is not None:
                entry.remaining -= 1
            done = (entry.remaining is not None and entry.remaining <= 0) or (
                phase.terminator is not None and _is_set(ev.fields.get(phase.terminator))
            )
        else:
            done = True

        if not closes:
            entry.body_done = entry.body_done or done
            return True
        if done and (entry.body_done or self.spec.body is None):
            entry.txn.end_time = ev.time
            self.open.remove(entry)
            self.done.append(entry.txn)
        return True

    def _match(self, ev: ChannelEvent, phase: Phase, closes: bool) -> _Open | None:
        """Which open transaction this beat belongs to.

        With `match_on`, the oldest open transaction the expression accepts —
        that is out-of-order completion, keyed on the protocol's own id. Without
        it, simply the oldest still waiting, which is AXI's rule for write data
        and the only defensible default when the protocol offers no key.
        """
        for entry in self.open:
            if closes and self.spec.body is not None and not entry.body_done:
                continue
            if not closes and entry.body_done:
                continue
            if phase.match_on is None:
                return entry
            # The incoming beat's own fields shadow the transaction's, so
            # `bid == AW.awid` reads the response's id on the left and the
            # stored address-phase id on the right.
            env = TxnEnv(entry.txn, self.iface.pack, self.clock, extra=dict(ev.fields))
            try:
                if expr.evaluate(expr.parse(phase.match_on), env):
                    return entry
            except expr.ExprError:
                continue
        return None

    def finish(self) -> None:
        """Transactions still open at the end of the trace stay open.

        Not an error and not dropped: an unfinished transaction is the single
        most informative row this engine produces (§8.18).
        """
        self.done.extend(e.txn for e in self.open)
        self.open.clear()


def _is_set(v: FieldValue) -> bool:
    return isinstance(v, int) and v != 0


# --- step 4: metrics ---------------------------------------------------------


def apply_metrics(txn: Transaction, pack: Pack, clock: Clock | None) -> None:
    """Evaluate `[metrics]` for one finished transaction.

    In declaration order, with each result visible to the next, so a pack can
    build `efficiency = data_beats * 100 / latency` without repeating either.
    """
    env = TxnEnv(txn, pack, clock)
    for name, source in pack.metrics.items():
        try:
            txn.metrics[name] = expr.evaluate(expr.parse(source), env)
        except expr.ExprError:
            txn.metrics[name] = None


# --- step 5: rules -----------------------------------------------------------


def check_transaction_rules(
    txn: Transaction, pack: Pack, clock: Clock | None
) -> list[Violation]:
    """Rules that are statements about a finished transaction."""
    out: list[Violation] = []
    if not txn.closed:
        # A rule is a statement about a finished transaction. Judging one the
        # trace cut short would report `count(W) == awlen + 1` as broken for
        # every burst still in flight at the end of the run — an artefact of
        # where the dump stops, not a protocol error. Unfinished transactions
        # are §8.18's subject, not this one's.
        return out
    for rule in pack.rules:
        if rule.is_temporal:
            continue
        env = TxnEnv(txn, pack, clock, params=rule.params)
        try:
            verdict = expr.evaluate(rule.node, env)
        except expr.ExprError:
            continue
        # `None` is *no verdict*, not a failure: a rule about a field the design
        # does not carry has to stay silent (P1).
        if verdict is not None and not verdict:
            out.append(
                Violation(
                    rule=rule.id,
                    severity=rule.severity,
                    msg=rule.msg or f"rule {rule.id} did not hold",
                    time=txn.end_time if txn.end_time is not None else txn.start_time,
                    txn=txn.ref,
                )
            )
    return out


class CycleEnv:
    """One clock edge's view of the interface, for temporal rules.

    `$past`, `$stable`, `$rose` and `$fell` read the neighbouring rows of the
    same sampled matrix the channel scan used, so a rule and a transaction can
    never disagree about what a signal was doing at a cycle.

    §8.17's stall cascade is the same question asked once per cycle, so it uses
    this too, through the two seams below:

    * **`extra`** binds derived per-cycle quantities — `valid`, `ready`,
      `outstanding` — as columns rather than scalars, so `$past(outstanding)`
      means what it says. Interface signals still win the name, which is what
      makes §8.17's literal `valid && !ready` correct on a bare handshake (where
      those *are* the wires) and on AXI (where they are the aggregate).
    * **`resolve`** is the last resort: a name that is neither is looked up as a
      hierarchical path in the trace. §8.17's arbitration rung — `req_valid &&
      grant != my_id` — is about a wire outside the interface by definition, so
      without this the most valuable rung of the cascade could not be written.
    """

    def __init__(
        self,
        iface: Interface,
        columns: dict[str, list[Any]],
        i: int,
        params: dict[str, int],
        extra: dict[str, Sequence[Any]] | None = None,
        resolve: Callable[[str], Sequence[Any] | None] | None = None,
    ) -> None:
        self.iface = iface
        self.columns = columns
        self.i = i
        self.params = params
        self.extra = extra or {}
        self.resolve = resolve

    def _column(self, name: str) -> Sequence[Any] | None:
        path = self.iface.signals.get(name)
        if path is not None:
            return self.columns.get(path)
        if name in self.extra:
            return self.extra[name]
        return self.resolve(name) if self.resolve is not None else None

    def _at(self, name: str, i: int) -> Any:
        col = self._column(name)
        if col is None or not 0 <= i < len(col):
            return None
        v = col[i]
        if v is None or isinstance(v, (int, str)):
            return v
        if v.has_x():
            # Unknown, not the four-state digits. `expr` treats a non-empty
            # string as true, so returning `"x"` here would make a signal
            # sampled at X satisfy every rung and every rule that mentions it —
            # the opposite of §8.14's rule that an undecidable input yields no
            # verdict. The digits are right for the *exported table*, where
            # `xxxx` and `0000` must stay distinguishable; they are wrong for
            # arithmetic.
            return None
        n = v.to_int()
        # A bus wider than 64 bits has no integer, but it is still known, so it
        # keeps its digits — that is what makes `$stable(addr)` work on one.
        return n if n is not None else v.bits

    def lookup(self, name: str) -> Any:
        head, _, tail = name.partition(".")
        if head == "param" and tail:
            return self.params.get(tail)
        if self._column(name) is not None:
            return self._at(name, self.i)
        raise expr.ExprError(f"`{name}` is not a signal of interface {self.iface.name}")

    def call(self, fn: str, args: Sequence[expr.Node], ev: Callable[[expr.Node], Any]) -> Any:
        if fn.startswith("$"):
            if not args or not isinstance(args[0], expr.Name):
                raise expr.ExprError(f"{fn}() takes a signal name")
            name = args[0].name
            now = self._at(name, self.i)
            match fn:
                case "$stable" | "$changed":
                    prev = self._at(name, self.i - 1)
                    if self.i == 0 or now is None or prev is None:
                        return None
                    same = int(now == prev)
                    return same if fn == "$stable" else int(not same)
                case "$past":
                    n = int(ev(args[1])) if len(args) > 1 else 1
                    return self._at(name, self.i - n)
                case "$rose" | "$fell":
                    prev = self._at(name, self.i - 1)
                    if self.i == 0 or now is None or prev is None:
                        return None
                    want = (0, 1) if fn == "$rose" else (1, 0)
                    return int((prev, now) == want)
            raise expr.ExprError(f"unknown sampled function `{fn}`")
        return expr.MappingEnv({}, {}).call(fn, args, ev)


def check_temporal_rules(
    iface: Interface,
    sampler: Sampler,
    in_reset: list[bool],
    clock: Clock | None,
) -> tuple[list[Violation], dict[str, str]]:
    """Evaluate `a |=> b` rules at every clock edge.

    Only the signals the rules actually name are sampled, so a pack with thirty
    rules costs no more than the union of what they read.
    """
    rules = [r for r in iface.pack.rules if r.is_temporal]
    out: list[Violation] = []
    notes: dict[str, str] = {}
    if not rules:
        return out, notes

    wanted: set[str] = set()
    for r in rules:
        wanted |= {n for n in expr.identifiers(r.node) if n in iface.signals}
    sampler.prefetch([iface.signals[n] for n in sorted(wanted)])
    columns = {iface.signals[n]: sampler.column(iface.signals[n]) for n in wanted}
    columns = {k: v for k, v in columns.items() if v is not None}

    n = len(sampler.edges)
    for rule in rules:
        node = rule.node
        assert isinstance(node, expr.Implication)
        step = 1 if node.op == "|=>" else 0
        hits = 0
        for i in range(n):
            if in_reset[i] or (i + step) >= n or in_reset[i + step]:
                continue
            try:
                ante = expr.evaluate(node.antecedent, CycleEnv(iface, columns, i, rule.params))
                if not ante:
                    continue
                verdict = expr.evaluate(
                    node.consequent, CycleEnv(iface, columns, i + step, rule.params)
                )
            except expr.ExprError as e:
                notes[rule.id] = str(e)
                break
            if verdict is None or verdict:
                continue
            hits += 1
            if hits <= MAX_VIOLATIONS_PER_RULE:
                out.append(
                    Violation(
                        rule=rule.id,
                        severity=rule.severity,
                        msg=rule.msg or f"rule {rule.id} did not hold",
                        time=sampler.edges[i + step],
                        signal=_first_signal(iface, node),
                    )
                )
        if hits > MAX_VIOLATIONS_PER_RULE:
            notes[rule.id] = (
                f"{hits} occurrences; the first {MAX_VIOLATIONS_PER_RULE} are listed"
            )
    return out, notes


def _first_signal(iface: Interface, node: expr.Node) -> str | None:
    """A signal to open the waveform on. The consequent names what broke."""
    for name in sorted(expr.identifiers(node)):
        if name in iface.signals:
            return iface.signals[name]
    return None
