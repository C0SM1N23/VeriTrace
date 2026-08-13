"""Repro — from a causal chain to something you can run and to something you
can read (§8.2, §8.3, §11.5).

Three steps, in this order, each consuming the previous one:

* `subtrace` minimises the causal tree to the handful of events that actually
  explain the bug (§8.2).
* `narrate` turns those events into sentences, from a fixed template table
  rather than free prose (§11.5).
* `testbench` projects them onto the design's primary inputs and writes SystemVerilog
  that reproduces the failure (§8.3).
"""

from veritrace.repro.subtrace import Event, Subtrace, minimise
from veritrace.repro.testbench import Repro, generate, validate

__all__ = ["Event", "Subtrace", "minimise", "Repro", "generate", "validate"]
