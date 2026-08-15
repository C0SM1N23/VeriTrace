"""Fixtures and helpers shared by more than one test module.

`make_vtx` lived in `test_api.py` and was imported from `test_share.py` as
`from tests.test_api import ...` — which works only when the repository root
happens to be on `sys.path`. It is under `python -m pytest`, because `-m` puts
the working directory there; it is not under a bare `pytest`, which is what
`make test` and CI run. The suite passed locally and failed to *collect* in CI.

Shared test code belongs here, where pytest loads it for every module without
anyone importing anyone.
"""

from __future__ import annotations

from pathlib import Path

from veritrace import convert

#: A trace small enough to reason about by hand: two scopes, a clock, a vector
#: that changes once, and a signal that starts X. Enough to exercise the store,
#: the API and the session layer without a simulator.
SMALL_VCD = """\
$timescale 1ns $end
$scope module tb $end
$var reg 1 ! clk $end
$var reg 8 " data [7:0] $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 4 # state [3:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0 "
bx #
#10
1!
b10100000 "
#20
0!
b1 #
#30
1!
"""


def make_vtx(tmp_path: Path, text: str = SMALL_VCD, name: str = "dump") -> Path:
    """Write `text` as a VCD under `tmp_path` and convert it to a `.vtx`."""
    src = tmp_path / f"{name}.vcd"
    src.write_text(text)
    out = tmp_path / f"{name}.vtx"
    convert(str(src), str(out))
    return out
