"""Tests for the TraceStore bindings and the `veritrace convert` command.

Mirrors the Rust-side time-model tests so a regression in the binding layer
cannot hide behind a green Rust suite.
"""

import pytest
from click.testing import CliRunner

from veritrace import TraceStore, convert, has_fst_support
from veritrace.cli import main

# Two writes to `sum` at t=10 (a combinational glitch settling at 2), and a
# register `q` whose input `d` changes on the same timestamp as the clock edge.
SAMPLE_VCD = """\
$timescale 1ns $end
$scope module top $end
$var reg 1 ! clk $end
$var reg 1 " d $end
$var reg 1 # q $end
$var wire 4 $ sum [3:0] $end
$var wire 4 % bus [3:0] $end
$scope module sub $end
$var wire 1 ! clk $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
1"
0#
b0 $
bx %
#10
1!
0"
1#
b1 $
b11 $
b10 $
#20
0!
#30
bz %
"""


@pytest.fixture
def store(tmp_path):
    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    out = tmp_path / "dump.vtx"
    convert(str(src), str(out))
    return TraceStore(str(out))


def test_store_metadata(store):
    assert store.n_signals == 6
    assert store.time_range == (0, 30)
    assert store.timescale == "1ns"
    assert len(store) == 6


def test_find_and_handle(store):
    assert store.find("top.clk") is not None
    assert store.find("top.nope") is None
    with pytest.raises(KeyError):
        store.handle("top.nope")


def test_value_at_returns_settled_value(store):
    """§5.5 problem 1: the last write at a timestamp, not an intermediate."""
    h = store.handle("top.sum")
    assert store.value_at(h, 10).to_int() == 2
    assert store.value_at(h, 15).to_int() == 2
    # Before the first event there is no value at all.
    assert store.value_at(h, -1) is None


def test_deltas_expose_the_glitch(store):
    h = store.handle("top.sum")
    assert store.deltas_at(h, 10) == 3
    assert store.deltas_at(h, 15) == 0
    assert store.value_at_delta(h, 10, 0).to_int() == 1
    assert store.value_at_delta(h, 10, 1).to_int() == 3
    assert store.value_at_delta(h, 10, 2).to_int() == 2
    # Past the last delta clamps to the settled value.
    assert store.value_at_delta(h, 10, 9).to_int() == 2
    assert store.value_at_delta(h, 15, 0) is None


def test_value_before_gives_nba_semantics(store):
    """§5.5 problem 2: `q` at the edge is explained by `d` *before* the edge."""
    d = store.handle("top.d")
    q = store.handle("top.q")
    assert store.value_at(q, 10).to_int() == 1
    # Sampling d at the edge reads the already-updated value and would give the
    # absurd "q is 1 because d is 0".
    assert store.value_at(d, 10).to_int() == 0
    assert store.value_before(d, 10).to_int() == 1
    assert store.value_before(d, 0) is None


def test_seven_functions(store):
    clk = store.handle("top.clk")
    bus = store.handle("top.bus")
    sum_ = store.handle("top.sum")

    assert [t for t, _ in store.transitions(clk, 0, 21)] == [0, 10, 20]
    assert store.last_change_before(clk, 15) == 10
    assert store.next_change_after(clk, 10) == 20
    assert store.next_change_after(clk, 20) is None
    assert store.is_constant(sum_, 11, 30) is True
    assert store.is_constant(sum_, 0, 30) is False
    assert store.edge_count(clk, 0, 21) == 3
    # X at t=0; the Z at t=30 must not be reported as X.
    assert store.first_x(bus) == 0
    assert store.first_x(clk) is None


def test_value_semantics(store):
    bus = store.handle("top.bus")
    v = store.value_at(bus, 0)
    assert v.has_x()
    assert not v.is_two_state()
    assert v.to_int() is None
    assert v.width == 4
    assert str(v) == "x"

    z = store.value_at(bus, 30)
    # Z is not X.
    assert not z.has_x()
    assert not z.is_two_state()

    two = store.value_at(store.handle("top.sum"), 10)
    assert two.is_two_state()
    assert two.to_int() == 2


def test_aliases_share_a_stream(store):
    a = store.handle("top.clk")
    b = store.handle("top.sub.clk")
    assert store.signal(a).stream_id == store.signal(b).stream_id
    assert sorted(store.aliases(a)) == ["top.clk", "top.sub.clk"]
    assert store.value_at(a, 10) == store.value_at(b, 10)


def test_signals_metadata(store):
    by_path = {s.path: s for s in store.signals()}
    assert by_path["top.sum"].width == 4
    assert by_path["top.sum"].msb == 3
    assert by_path["top.sum"].lsb == 0
    assert by_path["top.clk"].kind == "reg"
    assert by_path["top.bus"].encoding == "u64_4s"
    assert by_path["top.sum"].encoding == "u64_2s"


def test_parallel_scans(store):
    xs = dict(store.first_x_all())
    assert xs[store.handle("top.bus")] == 0
    assert store.handle("top.clk") not in xs
    consts = store.constant_signals(11, 19)
    assert store.handle("top.clk") in consts


def test_reconstructed_vcd_reparses_identically(tmp_path, store):
    """VCD -> .vtx -> VCD, then back again, must be a fixed point."""
    text = store.to_vcd()
    src2 = tmp_path / "again.vcd"
    src2.write_text(text)
    out2 = tmp_path / "again.vtx"
    convert(str(src2), str(out2))
    store2 = TraceStore(str(out2))

    assert store2.n_signals == store.n_signals
    assert store2.time_range == store.time_range
    assert store2.to_vcd() == text
    for s in store.signals():
        h1, h2 = store.handle(s.path), store2.handle(s.path)
        assert store.transitions(h1, 0, 31) == store2.transitions(h2, 0, 31)


def test_convert_records_source_hash(store):
    # §5.7 provenance: the store remembers which dump it came from.
    assert store.source_sha256 is not None
    assert len(store.source_sha256) == 64


def test_cli_convert(tmp_path):
    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    runner = CliRunner()
    result = runner.invoke(main, ["convert", str(src), "-o", str(tmp_path / "out.vtx")])
    assert result.exit_code == 0, result.output
    assert "6 signals" in result.output
    assert (tmp_path / "out.vtx" / "meta.json").exists()


def test_cli_convert_default_output_is_next_to_dump(tmp_path):
    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    runner = CliRunner()
    result = runner.invoke(main, ["convert", str(src)])
    assert result.exit_code == 0, result.output
    # §6.3 caches the store alongside the dump.
    assert (tmp_path / "dump.vcd.vtx").is_dir()


def test_fst_support_is_reported(tmp_path):
    # The flag must be honest either way; when off, asking for FST must fail
    # loudly rather than silently producing nothing.
    assert isinstance(has_fst_support(), bool)
    if not has_fst_support():
        src = tmp_path / "x.fst"
        src.write_bytes(b"not really an fst")
        runner = CliRunner()
        result = runner.invoke(main, ["convert", str(src)])
        assert result.exit_code != 0
        assert "fst" in result.output.lower()
