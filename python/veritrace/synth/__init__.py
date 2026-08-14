"""RTL vs post-synthesis — §8.29.

§8.11 finds the *patterns* that cause a sim/synth mismatch. This proves one
happened: synthesise, simulate the netlist with the same testbench, and run the
§8.7 diff between the two waveforms.
"""

from veritrace.synth.model import SynthDiff
from veritrace.synth.yosys import Netlist, synthesise

__all__ = ["SynthDiff", "Netlist", "synthesise"]
