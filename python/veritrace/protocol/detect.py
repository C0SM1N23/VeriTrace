"""Step 1 of §8.14: find the interfaces a design actually has.

*For every scope, look for signals satisfying `[detect]`. Result: the list of
interfaces found, with their prefix.*

Nobody should have to tell the tool that their design speaks AXI. That is the
whole reason `[detect]` is part of the pack rather than something configured per
project — and it is what makes §11.4b's "two or more interfaces -> open on
Transactions" possible at all, because the count has to exist before the first
tab is chosen.

Two rules decide what happens when more than one answer is available:

* **Specificity wins.** A generic valid/ready pack matches every AXI channel
  too. Ranking by how many signals a pack demands puts AXI4 above AXI4-Lite
  above handshake, with no priority numbers for pack authors to keep in sync.
* **The same wires are one interface.** A master port and the slave port it
  drives carry different names for the same nets, and the store already knows
  they share an event stream. Extracting both would double every transaction,
  so they collapse into one interface that remembers its other names.
"""

from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from typing import Any, Iterable

from veritrace.protocol.model import Interface
from veritrace.protocol.pack import Channel, Pack


def _scope_index(store: Any) -> dict[str, dict[str, Any]]:
    """`scope -> {lowercased leaf name: signal}`.

    Lowercased because dumps disagree about case (VHDL-sourced VCDs upcase
    everything) and a pack should not have to spell a protocol twice.
    """
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for s in store.signals():
        # Bit- and word-selects of a vector are the same signal; the whole
        # vector is what a payload refers to.
        if s.array_index is not None:
            continue
        out[s.scope].setdefault(s.name.lower(), s)
    return out


def _ancestors(scope: str) -> list[str]:
    """`a.b.c` -> `["a.b.c", "a.b", "a"]`."""
    parts = scope.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1)]


def _find_named(
    index: dict[str, dict[str, Any]], scope: str, pattern: str, prefix: str
) -> str | None:
    """A clock or reset for this interface: in its own scope first, then up.

    Prefixed candidates are preferred (`m_axi_aclk` over a bare `clk` that
    happens to be in the same scope) because a design with two clock domains has
    both, and picking the wrong one shifts every cycle number this interface
    reports.
    """
    try:
        rx = re.compile(f"(?:{pattern})", re.IGNORECASE)
    except re.error:
        return None
    for anc in _ancestors(scope):
        names = index.get(anc) or {}
        hits = [s for n, s in names.items() if rx.fullmatch(n) or rx.fullmatch(n.removeprefix(prefix.lower()))]
        if not hits:
            continue
        hits.sort(key=lambda s: (not s.name.lower().startswith(prefix.lower()), len(s.name), s.name))
        return hits[0].path
    return None


def configured_signal(store: Any, path: str | None) -> str | None:
    """Resolve a configured clock/reset path against this design.

    Configuration normally carries the full hierarchy, but ``init`` can only
    infer the declaration name before a trace is available.  A unique suffix
    therefore remains useful; an ambiguous suffix is deliberately refused
    rather than attaching the wrong reset to every protocol interface (P1).
    """
    if not path:
        return None
    handle = store.find(path)
    if handle is not None:
        try:
            # Trace stores return an integer handle; the RTL adapter returns the
            # path itself and its ``signal`` is an identity-class view without a
            # path.  Preserve the configured spelling in that case.
            signal = store.signal(handle)
            resolved = getattr(signal, "path", None)
            if resolved:
                return str(resolved)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        return path

    suffix = "." + path
    matches = [s.path for s in store.signals() if s.path == path or s.path.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


#: `prefix_strip = ["*"]` — infer the prefix from the design instead of listing
#: it. A generic pack cannot enumerate what people call their buses, and without
#: this `handshake.vtp` would only ever match a signal literally named `valid`.
INFER = "*"


def _prefixes(names: dict[str, Any], pack: Pack) -> list[str]:
    """The prefixes to try for this pack in this scope, in order."""
    out: list[str] = []
    for p in pack.detect.prefix_strip:
        if p != INFER:
            out.append(p)
            continue
        anchor = pack.detect.required_suffixes[0].lower()
        out += sorted(
            {n[: -len(anchor)] for n in names if n.endswith(anchor) and len(n) > len(anchor)}
        )
    # Longest first: given `push_valid`, `push_` is the intended reading and a
    # shorter accidental match would claim the same wires under a worse name.
    return sorted(dict.fromkeys(out), key=lambda p: (-len(p), p))


def _match(names: dict[str, Any], pack: Pack, prefix: str) -> dict[str, str] | None:
    """Resolve every pack signal name under `prefix`, or `None` if incomplete.

    Required suffixes gate the match; payload fields are looked up too but may
    be absent — a design that does not carry `awcache` is still AXI4, and the
    field simply has no value (which the expression layer reports as unknown
    rather than as zero).
    """
    resolved: dict[str, str] = {}
    for suffix in pack.detect.required_suffixes:
        sig = names.get((prefix + suffix).lower())
        if sig is None:
            return None
        resolved[suffix] = sig.path
    for ch in pack.channels:
        for suffix in ch.signals:
            sig = names.get((prefix + suffix).lower())
            if sig is not None:
                resolved[suffix] = sig.path
    return resolved


def _interface_name(scope: str, prefix: str) -> str:
    """`tb.dut` + `s_axi_` -> `dut.s_axi`; a bare prefix falls back to the scope.

    Short enough to type in `txn(dma0)` and specific enough to stay unique in a
    design with one interface per master.
    """
    leaf = scope.rsplit(".", 1)[-1] if scope else "top"
    stem = prefix.strip("_")
    return f"{leaf}.{stem}" if stem else leaf


def _handshake_names(ch: Any) -> list[str]:
    """The signals a channel's handshake is made of.

    Usually just `valid` and `ready`. A protocol with no valid/ready pair spells
    its handshake as an expression — AHB's `htrans[1]`, Wishbone's `cyc && stb` —
    and then the identity of the interface is the signals that expression reads.
    Taking the expression text as a name would find nothing, and two views of one
    bus would be reported as two buses.
    """
    from veritrace.protocol import expr

    out: list[str] = []
    for spec in (ch.valid, ch.ready):
        if not spec:
            continue
        try:
            node = expr.parse(spec)
        except expr.ExprError:
            out.append(spec)
            continue
        out.extend(sorted(expr.identifiers(node)) if not isinstance(node, expr.Name) else [spec])
    return out


def _identity(store: Any, iface: Interface) -> frozenset[int]:
    """The event streams of an interface's handshake signals.

    An interface *is* its handshake: two names for the same `valid`/`ready`
    pairs observe the same transfers, whatever the payload is called on either
    side. The payload deliberately does not take part, because dumps disagree
    about it — Icarus aliases scalar nets across scopes but gives each scope's
    view of a vector its own code, so a master port and the slave port it drives
    share every `awvalid` but no `awaddr`. Keying on the payload would report
    one bus three times.

    A memory pack (§8.20) has no channels at all — the command bus is sampled
    every cycle, not gated by valid/ready — so its identity is the *command
    encoding* instead: `cs_n`, `ras_n`, `cas_n`, `we_n`. That is the exact
    analogue, and for the exact same reason. Those pins are scalars, so a dump
    aliases them across scopes; keying on `ba`/`a` as well would fail to
    collapse a controller's ports with the testbench wires they drive (a
    vector gets its own code per scope) and report one command bus twice.
    """
    out: set[int] = set()
    if iface.pack.channels:
        paths = [
            p
            for ch in iface.pack.channels
            for name in _handshake_names(ch)
            if (p := iface.signals.get(name))
        ]
    else:
        encoded = {sig for c in iface.pack.commands for sig in c.encode}
        paths = [iface.signals[n] for n in sorted(encoded) if n in iface.signals]
    for path in paths:
        h = store.find(path)
        if h is not None:
            out.add(store.signal(h).stream_id)
    return frozenset(out)


def detect(
    store: Any,
    packs: Iterable[Pack],
    config: Any = None,
) -> list[Interface]:
    """Every interface in the trace, most specific pack per set of wires."""
    packs = list(packs)
    index = _scope_index(store)
    ignored = list(getattr(config, "protocol_ignore", ()) or ())
    configured_reset = configured_signal(store, getattr(config, "reset_signal", None))

    candidates: list[tuple[int, Interface]] = []
    for scope, names in index.items():
        if any(_glob(scope, pat) for pat in ignored):
            continue
        for pack in packs:
            for prefix in _prefixes(names, pack):
                resolved = _match(names, pack, prefix)
                if resolved is None:
                    continue
                candidates.append(
                    (
                        pack.detect.specificity,
                        Interface(
                            name=_interface_name(scope, prefix),
                            scope=scope,
                            prefix=prefix,
                            pack=pack,
                            signals=resolved,
                            clock=_find_named(index, scope, pack.detect.clock, prefix),
                            # §4.3 names this once for the project.  Previously
                            # the field was parsed and then never read, so a
                            # non-conventional reset name made transfers during
                            # reset appear as real protocol traffic.
                            reset=configured_reset
                            or _find_named(index, scope, pack.detect.reset, prefix),
                        ),
                    )
                )

    # Most specific pack first; among equals, the shortest name. Every candidate
    # left at that point describes the same wires, so the tie-break is purely
    # "what will someone have to type in `txn(...)`" — `cpu` over
    # `tb_axi_lite.s_axi` for the same bus.
    candidates.sort(key=lambda c: (-c[0], len(c[1].name), c[1].name))

    chosen: list[Interface] = []
    taken: dict[frozenset[int], Interface] = {}
    claimed: set[int] = set()
    used_names: set[str] = set()
    for _, iface in candidates:
        key = _identity(store, iface)
        if not key:
            continue
        first = taken.get(key)
        if first is not None:
            # Same wires under another name — a master port and the slave port
            # it drives. Keep the alias so the UI can show where else this
            # interface appears, and so a search by the other path still finds it.
            if iface.name not in first.aliases and iface.name != first.name:
                first.aliases.append(iface.name)
            continue
        if key <= claimed:
            # Every wire here is already part of a more specific interface. This
            # is what stops the generic handshake pack from reporting each AXI
            # channel a second time as an interface of its own.
            continue
        if iface.name in used_names:
            # Two different sets of wires that would print the same. Fall back
            # to the full scope so `txn(<name>)` still addresses exactly one.
            iface.name = f"{iface.scope}.{iface.prefix.strip('_')}".rstrip(".")
        used_names.add(iface.name)
        taken[key] = iface
        claimed |= key
        chosen.append(iface)

    chosen.sort(key=lambda i: i.name)
    return chosen


def near_misses(store: Any, pack: Pack, limit: int = 3) -> list[tuple[str, list[str]]]:
    """`(scope + prefix, suffixes that were not found)`, best candidates first.

    A pack that matches nothing looks exactly like a protocol the design does
    not use, and the cause is almost always one suffix spelled differently. This
    is what turns that into a sentence — for `veritrace packs --trace`, and for
    anyone writing a pack against docs/PACKS.md.

    An empty `missing` list is a *complete* match that lost the specificity
    contest in `detect` — AXI4's required signals are all present in an AXI4-Lite
    design. Reporting it as "not in this trace" would be false, so it is kept.
    """
    out: list[tuple[int, str, list[str]]] = []
    for scope, names in _scope_index(store).items():
        for prefix in _prefixes(names, pack):
            missing = [s for s in pack.detect.required_suffixes if (prefix + s).lower() not in names]
            # Nothing found at all is not a near miss — it is a scope that has
            # nothing to do with this bus.
            if len(missing) < len(pack.detect.required_suffixes):
                out.append((len(missing), f"{scope}.{prefix}".rstrip("."), missing))
    out.sort(key=lambda x: (x[0], x[1]))
    return [(where, missing) for _, where, missing in out[:limit]]


def _glob(text: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(text, pattern)


def channel_paths(iface: Interface, ch: Channel) -> dict[str, str | None]:
    """Trace paths for one channel's signals; `None` for fields not dumped."""
    return {suffix: iface.signals.get(suffix) for suffix in ch.signals}
