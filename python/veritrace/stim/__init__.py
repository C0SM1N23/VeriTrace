"""Constrained-random stimulus from a pack's own field domains — §8.32.

§8.21 shows the coverage holes. This closes them, without UVM and without a
licence: the pack already knows what a legal transaction looks like, and the
same `[[cover]]` predicate that scores a hole is what selects a candidate to
close it.
"""

from veritrace.stim.emit import TARGETS, render
from veritrace.stim.generate import generate, holes_from
from veritrace.stim.model import Item, Plan, Target

__all__ = ["TARGETS", "render", "generate", "holes_from", "Item", "Plan", "Target"]
