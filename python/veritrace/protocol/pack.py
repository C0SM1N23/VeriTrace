"""Protocol packs: the TOML schema of §8.14 and its loader.

A pack is *a description, not code*. §8.15 sets the bar it has to clear: **a new
protocol is one file and about thirty minutes** — if it takes a day, the DSL is
wrong. Everything here exists to keep that true:

* the shapes below are the whole vocabulary, and nothing in the engine knows a
  protocol name;
* an unknown key is a **warning carried on the pack**, not a fatal error, so a
  pack written against a later version still loads and still says what it did
  not understand;
* a malformed *expression*, by contrast, is fatal, because a rule that silently
  never fires is worse than one that refuses to load (P1).

Packs are looked up in three places, nearest first: an explicit path, the
project's own `packs/` directory, then the ones shipped in the wheel. A project
overriding `axi4.vtp.toml` with its own house variant is the expected case, not
an edge case.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from veritrace.protocol import expr

#: Every pack file is `<name>.vtp.toml`, so a directory of packs is obvious in a
#: file listing and a stray TOML is not mistaken for one.
SUFFIX = ".vtp.toml"

#: Bucket names §8.17's attribution fills in by itself, so a pack cannot claim
#: one and quietly make the shares stop adding up. `reset` and `transfer` are
#: decided before the cascade runs; `other` is what is left when no rung
#: matched, and is reported rather than hidden — a large `other` means the
#: cascade is incomplete, which is worth seeing (P1).
RESERVED_BUCKETS = ("reset", "transfer", "other")


class PackError(ValueError):
    """A pack that cannot be used as written."""


# --- schema -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detect:
    """How an interface is recognised in a design (§8.14, step 1)."""

    required_suffixes: tuple[str, ...] = ()
    #: Prefixes to try stripping, in order. `""` means "the bare suffixes".
    prefix_strip: tuple[str, ...] = ("",)
    #: Regex alternations, matched against leaf names in scope and its parents.
    clock: str = r"clk|clock"
    reset: str = r"rst_n|resetn|rst|reset"

    @property
    def specificity(self) -> int:
        """How exacting this pack is, for choosing between two that both match.

        AXI4 requires twelve signals, AXI4-Lite ten, generic handshake two — so
        the count is a faithful ordering and needs no hand-assigned priority
        that packs would then have to keep consistent with each other.
        """
        return len(self.required_suffixes)


@dataclass(frozen=True, slots=True)
class Channel:
    """One valid/ready handshake with a payload."""

    name: str
    valid: str
    ready: str | None = None
    payload: tuple[str, ...] = ()

    @property
    def signals(self) -> tuple[str, ...]:
        return (self.valid,) + ((self.ready,) if self.ready else ()) + self.payload


@dataclass(frozen=True, slots=True)
class Phase:
    """The `body =` / `end =` clause of a transaction."""

    channel: str
    #: How many beats, as an expression over the start event (`AW.awlen + 1`).
    count: str | None = None
    #: Payload field that marks the final beat (`wlast`).
    terminator: str | None = None
    #: Which open transaction this event belongs to (`bid == AW.awid`).
    match_on: str | None = None

    @property
    def counts_beats(self) -> bool:
        """True when this phase spans several beats and the assembler has to
        track how many are still expected."""
        return self.count is not None or self.terminator is not None


@dataclass(frozen=True, slots=True)
class Transaction:
    name: str
    start: str
    body: Phase | None = None
    end: Phase | None = None
    #: Payload field on the start channel that allows outstanding/out-of-order.
    id: str | None = None
    #: Named values lifted onto the transaction: `{"addr": "AW.awaddr"}`.
    key: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StallReason:
    """One rung of §8.17's attribution cascade.

    Order is the priority: the first `when` that holds owns the cycle. That is
    what makes the shares add to 100% — a cycle cannot be counted twice, and
    with the implicit `other` bucket at the bottom it cannot go uncounted
    either.
    """

    name: str
    when: str
    #: Shown under the slice in TAB 9. A share without a sentence explaining it
    #: is a number nobody acts on.
    msg: str = ""

    @property
    def node(self) -> expr.Node:
        return expr.parse(self.when)


@dataclass(frozen=True, slots=True)
class Perf:
    """What §8.17's metrics need to know about a protocol.

    Three facts no generic engine can infer, and all a pack has to state:
    which channels carry useful work, and how wide one beat of it is.
    """

    #: Channels whose accepted beats are the bus doing its job. Everything else
    #: — address, response — is overhead that a transfer cycle is measured
    #: against. Empty means "every channel", which is right for a bare
    #: handshake and wrong for AXI, hence the explicit list.
    transfer: tuple[str, ...] = ()
    #: Payload field carrying data. Its width gives bytes per beat.
    data: str | None = None
    #: Payload field of byte enables, if the protocol has them: its population
    #: count is the bytes a beat actually moved, which is what separates
    #: throughput from bus occupancy.
    strobe: str | None = None

    def channels(self, pack: "Pack") -> tuple[str, ...]:
        return self.transfer or tuple(c.name for c in pack.channels)


@dataclass(frozen=True, slots=True)
class Command:
    """One row of §8.20's command-decode table.

    A memory pack has no valid/ready channels: the command bus is sampled every
    clock edge regardless of backpressure, so `encode` is checked at every cycle
    in priority order — the same cascade shape as §8.17's `[[stall_reason]]`,
    reused rather than reinvented. The first row whose `encode` matches wins;
    a cycle that matches none is not a command at all (`cs_n` deasserted).
    """

    name: str
    #: Raw table, kept for `to_dict` and for the command-stream display.
    encode: dict[str, int]
    #: `encode` translated once at load time into `cs_n == 0 && ras_n == 0 && …`.
    when: str
    #: Output field name -> expression evaluated only once `when` holds, e.g.
    #: `col = "a[9:0]"`. §8.20's bit-slice syntax lives in the expression
    #: engine (`expr.Slice`) precisely so this line needs nothing special.
    args: dict[str, str] = field(default_factory=dict)

    @property
    def node(self) -> expr.Node:
        return expr.parse(self.when)


@dataclass(frozen=True, slots=True)
class AddressMap:
    """§8.20's address-map inspector: how a system-side address decomposes
    into (bank, row, col), stated once as expressions over `addr`.

    Not derivable from the observed command stream: the command bus already
    shows the *result* of this decode (a real ACTIVATE names its own bank and
    row directly), but the inspector answers a different question — "what
    would this address map to" — which needs the formula itself, not a replay
    of history. A pack states it once, from the controller's own address
    decode logic or the datasheet's memory map.
    """

    bank: str | None = None
    row: str | None = None
    col: str | None = None


@dataclass(frozen=True, slots=True)
class Port:
    """One direction of §8.19's data path: which transaction kind moves data
    which way, and which payload fields carry it."""

    kind: str
    data: str
    #: Byte enables, if the protocol has them. Absent means "every byte of every
    #: beat was written", which is what a protocol without strobes means.
    strobe: str | None = None


#: Byte address of beat `beat` when a pack does not say otherwise: a plain
#: incrementing burst. Named, because `slots=True` makes the dataclass default
#: unreadable from the class itself.
DEFAULT_BEAT_ADDR = "addr + beat * stride"


@dataclass(frozen=True, slots=True)
class Integrity:
    """§8.19's declaration: what a write is, what a read is, where they land.

    Three facts no generic engine can infer, and the same shape `[perf]` already
    uses for §8.17. Everything else §8.19 needs — byte width, beat count, the
    order writes happened in — the engine reads off the trace.

    `addr` names a *transaction* field (the pack's own `key = "addr = …"`), not a
    signal, so a pack that already lifts an address for `txn(iface, addr=…)`
    needs no second declaration.
    """

    addr: str | None = None
    write: Port | None = None
    read: Port | None = None
    #: Byte address of beat `beat`, given `addr` and `stride` (bytes per beat,
    #: taken from the data signal's width). The default is a plain incrementing
    #: burst, which is what every addressed protocol here does; AXI4 overrides it
    #: to keep FIXED bursts pinned, using the ternary the expression engine
    #: already has.
    beat_addr: str = DEFAULT_BEAT_ADDR

    @property
    def addressed(self) -> bool:
        """§8.19 splits here: with an address the reference memory is a
        dictionary keyed by byte; without one, all you can check is order."""
        return bool(self.addr)


#: `Cover.field` is a DSL key name and shadows `dataclasses.field` inside the
#: class body, so the default factory below needs a name that survives it.
_default = field


@dataclass(frozen=True, slots=True)
class Cover:
    """One §8.21 coverage point. Four shapes, exactly one per entry:

    * `field` — bins over one transaction field;
    * `cross` — the product of two or more of them;
    * `sequence` — the matrix of consecutive pairs of one field;
    * `corner` — a named condition, as an expression over the transaction.

    Corners are expressions rather than engine features on purpose: "a burst
    that crosses a 4 KB boundary" is a fact about AXI, and §8.14's whole bet is
    that the engine knows no protocol names. The same `[[rule]]` environment
    evaluates them.
    """

    field: str | None = None
    cross: tuple[str, ...] = ()
    sequence: str | None = None
    corner: str | None = None
    when: str | None = None
    #: `{ INCR = 1, WRAP = 2 }` or `{ short = "1:3" }`. Empty means "one bin per
    #: value the field's declared width allows", filled in at measure time.
    bins: dict[str, tuple[int, int]] = _default(default_factory=dict)
    msg: str = ""

    @property
    def shape(self) -> str:
        if self.corner:
            return "corner"
        if self.sequence:
            return "sequence"
        if self.cross:
            return "cross"
        return "field"

    @property
    def name(self) -> str:
        return self.corner or self.sequence or self.field or " x ".join(self.cross)

    @property
    def node(self) -> expr.Node:
        return expr.parse(self.when or "0")


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    check: str
    severity: str = "error"
    msg: str = ""
    #: `param.max = 16` — constants the check can refer to as `param.max`.
    params: dict[str, int] = field(default_factory=dict)

    @property
    def node(self) -> expr.Node:
        return expr.parse(self.check)

    @property
    def is_temporal(self) -> bool:
        """Temporal rules are evaluated per clock cycle on raw signals;
        the rest per transaction, at close."""
        return expr.is_temporal(self.node)


@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    version: str
    detect: Detect
    channels: tuple[Channel, ...]
    transactions: tuple[Transaction, ...]
    metrics: dict[str, str] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()
    #: §8.17. A pack without these still extracts transactions; it just has no
    #: opinion about why the bus was idle, and TAB 9 says so rather than
    #: inventing one.
    perf: Perf = field(default_factory=Perf)
    stall_reasons: tuple[StallReason, ...] = ()
    #: §8.20. Present on a memory pack, empty on a protocol pack — the two
    #: families are otherwise the same `Pack` shape and the same `[detect]`.
    commands: tuple[Command, ...] = ()
    address_map: AddressMap | None = None
    #: §8.19. Absent means the scoreboard has nothing to say about this pack and
    #: says so, rather than guessing which of its transactions is a write.
    integrity: Integrity | None = None
    #: §8.21. Empty is not "no coverage": the measurer falls back to automatic
    #: per-field bins over every field the transactions actually carry, which is
    #: what makes a pack written before this feature still produce a matrix.
    cover: tuple[Cover, ...] = ()
    path: Path | None = None
    #: Keys this build did not understand. Surfaced, never silently dropped.
    warnings: tuple[str, ...] = ()

    def channel(self, name: str) -> Channel:
        for c in self.channels:
            if c.name == name:
                return c
        raise PackError(f"pack {self.name}: no channel named `{name}`")

    @property
    def is_memory(self) -> bool:
        """A command-decode pack (§8.20) rather than a handshake protocol —
        what §11.4b's default-tab rule and the engine dispatch on."""
        return bool(self.commands)

    @property
    def slug(self) -> str:
        """Stable short name, e.g. `axi4lite` — used in cache keys and the UI."""
        if self.path is not None:
            return self.path.name[: -len(SUFFIX)] if self.path.name.endswith(SUFFIX) else self.path.stem
        return self.name.lower().replace(" ", "_")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "version": self.version,
            "channels": [c.name for c in self.channels],
            "transactions": [t.name for t in self.transactions],
            "rules": [r.id for r in self.rules],
            "stall_reasons": [s.name for s in self.stall_reasons],
            "commands": [c.name for c in self.commands],
            "cover": [c.name for c in self.cover],
            "integrity": bool(self.integrity),
            "is_memory": self.is_memory,
            "warnings": list(self.warnings),
        }


# --- loading ----------------------------------------------------------------


def _strs(v: Any, where: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return tuple(v)
    raise PackError(f"{where} must be a string or a list of strings")


def _phase(v: Any, where: str) -> Phase | None:
    """`body`/`end`, as either a bare channel name or a table."""
    if v is None:
        return None
    if isinstance(v, str):
        return Phase(channel=v)
    if not isinstance(v, dict):
        raise PackError(f"{where} must be a channel name or a table")
    if "channel" not in v:
        raise PackError(f"{where} needs a `channel`")
    return Phase(
        channel=str(v["channel"]),
        count=_expr_or_none(v.get("count"), f"{where}.count"),
        terminator=(str(v["terminator"]) if v.get("terminator") else None),
        match_on=_expr_or_none(v.get("match_on"), f"{where}.match_on"),
    )


def _expr_or_none(v: Any, where: str) -> str | None:
    """Validate an expression at load time.

    Deliberately eager: a pack is edited by hand and a typo in `match_on` would
    otherwise surface as an interface that mysteriously extracts nothing.
    """
    if v is None:
        return None
    src = str(v)
    try:
        expr.parse(src)
    except expr.ExprError as e:
        raise PackError(f"{where}: {e}") from e
    return src


def _key(v: Any, where: str) -> dict[str, str]:
    """`key = "addr = AW.awaddr"` or `key = { addr = "AW.awaddr" }`."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return {str(k): _expr_or_none(x, f"{where}.{k}") or "" for k, x in v.items()}
    out: dict[str, str] = {}
    for part in _strs(v, where):
        name, sep, rhs = part.partition("=")
        if not sep or not name.strip():
            raise PackError(f"{where}: expected `name = expression`, got {part!r}")
        out[name.strip()] = _expr_or_none(rhs.strip(), f"{where}.{name.strip()}") or ""
    return out


_TOP_KEYS = {
    "name", "version", "detect", "channel", "transaction", "metrics", "rule",
    "perf", "stall_reason", "command", "address_map", "integrity", "cover",
}
_INTEGRITY_KEYS = {"addr", "write", "read", "beat_addr"}
_PORT_KEYS = {"kind", "data", "strobe"}
_COVER_KEYS = {"field", "cross", "sequence", "corner", "when", "bins", "msg"}
_PERF_KEYS = {"transfer", "data", "strobe"}
_STALL_KEYS = {"name", "when", "msg"}
_DETECT_KEYS = {"required_suffixes", "prefix_strip", "clock", "reset"}
_CHANNEL_KEYS = {"name", "valid", "ready", "payload"}
_TXN_KEYS = {"name", "start", "body", "end", "id", "key"}
_RULE_KEYS = {"id", "check", "severity", "msg", "param"}
_COMMAND_KEYS = {"name", "encode", "args"}
_ADDRESS_MAP_KEYS = {"bank", "row", "col"}

_SEVERITIES = {"error", "warn", "warning", "info"}


def _port(v: Any, where: str, kinds: set[str]) -> Port | None:
    if v is None:
        return None
    if not isinstance(v, dict) or "kind" not in v or "data" not in v:
        raise PackError(f"{where} must be a table with `kind` and `data`")
    unknown = [k for k in v if k not in _PORT_KEYS]
    if unknown:
        raise PackError(f"{where}: unknown key `{unknown[0]}`")
    kind = str(v["kind"])
    if kinds and kind not in kinds:
        raise PackError(f"{where}: no transaction named `{kind}`")
    return Port(
        kind=kind,
        data=str(v["data"]),
        strobe=(str(v["strobe"]) if v.get("strobe") else None),
    )


def _bins(v: Any, where: str) -> dict[str, tuple[int, int]]:
    """`{ INCR = 1, short = "1:3" }` — a value or an inclusive range per bin."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise PackError(f"{where} must be a table of `name = value` or `name = \"lo:hi\"`")
    out: dict[str, tuple[int, int]] = {}
    for name, raw in v.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise PackError(f"{where}.{name} must be an integer or a `lo:hi` range")
        if isinstance(raw, int):
            out[str(name)] = (raw, raw)
            continue
        lo, sep, hi = str(raw).partition(":")
        try:
            span = (int(lo, 0), int(hi, 0) if sep else int(lo, 0))
        except ValueError as e:
            raise PackError(f"{where}.{name}: {raw!r} is not a value or a `lo:hi` range") from e
        if span[0] > span[1]:
            raise PackError(f"{where}.{name}: range {raw!r} is inverted")
        out[str(name)] = span
    return out


def loads(text: str, path: Path | None = None) -> Pack:
    """Parse a pack from TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PackError(f"{path or '<pack>'}: {e}") from e
    return _build(data, path)


def load(path: Path | str) -> Pack:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise PackError(f"cannot read pack {p}: {e}") from e
    return loads(text, p)


def _build(data: dict[str, Any], path: Path | None) -> Pack:
    warnings: list[str] = [f"unknown top-level key `{k}`" for k in data if k not in _TOP_KEYS]

    name = str(data.get("name") or (path.name[: -len(SUFFIX)] if path else "pack"))
    det = data.get("detect") or {}
    if not isinstance(det, dict):
        raise PackError(f"{name}: [detect] must be a table")
    warnings += [f"unknown key `detect.{k}`" for k in det if k not in _DETECT_KEYS]

    detect = Detect(
        required_suffixes=_strs(det.get("required_suffixes"), f"{name}: detect.required_suffixes"),
        prefix_strip=_strs(det.get("prefix_strip"), f"{name}: detect.prefix_strip") or ("",),
        clock=str(det.get("clock") or Detect.clock),
        reset=str(det.get("reset") or Detect.reset),
    )
    if not detect.required_suffixes:
        raise PackError(f"{name}: [detect] needs `required_suffixes`, or it matches everything")

    channels: list[Channel] = []
    for c in data.get("channel") or []:
        if not isinstance(c, dict) or "name" not in c or "valid" not in c:
            raise PackError(f"{name}: every [[channel]] needs `name` and `valid`")
        warnings += [f"unknown key `channel.{k}`" for k in c if k not in _CHANNEL_KEYS]
        channels.append(
            Channel(
                name=str(c["name"]),
                valid=str(c["valid"]),
                ready=(str(c["ready"]) if c.get("ready") else None),
                payload=_strs(c.get("payload"), f"{name}: channel {c['name']}.payload"),
            )
        )
    if len({c.name for c in channels}) != len(channels):
        raise PackError(f"{name}: two channels share a name")

    known = {c.name for c in channels}
    transactions: list[Transaction] = []
    for t in data.get("transaction") or []:
        if not isinstance(t, dict) or "name" not in t or "start" not in t:
            raise PackError(f"{name}: every [[transaction]] needs `name` and `start`")
        warnings += [f"unknown key `transaction.{k}`" for k in t if k not in _TXN_KEYS]
        where = f"{name}: transaction {t['name']}"
        txn = Transaction(
            name=str(t["name"]),
            start=str(t["start"]),
            body=_phase(t.get("body"), f"{where}.body"),
            end=_phase(t.get("end"), f"{where}.end"),
            id=(str(t["id"]) if t.get("id") else None),
            key=_key(t.get("key"), f"{where}.key"),
        )
        for phase, label in ((txn.body, "body"), (txn.end, "end")):
            if phase is not None and phase.channel not in known:
                raise PackError(f"{where}.{label}: no channel named `{phase.channel}`")
        if txn.start not in known:
            raise PackError(f"{where}: no channel named `{txn.start}`")
        transactions.append(txn)
    if channels and not transactions:
        raise PackError(f"{name}: a pack needs at least one [[transaction]]")

    metrics_raw = data.get("metrics") or {}
    if not isinstance(metrics_raw, dict):
        raise PackError(f"{name}: [metrics] must be a table")
    metrics = {
        str(k): _expr_or_none(v, f"{name}: metrics.{k}") or "" for k, v in metrics_raw.items()
    }

    rules: list[Rule] = []
    for r in data.get("rule") or []:
        if not isinstance(r, dict) or "id" not in r or "check" not in r:
            raise PackError(f"{name}: every [[rule]] needs `id` and `check`")
        warnings += [f"unknown key `rule.{k}`" for k in r if k not in _RULE_KEYS]
        sev = str(r.get("severity") or "error").lower()
        if sev not in _SEVERITIES:
            raise PackError(f"{name}: rule {r['id']} has unknown severity {sev!r}")
        params = r.get("param") or {}
        if not isinstance(params, dict):
            raise PackError(f"{name}: rule {r['id']}: `param` must be a table")
        rules.append(
            Rule(
                id=str(r["id"]),
                check=_expr_or_none(r["check"], f"{name}: rule {r['id']}.check") or "",
                severity="warn" if sev == "warning" else sev,
                msg=str(r.get("msg") or ""),
                params={str(k): int(v) for k, v in params.items()},
            )
        )
    if len({r.id for r in rules}) != len(rules):
        raise PackError(f"{name}: two rules share an id")

    perf_raw = data.get("perf") or {}
    if not isinstance(perf_raw, dict):
        raise PackError(f"{name}: [perf] must be a table")
    warnings += [f"unknown key `perf.{k}`" for k in perf_raw if k not in _PERF_KEYS]
    transfer = _strs(perf_raw.get("transfer"), f"{name}: perf.transfer")
    for c in transfer:
        if c not in known:
            raise PackError(f"{name}: perf.transfer names no channel `{c}`")
    perf = Perf(
        transfer=transfer,
        data=(str(perf_raw["data"]) if perf_raw.get("data") else None),
        strobe=(str(perf_raw["strobe"]) if perf_raw.get("strobe") else None),
    )

    stalls: list[StallReason] = []
    for s in data.get("stall_reason") or []:
        if not isinstance(s, dict) or "name" not in s or "when" not in s:
            raise PackError(f"{name}: every [[stall_reason]] needs `name` and `when`")
        warnings += [f"unknown key `stall_reason.{k}`" for k in s if k not in _STALL_KEYS]
        sname = str(s["name"])
        if sname in RESERVED_BUCKETS:
            raise PackError(
                f"{name}: `{sname}` is one of the buckets the engine fills in "
                f"({', '.join(RESERVED_BUCKETS)}); pick another name"
            )
        stalls.append(
            StallReason(
                name=sname,
                when=_expr_or_none(s["when"], f"{name}: stall_reason {sname}.when") or "",
                msg=str(s.get("msg") or ""),
            )
        )
    if len({s.name for s in stalls}) != len(stalls):
        raise PackError(f"{name}: two stall reasons share a name")

    # §8.20: a memory pack decodes a command bus instead of assembling
    # transactions from valid/ready channels, so it needs neither. Each row's
    # `encode` becomes one cascade rung — `cs_n == 0 && ras_n == 0 && …` — in
    # priority order, the same shape `[[stall_reason]]` already uses.
    commands: list[Command] = []
    for c in data.get("command") or []:
        if not isinstance(c, dict) or "name" not in c or "encode" not in c:
            raise PackError(f"{name}: every [[command]] needs `name` and `encode`")
        warnings += [f"unknown key `command.{k}`" for k in c if k not in _COMMAND_KEYS]
        cname = str(c["name"])
        where = f"{name}: command {cname}"
        encode = c["encode"]
        if not isinstance(encode, dict) or not encode:
            raise PackError(f"{where}.encode must be a non-empty table")
        try:
            encode = {str(sig): int(val) for sig, val in encode.items()}
        except (TypeError, ValueError) as e:
            raise PackError(f"{where}.encode: values must be integers") from e
        when = " && ".join(f"{sig} == {val}" for sig, val in encode.items())
        args_raw = c.get("args") or {}
        if not isinstance(args_raw, dict):
            raise PackError(f"{where}.args must be a table")
        args = {
            str(k): _expr_or_none(v, f"{where}.args.{k}") or "" for k, v in args_raw.items()
        }
        commands.append(
            Command(name=cname, encode=encode, when=_expr_or_none(when, f"{where}.encode") or when, args=args)
        )
    if len({c.name for c in commands}) != len(commands):
        raise PackError(f"{name}: two commands share a name")

    if not channels and not commands:
        raise PackError(f"{name}: a pack needs at least one [[channel]] or [[command]]")

    # §8.19. Validated against the transactions declared above, so a pack that
    # renames a transaction and forgets this table fails at load rather than
    # producing a scoreboard that silently watches nothing.
    integrity = None
    integ_raw = data.get("integrity")
    if integ_raw is not None:
        if not isinstance(integ_raw, dict):
            raise PackError(f"{name}: [integrity] must be a table")
        warnings += [f"unknown key `integrity.{k}`" for k in integ_raw if k not in _INTEGRITY_KEYS]
        kinds = {t.name for t in transactions}
        integrity = Integrity(
            addr=(str(integ_raw["addr"]) if integ_raw.get("addr") else None),
            write=_port(integ_raw.get("write"), f"{name}: integrity.write", kinds),
            read=_port(integ_raw.get("read"), f"{name}: integrity.read", kinds),
            beat_addr=(
                _expr_or_none(integ_raw.get("beat_addr"), f"{name}: integrity.beat_addr")
                or DEFAULT_BEAT_ADDR
            ),
        )
        if integrity.write is None and integrity.read is None:
            raise PackError(f"{name}: [integrity] needs a `write`, a `read`, or both")

    # §8.21. Exactly one shape per entry: a `[[cover]]` naming both a field and
    # a cross would have two readings and neither is obviously right.
    covers: list[Cover] = []
    for c in data.get("cover") or []:
        if not isinstance(c, dict):
            raise PackError(f"{name}: every [[cover]] must be a table")
        warnings += [f"unknown key `cover.{k}`" for k in c if k not in _COVER_KEYS]
        shapes = [k for k in ("field", "cross", "sequence", "corner") if c.get(k)]
        if len(shapes) != 1:
            raise PackError(
                f"{name}: every [[cover]] needs exactly one of "
                f"`field`, `cross`, `sequence`, `corner`; got {len(shapes)}"
            )
        where = f"{name}: cover {c[shapes[0]]}"
        cross = _strs(c.get("cross"), f"{where}.cross")
        if shapes[0] == "cross" and len(cross) < 2:
            raise PackError(f"{where}: a cross needs at least two fields")
        if shapes[0] == "corner" and not c.get("when"):
            raise PackError(f"{where}: a corner needs a `when`")
        if shapes[0] != "corner" and c.get("when"):
            raise PackError(f"{where}: `when` belongs to a corner, not a {shapes[0]}")
        covers.append(
            Cover(
                field=(str(c["field"]) if c.get("field") else None),
                cross=cross,
                sequence=(str(c["sequence"]) if c.get("sequence") else None),
                corner=(str(c["corner"]) if c.get("corner") else None),
                when=_expr_or_none(c.get("when"), f"{where}.when"),
                bins=_bins(c.get("bins"), f"{where}.bins"),
                msg=str(c.get("msg") or ""),
            )
        )
    if len({c.name for c in covers}) != len(covers):
        raise PackError(f"{name}: two [[cover]] entries share a name")

    address_map = None
    am_raw = data.get("address_map")
    if am_raw is not None:
        if not isinstance(am_raw, dict):
            raise PackError(f"{name}: [address_map] must be a table")
        warnings += [f"unknown key `address_map.{k}`" for k in am_raw if k not in _ADDRESS_MAP_KEYS]
        address_map = AddressMap(
            bank=_expr_or_none(am_raw.get("bank"), f"{name}: address_map.bank"),
            row=_expr_or_none(am_raw.get("row"), f"{name}: address_map.row"),
            col=_expr_or_none(am_raw.get("col"), f"{name}: address_map.col"),
        )

    return Pack(
        name=name,
        version=str(data.get("version") or "0"),
        detect=detect,
        channels=tuple(channels),
        transactions=tuple(transactions),
        metrics=metrics,
        rules=tuple(rules),
        perf=perf,
        stall_reasons=tuple(stalls),
        commands=tuple(commands),
        address_map=address_map,
        integrity=integrity,
        cover=tuple(covers),
        path=path,
        warnings=tuple(warnings),
    )


# --- discovery --------------------------------------------------------------


def builtin_dir() -> Path:
    """The packs shipped with the tool."""
    return Path(__file__).resolve().parent / "packs"


def search_path(project_root: Path | str | None = None) -> list[Path]:
    """Directories to look in, nearest first.

    A project's own `packs/` shadows the built-ins by file name, so a house
    variant of `axi4.vtp.toml` is a file you drop next to the RTL rather than a
    fork of the tool.
    """
    out: list[Path] = []
    if project_root is not None:
        root = Path(project_root)
        out += [d for d in (root / "packs", root) if d.is_dir()]
    out.append(builtin_dir())
    return out


def discover(
    project_root: Path | str | None = None, errors: list[str] | None = None
) -> list[Pack]:
    """Every pack on the search path, project ones shadowing built-ins.

    One broken pack must not hide the other eleven (P7) — but it must not
    disappear either, or a pack with a typo looks exactly like a protocol the
    design does not use. Failures go into `errors` when the caller offers a
    list, and are raised only when nothing loaded at all.
    """
    seen: dict[str, Pack] = {}
    failed: list[str] = []
    for d in search_path(project_root):
        for f in sorted(d.glob(f"*{SUFFIX}")):
            slug = f.name[: -len(SUFFIX)]
            if slug in seen:
                continue
            try:
                seen[slug] = load(f)
            except PackError as e:
                failed.append(str(e))
    packs = list(seen.values())
    if failed and not packs:
        raise PackError("; ".join(failed))
    if errors is not None:
        errors.extend(failed)
    return packs


def resolve(
    names: Iterable[str],
    project_root: Path | str | None = None,
    errors: list[str] | None = None,
) -> list[Pack]:
    """Packs named by slug, by file name or by path.

    An empty selection means "every pack on the search path", which is what
    automatic detection wants: the point of §8.14 is that nobody has to say
    which protocol their design speaks.
    """
    names = [str(n) for n in names]
    if not names:
        return discover(project_root, errors)
    available = {p.slug: p for p in discover(project_root, errors)}
    out: list[Pack] = []
    for n in names:
        p = Path(n)
        if p.is_file():
            out.append(load(p))
            continue
        slug = n[: -len(SUFFIX)] if n.endswith(SUFFIX) else n
        if slug not in available:
            raise PackError(
                f"no protocol pack named {n!r}; available: {', '.join(sorted(available)) or 'none'}"
            )
        out.append(available[slug])
    return out
