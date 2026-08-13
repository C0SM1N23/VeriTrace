# Writing an analysis plugin

§13.7's promise: **an analysis you add touches neither the core nor the UI.**
Findings you yield appear in the Checks tab like any built-in check; tables you
yield appear as a tab of their own.

§16.2 sets the bar for this page: *someone should be able to write a new plugin
from this documentation alone, in under 45 minutes.* If it took you longer, the
gap is here and not in you — say which part.

---

## Where a plugin lives

```
your-project/
  rtl/
  plugins/
    clock_gating.py      <- discovered automatically
  .veritrace.toml
```

Two directories are searched, user first so a project can override:

1. `~/.veritrace/plugins/`
2. `plugins/` beside your `.veritrace.toml`

No install step, no entry points, no registry. A plugin is a file, versioned
with the design and reviewable in the same pull request — the same rule §13.8
sets for every team feature: *files and git, nothing else.*

Files starting with `_` are skipped, so helpers can live beside plugins.

---

## The shape

```python
from veritrace.plugin import Analysis, register


@register
class ResetPolarity(Analysis):
    name        = "reset_polarity"
    needs       = ["trace", "graph"]
    description = "Resets that are held asserted for most of the run"

    def run(self, ctx):
        for sig in ctx.signals(match="*rst_n*"):
            low = ctx.count_where(sig, 0)
            if low > ctx.total_cycles * 0.5:
                yield ctx.finding(
                    f"{sig.name} is low for {low} of {ctx.total_cycles} cycles",
                    severity="warn",
                    signal=sig.path,
                    detail="An active-low reset held for most of the run usually "
                           "means the testbench never released it.",
                )
```

Three things and no more:

| | |
|---|---|
| `name` | stable identifier. Becomes `plugin.<name>` for `--fail-on` and `checks.disable`, and the tab title for tables. Required. |
| `needs` | what the analysis cannot run without. See below. |
| `run(ctx)` | a generator yielding `ctx.finding(...)` and `ctx.table(...)`. Both optional; yielding nothing is fine. |

---

## `needs` — say what you require

```python
needs = ["trace"]                  # a waveform
needs = ["graph"]                  # the elaborated RTL
needs = ["trace", "transactions"]  # a waveform and a detected bus
```

Available: `trace`, `graph`, `transactions`, `coverage`, `memory`,
`performance`.

**A plugin whose needs are not met is not run**, and the reason appears next to
the other skipped checks. That is deliberate: a plugin that runs anyway and
finds nothing is indistinguishable from a design with nothing wrong, which is
the one failure mode a checker cannot afford.

---

## What `ctx` gives you

### The trace

```python
ctx.total_cycles                      # clock cycles in the run
ctx.signals()                         # every signal
ctx.signals(kind="clock")             # clocks, from the graph rather than by name
ctx.signals(match="top.dma.*")        # glob on the full path
ctx.value_at(sig, t)                  # four-state digits, or None
ctx.transitions(sig)                  # [(time, bits), ...]
ctx.count_where(sig, 0)               # cycles the signal held that value
```

A `SignalView` has `path`, `name`, `handle`, `width`, `n_events`. Anywhere a
signal is expected you may pass one or its path.

### The design

```python
ctx.graph.get("top.dut.state")        # a Signal, or None
sig.drivers                           # what assigns it, with guards and locations
sig.kind                              # Kind.REG, Kind.WIRE, Kind.PORT_IN, ...

ctx.elaboration                       # parameters, instances, diagnostics
ctx.elaboration.parameters["top.dut"] # {"S_IDLE": ParamValue(value="2'd0", ...)}
ctx.elaboration.instances             # {"top.dut": "fifo_sync", ...}
```

**`graph` and `elaboration` are not the same thing**, and the difference bites.
The graph knows a guard reads `S_IDLE`; only the elaboration knows `S_IDLE` is
zero. An analysis that resolves parameter names needs both — the first version
of `designs/fsm/plugins/state_dwell.py` passed `None` where the elaboration
belonged and silently found no state machines at all.

### The transaction layer

```python
ctx.interfaces                        # detected interfaces
ctx.transactions()                    # every transaction
ctx.transactions("m0")                # one interface's
txn.kind, txn.fields, txn.start_time, txn.end_time, txn.closed
```

### Raw access

`ctx.store`, `ctx.graph`, `ctx.clock`, `ctx.protocol`, `ctx.config` are there
when the helpers are not enough. The helpers exist so the common case does not
depend on internals; reaching past them is allowed and unsupported.

---

## What you produce

### A finding

```python
yield ctx.finding(
    "awvalid never rose on m1",       # the headline; one line
    severity="warn",                  # "info" | "warn" | "error"
    signal="top.arb.m1_awvalid",      # optional
    detail="...",                     # optional, shown under the row
    why="why(top.arb.m1_awvalid)",    # optional; makes [why] work in the UI
)
```

It appears in the Checks tab under **PLUGINS**, is suppressible with a reason
like any other, and is addressable from CI:

```bash
veritrace check --fail-on plugin.reset_polarity
```

### A table

```python
yield ctx.table(
    "Clock gating",
    ["clock", "gated cycles", "%"],
    [[c.path, gated, f"{100 * gated / ctx.total_cycles:.1f}"] for c, gated in rows],
)
```

It becomes a tab. You do not touch the frontend.

---

## Trying it

```bash
veritrace plugins                  # what was found, and what could not load
veritrace check --rtl rtl/         # your findings, with everything else
```

A plugin that raises is reported as a skip with the exception on it — one broken
analysis never empties the Checks tab.

---

## Turning one off

```toml
# .veritrace.toml
[checks]
disable = ["plugin.reset_polarity"]
```

---

## The whole contract

```python
from veritrace.plugin import Analysis, register

@register
class Name(Analysis):
    name  = "..."          # required
    needs = [...]          # of: trace, graph, transactions, coverage, memory, performance
    def run(self, ctx):
        yield ctx.finding("...", severity="info", signal="...")
        yield ctx.table("...", ["col"], [["value"]])
```

That is the entire API. If something you want is not reachable through it, that
is worth reporting — the surface is deliberately small, but it is meant to be
sufficient.
