"""Performance and liveness — §8.17, §8.18, TAB 9.

The two acceptance criteria of Prompt 10 are the first two sections:

1. the injected deadlock is found automatically when the session opens, with the
   real agents and the real resources — and the *same design without the bug*
   yields no deadlock, which is the half that proves the detector is measuring
   the design rather than announcing itself;
2. stall attribution sums to exactly 100% on a hand-built trace whose causes are
   known cycle by cycle.

The second is built by hand on purpose. Checking that the shares add up on a
real dump proves only that the arithmetic is consistent; checking it against a
trace where every cycle's cause was decided in advance proves the labels are
*right*, which is the claim §8.17 actually makes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import checks, liveness as liveness_mod
from veritrace.analysis.findings import Group
from veritrace.config import Config
from veritrace.correlate.resolver import correlate
from veritrace.graph.elaborate import discover, elaborate
from veritrace.perf import deadlock, metrics, model, stalls
from veritrace.perf import query as perf_query
from veritrace.perf import report as perf_report
from veritrace.protocol import engine, pack
from veritrace.analysis import vtq

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


def _open(design: str, vcd: str, tmp_path_factory):
    """A design opened the way a session opens it."""
    out = tmp_path_factory.mktemp(design) / "dump.vtx"
    convert(str(DESIGNS / design / vcd), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(DESIGNS / design))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    clock = clocks.resolve(store, el.graph, Config.empty())
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    report, edges = perf_report.build(store, analysis.extractions, el.graph, clock)
    return store, el, clock, analysis, report, edges


@pytest.fixture(scope="module")
def deadlocked(tmp_path_factory):
    return _open("deadlock", "dump.vcd", tmp_path_factory)


@pytest.fixture(scope="module")
def healthy(tmp_path_factory):
    """The same RTL with `HOLD` compiled out — the control for every claim below."""
    return _open("deadlock", "dump_ok.vcd", tmp_path_factory)


@pytest.fixture(scope="module")
def arb(tmp_path_factory):
    return _open("axi_arb", "dump.vcd", tmp_path_factory)


# --- acceptance 1: the injected deadlock, found on its own ------------------


def test_the_injected_deadlock_is_found_when_the_session_opens(deadlocked):
    """No query typed, no flag passed: opening the trace is enough (§8.18)."""
    _store, _el, _clock, _analysis, report, _edges = deadlocked
    assert len(report.liveness.deadlocks) == 1, report.liveness.to_dict()
    d = report.liveness.deadlocks[0]
    assert len(d.agents) == 2
    assert d.cycles > deadlock.MIN_CYCLES


def test_the_wait_for_graph_names_the_real_agents_and_the_real_wires(deadlocked):
    """§8.18's warning: the report has to identify the actual agents and
    resources, not merely say that something looks stuck."""
    _store, el, _clock, analysis, report, _edges = deadlocked
    d = report.liveness.deadlocks[0]

    detected = {ex.interface.name for ex in analysis.extractions}
    assert set(d.agents) <= detected, (d.agents, detected)

    for e in d.edges:
        # The resource is a wire that exists, is in the trace, and is the
        # `awready` of the blocked agent's own interface — not an abstraction.
        assert el.graph.get(e.resource) is not None, e.resource
        assert e.resource.endswith("awready"), e.resource
        assert e.holder in d.agents and e.holder != e.agent

    # A cycle: each edge's holder is the next edge's waiter, and it closes.
    holders = [e.holder for e in d.edges]
    agents = [e.agent for e in d.edges]
    assert holders[:-1] == agents[1:] or set(holders) == set(agents)


def test_each_link_carries_a_why_that_runs(deadlocked):
    """§8.18 offers "[Why on each link]", so every link has to be a question the
    causal engine can actually answer."""
    _store, el, _clock, _analysis, report, _edges = deadlocked
    for e in report.liveness.deadlocks[0].edges:
        q = vtq.parse(e.to_dict()["why"])
        assert el.graph.get(q.signal) is not None
        assert q.time is not None


def test_the_first_blocked_transaction_is_named(deadlocked):
    """§8.18 reports it by name. Here the blockage stopped a transaction from
    ever starting, so the honest answer names the one that would have been
    next rather than leaving the field empty."""
    _store, _el, _clock, _analysis, report, _edges = deadlocked
    d = report.liveness.deadlocks[0]
    assert d.first_txn or d.first_blocked
    assert "WRITE#" in (d.first_txn or d.first_blocked)


def test_the_same_design_without_the_bug_has_no_deadlock(healthy):
    """The half that makes the detector worth having: "none found" has to be as
    trustworthy as "found", or the check is a horoscope."""
    _store, _el, _clock, analysis, report, edges = healthy
    assert not report.liveness.deadlocks
    assert not edges, [e.to_dict() for e in edges]
    # And it is not that the run was empty: the same testbench completed.
    assert len(analysis.transactions) == 32


def test_a_healthy_bus_is_fair_and_a_deadlocked_one_is_not(healthy, deadlocked):
    """Jain's index, checked against a case where the answer is known: two
    agents served equally is 1.0; 5 against 0 is 0.5."""
    *_rest, ok, _e = healthy
    *_rest2, bad, _e2 = deadlocked
    assert ok.fairness.index == pytest.approx(1.0)
    assert bad.fairness.index == pytest.approx(0.5, abs=0.01)


def test_the_deadlock_appears_in_checks_like_any_other_finding(deadlocked):
    """§8.18: all three run at session open, like the stuck detector, and §11.4
    says a finding is a finding."""
    store, el, clock, analysis, report, _edges = deadlocked
    got = checks.run_all(
        store, el.graph, el, clock, Config.empty(), analysis, report.liveness
    )
    found = [f for f in got if f.group is Group.LIVENESS]
    assert found, got.to_dict()
    assert found[0].check == liveness_mod.CHECK_DEADLOCK
    assert found[0].why is not None
    assert "waits on" in " ".join(found[0].notes)
    assert Group.LIVENESS.value not in got.skipped


def test_without_rtl_the_blockage_is_still_reported_but_not_attributed(deadlocked):
    """P7. Naming who holds a wire is a question about the design; without the
    RTL the tool says so instead of guessing an answer."""
    store, _el, clock, analysis, _report, _edges = deadlocked
    live, edges = deadlock.analyse(store, analysis.extractions, None, clock)
    assert not live.deadlocks
    assert edges and all(e.holder is None for e in edges)
    assert "RTL" in live.skipped["deadlock"]


# --- acceptance 2: attribution sums to exactly 100% -------------------------

#: A pack for the hand-built trace below. One channel, and a cascade whose
#: rungs are mutually exclusive by construction, so the expected label of every
#: cycle can be written down in advance.
HAND_PACK = """
name = "Hand"
version = "1"
[detect]
required_suffixes = ["valid", "ready"]
prefix_strip = [""]
clock = "clk"
reset = "rst_n"
[[channel]]
name = "X"
valid = "valid"
ready = "ready"
payload = ["data"]
[[transaction]]
name = "XFER"
start = "X"
[perf]
transfer = ["X"]
data = "data"
[[stall_reason]]
name = "backpressure"
when = "valid && !ready"
[[stall_reason]]
name = "starvation"
when = "!valid && ready"
[[stall_reason]]
name = "idle"
when = "!valid && !ready"
"""


def _hand_built_vcd() -> tuple[str, list[str]]:
    """A trace whose cause is decided one cycle at a time, plus the answers.

    Twenty cycles: four in reset, then a scripted sequence of transfers,
    refusals, ready-with-nothing-to-send and dead air, and finally two cycles
    where `valid` is X — which must land in `other`, because a handshake that
    cannot be decided has no cause and inventing one would be the exact
    dishonesty §8.17's `other` bucket exists to prevent.
    """
    #      (valid, ready) per cycle after reset, and the label each must get.
    script: list[tuple[str, str, str]] = [
        ("1", "1", "transfer"),
        ("1", "1", "transfer"),
        ("1", "0", "backpressure"),
        ("1", "0", "backpressure"),
        ("1", "0", "backpressure"),
        ("1", "1", "transfer"),
        ("0", "1", "starvation"),
        ("0", "1", "starvation"),
        ("0", "0", "idle"),
        ("0", "0", "idle"),
        ("0", "0", "idle"),
        ("1", "1", "transfer"),
        ("0", "1", "starvation"),
        ("1", "0", "backpressure"),
        ("x", "1", "other"),
        ("x", "0", "other"),
    ]
    lines = [
        "$timescale 1ns $end",
        "$scope module tb $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 # rst_n $end",
        "$var wire 1 $ valid $end",
        "$var wire 1 % ready $end",
        "$var wire 8 & data $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        "0#",
        "0$",
        "0%",
        "b0 &",
    ]
    t = 0
    labels = ["reset"] * 4
    # Reset is released after four rising edges; values are driven between
    # edges so `value_before` at an edge sees them, which is §5.5's rule and
    # the one the scan follows.
    for _ in range(4):
        t += 5
        lines += [f"#{t}", "1!"]
        t += 5
        lines += [f"#{t}", "0!"]
    lines += [f"#{t}", "1#"]
    for valid, ready, label in script:
        labels.append(label)
        lines += [f"#{t}", f"{valid}$", f"{ready}%", "b101 &"]
        t += 5
        lines += [f"#{t}", "1!"]
        t += 5
        lines += [f"#{t}", "0!"]
    return "\n".join(lines) + "\n", labels


@pytest.fixture(scope="module")
def hand(tmp_path_factory):
    d = tmp_path_factory.mktemp("hand")
    text, labels = _hand_built_vcd()
    (d / "dump.vcd").write_text(text, encoding="utf-8")
    # A real pack file on a real search path: the fixture goes through the same
    # loader a project's own pack would, rather than through a back door.
    (d / "hand.vtp.toml").write_text(HAND_PACK, encoding="utf-8")
    out = d / "dump.vtx"
    convert(str(d / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    analysis = engine.extract(
        store,
        out,
        clock,
        Config.empty(),
        project_root=d,
        pack_names=["hand"],
        use_cache=False,
    )
    return store, clock, analysis, labels


def test_stall_attribution_labels_every_cycle_the_way_it_was_built(hand):
    """The acceptance criterion, at its strongest: not just that the shares add
    to 100%, but that each cycle got the label it was constructed to have."""
    _store, _clock, analysis, labels = hand
    ex = analysis.extractions[0]
    perf = ex.perf
    assert perf is not None, ex.skipped
    got = [perf.buckets[i] for i in perf.labels]
    assert got == labels, list(zip(got, labels))


def test_the_shares_sum_to_exactly_one_hundred_percent(hand):
    """§8.17: *the sum is 100%, verifiable*. Verified on the counts, which are
    exact, and on the rounded shares, which is what a reader actually sees."""
    _store, _clock, analysis, labels = hand
    prof = metrics.stall_profile(analysis.extractions[0])
    assert sum(prof.counts.values()) == prof.total == len(labels)
    assert sum(prof.shares().values()) == 100.0
    assert sum(prof.shares(digits=3).values()) == 100.0


def test_the_counts_match_the_script_bucket_by_bucket(hand):
    _store, _clock, analysis, labels = hand
    prof = metrics.stall_profile(analysis.extractions[0])
    for bucket in prof.buckets:
        assert prof.counts[bucket] == labels.count(bucket), bucket


def test_an_undecidable_cycle_lands_in_other_rather_than_being_guessed(hand):
    """A handshake sampled at X has no cause. §8.17's `other` is what keeps the
    total honest instead of attributing it to whichever rung is written last."""
    _store, _clock, analysis, _labels = hand
    prof = metrics.stall_profile(analysis.extractions[0])
    assert prof.counts["other"] == 2


def test_the_shares_still_sum_to_one_hundred_on_every_real_design(arb, deadlocked, healthy):
    """The property has to hold everywhere, not only where it was designed to."""
    for fixture in (arb, deadlocked, healthy):
        *_rest, report, _edges = fixture
        for per in report.interfaces:
            if per.stalls is None:
                continue
            assert sum(per.stalls.counts.values()) == per.stalls.total
            assert sum(per.stalls.shares().values()) == 100.0


def test_reset_is_a_bucket_not_an_exclusion(hand):
    """Dropping cycles from the denominator is how attribution quietly stops
    adding up, so reset is counted and named."""
    _store, _clock, analysis, labels = hand
    prof = metrics.stall_profile(analysis.extractions[0])
    assert prof.counts["reset"] == 4
    assert prof.total == len(labels)


# --- the cascade ------------------------------------------------------------


def test_a_rung_that_cannot_be_evaluated_is_dropped_and_reported(hand):
    """P7 again: a rung that silently never fires is indistinguishable from a
    cause that never happened."""
    _store, clock, analysis, _labels = hand
    ex = analysis.extractions[0]
    broken = pack.loads(
        HAND_PACK.replace(
            '[[stall_reason]]\nname = "backpressure"',
            '[[stall_reason]]\nname = "bogus"\nwhen = "nowhere_at_all"\n[[stall_reason]]\nname = "backpressure"',
        )
    )
    from veritrace.protocol import channels as ch

    iface = ex.interface
    iface.pack = broken
    sampler = ch.Sampler(_store, clock.edges)
    in_reset = ch.reset_mask(iface, sampler)
    scans = ch.scan(iface, sampler, in_reset)
    perf = stalls.attribute(iface, sampler, in_reset, scans, ex.transactions)
    assert "bogus" in perf.notes
    # And the cycles the broken rung would have claimed fall through to the
    # rung below it rather than to `other`.
    counts = {perf.buckets[i]: 0 for i in perf.labels}
    for i in perf.labels:
        counts[perf.buckets[i]] += 1
    assert counts.get("backpressure") == 4
    assert counts.get("bogus", 0) == 0


def test_a_pack_may_not_claim_a_bucket_the_engine_owns():
    for reserved in pack.RESERVED_BUCKETS:
        with pytest.raises(pack.PackError):
            pack.loads(HAND_PACK + f'\n[[stall_reason]]\nname = "{reserved}"\nwhen = "1"\n')


def test_memoising_the_cascade_does_not_change_its_answer(hand):
    """The cascade is memoised on the values it reads, which is only sound if
    the key covers every cycle an expression can see."""
    _store, clock, analysis, labels = hand
    ex = analysis.extractions[0]
    from veritrace.protocol import channels as ch

    sampler = ch.Sampler(_store, clock.edges)
    in_reset = ch.reset_mask(ex.interface, sampler)
    scans = ch.scan(ex.interface, sampler, in_reset)

    # `$past` reaches back a cycle, so a memo key that ignored history would
    # answer the second occurrence of a repeated state with the first one's label.
    temporal = pack.loads(
        HAND_PACK.replace('when = "valid && !ready"', 'when = "valid && !ready && $past(valid)"')
    )
    ex.interface.pack = temporal
    memoised = stalls.attribute(ex.interface, sampler, in_reset, scans, ex.transactions)

    original = stalls.MAX_MEMO_SIGNALS
    stalls.MAX_MEMO_SIGNALS = 0  # forces the direct path
    try:
        direct = stalls.attribute(ex.interface, sampler, in_reset, scans, ex.transactions)
    finally:
        stalls.MAX_MEMO_SIGNALS = original
    assert memoised.labels == direct.labels


# --- the §8.17 metric table -------------------------------------------------


def test_latency_is_a_distribution_not_an_average(healthy):
    """§8.17 asks for the whole histogram: an average hides the tail, and the
    tail is the bug."""
    *_rest, report, _edges = healthy
    h = report.interfaces[0].latency
    assert h.n == 16
    assert h.min <= h.p50 <= h.p95 <= h.p99 <= h.max
    assert sum(n for _lo, _hi, n in h.bins) == h.n
    # The outliers name real transactions, or clicking one goes nowhere.
    refs = {t.ref for e in _rest[3].extractions for t in e.transactions}
    assert all(ref in refs for ref, _v in h.outliers)


def test_percentiles_are_values_some_transaction_actually_had(healthy):
    """Nearest-rank, not interpolated: "p99 = 41.5 cycles" names a transaction
    that never existed."""
    *_rest, report, _edges = healthy
    analysis = _rest[3]
    seen = {
        t.metrics.get("latency")
        for e in analysis.extractions
        for t in e.transactions
        if t.closed
    }
    h = report.interfaces[0].latency
    for p in (h.p50, h.p95, h.p99, h.min, h.max):
        assert p in seen


def test_an_unfinished_transaction_has_no_latency(deadlocked):
    """Counting the rest of the run as its latency would put a fabricated point
    in the p99."""
    _store, _el, _clock, analysis, report, _edges = deadlocked
    ex = analysis.extractions[0]
    closed = [t for t in ex.transactions if t.closed]
    assert report.get(ex.interface.name).latency.n == len(closed)


def test_outstanding_is_a_series_and_a_plateau_is_named(arb):
    *_rest, report, _edges = arb
    per = report.interfaces[0]
    assert per.outstanding is not None and per.outstanding.points
    # AXI4-Lite allows one in flight, so a peak of one is not a ceiling anyone
    # hit and must not be announced as the limit.
    assert per.outstanding_peak <= 1
    assert not per.outstanding_plateau


def test_jain_index_matches_the_definition():
    assert model.jain([1, 1, 1, 1]) == pytest.approx(1.0)
    assert model.jain([4, 0, 0, 0]) == pytest.approx(0.25)
    assert model.jain([]) == 1.0
    assert model.jain([0, 0]) == 1.0


def test_percentile_is_nearest_rank():
    v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert model.percentile(v, 0.5) == 5
    assert model.percentile(v, 0.95) == 10
    assert model.percentile([], 0.5) == 0


def test_a_histogram_of_one_value_has_one_bin():
    h = model.histogram([("a.WRITE[0]", 7), ("a.WRITE[1]", 7)])
    assert h.bins == [(7, 7, 2)]
    assert h.p50 == h.p99 == 7


# --- starvation and livelock ------------------------------------------------


def test_starvation_needs_someone_else_to_be_progressing(deadlocked):
    """§8.18: *asks continuously and never receives, while others progress.*
    Without the second clause an idle bus reads as starvation, which would put
    a finding on every design that is simply quiet."""
    _store, _el, clock, analysis, report, _edges = deadlocked
    # Everything is blocked here, so nobody is starved — they are deadlocked,
    # which is a different finding and the one that is reported.
    assert not report.liveness.starvation
    assert report.liveness.deadlocks


def test_starvation_is_found_when_one_agent_is_served_and_another_is_not(arb):
    """The fairness table sees it even where the strict starvation rule does
    not fire, because arb_master gates its own request on the grant."""
    *_rest, report, _edges = arb
    assert report.fairness is not None
    assert len(report.fairness.agents) == 3


def test_livelock_needs_an_interval_with_nothing_completing(healthy):
    """A busy design must not read as a livelock, whatever its FSMs do."""
    *_rest, report, _edges = healthy
    assert not report.liveness.livelocks


# --- §10.1 dispatch ---------------------------------------------------------


@pytest.mark.parametrize(
    "query,key",
    [
        ("stalls(node0.m)", "stalls"),
        ("latency(node0.m)", "latency"),
        ("throughput(node0.m)", "throughput"),
        ("outstanding(node0.m)", "outstanding"),
        ("fairness()", "fairness"),
        ("deadlock()", "deadlocks"),
        ("livelock()", "livelocks"),
        ("starvation()", "starvation"),
    ],
)
def test_every_section_10_1_performance_command_runs(deadlocked, query, key):
    _store, _el, _clock, analysis, report, edges = deadlocked
    got = perf_query.run(report, analysis, edges, vtq.parse_pipeline(query))
    assert key in got


def test_latency_by_master_groups_the_histogram(healthy):
    _store, _el, _clock, analysis, report, edges = healthy
    got = perf_query.run(
        report, analysis, edges, vtq.parse_pipeline("latency(node0.m, by=master)")
    )
    assert got["groups"]
    assert sum(g["n"] for g in got["groups"].values()) == got["latency"]["n"]


def test_an_unknown_interface_says_what_was_detected(deadlocked):
    _store, _el, _clock, analysis, report, edges = deadlocked
    with pytest.raises(vtq.QueryError, match="node0"):
        perf_query.run(report, analysis, edges, vtq.parse_pipeline("stalls(nope)"))


def test_a_performance_command_refuses_a_pipeline_stage(deadlocked):
    _store, _el, _clock, analysis, report, edges = deadlocked
    with pytest.raises(vtq.QueryError, match="stage"):
        perf_query.run(
            report, analysis, edges, vtq.parse_pipeline("stalls(node0.m) | slowest(3)")
        )


# --- persistence ------------------------------------------------------------


def test_the_cycle_profile_survives_the_cache(tmp_path_factory):
    """§8.17's profile is written beside the transaction table, so a reopened
    session has a Performance tab rather than an empty one."""
    d = tmp_path_factory.mktemp("cache")
    out = d / "dump.vtx"
    convert(str(DESIGNS / "deadlock" / "dump_ok.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)

    fresh = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    cached = engine.extract(store, out, clock, Config.empty(), use_cache=True)

    for a, b in zip(fresh.extractions, cached.extractions):
        assert b.perf is not None, b.skipped
        assert b.perf.labels == a.perf.labels
        assert b.perf.buckets == a.perf.buckets
        assert b.perf.edges == a.perf.edges
        assert b.perf.blocked_on == a.perf.blocked_on
        # And the aggregates a restored profile produces are identical, which is
        # the property that actually matters to the tab.
        assert metrics.stall_profile(b).counts == metrics.stall_profile(a).counts
        # Cached data feeds every other chart too.  A preserved cycle profile
        # beside empty transaction events used to turn real throughput into 0.
        assert metrics.measure(a, store, clock).to_dict() == metrics.measure(b, store, clock).to_dict()
        assert metrics.bytes_moved(b, store)[0] > 0


# --- the cascade reaches outside the interface ------------------------------


def test_a_project_pack_can_attribute_a_stall_to_a_wire_it_does_not_own(tmp_path_factory):
    """§8.17's cascade is only as good as what it can see.

    Its own example — `req_valid && grant != my_id` — is about the arbiter's
    grant, which no interface owns, so a name that is not an interface signal is
    resolved as a hierarchical path in the trace. `designs/axi_arb/packs/` is the
    worked example: the rung reads the slave's internal wait-state counter and
    turns "the address was refused" into "the slave was still counting", for all
    three interfaces at once.
    """
    out = tmp_path_factory.mktemp("arbpack") / "dump.vtx"
    convert(str(DESIGNS / "axi_arb" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    analysis = engine.extract(
        store,
        out,
        clock,
        Config.empty(),
        project_root=DESIGNS / "axi_arb",
        pack_names=["axi4lite"],
        use_cache=False,
    )
    assert not analysis.errors, analysis.errors
    assert len(analysis.extractions) == 3
    # Shadowed by file name, so the project's `axi4lite.vtp.toml` replaced the
    # shipped one rather than competing with it for the same interfaces.
    assert all(
        ex.interface.pack.path.parent.name == "packs" for ex in analysis.extractions
    )

    for ex in analysis.extractions:
        prof = metrics.stall_profile(ex)
        assert prof.notes == {}, prof.notes  # the rung was evaluated, not dropped
        assert prof.counts["slave_wait_states"] > 0, ex.interface.name
        # The shipped pack would have called these `address_stall`; the project
        # pack names the actual cause, and the total still adds up.
        assert prof.counts["address_stall"] == 0
        assert sum(prof.counts.values()) == prof.total
        assert sum(prof.shares().values()) == 100.0


def test_an_unresolvable_name_in_a_rung_is_reported_not_silently_false(hand):
    """A rung naming a wire that is in neither the interface nor the trace is a
    typo in the pack, and a typo that quietly never fires is the failure mode
    §8.14 refuses to have."""
    _store, clock, analysis, _labels = hand
    from veritrace.protocol import channels as ch

    ex = analysis.extractions[0]
    ex.interface.pack = pack.loads(
        HAND_PACK + '\n[[stall_reason]]\nname = "typo"\nwhen = "no.such.wire"\n'
    )
    sampler = ch.Sampler(_store, clock.edges)
    in_reset = ch.reset_mask(ex.interface, sampler)
    scans = ch.scan(ex.interface, sampler, in_reset)
    perf = stalls.attribute(ex.interface, sampler, in_reset, scans, ex.transactions)
    assert "typo" in perf.notes
    assert len(perf.labels) == len(clock.edges)
