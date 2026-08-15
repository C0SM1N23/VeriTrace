"""VTQ — the query language of §10.1.

Two grammars, because §10.1 has two shapes and pretending otherwise would make
both worse:

* `why(...)` has its own syntax — a signal, an optional `== value`, an optional
  `@ time` — and now also §8.16's transaction form,
  `why(txn.dma0.WRITE[128].not_issued)`.
* Everything else is `name(arg, key=value) | name(arg)`: `txn(iface, type=WRITE)`,
  `txn(iface) | slowest(10)`, and — when §8.17 and §8.20 arrive — `stalls(iface)`,
  `deadlock()`, `banks(sdram)`, `timing(sdram, chip=mt48lc16m16a2)`.

The second grammar is parsed generically into `Call(name, args, kwargs)` rather
than one regex per command, so those later commands are a dispatch entry and not
a parser change. That is the same bet §8.14 makes about protocols: get the shape
right once and the rest is data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "QueryError",
    "WhyQuery",
    "TxnRef",
    "Call",
    "Pipeline",
    "parse",
    "parse_pipeline",
    "ASPECTS",
]


class QueryError(ValueError):
    pass


#: What a transaction-level `why` can ask about. `not_issued` is §8.16's own
#: example; `not_completed` is the other half of the same question and the one
#: §8.18 will lean on.
ASPECTS = ("not_issued", "not_completed")


@dataclass(slots=True)
class TxnRef:
    """`txn.dma0.WRITE[128].not_issued` — §8.16."""

    iface: str
    kind: str
    index: int
    aspect: str = "not_issued"

    @property
    def ref(self) -> str:
        return f"{self.iface}.{self.kind}[{self.index}]"

    def __str__(self) -> str:
        return f"txn.{self.ref}.{self.aspect}"


#: why(top.ctrl.ready == 0 @ c1247) — value and time are both optional-ish:
#: the value is informational (the trace already says what it is) and the time
#: may be a raw timestamp or a cycle of the primary clock (§5.5, problem 3).
_WHY = re.compile(
    r"""^\s*why\s*\(\s*
        (?P<signal>[\w.\[\]$\\]+)\s*
        (?:(?P<op>==|!=)\s*(?P<value>[^@)]+?)\s*)?
        (?:@\s*(?P<cycle>c)?(?P<time>-?\d+)\s*)?
    \)\s*$""",
    re.X | re.I,
)

#: The same question without the wrapper, which is how §13 writes it on the
#: command line: `veritrace why dump.fst --rtl src/ "top.ctrl.ready == 0 @ c1247"`.
#: Requiring `why(...)` there meant the spec's own example was rejected — while
#: the error message quoted it back as the way to do it.
_BARE = re.compile(
    r"""^\s*(?P<signal>[\w.\[\]$\\]+)\s*
        (?:(?P<op>==|!=)\s*(?P<value>[^@]+?)\s*)?
        (?:@\s*(?P<cycle>c)?(?P<time>-?\d+)\s*)?$""",
    re.X | re.I,
)

#: txn.<iface>.<KIND>[<n>][.<aspect>], where the interface may itself be dotted.
_TXN_TARGET = re.compile(
    r"^txn\.(?P<iface>[\w.]+?)\.(?P<kind>[A-Za-z_]\w*)\[(?P<index>\d+)\]"
    r"(?:\.(?P<aspect>\w+))?$"
)


@dataclass(slots=True)
class WhyQuery:
    signal: str
    value: str | None = None
    op: str = "=="
    time: int | None = None
    #: True when the time was written as `c1247`, i.e. a clock cycle.
    is_cycle: bool = False
    #: Set instead of a plain signal for §8.16's transaction form. `signal`
    #: still holds the original text, so error messages quote what was typed.
    txn: TxnRef | None = None


def _txn_ref(text: str) -> TxnRef | None:
    m = _TXN_TARGET.match(text)
    if not m:
        return None
    aspect = (m.group("aspect") or "not_issued").lower()
    if aspect not in ASPECTS:
        raise QueryError(
            f"unknown transaction question {aspect!r}; try {' or '.join(ASPECTS)}"
        )
    return TxnRef(
        iface=m.group("iface"),
        kind=m.group("kind"),
        index=int(m.group("index")),
        aspect=aspect,
    )


def _looks_bare(text: str) -> bool:
    """Whether `text` is §13's unwrapped question rather than a typo.

    A hierarchical path, a time or a comparison — any one of them means someone
    asked something. A lone word does not: `veritrace why why` should be an
    error about the query, not a hunt for a signal called `why`.
    """
    if "(" in text or "|" in text:
        return False  # some other query form, with its own parser and errors
    return "." in text or "@" in text or "==" in text or "!=" in text


def parse(text: str) -> WhyQuery:
    """Parse a `why(...)` query, at signal or transaction level.

    The bare form of §13 (`top.ctrl.ready == 0 @ c1247`) is accepted too, but
    only when it cannot be another kind of query — `stalls(m0)` and friends have
    to keep reaching their own parsers with their own error messages.
    """
    m = _WHY.match(text or "")
    if m is None and _looks_bare(text or ""):
        m = _BARE.match(text)
    if not m:
        raise QueryError(
            "Only `why(signal @ time)` and `why(txn.iface.TYPE[n].not_issued)` "
            "are supported, for example: why(top.ctrl.ready == 0 @ c1247)"
        )
    t = m.group("time")
    signal = m.group("signal")
    return WhyQuery(
        signal=signal,
        value=(m.group("value") or "").strip() or None,
        op=m.group("op") or "==",
        time=int(t) if t is not None else None,
        is_cycle=bool(m.group("cycle")),
        txn=_txn_ref(signal) if signal.startswith("txn.") else None,
    )


def expectation(q: WhyQuery, observed: str) -> str | None:
    """The note to print when the question assumed a value the trace disagrees with.

    §8.1 carries `expected` for a reason: asking why a signal is 0 when it was
    never 0 is a different question, and answering the one that *was* true
    without a word is how someone spends an afternoon reading the wrong chain.
    `None` means the question and the trace agree, or no value was given.
    """
    if q.value is None or not observed:
        return None
    want = _bits(q.value, len(observed))
    got = observed.strip().lower()
    if want is None:
        return None
    matches = (want == got) if q.op == "==" else (want != got)
    if matches:
        return None
    verb = "is not" if q.op == "==" else "is"
    return f"note: the question assumed {q.signal} {verb} {q.value}; the trace says {observed}"


def _bits(text: str, width: int) -> str | None:
    """A written value as canonical binary digits, or None if it is not one."""
    t = text.strip().lower().replace("_", "")
    if not t:
        return None
    if "'" in t:  # 4'b1010, 8'hff, 5'd9
        _, _, rest = t.partition("'")
        base, digits = (rest[0], rest[1:]) if rest[:1] in "bodh" else ("d", rest)
    elif t.startswith("0x"):
        base, digits = "h", t[2:]
    elif t.startswith("0b"):
        base, digits = "b", t[2:]
    else:
        base, digits = "d", t
    try:
        if base == "b":
            # x and z survive as themselves: comparing them numerically would be
            # a lie, and they are exactly the values worth asking about.
            return digits.rjust(width, "0") if set(digits) <= set("01xz") else None
        n = int(digits, {"h": 16, "o": 8, "d": 10}[base])
    except (ValueError, KeyError):
        return None
    return format(n, "b").rjust(width, "0")


# --- the general call form ---------------------------------------------------


@dataclass(slots=True)
class Call:
    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Pipeline:
    """`txn(dma0, type=WRITE) | slowest(10)` — a source and its refinements."""

    source: Call
    stages: list[Call] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.source.name


_CALL = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*\((?P<body>.*)\)\s*$", re.S)
#: `0x4000:0x5000` — an inclusive range, the form §10.1 uses for addresses.
_RANGE = re.compile(r"^(?P<lo>-?(?:0[xXbB])?[0-9a-fA-F_]+)\s*:\s*(?P<hi>-?(?:0[xXbB])?[0-9a-fA-F_]+)$")


def _value(text: str) -> Any:
    """A query argument: number, `lo:hi` range, quoted string, or bare word."""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    m = _RANGE.match(s)
    if m:
        return (_number(m.group("lo")), _number(m.group("hi")))
    n = _number(s)
    return s if n is None else n


def _number(s: str) -> int | None:
    try:
        return int(s.replace("_", ""), 0)
    except ValueError:
        return None


def _split(body: str) -> list[str]:
    """Split on commas that are not inside brackets or quotes."""
    out: list[str] = []
    depth, quote, cur = 0, "", ""
    for ch in body:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch in "([{":
            depth, cur = depth + 1, cur + ch
        elif ch in ")]}":
            depth, cur = depth - 1, cur + ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p for p in (x.strip() for x in out) if p]


def _call(text: str) -> Call:
    m = _CALL.match(text)
    if not m:
        name = text.strip()
        if name.isidentifier():
            raise QueryError(f"`{name}` is a command; write it as `{name}()`")
        raise QueryError(f"cannot parse {text.strip()!r} as a query")
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for part in _split(m.group("body")):
        key, sep, val = part.partition("=")
        if sep and key.strip().isidentifier() and not val.startswith("="):
            kwargs[key.strip()] = _value(val)
        else:
            args.append(_value(part))
    return Call(name=m.group("name").lower(), args=tuple(args), kwargs=kwargs)


def _top_level_pipes(text: str) -> list[str]:
    out: list[str] = []
    depth, quote, cur = 0, "", ""
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch in "([{":
            depth, cur = depth + 1, cur + ch
        elif ch in ")]}":
            depth, cur = depth - 1, cur + ch
        elif ch == "|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p for p in (x.strip() for x in out) if p]


def parse_pipeline(text: str) -> Pipeline:
    """Parse `name(args) | stage(args) | ...`."""
    parts = _top_level_pipes(text or "")
    if not parts:
        raise QueryError("empty query")
    return Pipeline(source=_call(parts[0]), stages=[_call(p) for p in parts[1:]])
