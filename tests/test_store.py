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
    assert store.source_bytes and store.source_bytes > 0


def test_a_dump_that_changed_without_a_newer_timestamp_is_reconverted(tmp_path):
    """§5.7's failure, and it is not theoretical.

    Freshness used to be an mtime comparison alone. A dump restored by `git
    checkout`, copied with `cp -p` or extracted from an archive keeps its old
    timestamp, so a store built later reads as fresh — and every answer that
    follows is about the previous run, with total confidence.
    """
    import os

    from veritrace import TraceStore
    from veritrace import store as store_mod

    vcd = "$timescale 1ns $end\n$scope module tb $end\n$var reg 8 ! d [7:0] $end\n" \
          "$upscope $end\n$enddefinitions $end\n#0\nb0 !\n#10\nb{v} !\n"
    src = tmp_path / "dump.vcd"
    src.write_text(vcd.format(v="1010"))
    first = TraceStore(str(store_mod.ensure(src)))
    assert str(first.value_at(0, 10)) == "1010"
    mtime = os.stat(store_mod.ensure(src)).st_mtime

    # A different run, deliberately not newer than the store.
    src.write_text(vcd.format(v="11110000"))
    os.utime(src, (mtime - 5, mtime - 5))

    second = TraceStore(str(store_mod.ensure(src)))
    assert str(second.value_at(0, 10)) == "11110000"
    assert second.source_sha256 != first.source_sha256


def test_same_size_dump_with_exact_preserved_mtime_is_reconverted(tmp_path):
    """Size and mtime together are not source identity.

    This is the hard stale-cache case on Windows: ``st_ctime`` is creation
    time there, so even writing different same-length bytes and restoring the
    exact nanosecond mtime leaves every Python ``stat`` field unchanged. The
    native verifier uses NTFS ChangeTime (inode/ctime on Unix) and hashes only
    after that O(1) identity changes.
    """
    import os

    from veritrace import TraceStore
    from veritrace import store as store_mod

    template = (
        "$timescale 1ns $end\n$scope module tb $end\n"
        "$var reg 4 ! d [3:0] $end\n$upscope $end\n"
        "$enddefinitions $end\n#0\nb0 !\n#10\nb{value} !\n"
    )
    src = tmp_path / "same.vcd"
    src.write_text(template.format(value="1010"))
    before_stat = src.stat()
    first_path = store_mod.ensure(src)
    first = TraceStore(str(first_path))
    assert str(first.value_at(0, 10)) == "1010"

    src.write_text(template.format(value="0101"))
    assert src.stat().st_size == before_stat.st_size
    os.utime(src, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    assert src.stat().st_mtime_ns == before_stat.st_mtime_ns

    second_path = store_mod.ensure(src)
    second = TraceStore(str(second_path))
    assert str(second.value_at(0, 10)) == "101"
    assert second.source_sha256 != first.source_sha256
    assert second_path != first_path


def test_a_legacy_store_without_identity_is_verified_and_upgraded(tmp_path):
    """A legacy store is hash-verified once, then gets the O(1) identity."""
    import json

    from veritrace import store as store_mod

    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    out = store_mod.ensure(src)
    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["source_bytes"]
    del meta["source_identity"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert store_mod._matches_source(out, src) is True
    upgraded = json.loads(meta_path.read_text(encoding="utf-8"))
    assert upgraded["source_identity"]["change_token"]


def test_cli_convert(tmp_path):
    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    runner = CliRunner()
    result = runner.invoke(main, ["convert", str(src), "-o", str(tmp_path / "out.vtx")])
    assert result.exit_code == 0, result.output
    assert "6 signals" in result.output
    assert (tmp_path / "out.vtx" / "meta.json").exists()


def test_native_conversion_reports_truthful_elapsed_progress(capsys, tmp_path, monkeypatch):
    """A long native parse must not leave the CLI looking frozen (§13.4)."""
    import threading
    import click

    from veritrace.cli import _convert_with_progress

    progress_seen = threading.Event()
    original_echo = click.echo

    def observe_echo(message, **kwargs):
        original_echo(message, **kwargs)
        if "elapsed" in str(message):
            progress_seen.set()

    monkeypatch.setattr(click, "echo", observe_echo)

    class SlowNative:
        @staticmethod
        def convert(_source, _output):
            # Wait for actual progress rather than racing 0.75s against a
            # 0.2s + 0.5s polling loop on a scheduled macOS CI worker.
            assert progress_seen.wait(5), "conversion did not report progress"
            return 17

    events = _convert_with_progress(SlowNative, tmp_path / "large.vcd", tmp_path / "large.vtx")

    assert events == 17
    progress = capsys.readouterr().err
    assert "converting large.vcd" in progress
    assert "elapsed" in progress


def test_native_conversion_worker_error_is_not_turned_into_success(tmp_path):
    from veritrace.cli import _convert_with_progress

    class BrokenNative:
        @staticmethod
        def convert(_source, _output):
            raise ValueError("bad waveform")

    with pytest.raises(ValueError, match="bad waveform"):
        _convert_with_progress(BrokenNative, tmp_path / "bad.vcd", tmp_path / "bad.vtx")


def test_cli_convert_refuses_to_destroy_an_unrelated_output_directory(tmp_path):
    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    out = tmp_path / "existing-project"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("do not delete")

    result = CliRunner().invoke(main, ["convert", str(src), "-o", str(out)])

    assert result.exit_code != 0
    assert "not a VeriTrace store" in result.output
    assert sentinel.read_text() == "do not delete"
    assert not (out / "meta.json").exists()


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


def test_a_store_pointed_at_directly_is_still_checked_against_its_dump(tmp_path):
    """The hole the first fix left, found the first time it met a real project.

    `trace.default` in `.veritrace.toml` resolves to the `.vtx` when one is
    already there, so `veritrace serve` hands `ensure` a *directory*. That
    branch only ever asked whether the store was readable — so re-running the
    simulation and then serving produced a session built from yesterday's dump,
    with the RTL-changed banner on it because the sources had moved on and the
    trace, silently, had not.
    """
    from veritrace import TraceStore
    from veritrace import store as store_mod

    vcd = "$timescale 1ns $end\n$scope module tb $end\n$var reg 8 ! d [7:0] $end\n" \
          "$upscope $end\n$enddefinitions $end\n#0\nb0 !\n#10\nb{v} !\n"
    src = tmp_path / "dump.vcd"
    src.write_text(vcd.format(v="1010"))
    store = store_mod.ensure(src)
    assert str(TraceStore(str(store)).value_at(0, 10)) == "1010"

    # Re-run the simulation, then ask for the store by name rather than by dump.
    src.write_text(vcd.format(v="11110000"))
    again = store_mod.ensure(store)
    # Stores are immutable generations because a live Windows reader mmaps the
    # old index. The direct-store path must return the new generation while the
    # already-open old path remains a valid snapshot.
    assert again != store
    assert str(TraceStore(str(again)).value_at(0, 10)) == "11110000"
    assert str(TraceStore(str(store)).value_at(0, 10)) == "1010"

    # `trace.default` continues to point at the canonical store.  Once a live
    # reader forced an immutable generation, reopening that configured path
    # must reuse it rather than hashing and writing another directory forever.
    reopened = store_mod.ensure(store)
    assert reopened == again
    assert len(list(tmp_path.glob("dump.vcd.*.vtx"))) == 1


def test_a_store_with_no_dump_beside_it_is_served_as_it_is(tmp_path):
    """§13.8 ships `.vtx` stores without their dumps — a colleague's bundle, or
    a store committed on its own. Nothing to compare against is not a reason to
    refuse to open one."""
    import shutil

    from veritrace import store as store_mod

    src = tmp_path / "dump.vcd"
    src.write_text(SAMPLE_VCD)
    store = store_mod.ensure(src)
    alone = tmp_path / "elsewhere" / "dump.vcd.vtx"
    alone.parent.mkdir()
    shutil.copytree(store, alone)

    assert store_mod.ensure(alone) == alone
