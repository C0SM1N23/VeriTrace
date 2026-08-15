"""Reading a `uvm_text_tr_database` — §8.34.

UVM's free, text-based transaction recorder writes one line per event:

    CREATE_STREAM @0 {NAME:mon STREAM:2 SCOPE:uvm_test_top.env.agt TYPE:TVM}
    BEGIN @1000 {TXH:7 STREAM:2 NAME:write}
    SET_ATTR @1000 {TXH:7 NAME:addr VALUE:'h1000 RADIX:UVM_HEX BITS:32}
    END @1500 {TXH:7}

That is the whole grammar. It is parsed straight through rather than via a pack,
because the transactions are already transactions — §8.14's job was to *find*
them in wires, and here somebody's monitor already did.
"""

from __future__ import annotations

import re
from typing import Any

from veritrace.ingest import model as m
from veritrace.protocol.model import Extraction, Transaction

#: `BEGIN @1000 {TXH:7 STREAM:2 NAME:write}` — verb, time, and a brace-delimited
#: attribute list.
_LINE = re.compile(r"^\s*(\w+)\s+@(-?\d+)\s*\{(.*)\}\s*$")
#: `NAME:write` / `VALUE:'h1000`. Values run to the next `KEY:` or the end, so a
#: value containing a space survives.
_ATTR = re.compile(r"(\w+):(.*?)(?=\s+\w+:|$)")

#: UVM writes Verilog literals (`'h1f`, `32'd7`) and plain decimals.
_LITERAL = re.compile(r"^(?:\d+)?'([bodh])([0-9a-fA-FxXzZ_]+)$", re.I)
_BASE = {"b": 2, "o": 8, "d": 10, "h": 16}


def _value(text: str) -> Any:
    text = text.strip()
    lit = _LITERAL.match(text)
    if lit:
        digits = lit.group(2).replace("_", "")
        try:
            return int(digits, _BASE[lit.group(1).lower()])
        except ValueError:
            return text  # 'hxx — unknown, and saying so beats guessing zero
    try:
        return int(text, 0)
    except ValueError:
        return text


def parse(text: str, timescale_ratio: int = 1) -> list[Extraction]:
    """Every transaction in the database, grouped by the stream that recorded it.

    `timescale_ratio` scales UVM's time unit into the trace's ticks; it is 1 when
    both are the same, which is the common case because both usually come from
    the same simulation.
    """
    streams: dict[int, str] = {}
    #: TXH -> (stream id, transaction). UVM interleaves several open at once.
    live: dict[str, tuple[int, Transaction]] = {}
    out: dict[str, list[Transaction]] = {}

    for line in text.splitlines():
        found = _LINE.match(line)
        if found is None:
            continue
        verb, at, body = found.group(1).upper(), int(found.group(2)), found.group(3)
        attrs = {k: v.strip() for k, v in _ATTR.findall(body)}
        at *= timescale_ratio

        match verb:
            case "CREATE_STREAM":
                sid = attrs.get("STREAM") or attrs.get("ID") or "0"
                # The scope is the monitor's path in the testbench, which is a
                # far better interface name than "stream 2".
                streams[int(sid)] = attrs.get("SCOPE") or attrs.get("NAME") or f"stream{sid}"
            case "BEGIN":
                txh = attrs.get("TXH")
                if txh is None:
                    continue
                sid = int(attrs.get("STREAM", 0))
                iface = streams.get(sid, f"stream{sid}")
                txn = Transaction(
                    iface=iface,
                    kind=(attrs.get("NAME") or "TXN").upper(),
                    index=0,
                    start_time=at,
                )
                live[txh] = (sid, txn)
                out.setdefault(iface, []).append(txn)
            case "SET_ATTR":
                entry = live.get(attrs.get("TXH", ""))
                if entry and "NAME" in attrs:
                    entry[1].fields[attrs["NAME"]] = _value(attrs.get("VALUE", ""))
            case "END":
                entry = live.pop(attrs.get("TXH", ""), None)
                if entry:
                    entry[1].end_time = at

    return [
        m.extraction("uvm", iface, m.order(txns), scope=iface)
        for iface, txns in sorted(out.items())
    ]
