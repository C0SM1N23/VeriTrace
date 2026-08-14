"""Navigation over someone else's static timing analysis — §8.30.

Not STA. Vivado already computed the paths; what no tool can tell you is which
of them the design actually exercises, because the timing tool has never run a
simulation and the simulator has never read a timing report.
"""

from veritrace.timing.correlate import correlate, rtl_name
from veritrace.timing.model import Hop, Path, TimingReport
from veritrace.timing.vivado import load, parse

__all__ = ["correlate", "rtl_name", "Hop", "Path", "TimingReport", "load", "parse"]
