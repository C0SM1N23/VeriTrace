"""Reading a cocotb monitor's log — §8.34.

The bar this has to clear is stated in the spec: *"nu le ceri sa scrie un
protocol pack — le ceri un regex de o linie peste logul pe care il au deja"*. So
the parser is one configurable regex per stream, the same mechanism §8.10b already
uses for triage, and the only thing it must understand is named groups.

Four names are special and everything else becomes a transaction field:

    iface   which interface this line is about   (default: the pattern's name)
    kind    READ / WRITE / whatever the monitor calls it
    index   the monitor's own ordinal, if it has one
    end     a closing line for a transaction opened earlier

A monitor that logs one line per completed transaction needs none of them beyond
`kind`. A monitor that logs a start and an end uses `end` and gets latency for
free, because latency is `end_time - start_time` and nothing downstream cares
that a human wrote the line.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from veritrace.protocol.model import Extraction, Transaction
from veritrace.ingest import model as m

#: cocotb's real log line, which is what a monitor written in an afternoon
#: actually produces:
#:
#:     320.00ns INFO  cocotb.axil_slave  axi_mon WRITE_BEGIN addr=0x4 data=0xa7
#:     340.00ns INFO  cocotb.axil_slave  axi_mon WRITE_END   addr=0x4 resp=0
#:
#: A dict rather than one pattern, and in this order, because a line is
#: attributed to the first pattern that matches — the same rule §8.10b uses, so
#: a specific form wins over the catch-all. `.veritrace.toml` layers user
#: patterns on top under `[ingest] patterns`.
DEFAULTS = {
    # A monitor that logs a start and an end. `end` is present only on the
    # closing line, and its presence is what pairs the two — which is how an
    # ingested transaction gets a latency without the monitor computing one.
    "phase": (
        r"^\s*(?P<time>[\d.]+)\s*(?P<unit>[fpnum]?s)\s+\w+\s+\S+\s+"
        r"(?P<iface>\w+)\s+(?P<kind>[A-Z][A-Z0-9]*)_(?:BEGIN|(?P<end>END))\b(?P<rest>.*)$"
    ),
    # A monitor that logs one line per completed transaction. At least one
    # `key=value` is required, and that is not cosmetic: without it this pattern
    # also swallows every status line a monitor prints — `DONE`, `RESET`, `PASS`
    # — and each becomes a transaction with no fields and no duration. A
    # transaction carries data; a status line does not.
    "single": (
        r"^\s*(?P<time>[\d.]+)\s*(?P<unit>[fpnum]?s)\s+\w+\s+\S+\s+"
        r"(?P<iface>\w+)\s+(?P<kind>[A-Z][A-Z0-9_]*)\b(?P<rest>\s+\w+\s*=.*)$"
    ),
}

#: `addr=0x1000`, `len = 4`, `data=0xdead_beef` — how a monitor writes a field.
FIELD = re.compile(r"(\w+)\s*=\s*(0x[0-9a-fA-F_]+|-?\d+|\w+)")

_UNITS = {"fs": 1, "ps": 1_000, "ns": 1_000_000, "us": 1_000_000_000, "ms": 10**12, "s": 10**15}


def _fs(value: str, unit: str) -> int:
    return int(float(value) * _UNITS.get(unit, _UNITS["ns"]))


def _ticks(fs: int, timescale: str) -> int:
    """Femtoseconds into the trace's own ticks.

    A log is written in wall-clock simulation time and a trace is indexed in
    ticks; ingesting without converting would put every transaction at the wrong
    cycle, and every latency in a unit nothing else uses.
    """
    m_ = re.match(r"(\d+)\s*([fpnum]?s)", timescale or "1ns")
    if not m_:
        return fs
    per_tick = int(m_.group(1)) * _UNITS.get(m_.group(2), _UNITS["ns"])
    return fs // max(1, per_tick)


def _value(text: str) -> Any:
    text = text.replace("_", "")
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return text


def parse(
    text: str,
    patterns: dict[str, str] | None = None,
    timescale: str = "1ns",
) -> list[Extraction]:
    """Every transaction the log records, grouped by interface.

    A line matching no pattern is skipped in silence — a cocotb log is mostly
    cocotb's own chatter, and reporting each unmatched line would bury the ones
    that did match.
    """
    compiled = []
    for name, pattern in (patterns or DEFAULTS).items():
        try:
            compiled.append((name, re.compile(pattern)))
        except re.error as e:
            raise ValueError(f"pattern {name!r} is not a valid regex: {e}") from None

    streams: dict[str, list[Transaction]] = {}
    #: (iface, kind, index) -> transactions still waiting for their end line, in
    #: the order they opened. A list rather than one entry because a monitor on a
    #: pipelined interface has several in flight, and an end line with no index
    #: closes the oldest — which is what an in-order protocol does.
    open_txns: dict[tuple[str, str, Any], deque[Transaction]] = {}

    for line in text.splitlines():
        for name, rx in compiled:
            found = rx.search(line)
            if found is None:
                continue
            got = {k: v for k, v in found.groupdict().items() if v is not None}
            at = _ticks(_fs(got.get("time", "0"), got.get("unit", "ns")), timescale)
            iface = got.get("iface", name)
            kind = (got.get("kind") or "TXN").upper()

            fields = {
                k: _value(v)
                for k, v in got.items()
                if k not in ("time", "unit", "iface", "kind", "index", "end", "rest")
            }
            fields |= {k: _value(v) for k, v in FIELD.findall(got.get("rest", ""))}

            # Whether the *pattern* has an `end` group decides what a line means:
            # with one, the monitor logs phases and this line is an open or a
            # close; without one, every line is a transaction that already
            # finished. Reading it off the match instead would make a
            # single-line monitor's transactions all look permanently open.
            phased = "end" in rx.groupindex
            if not phased:
                txn = Transaction(iface=iface, kind=kind, index=0, start_time=at,
                                  end_time=at, fields=fields)
                streams.setdefault(iface, []).append(txn)
                break

            key = (iface, kind, got.get("index"))
            closing = "end" in got
            if closing and open_txns.get(key):
                # A closing line for something already open: fill in the end and
                # merge whatever fields it carried (a response code, usually).
                txn = open_txns[key].popleft()
                txn.end_time = at
                txn.fields |= fields
                break

            # An opening line, or a closing one with nothing open — which is a
            # transaction that started before the log did, and is recorded as
            # complete rather than dropped.
            txn = Transaction(
                iface=iface, kind=kind, index=0, start_time=at,
                end_time=at if closing else None, fields=fields,
            )
            streams.setdefault(iface, []).append(txn)
            if not closing:
                open_txns.setdefault(key, deque()).append(txn)
            break

    return [m.extraction("cocotb", iface, m.order(txns)) for iface, txns in sorted(streams.items())]
