"""What license the files VeriTrace *emits* carry — §1.8.

VeriTrace is MIT, but §1.8 singles out the emitters: a checker from `gen-sva`
or a testbench from `stimgen` lands next to someone's RTL and gets committed,
so "does the output inherit the tool's license?" is a question that must never
need asking. It does not, and the answer travels in the generated file's own
header rather than living only in a top-level text file nobody opens.
"""

from __future__ import annotations

__all__ = ["SPDX", "banner"]

#: Dual, so a jurisdiction that does not recognise a public-domain dedication
#: still has a permissive option. See LICENSE-GENERATED.
SPDX = "CC0-1.0 OR MIT"


def banner(comment: str = "//") -> list[str]:
    """The license header for a generated source file, in its comment syntax."""
    return [
        f"{comment} SPDX-License-Identifier: {SPDX}",
        f"{comment} Generated output — yours to keep, edit and relicense. VeriTrace's own",
        f"{comment} MIT terms do not attach to this file (§1.8, LICENSE-GENERATED).",
    ]
