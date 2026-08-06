# Protocol packs

A pack describes how to lift raw signals into transactions. It is a file, not
code, and the extraction engine that reads it knows nothing about any protocol —
which is the whole point: **one engine, every protocol.**

The bar this format has to clear: *a new protocol is one file and about thirty
minutes.* If yours takes a day, that is a bug in the format, not in you.

Packs are looked up nearest-first:

1. an explicit path (`veritrace txn --packs ./my.vtp.toml`),
2. `packs/` in your project, then the project root,
3. the ones shipped here.

A file named `axi4.vtp.toml` in your project's `packs/` **replaces** the shipped
one, so a house variant is a file you drop next to the RTL rather than a fork.

Restrict which packs are tried, or exclude a scope, in `.veritrace.toml`:

```toml
[protocol]
packs  = ["axi4lite"]        # default: every pack on the search path
ignore = ["tb.stimulus.*"]   # scopes never scanned
```

---

## The format

### `[detect]` — how an interface is recognised

```toml
[detect]
required_suffixes = ["awvalid", "awready", "awaddr", "wvalid", "wready", "wdata"]
prefix_strip      = ["m_axi_", "s_axi_", ""]
clock             = "aclk|clk"
reset             = "aresetn|rst_n"
```

| key | meaning |
|---|---|
| `required_suffixes` | every one must exist in a scope for the pack to match. Also the specificity: a pack demanding twelve signals beats one demanding two on the same wires. |
| `prefix_strip` | prefixes to try, longest first. `""` matches bare names. **`"*"` infers the prefix from the design** — anything ending in the first required suffix — which is what makes a generic pack usable. |
| `clock` / `reset` | regexes matched against leaf names in the scope and its parents. Prefixed candidates win, so a two-clock design gets the right one. |

Detection is automatic and needs no per-project configuration. Two names for the
same wires collapse into one interface — a master port and the slave port it
drives are one bus — and the extra names are kept as aliases.

### `[[channel]]` — a valid/ready handshake

```toml
[[channel]]
name    = "AW"
valid   = "awvalid"
ready   = "awready"          # optional: no ready means always accepted
payload = ["awaddr", "awlen", "awid"]
```

Names are pack-relative: the prefix found during detection is added
automatically. A payload field the design does not carry is simply absent, and
reads as *unknown* rather than as zero.

### `[[transaction]]` — how beats become a transaction

```toml
[[transaction]]
name  = "WRITE"
start = "AW"
body  = { channel = "W", count = "AW.awlen + 1", terminator = "wlast" }
end   = { channel = "B", match_on = "bid == AW.awid" }
id    = "awid"
key   = "addr = AW.awaddr"
```

| key | meaning |
|---|---|
| `start` | a beat on this channel opens a transaction. |
| `body` | optional beats in between. `count` is an expression; `terminator` is a payload field that marks the last beat. Either ends the body. |
| `end` | the beat that closes it. `match_on` picks which open transaction it belongs to; without it, the oldest one waiting. |
| `id` | payload field enabling outstanding and out-of-order matching. |
| `key` | named values lifted onto the transaction, so `txn(iface, addr=0x4000)` can filter on them. |

Omit `body` and `end` entirely and each accepted beat is its own transaction —
which is all a bare handshake needs.

A transaction that never closes is **kept**, marked `open`, and is what
`txn(iface) | open()` finds.

### `[metrics]` — computed at close

```toml
[metrics]
latency      = "end.time - start.time"
data_beats   = "count(W)"
stall_cycles = "sum(stall)"
```

Evaluated in order, each visible to the next.

### `[[rule]]` — protocol checks

```toml
[[rule]]
id        = "AXI_NODROP"
severity  = "error"          # error | warn | info
check     = "awvalid && !awready |=> awvalid"
msg       = "valid was withdrawn before ready"
param.max = 16               # available as `param.max` inside `check`
```

Two kinds, told apart by shape rather than by configuration:

* **temporal** — contains `|=>` (next cycle) or `|->` (same cycle). Evaluated at
  every clock edge on the raw signals.
* **per transaction** — everything else. Evaluated once, at close, and only on
  transactions that actually completed.

Violations become findings in the Checks tab, and `--fail-on protocol` makes
them fail a build.

### `[perf]` / `[[stall_reason]]` — §8.17's stall attribution

```toml
[perf]
transfer = ["W", "R"]        # channels whose accepted beats are useful work
data     = "wdata"           # payload field carrying data; its width -> bytes/beat
strobe   = "wstrb"           # optional: byte-enable field, for exact bytes moved

[[stall_reason]]
name = "backpressure"
when = "wvalid && !wready"
msg  = "the sink was not accepting"
```

Every cycle that moved no data gets exactly one label — `reset` and `transfer`
from the engine, then the first `[[stall_reason]]` whose `when` holds, in
priority order, then `other` if none did. The shares always add to 100%; a
large `other` means the cascade above is missing a rung, not that the design
is idle (`other` is reserved, alongside `reset` and `transfer` — a pack may not
redefine them). `when` can name a signal outside the interface entirely (an
arbiter's grant, say) — anything that is not one of the interface's own
payload fields resolves as a hierarchical trace path instead.

### `[[command]]` / `[address_map]` — §8.20's memory packs

A different shape for a different kind of bus. SDRAM has no valid/ready
handshake — the controller drives a command every cycle and the array obeys —
so a memory pack replaces `[[channel]]`/`[[transaction]]` with a decode table:

```toml
[[command]]
name   = "ACTIVATE"
encode = { cs_n = 0, ras_n = 0, cas_n = 1, we_n = 1 }
args   = { bank = "ba", row = "a" }

[address_map]
bank = "addr[11:10]"
row  = "addr[24:12]"
col  = "addr[9:1]"
```

`encode` is checked in priority order, like `[[stall_reason]]`: the first row
whose signals all match wins, and a cycle matching none is not a command at
all. `args` reads the payload out of the matched cycle — `a[9:0]` is a
SystemVerilog-style bit slice (`x[hi:lo]` on an integer), the one addition the
expression language needed for this. `[address_map]` is unrelated to the
observed commands; it is the formula a system address turns into
(bank, row, col), for the address-map inspector (TAB 10).

A pack with `[[command]]` needs neither channels nor transactions — see
`sdram.vtp.toml` — and one with `[[channel]]` needs neither `[[command]]` nor
`[address_map]`. The two families share `[detect]` and nothing else.

---

## The expression language

Used by `count`, `match_on`, `key`, `[metrics]` and `[[rule]]`.

**Operators:** `+ - * / %`, `== != < <= > >=`, `&& || !`, `& | ^ << >>`,
`?:`, `[]`, `[hi:lo]`, and `|=>` / `|->` in rules.

**Literals:** `42`, `0x40`, `0b1010`, and SystemVerilog forms `4'hF`, `3'b010`.

**Names:**

| form | is |
|---|---|
| `awaddr` | a payload field, from the start beat or any beat |
| `AW.awaddr` | that channel's first beat, explicitly |
| `AW.assert_time` / `AW.accept_time` / `AW.stall` / `AW.cycle` | that beat's timing |
| `start.time` / `end.time` / `start.cycle` | the transaction's own span |
| `outstanding` | transactions open when this one started |
| `param.x` | a rule's own constant |

**Functions:** `count(CH)`, `sum(e)`, `avg(e)`, `any(e)`, `all(e)` over the
transaction's beats (with `stall`, `time`, `channel` bound per beat);
`min`, `max`, `abs`; and in temporal rules `$stable(x)`, `$changed(x)`,
`$past(x[, n])`, `$rose(x)`, `$fell(x)`.

**Unknowns are values, not errors.** A field sampled while it carried X
evaluates to unknown and propagates; a rule with no verdict stays *silent*
rather than failing. Two unknowns do not compare equal — reporting a match there
would be an invented answer. A controlling value still wins: `0 && x` is `0`.

An unknown *name*, on the other hand, is an error, because that is a typo in the
pack and a rule that silently never fires is worse than one that refuses to
load. Expressions are parsed, never `eval`'d: the grammar can only produce a
number.

---

## What ships here

| pack | covers |
|---|---|
| `handshake.vtp.toml` | generic valid/ready — any custom interface, and the shortest worked example |
| `axi4lite.vtp.toml` | AXI4-Lite: one beat per transaction, no ids |
| `axi4.vtp.toml` | AXI4 full: bursts, ids, outstanding, out-of-order |
| `sdram.vtp.toml` | SDR SDRAM command bus — §8.20, TAB 10; timing parameters live in `timing/<chip>.toml` next to it |

Start from `handshake.vtp.toml`. It is thirty lines.
