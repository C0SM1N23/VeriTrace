"""Subtrace, repro and the bug report — §8.2, §8.3, §11.5, §12.

The acceptance criterion of prompt 13 is the one test in here that matters:
*the testbench generated for a control module compiles and reproduces the
original failure, checked programmatically*. `test_the_generated_testbench_
reproduces_the_failure` is that criterion, and it asserts on the simulator's own
verdict rather than on the text of the generated file — a repro that looks right
and does not fail is exactly what §8.3's validation loop exists to catch.

Everything needing a simulator is skipped when none is installed. The rest —
minimisation, the don't-care check, the narration table, the report — runs
unconditionally, because those are where the reasoning lives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import design_store
from click.testing import CliRunner

from veritrace import TraceStore, clocks, convert, simulate
from veritrace.analysis.whytrace import Reason, WhyTracer
from veritrace.cli import main
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import elaborate
from veritrace.graph.model import (
    Const,
    DesignGraph,
    Driver,
    DriverKind,
    Kind,
    Signal,
    SignalId,
    SourceLoc,
)
from veritrace.repro import narrate, subtrace, testbench

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
BUGGY = DESIGNS / "fifo_buggy"
BUGGY_RTL = [BUGGY / "tb_fifo_buggy.sv", BUGGY / "fifo_buggy.sv"]
SYMPTOM = "tb_fifo_buggy.dut.full"

needs_simulator = pytest.mark.skipif(
    simulate.find_iverilog() is None and testbench._verilator() is None,
    reason="neither Verilator nor Icarus is installed",
)


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """The golden causal answer, plus everything the repro needs to work from."""
    out = tmp_path_factory.mktemp("repro") / "buggy.vtx"
    convert(str(BUGGY / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    el = elaborate(BUGGY_RTL)
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, None)
    _t0, t1 = store.time_range
    result = WhyTracer(el.graph, store).why(SYMPTOM, t1)
    return el, store, clock, result


@pytest.fixture(scope="module")
def sub(chain):
    _el, store, clock, result = chain
    return narrate.narrate(subtrace.minimise(result.root, store, clock))


# --- §8.2, the minimisation ------------------------------------------------


def test_the_subtrace_is_a_narrative_not_a_tree(sub):
    """§8.2 asks for 5–12 events out of the whole chain, ending on the cause."""
    assert 1 <= len(sub.events) <= 12, [e.signal for e in sub.events]
    assert sub.dropped > 0, "the minimality check removed nothing at all"
    assert sub.considered > len(sub.events)
    assert sub.reached_root_cause
    assert sub.root_cause is not None
    assert sub.root_cause.signal == "tb_fifo_buggy.dut.rd_rst_n"
    assert sub.symptom is not None and sub.symptom.signal == SYMPTOM


def test_events_are_sorted_and_the_cause_comes_first(sub):
    times = [e.time for e in sub.events]
    assert times == sorted(times)
    assert sub.events[0].is_root_cause, "the story has to start with the cause"


def test_an_event_is_dated_when_the_value_was_established(chain, sub):
    """§8.2 step 2: a question asked at c17 about a signal that last moved at c0
    is a *sample*, not an event, and dating it c17 would make the narrative say
    something that did not happen."""
    _el, store, clock, _result = chain
    for e in sub.events:
        handle = store.find(e.signal)
        if handle is None:
            continue
        last = store.last_change_before(handle, e.time + 1)
        assert last is None or last == e.time, f"{e.signal} dated at a non-transition"


def test_the_symptom_and_the_cause_survive_minimisation(chain):
    """Delta-debugging may drop anything else; without these two there is no
    question and no answer."""
    _el, store, clock, result = chain
    minimal = subtrace.minimise(result.root, store, clock)
    assert minimal.symptom is not None
    assert minimal.root_cause is not None


def test_skipping_the_minimality_check_keeps_a_superset(chain):
    _el, store, clock, result = chain
    quick = subtrace.minimise(result.root, store, clock, delta=False)
    full = subtrace.minimise(result.root, store, clock, delta=True)
    assert quick.dropped == 0
    assert {e.signal for e in full.events} <= {e.signal for e in quick.events}


# --- §11.5, the narration --------------------------------------------------


def test_every_reason_has_a_sentence():
    """§11.5: about twelve templates, keyed by reason — and *all* of them. A
    reason without one is how a replay ends up reading "assigned by an active
    driver (out_of_range)"."""
    missing = [r.value for r in Reason if r.value not in narrate.TEMPLATES]
    assert not missing, f"no §11.5 template for: {', '.join(missing)}"


def test_the_sentences_are_from_templates_not_prose(sub):
    for e in sub.events:
        assert e.text, f"{e.signal} has no sentence"
        assert e.signal.rsplit(".", 1)[-1] in e.text or e.detail in e.text


def test_the_headline_names_the_symptom(sub):
    assert narrate.headline(sub) == "full stuck at 1"
    assert "rd_rst_n" in narrate.symptom_paragraph(sub)


def test_replay_steps_run_forwards(sub):
    steps = narrate.steps(sub)
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["time"] for s in steps] == sorted(s["time"] for s in steps)


# --- §8.3, choosing the mode ------------------------------------------------


def test_a_fifo_is_a_control_module(chain):
    """§8.3's table lists FIFOs as minimisable. An internal buffer is state the
    interface recreates, not a program image — and the classifier has to know
    the difference or every FIFO is misfiled as a CPU."""
    el, _store, _clock, _result = chain
    instance, module = testbench.choose_dut(el, SYMPTOM)
    assert (instance, module) == ("tb_fifo_buggy.dut", "fifo_buggy")
    ports = testbench.ports_of(el.graph, instance)
    assert {p.name for p in ports if p.direction == "in"} == {
        "clk",
        "rst_n",
        "wr_en",
        "wr_data",
        "rd_en",
    }
    mode, why = testbench.classify(el.graph, instance, ports)
    assert mode == "minimal", why


def _graph_with(memory: Signal):
    graph = DesignGraph()
    loc = SourceLoc("cpu.v", 1)
    graph.add(Signal(SignalId.parse("tb.cpu.clk"), 1, False, Kind.PORT_IN, loc))
    graph.add(memory)
    return graph


def test_a_memory_loaded_from_outside_forces_a_focused_testbench():
    """§8.3's CPU row: when the stimulus *is* a program image, minimising it is a
    different problem, and the honest answer is a window cut."""
    loc = SourceLoc("cpu.v", 9)
    mem = Signal(SignalId.parse("tb.cpu.imem"), 32, False, Kind.MEM, loc)
    mem.stimulus_only = True  # $readmemh, i.e. written only by `initial`
    graph = _graph_with(mem)
    ports = testbench.ports_of(graph, "tb.cpu")
    mode, why = testbench.classify(graph, "tb.cpu", ports)
    assert mode == "focused"
    assert "imem" in why


def test_a_memory_the_design_writes_does_not():
    loc = SourceLoc("cpu.v", 9)
    mem = Signal(SignalId.parse("tb.cpu.buf"), 32, False, Kind.MEM, loc)
    mem.drivers = [
        Driver(
            target=mem.id,
            kind=DriverKind.ALWAYS_FF,
            guard=Const(1, 1),
            value=Const(0, 32),
            loc=loc,
        )
    ]
    graph = _graph_with(mem)
    ports = testbench.ports_of(graph, "tb.cpu")
    assert testbench.classify(graph, "tb.cpu", ports)[0] == "minimal"


def test_a_symptom_outside_any_instance_is_refused(chain):
    el, _store, _clock, _result = chain
    with pytest.raises(testbench.ReproError, match="not inside any module instance"):
        testbench.choose_dut(el, "nowhere")


# --- §8.3 step 4, the don't-care hypothesis --------------------------------


def test_inputs_the_chain_does_not_need_are_tied_off(chain, sub):
    """Zeroing an input must leave every guard on the chain evaluating the same
    way; the ones that survive that are the don't-cares."""
    el, store, clock, result = chain
    built = testbench.generate(sub, el.graph, store, clock, el, root=result.root)
    assert built.tied, "nothing was tied off, so step 4 did nothing"
    assert set(built.tied) & {"rd_en", "wr_data"}
    assert set(built.tied).isdisjoint(built.driven)
    for name in built.tied:
        assert f"{name} = " in built.code


def test_the_generated_source_is_the_shape_8_3_asks_for(chain, sub):
    el, store, clock, result = chain
    built = testbench.generate(sub, el.graph, store, clock, el, root=result.root)
    assert built.code.startswith("`timescale")
    assert f"module {built.top};" in built.code
    assert "fifo_buggy #(.WIDTH(8), .DEPTH(16)) u_dut (" in built.code
    assert "always #" in built.code and "clk = ~clk;" in built.code
    assert "@(posedge clk);" in built.code
    assert "assert (u_dut.full !== 1'b1)" in built.code
    assert testbench.REPRO_MARKER in built.code
    assert built.cycles < clock.n_cycles, "the repro is not shorter than the run"


def test_a_trace_without_a_clock_period_is_refused(chain, sub):
    el, store, _clock, result = chain
    with pytest.raises(testbench.ReproError, match="needs a clock"):
        testbench.generate(sub, el.graph, store, None, el, root=result.root)


# --- §8.3, the acceptance criterion ----------------------------------------


@needs_simulator
def test_the_generated_testbench_reproduces_the_failure(chain, sub, tmp_path):
    """**Prompt 13's acceptance criterion.**

    Not "it compiles" and not "it looks right": the simulator is run and its own
    verdict is asserted on. §8.3's loop backs off through three fidelities when
    the reduction was too aggressive, so a pass here also means the ladder works.
    """
    el, store, clock, result = chain
    built = testbench.build(
        sub,
        el.graph,
        store,
        clock,
        el,
        root=result.root,
        sources=[BUGGY / "fifo_buggy.sv"],
        work=tmp_path / "repro",
    )
    v = built.validation
    assert v.ran, v.error + "\n" + v.output[-2000:]
    assert v.reproduced, v.error + "\n" + v.output[-2000:]
    assert v.tool in ("iverilog", "verilator")
    # And it is a *reduction*: fewer cycles and fewer driven inputs than the run.
    assert built.cycles < clock.n_cycles
    assert len(built.driven) < 5


@needs_simulator
def test_a_repro_that_needed_every_input_is_not_called_minimal(chain, sub, tmp_path):
    """§8.3 refuses to let a window cut be called a minimisation. Asking for
    `focused` explicitly must produce a testbench labelled that way."""
    el, store, clock, result = chain
    built = testbench.build(
        sub,
        el.graph,
        store,
        clock,
        el,
        root=result.root,
        sources=[BUGGY / "fifo_buggy.sv"],
        work=tmp_path / "focused",
        mode="focused",
    )
    assert built.mode == "focused"
    assert "FOCUSED" in built.code and "not a minimal one" in built.code
    assert not built.tied, "a focused testbench replays everything"


def test_without_sources_the_result_says_it_was_not_validated(chain, sub):
    el, store, clock, result = chain
    built = testbench.build(
        sub, el.graph, store, clock, el, root=result.root, sources=[], validate_it=True
    )
    assert not built.validation.ran
    assert any("not validated" in n for n in built.notes)


# --- §12, the report --------------------------------------------------------


@pytest.fixture(scope="module")
def report(chain, sub):
    from veritrace.export import report as report_mod

    el, store, clock, result = chain
    return report_mod.build(
        result.root,
        sub,
        store,
        clock,
        query=f"why({SYMPTOM})",
        sources={p.name: p for p in BUGGY_RTL},
        top="tb_fifo_buggy",
    )


def test_the_report_is_standalone(report):
    """§12: no CDN, no fetch, no external asset. The only URL allowed on the page
    is the SVG namespace, which is an identifier and not a request."""
    import re

    urls = set(re.findall(r"https?://[^\"'\s)]+", report.html))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls
    assert "<script src" not in report.html
    assert "<link rel=\"stylesheet\" href" not in report.html


def test_the_report_has_the_sections_12_lists(report):
    for heading in ("Causal chain", "Minimal subtrace", "Waveform"):
        assert f">{heading}<" in report.html, heading
    assert "full stuck at 1" in report.html
    assert "rd_rst_n" in report.html
    # Section 5 is a real drawing, not a placeholder.
    assert "<svg" in report.html and 'class="sig"' in report.html


def test_the_report_fits_in_12s_budget(report):
    from veritrace.export import report as report_mod

    assert report.bytes < report_mod.SIZE_TARGET
    assert not report.oversize


def test_source_snippets_are_escaped(report):
    """The RTL is full of `<=`. Unescaped, the first non-blocking assignment in a
    snippet ends the page."""
    assert "&lt;=" in report.html or "<=" not in report.html


# --- §13, the CLI -----------------------------------------------------------


def test_repro_command_writes_a_testbench(tmp_path):
    out = tmp_path / "tb.sv"
    got = CliRunner().invoke(
        main,
        [
            "repro",
            str(BUGGY / "dump.vcd"),
            f"why({SYMPTOM})",
            "--rtl",
            str(BUGGY),
            "-o",
            str(out),
            "--no-validate",
        ],
    )
    assert got.exit_code == 0, got.output
    assert out.is_file()
    assert "module tb_repro_full;" in out.read_text(encoding="utf-8")


def test_export_command_writes_a_report(tmp_path):
    out = tmp_path / "bug.html"
    got = CliRunner().invoke(
        main,
        [
            "export",
            str(BUGGY / "dump.vcd"),
            "--why",
            f"why({SYMPTOM})",
            "--rtl",
            str(BUGGY),
            "-o",
            str(out),
            "--no-repro",
        ],
    )
    assert got.exit_code == 0, got.output
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert "full stuck at 1" in text


# --- §10.1, the routes ------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    # A `.vtx`, not the raw dump: conversion is the CLI's job (`_default_trace`),
    # and a session is handed a store it can open.
    return TestClient(create_app(str(design_store("fifo_buggy")), [str(BUGGY)]))


def _session(client) -> str:
    return client.get("/", headers={"Accept": "application/json"}).json()["default_session"]


def test_subtrace_route_returns_narrated_steps(client):
    sid = _session(client)
    got = client.post(f"/session/{sid}/subtrace", json={"vtq": f"why({SYMPTOM})"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["title"] == "full stuck at 1"
    assert body["steps"] and all(s["text"] for s in body["steps"])
    assert body["steps"][0]["is_root_cause"]
    assert body["dropped"] > 0


def test_repro_route_can_skip_the_simulator(client):
    sid = _session(client)
    got = client.post(
        f"/session/{sid}/repro", json={"vtq": f"why({SYMPTOM})", "validate": False}
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["mode"] in ("minimal", "focused")
    assert body["code"].startswith("`timescale")
    assert not body["validation"]["ran"]


def test_export_route_returns_an_attachment(client):
    sid = _session(client)
    got = client.post(
        f"/session/{sid}/export", json={"vtq": f"why({SYMPTOM})", "validate": False}
    )
    assert got.status_code == 200, got.text
    assert "attachment" in got.headers["content-disposition"]
    assert got.text.startswith("<!doctype html>")


def test_asking_the_same_question_twice_reuses_the_tree(client):
    """The tree behind `/query`, `/subtrace`, `/repro` and `/export` is one
    object; rebuilding it per route would make pressing Replay a second full
    why-trace."""
    sid = _session(client)
    session = client.app.state.registry.get(sid)
    session._why.clear()
    client.post(f"/session/{sid}/query", json={"vtq": f"why({SYMPTOM})"})
    client.post(f"/session/{sid}/subtrace", json={"vtq": f"why({SYMPTOM})"})
    assert len(session._why) == 1
