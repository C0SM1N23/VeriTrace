"""Mutation testing — §8.28.

Coverage says which lines ran. This says whether the tests would *notice* a bug
on them, which is the question a testbench is actually for.
"""

from veritrace.mutate.model import Mutation, MutationReport, Survivor
from veritrace.mutate.operators import OPERATORS, sites
from veritrace.mutate.run import run

__all__ = ["Mutation", "MutationReport", "Survivor", "OPERATORS", "sites", "run"]
