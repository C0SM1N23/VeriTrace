"""Data integrity — §8.19, the automatic scoreboard and `track()`.

The first acceptance criterion of Prompt 12 is the first section: the injected
DMA corruption is found *with its exact location* — which byte lanes, and where
in the path it happened — and the same RTL with the bug compiled out reports
nothing at all. The second half of that is not decoration: a scoreboard that
finds corruption everywhere is as useless as one that finds it nowhere.

The unit sections below build their beats by hand rather than from a dump, for
the same reason `test_memory` builds its bank timeline by hand: checking an
answer against the trace it was derived from proves only self-consistency,
while checking it against a sequence whose answer was written down in advance
proves the analysis is right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import checks, integrity as integrity_findings, vtq
from veritrace.analysis.findings import Group, Severity
from veritrace.config import Config
from veritrace.coverage import query as cov_query
from veritrace.integrity import order, path, report as int_report, scoreboard, track
from veritrace.integrity.model import IfaceStats, format_lanes
from veritrace.protocol import engine
from veritrace.protocol.model import Beat

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

#: The lanes designs/dma drops: `wstrb & 4'b1100` loses bits 0 and 1.
CORRUPTED_LANES = (0, 1)
#: §8.19's own value, and the first word the DMA testbench writes.
DEADBEEF = 0xDEADBEEF


def _open(vcd: str, tmp_path_factory):
    out = tmp_path_factory.mktemp("dma") / "dump.vtx"
    convert(str(DESIGNS / "dma" / vcd), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    return store, clock, analysis, int_report.build(analysis, store, clock)


@pytest.fixture(scope="module")
def corrupted(tmp_path_factory):
    return _open("dump.vcd", tmp_path_factory)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    """The same RTL with `NO_CORRUPTION` — the control for every claim below."""
    return _open("dump_ok.vcd", tmp_path_factory)


# --- the acceptance criterion ----------------------------------------------


def test_the_injected_corruption_is_found(corrupted):
    """Prompt 12, first half: found, with the byte lanes and the place named."""
    _store, _clock, _an, report = corrupted
    assert report.mismatches, "the injected wstrb bug was not found at all"
    for m in report.mismatches:
        assert m.lanes == CORRUPTED_LANES, f"wrong lanes reported: {m.lane_text}"


def test_the_scoreboard_names_the_read_that_disagreed(corrupted):
    """`0xDEADBEEF` went in at 0x0 and `0xDEAD0000` came back — §8.19's example."""
    _store, _clock, _an, report = corrupted
    first = next(
        m for m in report.mismatches if m.kind == "scoreboard" and m.addr == 0x0
    )
    assert first.expected == DEADBEEF
    assert first.observed == 0xDEAD0000
    assert first.where == "dma.s_axi"
    # Both transactions, so the finding points at a write and a read, not a time.
    assert first.source.endswith("WRITE[0]")
    assert first.victim.endswith("READ[0]")


def test_the_path_comparison_names_where_it_happened(corrupted):
    """Which two interfaces the bytes changed between — §8.19's "unde in cale"."""
    _store, _clock, _an, report = corrupted
    hop = next(m for m in report.mismatches if m.kind == "path" and m.addr == 0x0)
    assert hop.where == "dma.s_axi -> mem"
    assert hop.lanes == CORRUPTED_LANES
    assert "not downstream" in hop.detail
    # The path was compared, and the report says how much of it.
    assert ("dma.s_axi", "mem", 8) in report.compared_paths


def test_every_written_word_is_caught_on_both_paths(corrupted):
    """Eight writes, each corrupted, each seen twice: once as a bad read-back on
    the CPU side and once as a byte lost between the two interfaces."""
    _store, _clock, _an, report = corrupted
    kinds = [m.kind for m in report.mismatches]
    assert kinds.count("path") == 8
    assert kinds.count("scoreboard") == 8


def test_the_same_design_without_the_bug_reports_nothing(clean):
    """Prompt 12's other half — and the harder one.

    The path is still compared and the reads are still checked; they simply
    agree. `compared_paths` is what separates that from a scan that did not run.
    """
    _store, _clock, _an, report = clean
    assert report.mismatches == []
    assert ("dma.s_axi", "mem", 8) in report.compared_paths
    assert all(s.compared for s in report.interfaces)


def test_track_follows_the_word_through_every_level(corrupted):
    """§8.19's chain: in on one interface, out on the other, mismatch marked."""
    _store, _clock, _an, report = corrupted
    got = track.by_addr(report, 0x0)
    assert [(s.iface, s.dir) for s in got.steps] == [
        ("dma.s_axi", "write"),
        ("mem", "write"),
        ("mem", "read"),
        ("dma.s_axi", "read"),
    ]
    # The write that reached memory carries the strobe that dropped the bytes.
    written = got.steps[1]
    assert written.strobe == 0b1100
    assert "not enabled by the strobe" in written.note
    assert any(s.mismatch for s in got.steps)


def test_track_by_data_resolves_to_the_address_that_carried_it(corrupted):
    """§8.19 is explicit that a value cannot be followed; it is resolved to an
    address first, and the report says so when the value was not unique."""
    _store, _clock, _an, report = corrupted
    got = track.by_data(report, DEADBEEF)
    assert got.steps, "0xDEADBEEF was on the bus and track() did not find it"
    assert all(s.addr == 0x0 for s in got.steps)


def test_track_says_so_when_nothing_touched_the_address(corrupted):
    _store, _clock, _an, report = corrupted
    got = track.by_addr(report, 0xDEAD_0000)
    assert got.steps == []
    assert "no transaction" in got.note


# --- the mismatches are findings (§8.19, §11.4) -----------------------------


def test_a_mismatch_is_an_ordinary_finding(corrupted):
    _store, clock, analysis, report = corrupted
    found = list(integrity_findings.scan(report))
    assert len(found) == len(report.mismatches)
    f = found[0]
    assert f.group is Group.INTEGRITY
    # Corrupted data is never a "might not matter" finding.
    assert f.severity is Severity.ERROR
    assert "byte(s) 0-1" in f.title


def test_every_mismatch_carries_a_working_why(corrupted):
    """§11.4 requires a working `[why]` on every row, and a data mismatch has
    the most obvious question of any finding: where did this value come from.

    The wire is read out of the pack's `[integrity]` declaration, so the two
    kinds point at two different things on purpose — a read that came back
    wrong asks about the read data, and bytes dropped along the path ask about
    the strobe that dropped them.
    """
    _store, _clock, _an, report = corrupted
    assert all(m.why and m.signal for m in report.mismatches)

    scoreboard_m = next(m for m in report.mismatches if m.kind == "scoreboard")
    assert scoreboard_m.signal.endswith("rdata")
    assert scoreboard_m.why == f"why({scoreboard_m.signal} @ {scoreboard_m.time})"

    # The injected bug is a byte-enable mask, and this is the wire it is on.
    path_m = next(m for m in report.mismatches if m.kind == "path")
    assert path_m.signal.endswith("wstrb")


def test_the_why_query_resolves_against_the_design(corrupted):
    """A `[why]` that parses but names nothing is worse than none at all."""
    from veritrace.analysis import vtq

    _store, _clock, _an, report = corrupted
    for m in report.mismatches:
        parsed = vtq.parse(m.why)
        assert parsed.signal == m.signal
        assert parsed.time == m.time


def test_the_check_runs_with_everything_else(corrupted):
    """It arrives unasked, next to the stuck signals — §11.4's whole point."""
    store, clock, analysis, report = corrupted
    got = checks.run_all(
        store, None, None, clock, Config.empty(), analysis, None, None, report
    )
    assert integrity_findings.CHECK in got.counts()
    assert "integrity" not in got.skipped


def test_not_running_the_scan_is_different_from_finding_nothing(corrupted):
    """P1: a scan that never ran must not look like a clean design."""
    store, clock, analysis, _report = corrupted
    got = checks.run_all(store, None, None, clock, Config.empty(), analysis)
    assert "integrity" in got.skipped


def test_it_fails_the_build_when_asked(corrupted):
    """§13.9's gate — `--fail-on integrity` resolves to the check."""
    assert integrity_findings.CHECK in checks.expand_checks(["integrity"])


# --- the scoreboard, against hand-written beats -----------------------------


def _beat(dir_, time, addr, data, strobe=0xF, iface="i", stride=4, beat=0):
    return Beat(
        iface=iface,
        txn=f"{iface}.{'WRITE' if dir_ == 'write' else 'READ'}[{beat}]",
        dir=dir_,
        time=time,
        beat=beat,
        addr=addr,
        data=data,
        strobe=strobe,
        stride=stride,
    )


def test_a_read_of_a_byte_nobody_wrote_is_not_a_mismatch():
    """The model knows what the bus did, not what the memory booted with."""
    stats = IfaceStats(iface="i", pack="p")
    got = scoreboard.check([_beat("read", 10, 0x0, 0x1234)], stats)
    assert got == []
    assert stats.compared == 0


def test_a_partial_strobe_writes_only_the_lanes_it_enabled():
    stats = IfaceStats(iface="i", pack="p")
    got = scoreboard.check(
        [
            _beat("write", 10, 0x0, 0xAABBCCDD, strobe=0b0011),
            _beat("read", 20, 0x0, 0x0000CCDD),
        ],
        stats,
    )
    assert got == []
    # Only the two written bytes were comparable, so only two were compared.
    assert stats.compared == 2


def test_a_changed_byte_is_reported_with_its_lane():
    stats = IfaceStats(iface="i", pack="p")
    got = scoreboard.check(
        [
            _beat("write", 10, 0x0, 0xAABBCCDD),
            _beat("read", 20, 0x0, 0xAABB00DD),
        ],
        stats,
    )
    assert len(got) == 1
    assert got[0].lanes == (1,)
    assert got[0].expected == 0xAABBCCDD


def test_a_beat_carrying_x_never_enters_the_model():
    """P1: an unknown byte is unknown, not zero."""
    stats = IfaceStats(iface="i", pack="p")
    got = scoreboard.check(
        [_beat("write", 10, 0x0, "xxxxxxxx"), _beat("read", 20, 0x0, 0x0)], stats
    )
    assert got == []
    assert stats.compared == 0


# --- the path comparison, against hand-written beats ------------------------


def test_two_interfaces_with_different_address_sequences_are_not_compared():
    """The rule that keeps two unrelated masters from being called corruption."""
    a = [_beat("write", 10, 0x0, 0x1, iface="a"), _beat("write", 20, 0x4, 0x2, iface="a")]
    b = [_beat("write", 12, 0x0, 0x1, iface="b")]
    got, n = path.compare("a", a, "b", b)
    assert (got, n) == ([], 0)


def test_the_upstream_interface_is_the_one_that_saw_it_first():
    a = [_beat("write", 30, 0x0, 0xFF, iface="a")]
    b = [_beat("write", 10, 0x0, 0x00, iface="b")]
    got, n = path.compare("a", a, "b", b)
    assert n == 1
    assert got[0].where == "b -> a"


def test_a_byte_written_downstream_that_was_never_sent_is_reported():
    a = [_beat("write", 10, 0x0, 0xAABBCCDD, strobe=0b0011, iface="a")]
    b = [_beat("write", 20, 0x0, 0xAABBCCDD, strobe=0b1111, iface="b")]
    got, _n = path.compare("a", a, "b", b)
    assert got[0].lanes == (2, 3)
    assert "did not send" in got[0].detail


# --- streams with no address (§8.19's third check) --------------------------


def _stream(values, iface):
    return [_beat("write", 10 * i, None, v, iface=iface, beat=i) for i, v in enumerate(values)]


def test_a_stream_that_arrives_intact_is_clean():
    got = order.compare("src", _stream([1, 2, 3, 4], "src"), "snk", _stream([1, 2, 3, 4], "snk"))
    assert got.clean
    assert (got.n_in, got.n_out) == (4, 4)


def test_a_lost_word_is_reported_as_lost():
    got = order.compare("src", _stream([1, 2, 3, 4], "src"), "snk", _stream([1, 2, 4], "snk"))
    assert got.lost == [(2, 3)]
    assert not got.duplicated and not got.reordered


def test_a_duplicated_word_is_reported_as_duplicated():
    got = order.compare("src", _stream([1, 2, 3], "src"), "snk", _stream([1, 2, 2, 3], "snk"))
    assert [v for _i, v in got.duplicated] == [2]
    assert not got.lost


def test_a_swapped_pair_is_reordering_and_not_a_loss():
    """The three classic FIFO bugs have to be told apart, not merged."""
    got = order.compare("src", _stream([1, 2, 3], "src"), "snk", _stream([1, 3, 2], "snk"))
    assert got.reordered
    assert not got.lost and not got.spurious


def test_a_word_that_was_never_sent_is_its_own_category():
    got = order.compare("src", _stream([1, 2], "src"), "snk", _stream([1, 2, 9], "snk"))
    assert [v for _i, v in got.spurious] == [9]
    assert not got.clean


# --- the §10.1 commands -----------------------------------------------------


def test_the_track_command_needs_something_to_track(corrupted):
    _store, _clock, _an, report = corrupted
    with pytest.raises(vtq.QueryError, match="addr="):
        cov_query.run(report, None, vtq.parse_pipeline("track()"))


def test_the_track_command_reaches_the_mismatch(corrupted):
    _store, _clock, _an, report = corrupted
    got = cov_query.run(report, None, vtq.parse_pipeline("track(addr=0x0)"))
    assert got["kind"] == "track"
    assert got["mismatches"]


def test_the_scoreboard_command_filters_by_interface(corrupted):
    _store, _clock, _an, report = corrupted
    got = cov_query.run(report, None, vtq.parse_pipeline("scoreboard(mem)"))
    assert [i["iface"] for i in got["interfaces"]] == ["mem"]


def test_an_unknown_interface_says_which_ones_exist(corrupted):
    _store, _clock, _an, report = corrupted
    with pytest.raises(vtq.QueryError, match="tracked:"):
        cov_query.run(report, None, vtq.parse_pipeline("scoreboard(nope)"))


def test_a_stream_query_needs_both_ends(corrupted):
    _store, _clock, _an, report = corrupted
    with pytest.raises(vtq.QueryError, match="both ends"):
        cov_query.run(report, None, vtq.parse_pipeline("track(from=mem)"))


# --- the beat table survives the cache (§6.3) -------------------------------


def test_the_scoreboard_still_works_from_a_cached_extraction(tmp_path):
    """The transaction table does not carry per-beat payloads, so §8.19's beats
    are persisted beside it. Without that, a re-opened session would silently
    scoreboard nothing — the exact failure P1 exists to prevent."""
    out = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "dma" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)

    first = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    assert int_report.build(first, store, clock).mismatches

    cached = engine.extract(store, out, clock, Config.empty(), use_cache=True)
    assert any(ex.parquet for ex in cached.extractions)
    again = int_report.build(cached, store, clock)
    assert len(again.mismatches) == 16
    assert again.mismatches[0].lanes == CORRUPTED_LANES


def test_a_missing_beat_cache_is_rebuilt_instead_of_becoming_fake_clean(tmp_path):
    """A surviving transaction table must not conceal a damaged beat table."""
    from veritrace.protocol import persist

    out = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "dma" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    cfg = Config.empty()
    first = engine.extract(store, out, clock, cfg, use_cache=False)
    victim = next(ex for ex in first.extractions if ex.beats)
    beat_file = persist.beats_path(out, victim.interface.name)
    beat_file.unlink()

    reopened = engine.extract(store, out, clock, cfg, use_cache=True)
    assert beat_file.is_file(), "the damaged cache was reused instead of rebuilt"
    again = int_report.build(reopened, store, clock)
    assert len(again.mismatches) == 16


# --- presentation -----------------------------------------------------------


@pytest.mark.parametrize(
    "lanes,text",
    [((0, 1), "0-1"), ((0,), "0"), ((0, 1, 3), "0-1, 3"), ((), "none")],
)
def test_byte_lanes_read_the_way_a_person_writes_them(lanes, text):
    assert format_lanes(lanes) == text
