"""SDRAM/DDR controller analysis — §8.20, TAB 10.

Command decode, per-bank state tracking, JEDEC timing-constraint checking and
efficiency metrics, all derived from the raw command bus a memory pack
describes — no simulator support and no testbench code required, the same
promise §8.14 makes for protocol packs. `report.build` is the entry point.
"""
