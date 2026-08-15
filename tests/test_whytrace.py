"""Why-trace — §8.1.

The golden test is the point of this file: a bug is injected into a copy of
fifo_async, and why-trace must land on *that* signal, not merely produce a
plausible-looking tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from veritrace import TraceStore, convert
from veritrace.analysis import vtq
from veritrace.analysis.whytrace import NodeKind, Reason, WhyTracer
from veritrace.api import create_app
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
BUGGY = DESIGNS / "fifo_buggy"
BUGGY_RTL = [BUGGY / "tb_fifo_buggy.sv", BUGGY / "fifo_buggy.sv"]
GOOD = DESIGNS / "fifo_async"
GOOD_RTL = [GOOD / "tb_fifo_sync.sv", GOOD / "fifo_sync.sv"]

#: Budget from §4.2.
WHY_BUDGET_MS = 150.0


def build(tmp_path: Path, rtl, vcd: Path, name: str):
    out = tmp_path / f"{name}.vtx"
    convert(str(vcd), str(out))
    store = TraceStore(str(out))
    el = elaborate(rtl)
    rep = correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    return el.graph, store, rep


@pytest.fixture(scope="module")
def buggy(tmp_path_factory):
    return build(tmp_path_factory.mktemp("buggy"), BUGGY_RTL, BUGGY / "dump.vcd", "buggy")


# --- the golden case -------------------------------------------------------


def test_finds_the_injected_root_cause(buggy):
    """`full` is stuck high; the cause is `rd_rst_n`, tied low three hops away.

    Asserting the exact root, not just "a tree came back" — a why-trace that
    returns the wrong signal confidently is worse than none at all.
    """
    graph, store, rep = buggy
    assert rep.percent >= 98.0, rep.summary()

    _, t_end = store.time_range
    full = store.handle("tb_fifo_buggy.dut.full")
    assert store.value_at(full, t_end).to_int() == 1, "symptom not reproduced"

    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.full", t_end)

    # The primary path is the chain the heuristic ranks first; follow it down.
    chain, node = [], result.root
    while True:
        chain.append(node)
        nxt = next((c for c in node.children if c.is_primary_path), None)
        if nxt is None:
            break
        node = nxt

    paths = [n.signal.path() for n in chain]
    assert paths[0] == "tb_fifo_buggy.dut.full"
    assert "tb_fifo_buggy.dut.rd_ptr" in paths, paths
    assert paths[-1] == "tb_fifo_buggy.dut.rd_rst_n", paths

    root_cause = chain[-1]
    assert root_cause.kind is NodeKind.TERMINAL
    assert root_cause.reason is Reason.CONSTANT
    # And it is localised (P2).
    assert root_cause.loc is not None
    assert root_cause.loc.file == "fifo_buggy.sv"
    assert root_cause.loc.line > 0


def test_why_stays_within_the_latency_budget(buggy):
    graph, store, _ = buggy
    _, t_end = store.time_range
    tracer = WhyTracer(graph, store)
    result = tracer.why("tb_fifo_buggy.dut.full", t_end)
    assert result.elapsed_ms < WHY_BUDGET_MS, f"{result.elapsed_ms:.1f} ms"
    assert result.nodes < 200, "a real design converges well under 200 nodes (§8.1)"


def test_result_is_deterministic(buggy):
    """P1: same input, same answer, every time."""
    graph, store, _ = buggy
    _, t_end = store.time_range
    runs = [
        WhyTracer(graph, store).why("tb_fifo_buggy.dut.full", t_end).root.to_dict()
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]


def test_healthy_design_does_not_blame_a_reset(tmp_path):
    """The same question on the working FIFO must not reach a stuck reset."""
    graph, store, _ = build(tmp_path, GOOD_RTL, GOOD / "dump.vcd", "good")
    _, t_end = store.time_range
    result = WhyTracer(graph, store).why("tb_fifo_sync.dut.full", t_end)
    reasons = {n.reason for n in result.root.walk()}
    assert Reason.CONSTANT not in reasons or "rd_rst_n" not in str(result.root.to_dict())


# --- algorithm behaviour ---------------------------------------------------


def test_terminals_are_classified(buggy):
    graph, store, _ = buggy
    _, t_end = store.time_range
    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.full", t_end)
    kinds = {n.reason for n in result.root.walk()}
    # The injected constant terminates the chain, and the walk crosses the port
    # boundary out of the DUT to reach the testbench signals feeding it — which
    # are primary inputs as far as the design is concerned.
    assert Reason.CONSTANT in kinds
    assert Reason.PRIMARY_INPUT in kinds


def test_testbench_stimulus_is_a_primary_input_not_an_undriven_net(buggy):
    """§8.1 distinguishes "comes from outside" from "nothing drives this".

    A signal only an `initial` block writes has no design driver, but it is not
    floating either — calling it undriven would report the testbench as a bug.
    """
    graph, _, _ = buggy
    rst = graph.get("tb_fifo_buggy.rst_n")
    assert rst is not None and not rst.drivers
    assert rst.stimulus_only


def test_hold_explains_why_no_driver_fired(buggy):
    """A signal that kept its value asks "why was every guard false" (§8.1)."""
    graph, store, _ = buggy
    _, t_end = store.time_range
    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.wr_ptr", t_end)
    assert result.root.kind is NodeKind.HOLD
    assert result.root.children, "a hold must name the signals keeping it held"
    # wr_en is the guard term that is false.
    assert any("wr_en" in c.signal.path() for c in result.root.children)


def test_feedback_loops_terminate(buggy):
    """Feedback is normal in RTL (§5.4); it must end the branch, not the run."""
    graph, store, _ = buggy
    _, t_end = store.time_range
    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.full", t_end)
    assert result.nodes > 0
    assert not result.truncated


def test_ordering_puts_the_oldest_transition_first(buggy):
    """The §8.1 heuristic: what has not moved is the suspect."""
    graph, store, _ = buggy
    _, t_end = store.time_range
    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.full", t_end)
    kids = result.root.children
    assert len(kids) >= 2
    assert kids[0].is_primary_path and not kids[1].is_primary_path
    times = [k.last_change if k.last_change is not None else -1 for k in kids]
    assert times == sorted(times), times


def test_sequential_signals_are_explained_at_the_clock_edge(buggy):
    """§5.5 problem 2 inside why-trace, not just in the store.

    A register is explained by the edge that produced it, so its causes are
    evaluated at that edge — not at the time the question was asked.
    """
    graph, store, _ = buggy
    _, t_end = store.time_range
    clk = store.handle("tb_fifo_buggy.dut.clk")
    edges = [t for t, v in store.transitions(clk, 0, t_end + 1) if v.to_int() == 1]
    assert edges, "the clock never rises"

    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.rd_ptr", t_end)
    assert result.root.time == t_end
    assert result.root.children
    # Causes sit on the last rising edge at or before the question's time.
    assert result.root.children[0].time == edges[-1]

    # Asking mid-cycle still resolves back to the preceding edge.
    mid = edges[-1] - 1
    result2 = WhyTracer(graph, store).why("tb_fifo_buggy.dut.rd_ptr", mid)
    assert result2.root.children[0].time == edges[-2]


# --- VTQ parsing -----------------------------------------------------------


def test_parse_why_forms():
    q = vtq.parse("why(top.ctrl.ready == 0 @ c1247)")
    assert q.signal == "top.ctrl.ready" and q.value == "0" and q.time == 1247 and q.is_cycle
    q = vtq.parse("why(top.a @ 500)")
    assert q.signal == "top.a" and q.time == 500 and not q.is_cycle
    q = vtq.parse("why(top.a)")
    assert q.signal == "top.a" and q.time is None


def test_parse_rejects_other_queries():
    for bad in ["cone(top.a)", "why", "", "why()"]:
        with pytest.raises(vtq.QueryError):
            vtq.parse(bad)


# --- API -------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    out = tmp / "buggy.vtx"
    convert(str(BUGGY / "dump.vcd"), str(out))
    app = create_app(default_trace=out, rtl=[str(p) for p in BUGGY_RTL])
    with TestClient(app) as c:
        yield c


def sid(client) -> str:
    return client.get("/").json()["default_session"]


def test_status_reports_correlation_and_provenance(client):
    body = client.get(f"/session/{sid(client)}/status").json()
    assert body["has_rtl"] is True
    assert body["correlation_rate"] >= 98.0
    assert body["rtl_sha256"] and len(body["rtl_sha256"]) == 64
    assert body["rtl_changed"] is False


def test_query_endpoint_returns_the_causal_tree(client):
    r = client.post(
        f"/session/{sid(client)}/query",
        json={"vtq": "why(tb_fifo_buggy.dut.full == 1 @ 455000)"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"]["signal"] == "tb_fifo_buggy.dut.full"
    assert body["stats"]["nodes"] > 1
    assert body["stats"]["ms"] < WHY_BUDGET_MS
    # The injected cause is somewhere in the returned tree.
    assert "rd_rst_n" in r.text


def test_query_rejects_unknown_signal_and_bad_syntax(client):
    s = sid(client)
    assert client.post(f"/session/{s}/query", json={"vtq": "why(nope.nope @ 10)"}).status_code == 404
    assert client.post(f"/session/{s}/query", json={"vtq": "cone(x)"}).status_code == 400


def test_source_endpoint_serves_rtl_with_line_decorations(client):
    r = client.get(f"/session/{sid(client)}/source/fifo_buggy.sv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "module fifo_buggy" in body["text"]
    assert body["signals"], "expected per-line signal decorations"
    # Every decoration names a real line of the file.
    n_lines = len(body["text"].splitlines())
    assert all(1 <= int(k) <= n_lines for k in body["signals"])


def test_source_404_for_unknown_file(client):
    assert client.get(f"/session/{sid(client)}/source/nope.sv").status_code == 404


def test_cycle_times_resolve_against_the_primary_clock(client):
    r = client.post(f"/session/{sid(client)}/query", json={"vtq": "why(tb_fifo_buggy.dut.full @ c20)"})
    assert r.status_code == 200, r.text
    assert r.json()["time"] > 0


# --- graceful degradation without RTL (§7.4) -------------------------------


def test_without_rtl_the_viewer_still_works(tmp_path):
    out = tmp_path / "norlt.vtx"
    convert(str(GOOD / "dump.vcd"), str(out))
    with TestClient(create_app(default_trace=out)) as c:
        s = c.get("/").json()["default_session"]
        status = c.get(f"/session/{s}/status").json()
        assert status["has_rtl"] is False
        assert status["correlation_rate"] is None
        # Waveform data is unaffected.
        assert status["n_signals"] > 0
        # why() says what is missing instead of failing obscurely.
        r = c.post(f"/session/{s}/query", json={"vtq": "why(tb_fifo_sync.dut.full @ 100)"})
        assert r.status_code == 409
        assert "--rtl" in r.json()["detail"]
        assert c.get(f"/session/{s}/source/fifo_sync.sv").status_code == 404


def test_rtl_change_is_detected_and_warned_not_blocked(tmp_path):
    """§5.7: editing RTL after the run must be noticed, and must not block."""
    out = tmp_path / "prov.vtx"
    convert(str(BUGGY / "dump.vcd"), str(out))

    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    for p in BUGGY_RTL:
        (rtl_dir / p.name).write_text(p.read_text())
    files = [str(rtl_dir / p.name) for p in BUGGY_RTL]

    with TestClient(create_app(default_trace=out, rtl=files)) as c:
        s = c.get("/").json()["default_session"]
        assert c.get(f"/session/{s}/status").json()["rtl_changed"] is False

    # Edit the RTL, reopen against the same trace.
    target = rtl_dir / "fifo_buggy.sv"
    target.write_text(target.read_text() + "\n// touched after the run\n")

    with TestClient(create_app(default_trace=out, rtl=files)) as c:
        s = c.get("/").json()["default_session"]
        status = c.get(f"/session/{s}/status").json()
        assert status["rtl_changed"] is True, "stale RTL went unnoticed"
        # Still fully usable — a warning, not a wall.
        assert status["has_rtl"] is True
        assert c.post(
            f"/session/{s}/query", json={"vtq": "why(tb_fifo_buggy.dut.full @ 455000)"}
        ).status_code == 200


# --- §5.5 problem 2, at the level that actually matters ---------------------


def test_a_register_is_explained_by_the_value_going_into_the_edge(buggy):
    """The NBA rule of §5.5, asserted on *values* and not only on times.

    `test_sequential_signals_are_explained_at_the_clock_edge` checks that the
    causes sit on the right edge. That passes even if the tracer reads them
    with `value_at` instead of `value_before` — and reading them at the edge is
    exactly the mistake §5.5 calls the number-one source of wrong answers, so
    the distinction needs a test of its own.
    """
    graph, store, _ = buggy
    clk = store.handle("tb_fifo_buggy.dut.clk")
    _, t_end = store.time_range
    edges = [t for t, v in store.transitions(clk, 0, t_end + 1) if v.to_int() == 1]

    # A signal that changes *at* an edge: its pre-edge and post-edge values
    # differ, so which one the walk used is observable.
    ptr = store.handle("tb_fifo_buggy.dut.wr_ptr")
    edge = next(
        (
            t
            for t in edges
            if store.value_before(ptr, t) is not None
            and store.value_at(ptr, t).bits != store.value_before(ptr, t).bits
        ),
        None,
    )
    assert edge is not None, "no register in this trace changes on an edge"

    node = WhyTracer(graph, store).why("tb_fifo_buggy.dut.wr_ptr", edge).root
    before = store.value_before(ptr, edge)
    after = store.value_at(ptr, edge)
    assert before.to_int() != after.to_int()  # the fixture does what the test needs
    # The question is about the settled value at `edge`...
    assert int(node.value, 2) == after.to_int()

    # ...and every cause evaluated at that edge must carry the value it had
    # going *into* it. Reading them at the edge is the §5.5 mistake, and it is
    # visible on any cause that also moved at this instant.
    checked = 0
    for child in node.children:
        h = store.handle(child.signal.path())
        if h is None or child.time != edge:
            continue
        pre, post = store.value_before(h, edge), store.value_at(h, edge)
        if pre is None or post is None or pre.to_int() == post.to_int():
            continue
        assert int(child.value, 2) == pre.to_int(), (
            f"{child.signal.path()} was read at the edge, not before it — §5.5"
        )
        checked += 1
    assert checked or node.children, "the register had no causes to check"


# --- §5.6 --------------------------------------------------------------------


def test_a_memory_read_names_the_element_and_the_write(tmp_path_factory):
    """§5.6, end to end: evaluate the index, name `mem[i]`, jump to the write.

    Before this, the walk descended into the array as a whole, found it
    unreadable, and blamed whichever guard term happened to be false — a
    confident answer about the wrong signal.
    """
    graph, store, _ = build(tmp_path_factory.mktemp("mem"), GOOD_RTL, GOOD / "dump.vcd", "good")
    result = WhyTracer(graph, store).why("tb_fifo_sync.dut.rd_data", store.time_range[1])
    nodes = list(result.root.walk())

    elem = next((n for n in nodes if "mem[" in n.signal.path()), None)
    assert elem is not None, "the chain never named a memory element"
    assert elem.time < result.root.time, "no jump back to the write"
    # And the write's own cause is in the tree: `wr_data`, not the read's guards.
    assert any("wr_data" in n.signal.path() for n in nodes)


def test_an_index_is_a_cause_of_the_value_it_selects(tmp_path_factory):
    graph, store, _ = build(tmp_path_factory.mktemp("idx"), GOOD_RTL, GOOD / "dump.vcd", "good")
    result = WhyTracer(graph, store).why("tb_fifo_sync.dut.rd_data", store.time_range[1])
    paths = {n.signal.path() for n in result.root.walk()}
    assert any("rd_ptr" in p for p in paths), "the index never appeared in the chain"


def test_an_index_within_the_declared_depth_is_not_out_of_range(tmp_path_factory):
    """The bound on an element index is the declared depth — §5.6.

    `lanes` holds 64 words of 8 bits. Deriving the bound from the element width
    made every index above 7 "outside the declared bounds", which stopped the
    chain at one node and named a cause that does not exist.
    """
    lanes = DESIGNS / "lanes"
    graph, store, _ = build(
        tmp_path_factory.mktemp("lanes"), discover(lanes), lanes / "dump.vcd", "lanes"
    )
    mem = graph.get("tb_lanes.dut.g_lane[2].u_fifo.mem")
    assert mem.depth == 64 and mem.width == 8, "fixture no longer has depth != width"

    result = WhyTracer(graph, store).why("tb_lanes.dut.g_lane[2].u_fifo.dout", 200000)
    reasons = {n.reason for n in result.root.walk()}
    assert Reason.OUT_OF_RANGE not in reasons


def test_a_memory_that_was_not_dumped_says_so(tmp_path_factory):
    """§5.6's own instruction: say it plainly instead of answering around it."""
    lanes = DESIGNS / "lanes"
    graph, store, _ = build(
        tmp_path_factory.mktemp("lanes2"), discover(lanes), lanes / "dump.vcd", "lanes"
    )
    result = WhyTracer(graph, store).why("tb_lanes.dut.g_lane[2].u_fifo.dout", 200000)
    node = next(n for n in result.root.walk() if "mem[" in n.signal.path())
    assert node.reason is Reason.NOT_TRACED
    assert "not in the trace" in node.detail
    assert "--trace-max-array" in node.detail


# --- §7.3 --------------------------------------------------------------------


def test_a_reconstructed_value_is_marked_as_derived(tmp_path_factory):
    """P2: inference must never be presented as observation."""
    lanes = DESIGNS / "lanes"
    graph, store, _ = build(
        tmp_path_factory.mktemp("lanes3"), discover(lanes), lanes / "dump.vcd", "lanes"
    )
    derivable = [
        s.path for s in graph if s.trace_handle is None and s.drivers
    ]
    assert derivable, "fixture no longer has a signal that is not in the dump"
    result = WhyTracer(graph, store).why(derivable[0], 200000)
    assert result.root.derived is True
    assert result.root.to_dict()["derived"] is True


# --- §8.1's expected value ---------------------------------------------------


def test_a_question_about_a_value_the_trace_disagrees_with_is_called_out():
    q = vtq.parse("why(top.a == 0 @ c1)")
    assert vtq.expectation(q, "1") is not None
    assert vtq.expectation(q, "0") is None
    # Widths and radices line up rather than comparing text to text.
    assert vtq.expectation(vtq.parse("why(top.a == 0xff @ c1)"), "11111111") is None
    assert vtq.expectation(vtq.parse("why(top.a == 8'hff @ c1)"), "00000001") is not None
    # No stated value, nothing to disagree with.
    assert vtq.expectation(vtq.parse("why(top.a @ c1)"), "1") is None


def test_the_bare_form_from_the_spec_is_accepted():
    q = vtq.parse("top.ctrl.ready == 0 @ c1247")
    assert (q.signal, q.value, q.time, q.is_cycle) == ("top.ctrl.ready", "0", 1247, True)
    # ...without swallowing a typo that is not a question at all.
    for bad in ["why", "cone(top.a)", ""]:
        with pytest.raises(vtq.QueryError):
            vtq.parse(bad)


def test_json_carries_the_flat_chain_the_cli_documents(buggy):
    """§13's own example is `--json | jq '.chain[0].loc'`.

    The tree under `root` is the complete answer; `chain` is the primary path
    through it, flat, so a shell script does not have to walk a DAG. They must
    describe the same walk — the first entry is the question and the last is
    what `root_cause` concluded.
    """
    from veritrace.analysis.whytrace import root_cause

    graph, store, _ = buggy
    result = WhyTracer(graph, store).why("tb_fifo_buggy.dut.full", 455000)
    payload = result.to_dict()

    chain = payload["chain"]
    assert chain, "no chain in the JSON"
    assert chain[0]["signal"] == result.root.signal.path()
    assert chain[0]["loc"]["file"] and chain[0]["loc"]["line"]
    cause = root_cause(result.root)
    assert chain[-1]["signal"] == cause.signal.path()
    assert chain[-1]["reason"] == cause.reason.value
    # And it is a path, not the whole tree: no node appears twice.
    assert len({n["signal"] for n in chain}) == len(chain)
