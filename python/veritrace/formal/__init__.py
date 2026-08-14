"""Formal verification from protocol packs — §8.27, and §8.35's reachability.

A `[[rule]]` is already a property. §8.14 checks it against one run; this checks
it against *every* run up to a bounded depth, and when it fails the
counterexample comes back as a waveform VeriTrace can open like any other.
"""

from veritrace.formal.model import FormalReport, Property, Reachability, Verdict
from veritrace.formal.prove import prove, reach

__all__ = ["FormalReport", "Property", "Reachability", "Verdict", "prove", "reach"]
