"""§8.11b — on-board captures from ILA and SignalTap.

A capture is not a simulation, and every test here is about the tool saying so:
the window has a front edge, only probed signals exist, and a sample index is
not a timestamp. The one thing that must never happen is a chain that walks off
the front of the capture and calls a value constant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from veritrace import TraceStore, convert
from veritrace.analysis.whytrace import Reason, WhyTracer
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate
from veritrace.ingest import capture as cap
from veritrace.cli import main

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

VIVADO = """\
ILA Data
Trigger position: 8
Sample in Buffer,Sample in Window,TRIGGER,dut/rd_rst_n,dut/full,dut/rd_ptr[4:0]
0,-8,0,0,0,00000
1,-7,0,0,0,00000
2,-6,0,0,1,00000
3,-5,0,0,1,00000
"""

SIGNALTAP = """\
;; SignalTap II export
index,i_top|u_dma|state,i_top|u_dma|busy
0,0x2,1
1,0x2,1
2,0x3,0
"""


def test_vivado_columns_that_are_not_probes_are_dropped(tmp_path):
    p = tmp_path / "ila.csv"
    p.write_text(VIVADO, encoding="utf-8")
    c = cap.read(p, fmt="vivado-ila", scope="tb")
    # Sample-in-buffer, sample-in-window and TRIGGER are bookkeeping, not signals.
    assert c.names == ["tb.dut.rd_rst_n", "tb.dut.full", "tb.dut.rd_ptr"]
    assert c.n_samples == 4
    assert c.trigger == 8


def test_signaltap_hierarchy_separators_become_dots(tmp_path):
    p = tmp_path / "stp.csv"
    p.write_text(SIGNALTAP, encoding="utf-8")
    c = cap.read(p, fmt="signaltap")
    assert c.names == ["i_top.u_dma.state", "i_top.u_dma.busy"]
    assert c.n_samples == 3


def test_a_declared_width_survives_into_the_vcd(tmp_path):
    p = tmp_path / "ila.csv"
    p.write_text(VIVADO, encoding="utf-8")
    text = cap.to_vcd(cap.read(p, scope="tb"))
    assert "$var wire 5 " in text, "the [4:0] width was lost"
    assert "$var wire 1 " in text
    # No wrapper scope: a probe has to arrive under the name the RTL uses.
    assert "$scope module tb $end" in text
    assert "module capture" not in text


def test_an_unreadable_sample_is_x_not_zero(tmp_path):
    p = tmp_path / "ila.csv"
    p.write_text(
        "Sample in Buffer,a\n0,1\n1,????\n", encoding="utf-8"
    )
    text = cap.to_vcd(cap.read(p))
    assert "x" in text.lower().split("$enddefinitions")[1], "a bad cell became a value"


def test_one_sample_is_one_tick(tmp_path):
    p = tmp_path / "ila.csv"
    p.write_text(VIVADO, encoding="utf-8")
    text = cap.to_vcd(cap.read(p, scope="tb"))
    times = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert times[:3] == ["#0", "#2"] or times[0] == "#0"
    # Samples, not nanoseconds: the last tick is the last sample index.
    assert int(times[-1][1:]) <= 3


def test_a_chain_stops_at_the_first_sample_instead_of_claiming_a_cause(tmp_path):
    """§8.11b's whole point, and the reason `CAPTURE_BOUNDARY` exists.

    `rd_ptr` is already 0 at sample 0 and never moves. Without the boundary
    terminal the walk calls it constant — a claim about the design that the
    capture cannot support, since whatever set it happened before the trigger.
    """
    csv_path = tmp_path / "ila.csv"
    rows = ["Trigger position: 8", "Sample in Buffer,dut/rd_rst_n,dut/full,dut/rd_ptr[4:0]"]
    rows += [f"{i},0,{1 if i > 3 else 0},00000" for i in range(32)]
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    vcd = tmp_path / "ila.vcd"
    vcd.write_text(cap.to_vcd(cap.read(csv_path, scope="tb_fifo_buggy")), encoding="utf-8")
    vtx = tmp_path / "ila.vtx"
    convert(str(vcd), str(vtx))
    store = TraceStore(str(vtx))

    el = elaborate(discover(DESIGNS / "fifo_buggy"))
    rep = correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    # Narrow by construction, and the number says so rather than hiding it.
    assert rep.percent < 50, "a three-probe capture should not read as well correlated"

    t0, t1 = store.time_range
    result = WhyTracer(el.graph, store, capture_start=t0).why("tb_fifo_buggy.dut.full", t1)
    boundary = [n for n in result.root.walk() if n.reason is Reason.CAPTURE_BOUNDARY]
    assert boundary, "the chain ran past the front of the window"
    assert "before the window" in boundary[0].detail
    # §8.11b: it does not merely say "I don't know" — it says what to trigger on.
    assert "Trigger on a change of" in boundary[0].detail

    # Without the flag the same trace gives a *different* answer, which is the
    # point: a capture and a simulation are not interchangeable.
    plain = WhyTracer(el.graph, store).why("tb_fifo_buggy.dut.full", t1)
    assert not any(n.reason is Reason.CAPTURE_BOUNDARY for n in plain.root.walk())


def test_an_unknown_format_is_refused_by_name(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(VIVADO, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown capture format"):
        cap.read(p, fmt="chipscope")


def test_serve_opens_csv_directly_as_an_honest_capture(tmp_path, monkeypatch):
    """The exact §8.11b command reaches a live API session, not an import detour."""
    csv_path = tmp_path / "ila.csv"
    csv_path.write_text(VIVADO, encoding="utf-8")
    served = {}

    def run_server(app, **_kwargs):
        served["app"] = app

    monkeypatch.setattr("uvicorn.run", run_server)
    got = CliRunner().invoke(
        main,
        [
            "serve",
            str(csv_path),
            "--format",
            "vivado-ila",
            "--scope",
            "tb",
            "--no-browser",
        ],
    )

    assert got.exit_code == 0, got.output
    app = served["app"]
    session = app.state.registry.get(app.state.default_session_id)
    status = session.status()
    assert status["capture"] is True
    assert status["n_signals"] == 3
    assert session.store.find("tb.dut.full") is not None
    assert cap.marker_path(session.trace_path).is_file()


def test_import_capture_marker_survives_a_later_session_open(tmp_path):
    from veritrace.api.sessions import Session

    csv_path = tmp_path / "ila.csv"
    csv_path.write_text(VIVADO, encoding="utf-8")
    out = tmp_path / "ila.vcd"
    got = CliRunner().invoke(
        main,
        ["import-capture", str(csv_path), "--format", "vivado-ila", "-o", str(out)],
    )
    assert got.exit_code == 0, got.output
    assert cap.marker_path(out).is_file()

    session = Session.open(out)
    assert session.status()["capture"] is True
