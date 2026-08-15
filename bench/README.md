# bench — time to root cause (§15)

§15 is a comparison, and only one half of it can be automated.

| Run | What it measures | How |
|---|---|---|
| **A — baseline** | GTKWave + editor + grep, from "I saw the symptom" to "I wrote the correct sentence about the root cause" | a person, with a stopwatch |
| **B — VeriTrace** | the same two points, using the tool | `python bench/ttrc.py` |

**No script can produce column A.** A number here that claimed to would be
exactly the kind of invented statistic §15's anti-bias rules exist to prevent,
and it would make the whole comparison worthless. Run A is measured by a person
or it is not measured.

## Column B

```sh
python bench/ttrc.py           # table
python bench/ttrc.py --json    # for a spreadsheet
```

It walks every reference design that declares expectations in `expected.toml`,
asks the question a user would ask, and records:

- **setup** — elaborating the RTL and correlating it against the dump. Part of
  the cost of the *first* answer and not of later ones, so it is timed
  separately rather than hidden;
- **why** — the causal walk itself;
- **root cause** — whether the chain landed on the signal the design's own
  `expected.toml` names. A `MISS` here is a §14.3 release blocker, not a slow
  number.

The script exits non-zero if any question misses, so it doubles as a check that
the reference designs still behave as documented.

## Reading it honestly

The B column is the tool's *floor*, not a claim about a human using it: it does
not include reading the answer, deciding it is right, or opening the file it
points at. A fair §15 table pairs it with a real A run on the same bug, and
records who ran which order — the protocol in §15.1 exists because the second
run of the same bug is always faster, whatever tool it uses.
