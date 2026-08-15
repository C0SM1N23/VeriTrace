# Writing a protocol pack

A **pack** teaches VeriTrace one bus. It is a single TOML file, it lives next to
your RTL, and it needs no code: everything in the Transactions, Performance,
Memory, Coverage and Checks tabs for that bus comes out of it.

§8.15's rule is that a pack describes the protocol, never a design. If a line in
your pack mentions a module name or a testbench signal, that line is in the
wrong file.

Fifteen packs ship built in — AXI4, AXI4-Lite, AXI4-Stream, AHB-Lite, APB,
Avalon-MM, Avalon-ST, Wishbone B4, SDR SDRAM, DDR3, SPI, I2C, UART, RISC-V
retire, and a generic `valid/ready` handshake. `veritrace packs` lists them with
what each one requires. Read
[`apb.vtp.toml`](../python/veritrace/protocol/packs/apb.vtp.toml) first: it is
the shortest complete one and its comments explain the awkward parts.

---

## Where a pack lives

```
your-project/
  rtl/
  packs/
    ocp_lite.vtp.toml       <- discovered automatically
  .veritrace.toml
```

Three directories are searched, nearest first:

1. `packs/` beside your `.veritrace.toml`
2. the project root itself
3. the built-ins that ship with VeriTrace

The file name is the identity, so `packs/axi4.vtp.toml` **shadows** the built-in
AXI4 pack. A house variant of a standard bus is a file you drop in, not a fork
of the tool.

```sh
veritrace packs                        # what is loaded, from where
veritrace packs --trace dump.vcd       # and which of them match this dump
```

A pack that fails to parse is reported by name. It never disappears quietly — a
pack with a typo would otherwise look exactly like a protocol the design does
not use.

---

## The eight sections

Only the first three are required. Add the rest as you want the tabs they feed.

| Section | Feeds | Required |
|---|---|---|
| `[detect]` | finding the interface at all | yes |
| `[[channel]]` | the handshake events | yes |
| `[[transaction]]` | what a transaction *is* | yes |
| `[metrics]` | Performance (§8.17) | no |
| `[[stall_reason]]` | "why is it slow" (§8.17) | no |
| `[integrity]` | the scoreboard (§8.19) | no |
| `[[cover]]` | functional coverage (§8.21) | no |
| `[[rule]]` | Checks, and `gen-sva` (§8.22) | no |

Plus three specialised ones: `[perf]` (throughput/efficiency), `[address_map]`
(DRAM bank/row/column, §8.20) and `[[command]]` (buses with an encoded command
field rather than a channel per operation).

### `[detect]` — recognising the bus

```toml
name    = "OCP-Lite"
version = "1.0"

[detect]
required_suffixes = ["mcmd", "maddr", "scmdaccept", "sresp"]
prefix_strip      = ["m_ocp_", "ocp_", "m_", ""]
clock             = "clk|.*_clk"
reset             = "rst_n|resetn|.*_rst_n"
```

Detection is by **suffix**, after stripping one of `prefix_strip`. That is what
makes one pack match `m_ocp_mcmd`, `cpu_mcmd` and plain `mcmd` without knowing
any of those names in advance. Every suffix in `required_suffixes` must be
present or the interface is not this protocol — list the signals without which
the pack could not do its job, and no more. Each extra one is a design that
legitimately uses the bus and is not detected.

`clock` and `reset` are regular expressions over the leaf name. `prefix_strip`
accepts `"*"` as one of its entries, meaning *infer the prefix from the design*:
whatever precedes the first required suffix is tried. The generic handshake pack
uses it, which is how `push_valid`/`push_ready` is found without anyone naming
`push_` anywhere.

### `[[channel]]` — when something happens

```toml
[[channel]]
name    = "REQ"
valid   = "mcmd != 0"
ready   = "scmdaccept"
payload = ["maddr", "mdata", "mbyteen"]
```

A channel event is `valid && ready` on a rising clock edge. `valid` and `ready`
are expressions, not just signal names, which is what lets a bus with no
valid/ready pair use the same engine — APB maps `valid = psel && penable`,
`ready = pready`, and its transactions come out right.

`payload` names the fields sampled at that event. They become the columns in the
Transactions tab and the fields `[[cover]]` and `[integrity]` can talk about.

### `[[transaction]]` — start, body, end

```toml
[[transaction]]
name  = "WRITE"
start = "REQ"
end   = { channel = "RESP", match_on = "id" }
key   = "addr = REQ.maddr"
```

`start` opens it, the optional `body` carries beats (`count`, `terminator`), and
`end` closes it. `match_on` is how an out-of-order bus pairs a response with its
request; without it, the pairing is first-in-first-out, which is right for a bus
that cannot reorder and wrong for one that can.

`key` names the fields that identify the transaction in the UI and in `why()`.

### The rest, briefly

```toml
[metrics]
latency     = "end.cycle - start.cycle"
wait_states = "sum(stall)"

[[stall_reason]]          # in priority order — the first match wins
name = "wait_states"
when = "mcmd != 0 && !scmdaccept"
msg  = "the target was inserting wait states"

[integrity]               # §8.19: writes are remembered, reads are compared
addr  = "addr"
write = { kind = "WRITE", data = "mdata", strobe = "mbyteen" }
read  = { kind = "READ",  data = "sdata" }

[[cover]]                 # §8.21: field bins, corners, sequences, crosses
field = "mcmd"
bins  = { IDLE = 0, WRITE = 1, READ = 2 }

[[rule]]                  # §8.4 checks, and the source for gen-sva
id       = "OCP_ADDR_STABLE"
severity = "error"
check    = "mcmd != 0 && !scmdaccept |=> $stable(maddr)"
msg      = "maddr changed before the target accepted the command"
```

`check` is a small SVA subset: `|->` and `|=>`, the sampled functions
`$stable`, `$changed`, `$past`, `$rose` and `$fell`, and the usual comparison,
arithmetic and boolean operators over payload fields and `param.*`. The same
line is evaluated over the trace by the checker **and** emitted as real SVA,
plain Verilog or formal assertions by `veritrace gen-sva` — so one statement of
protocol knowledge serves the Checks tab, the simulator and the prover. (`gen-
sva` additionally passes `$onehot` and `$countones` through to the emitted
checker; the trace-side checker does not evaluate those, and says so rather than
scoring the rule.)

---

## Writing one, in order

The loop is short because everything is a file:

```sh
veritrace packs                             # 1. does it load?
veritrace packs --trace dump.vcd            # 2. does it match the design?
veritrace txn dump.vcd "txn(cpu)"           # 3. are the transactions right?
veritrace check dump.vcd --rtl rtl/         # 4. do the rules fire?
veritrace coverage dump.vcd --rtl rtl/      # 5. do the bins fill?
```

Step 2 is the one that saves the afternoon. One suffix your design spells
differently is enough to reject the whole interface, so `--trace` names it:

```
  House AXI        tb_axi_lite.cpu: no awaddress
  AXI4             tb_axi_lite.cpu: complete, but a more specific pack claimed these wires
  APB              not in this trace
```

Three different situations that would otherwise all read as silence: a typo in
your pack, a pack that matched but lost to a more specific one, and a bus the
design genuinely does not have.

Two habits worth keeping from the built-ins:

- **Comment the protocol decisions, not the TOML.** The value of
  `apb.vtp.toml`'s header is that it explains why `valid = psel && penable` is
  the honest mapping. Someone will ask.
- **Write `[[rule]]`s only for what the specification mandates.** A rule that
  encodes a house convention will fire on a correct design that follows a
  different one, and a checks list people learn to ignore is worse than none.

---

## Sharing it

A pack is a text file: commit it, review it, and if it describes a public
protocol correctly, it belongs upstream. There is no registry and no plugin API
to satisfy — `packs/ocp_lite.vtp.toml` in a pull request is the whole
contribution.

For analysis that is not about a bus, see [PLUGINS.md](PLUGINS.md): a Python
file in `plugins/` yields findings and tables without touching the core or the
UI either.
