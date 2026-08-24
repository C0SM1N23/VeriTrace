"""The transaction engine — §8.13-8.16, TAB 8.

The three acceptance criteria of Prompt 9 are the first three sections:

1. extraction on the AXI4-Lite design finds every transaction, checked against
   the raw handshake edges rather than against itself;
2. the correlation between signals and transactions is reported explicitly;
3. transaction-level why-trace crosses the arbiter to the real cause.

Everything after that defends a specific decision in the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import vtq
from veritrace.analysis.whytrace import NodeKind, WhyTracer
from veritrace.config import Config
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate
from veritrace.protocol import channels, detect, engine, link, pack, persist
from veritrace.protocol import query as txn_query

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


def _open(name: str, tmp_path_factory):
    out = tmp_path_factory.mktemp(name) / "dump.vtx"
    convert(str(DESIGNS / name / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    return store, clock, analysis, out


@pytest.fixture(scope="module")
def axi_lite(tmp_path_factory):
    return _open("axi_lite", tmp_path_factory)


@pytest.fixture(scope="module")
def axi_arb(tmp_path_factory):
    return _open("axi_arb", tmp_path_factory)


def handshakes(store, clock, valid: str, ready: str) -> list[int]:
    """Accepted edges, computed from the store alone.

    Deliberately does not use anything in `veritrace.protocol`: a test that
    reproduces the engine's own logic proves nothing about it.
    """
    hv, hr = store.find(valid), store.find(ready)
    rows = store.sample_before([hv, hr], clock.edges)
    return [
        t
        for i, t in enumerate(clock.edges)
        if rows[0][i] and rows[0][i].to_int() == 1 and rows[1][i] and rows[1][i].to_int() == 1
    ]


# --- acceptance 1: every transaction, verified against the raw trace --------


def test_axi_lite_extraction_matches_the_raw_handshakes(axi_lite):
    store, clock, analysis, _ = axi_lite
    ex = analysis.get("cpu")
    assert ex is not None, [i.name for i in analysis.interfaces]

    p = "tb_axi_lite.s_axi_"
    aw = handshakes(store, clock, p + "awvalid", p + "awready")
    w = handshakes(store, clock, p + "wvalid", p + "wready")
    b = handshakes(store, clock, p + "bvalid", p + "bready")
    ar = handshakes(store, clock, p + "arvalid", p + "arready")
    r = handshakes(store, clock, p + "rvalid", p + "rready")

    writes = [t for t in ex.transactions if t.kind == "WRITE"]
    reads = [t for t in ex.transactions if t.kind == "READ"]
    assert len(writes) == len(aw) == len(w) == len(b) == 12
    assert len(reads) == len(ar) == len(r) == 12

    # More than the sample of 20 the criterion asks for: all 24, each pinned to
    # the exact edges its channels transferred on.
    for n, t in enumerate(writes):
        assert t.beats("AW")[0].time == aw[n]
        assert t.beats("W")[0].time == w[n]
        assert t.end_time == b[n]
    for n, t in enumerate(reads):
        assert t.beats("AR")[0].time == ar[n]
        assert t.end_time == r[n]


def test_payload_fields_are_the_values_the_trace_carried(axi_lite):
    store, _clock, analysis, _ = axi_lite
    ex = analysis.get("cpu")
    h_addr = store.find("tb_axi_lite.s_axi_awaddr")
    h_data = store.find("tb_axi_lite.s_axi_wdata")
    for t in [x for x in ex.transactions if x.kind == "WRITE"]:
        at = t.beats("AW")[0].time
        assert t.fields["addr"] == store.value_before(h_addr, at).to_int()
        assert t.beats("W")[0].fields["wdata"] == store.value_before(h_data, at).to_int()


def test_latency_is_measured_from_issue_as_section_8_13_prints_it(axi_lite):
    """§8.13: "issued c1200 ... resp c1222, latency 22 cycles" — from the edge
    valid went high, not from the edge it was accepted on."""
    _store, clock, analysis, _ = axi_lite
    t = analysis.get("cpu").transactions[0]
    # In cycles, which is what §8.13 prints and what §8.17's histogram plots.
    assert t.metrics["latency"] == clock.cycle_of(t.end_time) - clock.cycle_of(t.start_time)
    assert t.metrics["latency"] == (t.end_time - t.start_time) // clock.period
    # Two wait states in the slave, so acceptance is two cycles after issue —
    # and in cycles, like the latency beside it. These two sit in the same row
    # of the Transactions table, so a reader has nothing but the unit to go on;
    # while this one was in raw ticks the table read "latency 9, addr_latency
    # 80000" for a wait of eight cycles.
    assert t.metrics["addr_latency"] == 2
    assert t.metrics["addr_latency"] == (
        clock.cycle_of(t.beats("AW")[0].time) - clock.cycle_of(t.beats("AW")[0].assert_time)
    )
    assert t.start_time == t.beats("AW")[0].assert_time
    assert t.beats("AW")[0].time > t.start_time


def test_transactions_are_numbered_stably_per_kind(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    ex = analysis.get("cpu")
    for kind in ("WRITE", "READ"):
        idx = [t.index for t in ex.transactions if t.kind == kind]
        assert idx == list(range(len(idx)))
    assert ex.transactions[0].ref.startswith("cpu.")


# --- acceptance 2: the correlation rate is stated ---------------------------


def test_correlation_between_signals_and_transactions_is_reported(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    ex = analysis.get("cpu")
    assert ex.n_events == 60  # 12 x (AW, W, B) + 12 x (AR, R)
    assert ex.n_matched == ex.n_events
    assert ex.correlation == 100.0
    assert ex.to_dict()["correlation"] == 100.0


def test_an_interface_with_no_traffic_has_no_rate_rather_than_zero_or_a_hundred():
    """P7: inventing either number would be a claim the run does not support."""
    from veritrace.protocol.model import Extraction, Interface

    p = pack.discover()[0]
    empty = Extraction(interface=Interface("i", "s", "", p, {}))
    assert empty.correlation is None
    assert empty.to_dict()["correlation"] is None


# --- acceptance 3: why-trace crosses the arbiter (§8.16) --------------------


def test_why_a_transaction_was_not_issued_reaches_the_other_master(axi_arb):
    """The whole point of §8.16: the cause of m0's silence is not in m0."""
    store, clock, analysis, out = axi_arb
    el = elaborate(discover(DESIGNS / "axi_arb"))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    index = link.TxnIndex.build(analysis, store)

    ref = vtq.parse("why(txn.m0.WRITE[0].not_issued)").txn
    assert ref is not None
    q = link.question(analysis, index, store, clock, ref)
    assert q.signal.endswith("awvalid")
    # The instant asked about must be one where m0 really was silent.
    assert store.value_at(store.find(q.signal), q.time).to_int() == 0

    result = WhyTracer(el.graph, store, txn_index=index).why(q.signal, q.time)
    nodes = list(result.root.walk())

    # The chain crossed the arbiter into the *other* master's transaction.
    links = [n for n in nodes if n.kind is NodeKind.TXN_LINK]
    assert links, "no TXN_LINK node: the chain never left signal level"
    assert any(n.txn.startswith("m1.") for n in links), [n.txn for n in links]

    # And it went through the arbiter to get there, rather than guessing.
    paths = {n.signal.path() for n in nodes}
    assert any("grant" in p for p in paths), sorted(paths)
    assert any(p.endswith("arb.owner") or p.endswith("arb.lock") for p in paths)


def test_the_transaction_hop_continues_into_what_that_transaction_waits_for(axi_arb):
    """§8.16 inserts the node *and carries on from there*, so the answer keeps
    descending: m0's signal -> m1's transaction -> the slave's."""
    store, clock, analysis, _ = axi_arb
    el = elaborate(discover(DESIGNS / "axi_arb"))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    index = link.TxnIndex.build(analysis, store)
    q = link.question(analysis, index, store, clock, vtq.parse("why(txn.m0.WRITE[0].not_issued)").txn)
    root = WhyTracer(el.graph, store, txn_index=index).why(q.signal, q.time).root

    ifaces = {n.txn.split(".")[0] for n in root.walk() if n.kind is NodeKind.TXN_LINK}
    assert {"m1", "slv"} <= ifaces, ifaces


def test_a_transaction_question_says_what_was_actually_asked(axi_arb):
    store, clock, analysis, _ = axi_arb
    index = link.TxnIndex.build(analysis, store)
    q = link.question(analysis, index, store, clock, vtq.parse("why(txn.m0.WRITE[0].not_issued)").txn)
    assert "m0" in q.headline and "WRITE#0" in q.headline


def test_a_transaction_reference_that_names_nothing_says_so(axi_arb):
    store, clock, analysis, _ = axi_arb
    index = link.TxnIndex.build(analysis, store)
    with pytest.raises(ValueError, match="no interface named"):
        link.question(analysis, index, store, clock, vtq.TxnRef("nope", "WRITE", 0))
    with pytest.raises(ValueError, match="no transaction type"):
        link.question(analysis, index, store, clock, vtq.TxnRef("m0", "BURST", 0))


def test_the_index_finds_a_transaction_under_any_of_its_names(axi_arb):
    """A master port, the slave port and the wire between them are one net; the
    index is keyed on the event stream so all three resolve identically."""
    store, _clock, analysis, _ = axi_arb
    index = link.TxnIndex.build(analysis, store)
    t = analysis.get("m1").transactions[3]
    mid = (t.start_time + (t.end_time or t.start_time)) // 2
    assert index.at(store, "tb_axi_arb.m1.awvalid", mid) is t
    assert index.at(store, "tb_axi_arb.m1_awvalid", mid) is t
    assert index.at(store, "tb_axi_arb.aclk", mid) is None


# --- detection (§8.14 step 1) ----------------------------------------------


def test_the_same_wires_are_one_interface_not_three(axi_lite):
    """The master port, the slave port and the testbench wire are one bus."""
    _store, _clock, analysis, _ = axi_lite
    assert len(analysis.extractions) == 1
    iface = analysis.interfaces[0]
    assert iface.name == "cpu"
    assert set(iface.aliases) == {"regs", "tb_axi_lite.s_axi"}


def test_three_masters_and_a_slave_are_three_interfaces(axi_arb):
    """§11.4b counts interfaces to choose a default tab, so the count matters."""
    _store, _clock, analysis, _ = axi_arb
    assert {i.name for i in analysis.interfaces} == {"m0", "m1", "slv"}


def test_a_generic_pack_does_not_shadow_a_specific_one(axi_lite):
    """handshake.vtp matches every AXI channel. Reporting five extra `interfaces`
    per bus would make the tab useless."""
    _store, _clock, analysis, _ = axi_lite
    assert {i.pack.slug for i in analysis.interfaces} == {"axi4lite"}


def test_an_interface_is_addressable_by_a_short_unambiguous_suffix(axi_arb):
    _store, _clock, analysis, _ = axi_arb
    assert analysis.get("m0") is analysis.get("m0")
    assert analysis.get("nope") is None


def test_the_interface_clock_and_reset_are_found(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    i = analysis.interfaces[0]
    assert i.clock and i.clock.endswith("aclk")
    assert i.reset and i.reset.endswith("aresetn")


def test_prefix_inference_finds_a_bus_nobody_declared_a_prefix_for(axi_arb):
    """`prefix_strip = ["*"]` reads the prefix out of the design, which is what
    makes the generic handshake pack usable at all."""
    store, _clock, _analysis, _ = axi_arb
    hs = [p for p in pack.discover() if p.slug == "handshake"]
    found = detect.detect(store, hs, Config.empty())
    assert found, "handshake pack matched nothing"
    assert any("valid" in s for i in found for s in i.signals)


def test_a_near_miss_names_the_suffix_that_was_not_found(axi_lite):
    """The pack author's whole debugging loop (docs/PACKS.md): one suffix spelt
    differently rejects the interface, and silence is indistinguishable from a
    bus the design does not have."""
    store, *_ = axi_lite
    typo = pack.loads(
        'name = "House AXI"\n'
        "[detect]\n"
        'required_suffixes = ["awvalid", "awready", "awaddress"]\n'
        'prefix_strip = [""]\n'
        "[[channel]]\n"
        'name = "AW"\nvalid = "awvalid"\nready = "awready"\n'
        "[[transaction]]\n"
        'name = "WRITE"\nstart = "AW"\n'
    )
    near = detect.near_misses(store, typo)
    assert near and all(missing == ["awaddress"] for _where, missing in near)


def test_a_pack_that_lost_on_specificity_is_not_reported_as_absent(axi_lite):
    # Every signal AXI4 requires is present in an AXI4-Lite design; it loses the
    # tie to the more specific pack. Saying "not in this trace" would be false.
    store, *_ = axi_lite
    (axi4,) = [p for p in pack.discover() if p.slug == "axi4"]
    assert any(missing == [] for _where, missing in detect.near_misses(store, axi4))


def test_a_scope_can_be_excluded_from_detection(axi_arb):
    store, _clock, _analysis, _ = axi_arb
    cfg = Config.empty()
    cfg.protocol_ignore = ["tb_axi_arb.m1"]
    names = {i.name for i in detect.detect(store, pack.resolve(["axi4lite"]), cfg)}
    assert "m1" not in names and "m0" in names


# --- reset and the channel scan (§8.14 step 2) ------------------------------


def test_reset_polarity_is_read_from_the_trace_not_from_the_name():
    """A run starts in reset and leaves it, so the level it starts at is the
    asserted one. Names are the fallback, not the rule."""
    from veritrace.protocol.model import Interface

    p = pack.discover()[0]
    low = Interface("i", "s", "", p, {}, reset="top.weird_name")
    assert channels.reset_polarity(low, [False, False, True, True]) is True
    assert channels.reset_polarity(low, [True, True, False, False]) is False


def test_an_explicit_config_overrides_both(axi_lite):
    from veritrace.protocol.model import Interface

    cfg = Config.empty()
    cfg.reset_active = "high"
    i = Interface("i", "s", "", pack.discover()[0], {}, reset="top.rst_n")
    assert channels.reset_polarity(i, [False, True], cfg) is False


def test_nothing_is_extracted_while_reset_is_asserted(axi_lite):
    """Every protocol here forbids transfers during reset; a bus idling at X
    would otherwise produce a burst of phantom beats at time zero."""
    store, clock, analysis, _ = axi_lite
    ex = analysis.get("cpu")
    reset_release = store.next_change_after(store.find("tb_axi_lite.aresetn"), 0)
    assert all(t.start_time >= reset_release for t in ex.transactions)


def test_stall_cycles_count_the_waiting_the_slave_imposed(axi_lite):
    """Two wait states on each of the address and data channels."""
    _store, _clock, analysis, _ = axi_lite
    write = analysis.get("cpu").transactions[0]
    assert write.metrics["stall_cycles"] == 4
    assert write.beats("AW")[0].stall == 2


# --- assembly (§8.14 step 3) ------------------------------------------------


def test_a_beat_that_arrives_before_its_address_is_not_lost():
    """AXI permits write data ahead of the address phase. Dropping those beats
    would silently under-count every such burst."""
    from veritrace.protocol.assemble import assemble
    from veritrace.protocol.channels import ChannelScan
    from veritrace.protocol.model import ChannelEvent, Interface

    p = pack.resolve(["axi4lite"])[0]
    iface = Interface("i", "s", "", p, {})
    scans = {
        "AW": ChannelScan("AW", [ChannelEvent("AW", 20, 20, 0, {"awaddr": 4})]),
        "W": ChannelScan("W", [ChannelEvent("W", 10, 10, 0, {"wdata": 7})]),
        "B": ChannelScan("B", [ChannelEvent("B", 30, 30, 0, {"bresp": 0})]),
        "AR": ChannelScan("AR"),
        "R": ChannelScan("R"),
    }
    out = assemble(iface, scans, None)
    assert len(out.transactions) == 1
    t = out.transactions[0]
    assert t.closed and len(t.beats("W")) == 1
    assert out.n_matched == 3


def test_out_of_order_completion_is_matched_on_the_protocol_id():
    """AXI4's `id` is what makes outstanding and out-of-order possible; the
    engine must use it rather than assuming responses come back in order."""
    from veritrace.protocol.assemble import assemble
    from veritrace.protocol.channels import ChannelScan
    from veritrace.protocol.model import ChannelEvent, Interface

    p = pack.resolve(["axi4"])[0]
    iface = Interface("i", "s", "", p, {})
    aw = [
        ChannelEvent("AW", 10, 10, 0, {"awid": 1, "awaddr": 0x100, "awlen": 0}),
        ChannelEvent("AW", 20, 20, 0, {"awid": 2, "awaddr": 0x200, "awlen": 0}),
    ]
    w = [
        ChannelEvent("W", 12, 12, 0, {"wdata": 0xAA, "wlast": 1}),
        ChannelEvent("W", 22, 22, 0, {"wdata": 0xBB, "wlast": 1}),
    ]
    # Responses come back the other way round.
    b = [
        ChannelEvent("B", 30, 30, 0, {"bid": 2, "bresp": 0}),
        ChannelEvent("B", 40, 40, 0, {"bid": 1, "bresp": 0}),
    ]
    scans = {
        "AW": ChannelScan("AW", aw), "W": ChannelScan("W", w), "B": ChannelScan("B", b),
        "AR": ChannelScan("AR"), "R": ChannelScan("R"),
    }
    out = assemble(iface, scans, None)
    by_id = {t.id: t for t in out.transactions}
    assert by_id[2].end_time == 30
    assert by_id[1].end_time == 40
    assert by_id[1].fields["addr"] == 0x100


def test_a_transaction_that_never_closes_is_kept_not_discarded():
    """It is the most informative row the engine produces — §8.18 is built on
    exactly these."""
    from veritrace.protocol.assemble import assemble
    from veritrace.protocol.channels import ChannelScan
    from veritrace.protocol.model import ChannelEvent, Interface

    p = pack.resolve(["axi4lite"])[0]
    iface = Interface("i", "s", "", p, {})
    scans = {
        "AW": ChannelScan("AW", [ChannelEvent("AW", 10, 10, 0, {"awaddr": 8})]),
        "W": ChannelScan("W", [ChannelEvent("W", 12, 12, 0, {"wdata": 1})]),
        "B": ChannelScan("B"),
        "AR": ChannelScan("AR"), "R": ChannelScan("R"),
    }
    out = assemble(iface, scans, None)
    assert len(out.transactions) == 1
    t = out.transactions[0]
    assert not t.closed and t.status == "open" and t.duration() is None


def test_rules_are_not_judged_on_an_unfinished_transaction():
    """`count(W) == awlen + 1` is false for every burst still in flight when the
    dump stops. That is where the trace ends, not a protocol error."""
    from veritrace.protocol.assemble import check_transaction_rules
    from veritrace.protocol.model import Interface, Transaction

    p = pack.resolve(["axi4"])[0]
    open_txn = Transaction(iface="i", kind="WRITE", index=0, start_time=0)
    assert check_transaction_rules(open_txn, p, None) == []


# --- metrics and rules (§8.14 steps 4-5) ------------------------------------


def test_a_metric_over_a_field_the_design_lacks_is_unknown_not_zero(axi_lite):
    """AXI4-Lite has no AW phase on a read, so `addr_latency` has no value
    there. Reporting 0 would look like an instant acceptance."""
    _store, _clock, analysis, _ = axi_lite
    read = next(t for t in analysis.get("cpu").transactions if t.kind == "READ")
    assert read.metrics["addr_latency"] is None
    assert read.metrics["latency"] is not None


def test_a_response_rule_fires_on_the_deliberately_unmapped_access(axi_lite):
    """designs/axi_lite aims one transaction at an unmapped address, so the
    pack's response rules have something real to catch."""
    _store, _clock, analysis, _ = axi_lite
    ex = analysis.get("cpu")
    rules = {v.rule for v in ex.violations}
    assert {"AXI_SLVERR", "AXI_RRESP"} <= rules
    bad = next(v for v in ex.violations if v.rule == "AXI_SLVERR")
    assert bad.txn == "cpu.WRITE[9]"


def test_a_correct_design_breaks_no_temporal_rule(axi_arb):
    """AXI_AWSTABLE and AXI_NODROP hold across both masters and the slave; a
    false positive here would make every rule suspect."""
    _store, _clock, analysis, _ = axi_arb
    temporal = {"AXI_AWSTABLE", "AXI_ARSTABLE", "AXI_NODROP", "AXI_WDATA_STABLE"}
    for ex in analysis:
        assert not ({v.rule for v in ex.violations} & temporal), ex.interface.name


def test_a_temporal_rule_catches_a_payload_that_moved_before_acceptance():
    """The other half: the rule has to fire when the protocol really is broken."""
    from veritrace.protocol.assemble import check_temporal_rules
    from veritrace.protocol.model import Interface

    class FakeSampler:
        def __init__(self, cols, edges):
            self._c, self.edges = cols, edges

        def prefetch(self, _paths):
            pass

        def column(self, path):
            return self._c.get(path)

    class V:
        """The part of `Value` the cycle environment uses."""

        def __init__(self, n):
            self._n = n

        def to_int(self):
            return self._n

        def has_x(self):
            return self._n is None

        bits = "?"

    edges = [0, 10, 20, 30]
    cols = {
        "p.awvalid": [V(1), V(1), V(1), V(0)],
        "p.awready": [V(0), V(0), V(1), V(0)],
        "p.awaddr": [V(0x40), V(0x80), V(0x80), V(0)],  # moved between edge 0 and 1
    }
    p = pack.resolve(["axi4lite"])[0]
    iface = Interface(
        "i", "s", "", p,
        {"awvalid": "p.awvalid", "awready": "p.awready", "awaddr": "p.awaddr"},
    )
    out, _notes = check_temporal_rules(iface, FakeSampler(cols, edges), [False] * 4, None)
    assert {v.rule for v in out} == {"AXI_AWSTABLE"}
    assert out[0].time == 10


def test_protocol_violations_reach_the_checks_tab(axi_lite):
    """§11.4: a violation is a finding like any other, with a working [why]."""
    from veritrace.analysis import checks as checks_mod
    from veritrace.analysis.findings import Group

    store, clock, analysis, _ = axi_lite
    report = checks_mod.run_all(store, None, None, clock, Config.empty(), analysis)
    protocol = [f for f in report if f.group is Group.PROTOCOL]
    assert protocol
    assert all(f.why and vtq.parse(f.why).signal for f in protocol)
    assert "protocol" not in report.skipped


# --- persistence (§8.14 step 6, §6.3) ---------------------------------------


def test_the_table_is_written_as_parquet_where_section_6_3_says(axi_lite):
    _store, _clock, analysis, out = axi_lite
    ex = analysis.get("cpu")
    p = Path(ex.parquet)
    assert p.parent == Path(out) / "txn"
    assert p.name == "cpu.parquet" and p.is_file()


def test_the_columns_are_the_pack_s_own_names(axi_lite):
    """§6.3's whole argument: a colleague opens this in polars without needing
    the tool or its documentation."""
    _store, _clock, analysis, _ = axi_lite
    cols = persist.read(analysis.get("cpu").parquet)
    assert {"kind", "status", "start_time", "end_time", "duration"} <= set(cols)
    assert {"addr", "awaddr", "latency", "stall_cycles"} <= set(cols)
    assert len(cols["kind"]) == 24


def test_a_field_absent_on_one_transaction_type_is_null_not_zero(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    rows = persist.rows(analysis.get("cpu").parquet)
    read = next(r for r in rows if r["kind"] == "READ")
    assert read["awaddr"] is None
    assert read["araddr"] is not None


def test_a_cached_table_is_reused_only_when_the_inputs_are_identical(
    axi_lite, tmp_path_factory
):
    store, clock, _analysis, out = axi_lite
    iface = detect.detect(store, pack.resolve(["axi4lite"]), Config.empty())[0]

    first = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    assert persist.is_fresh(out, store, iface)
    again = engine.extract(store, out, clock, Config.empty(), use_cache=True)
    assert [t.ref for t in again.transactions] == [t.ref for t in first.transactions]
    assert again.get("cpu").correlation == first.get("cpu").correlation

    # A different pack must invalidate it: a stale table is a silent lie.
    other = detect.detect(store, pack.resolve(["handshake"]), Config.empty())
    if other:
        assert not persist.is_fresh(out, store, other[0])


def test_a_restored_transaction_is_identical_to_a_freshly_extracted_one(axi_lite):
    """Fields and metrics keep their pack-given names in the table, so the
    split between them has to be recorded rather than guessed from the name."""
    store, clock, _a, out = axi_lite
    fresh = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    cached = engine.extract(store, out, clock, Config.empty(), use_cache=True)
    a = fresh.get("cpu").transactions[0]
    b = cached.get("cpu").transactions[0]
    assert (a.ref, a.start_time, a.end_time, a.status) == (b.ref, b.start_time, b.end_time, b.status)
    assert a.fields == b.fields
    assert a.metrics == b.metrics and "latency" in b.metrics
    assert [v.rule for v in a.violations] == [v.rule for v in b.violations]
    assert fresh.get("cpu").n_events == cached.get("cpu").n_events


def test_a_write_that_fails_does_not_lose_the_analysis(axi_lite, monkeypatch):
    """P7: persistence is a cache, not a precondition. A read-only tree, a full
    disk or a locked file costs the table, never the answer."""
    store, clock, _a, out = axi_lite

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(persist, "write", boom)
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    ex = analysis.get("cpu")
    assert ex is not None and len(ex.transactions) == 24
    assert "read-only" in ex.skipped.get("persist", "")


def test_a_column_that_mixes_integers_and_x_is_still_written(tmp_path):
    """A read that came back X part-way through a run is ordinary — the memory
    was not ready yet — and it must not cost the whole table.

    The column type used to be decided from the *first* value, so a column that
    started with integers and later carried four-state digits was declared
    numeric and then failed to write, and §6.3's export was silently skipped for
    that interface. One four-state value makes the column text; the integers
    alongside render into it losslessly.

    Found by running on a real CPU: none of the reference designs has a bus that
    goes unknown mid-run.
    """
    from veritrace._native import read_txn_table, write_txn_table

    path = tmp_path / "mixed.parquet"
    write_txn_table(
        str(path),
        [
            ("index", [0, 1, 2]),
            ("rdata", [0x1234, "xxxxxxxx", 0x5678]),
            ("bresp", [0, None, 2]),
        ],
    )
    got = dict(read_txn_table(str(path)))
    assert got["rdata"] == ["4660", "xxxxxxxx", "22136"]
    # A column that is genuinely numeric is untouched by the rule.
    assert got["bresp"] == [0, None, 2]


# --- VTQ (§10.1) ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,name,args,kwargs",
    [
        ("txn(m0)", "txn", ("m0",), {}),
        ("txn(m0, type=WRITE)", "txn", ("m0",), {"type": "WRITE"}),
        ("txn(m0, addr=0x4000:0x5000)", "txn", ("m0",), {"addr": (0x4000, 0x5000)}),
        ("txn()", "txn", (), {}),
    ],
)
def test_the_call_grammar_parses(text, name, args, kwargs):
    p = vtq.parse_pipeline(text)
    assert p.source.name == name
    assert p.source.args == args
    assert p.source.kwargs == kwargs


def test_pipeline_stages_parse():
    p = vtq.parse_pipeline("txn(m0, type=WRITE) | slowest(5) | limit(2)")
    assert [s.name for s in p.stages] == ["slowest", "limit"]
    assert p.stages[0].args == (5,)


def test_a_transaction_target_parses_at_transaction_level():
    q = vtq.parse("why(txn.dma0.WRITE[128].not_issued)")
    assert q.txn == vtq.TxnRef("dma0", "WRITE", 128, "not_issued")
    assert vtq.parse("why(txn.a.b.READ[3])").txn.aspect == "not_issued"
    with pytest.raises(vtq.QueryError, match="unknown transaction question"):
        vtq.parse("why(txn.a.READ[3].not_purple)")


def test_a_plain_signal_query_is_unchanged():
    q = vtq.parse("why(top.ctrl.ready == 0 @ c1247)")
    assert q.txn is None and q.signal == "top.ctrl.ready"
    assert q.time == 1247 and q.is_cycle


def test_txn_queries_filter_on_fields_and_metrics(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    run = lambda q: txn_query.run(analysis, vtq.parse_pipeline(q))  # noqa: E731
    assert run("txn(cpu)").n == 24
    assert run("txn(cpu, type=WRITE)").n == 12
    assert len(run("txn(cpu, addr=0x0:0x0)").transactions) > 0
    assert all(t.fields["addr"] == 0 for t in run("txn(cpu, addr=0x0:0x0)").transactions)
    assert len(run("txn(cpu) | slowest(3)").transactions) == 3
    assert run("txn(cpu) | open()").transactions == []
    assert len(run("txn(cpu) | violations()").transactions) == 2


def test_a_query_reports_what_it_scanned_not_just_what_matched(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    r = txn_query.run(analysis, vtq.parse_pipeline("txn(cpu, type=WRITE)"))
    assert r.n == 12 and r.scanned == 24 and r.ifaces == ["cpu"]


def test_an_unknown_field_is_an_error_rather_than_an_empty_result(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    with pytest.raises(vtq.QueryError, match="no field or metric"):
        txn_query.run(analysis, vtq.parse_pipeline("txn(cpu, colour=red)"))


def test_an_unknown_interface_lists_the_ones_that_exist(axi_lite):
    _store, _clock, analysis, _ = axi_lite
    with pytest.raises(vtq.QueryError, match="detected: cpu"):
        txn_query.run(analysis, vtq.parse_pipeline("txn(nope)"))


# --- §11.4b -----------------------------------------------------------------


def test_two_or_more_interfaces_opens_on_transactions(axi_arb):
    from veritrace.analysis.checks import default_tab
    from veritrace.analysis.findings import Report

    _store, _clock, analysis, _ = axi_arb
    assert default_tab(Report(), n_interfaces=len(analysis.extractions)) == "transactions"
    assert default_tab(Report(), n_interfaces=1) == "wave"
    assert default_tab(Report(), n_interfaces=5, configured="wave") == "wave"


def test_one_broken_pack_does_not_lose_the_extraction(axi_lite, tmp_path):
    (tmp_path / "packs").mkdir()
    (tmp_path / "packs" / "bad.vtp.toml").write_text("name =", encoding="utf-8")
    store, clock, _a, out = axi_lite
    analysis = engine.extract(
        store, out, clock, Config.empty(), project_root=tmp_path, use_cache=False
    )
    assert analysis.get("cpu") is not None
    assert any("bad" in e for e in analysis.errors)
