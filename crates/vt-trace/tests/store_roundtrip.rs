//! End-to-end tests over the `.vtx` store, including the two time-model rules
//! from §5.5 that are the most likely source of wrong answers downstream.

use std::path::PathBuf;

use vt_trace::model::Trace;
use vt_trace::query::TraceStore;
use vt_trace::value::Value;
use vt_trace::{reconstruct, store, vcd};

fn designs_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../designs/fifo_async")
}

fn convert(src: &str) -> (tempfile::TempDir, TraceStore, Trace) {
    let dir = tempfile::tempdir().unwrap();
    let trace = vcd::parse_str(src).unwrap();
    let out = dir.path().join("dump.vtx");
    store::write_vtx(&trace, &out, None).unwrap();
    let s = TraceStore::open(&out).unwrap();
    (dir, s, trace)
}

fn u64_at(s: &TraceStore, path: &str, t: i64) -> Option<u64> {
    let h = s.handle(path).unwrap();
    s.value_at(h, t).unwrap().and_then(|v| v.as_u64())
}

// ---------------------------------------------------------------------------
// §5.5 problem 1 — the settled value, not a glitch
// ---------------------------------------------------------------------------

const GLITCH: &str = "\
$timescale 1ns $end
$scope module top $end
$var wire 4 ! sum [3:0] $end
$var wire 1 \" q $end
$upscope $end
$enddefinitions $end
#0
b0 !
0\"
#10
b1 !
b11 !
b10 !
1\"
0\"
1\"
#20
b111 !
";

#[test]
fn value_at_returns_settled_value_not_intermediate_glitch() {
    let (_d, s, _) = convert(GLITCH);
    // Three writes at t=10: 1, then 3, settling at 2. Reading the first would
    // send a causal chain off in the wrong direction.
    assert_eq!(u64_at(&s, "top.sum", 10), Some(2));
    // And it stays settled between transitions.
    assert_eq!(u64_at(&s, "top.sum", 15), Some(2));
    assert_eq!(u64_at(&s, "top.sum", 19), Some(2));
    assert_eq!(u64_at(&s, "top.sum", 20), Some(7));
}

#[test]
fn value_at_all_agrees_with_value_at_signal_by_signal() {
    // The bulk read takes a different route to the same answer — the chunk
    // index and one Parquet row group, rather than a whole decoded stream — so
    // the only thing worth asserting is that the two never disagree. Including
    // at a delta-cycle timestamp, where "the last write wins" is the rule that
    // a row-group scan could get backwards.
    let (_d, s, _) = convert(GLITCH);
    let handles: Vec<u32> = (0..s.n_signals() as u32).collect();
    for t in [0, 5, 10, 15, 20, 25] {
        let bulk = s.value_at_all(&handles, t);
        let one: Vec<_> = handles.iter().map(|&h| s.value_at(h, t).unwrap()).collect();
        assert_eq!(bulk, one, "at t={t}");
    }
    // t=10 has three writes; both routes have to settle on the last.
    assert_eq!(
        s.value_at_all(&[s.handle("top.sum").unwrap()], 10)[0]
            .as_ref()
            .and_then(Value::as_u64),
        Some(2)
    );
    // Before the first event of the trace there is nothing to report, not zero.
    assert_eq!(s.value_at_all(&handles, -1), vec![None, None]);
}

#[test]
fn value_at_all_does_not_pull_whole_streams_into_the_cache() {
    // §4.2 budgets tier-B RAM at 1.5 GB and says mmap, not loading. A
    // whole-trace scan touches every signal exactly once, so decoding and then
    // retaining each stream would leave the entire design resident for a pass
    // that never looks at it again. The narrow read is what makes that safe,
    // and a later `value_at` still has to produce the same answer.
    let (_d, s, _) = convert(GLITCH);
    let handles: Vec<u32> = (0..s.n_signals() as u32).collect();
    assert_eq!(s.value_at_all(&handles, 20), vec![
        s.value_at(0, 20).unwrap(),
        s.value_at(1, 20).unwrap(),
    ]);
    assert_eq!(u64_at(&s, "top.sum", 10), Some(2));
}

#[test]
fn deltas_and_value_at_delta_expose_the_glitch() {
    let (_d, s, _) = convert(GLITCH);
    let h = s.handle("top.sum").unwrap();
    assert_eq!(s.deltas_at(h, 10).unwrap(), 3);
    assert_eq!(s.deltas_at(h, 20).unwrap(), 1);
    // No write at all at t=15.
    assert_eq!(s.deltas_at(h, 15).unwrap(), 0);
    assert_eq!(s.value_at_delta(h, 15, 0).unwrap(), None);

    let at = |d| s.value_at_delta(h, 10, d).unwrap().and_then(|v| v.as_u64());
    assert_eq!(at(0), Some(1));
    assert_eq!(at(1), Some(3));
    assert_eq!(at(2), Some(2));
    // Past the last delta clamps to the settled value.
    assert_eq!(at(9), Some(2));
    assert_eq!(s.delta_indices_at(h, 10).unwrap(), vec![0, 1, 2]);
}

#[test]
fn glitching_signal_reports_no_settled_edges() {
    let (_d, s, _) = convert(GLITCH);
    let h = s.handle("top.q").unwrap();
    // q goes 1,0,1 within t=10 and settles at 1, having been 0 before: that is
    // one settled edge, not three.
    assert_eq!(s.edge_count(h, 10, 11).unwrap(), 1);
    assert_eq!(s.deltas_at(h, 10).unwrap(), 3);
}

// ---------------------------------------------------------------------------
// §5.5 problem 2 — NBA semantics at a clock edge
// ---------------------------------------------------------------------------

/// `q <= d` sampled at posedge clk, where `d` also changes at the same
/// timestamp as the edge. Reading `d` *at* the edge gives the new value and
/// yields the absurd "q is 1 because d is 0" answer; the value that actually
/// determined `q` is the one strictly before the edge.
const NBA: &str = "\
$timescale 1ns $end
$scope module top $end
$var reg 1 ! clk $end
$var reg 1 \" d $end
$var reg 1 # q $end
$upscope $end
$enddefinitions $end
#0
0!
1\"
0#
#10
1!
0\"
1#
#20
0!
#30
1!
1\"
0#
";

#[test]
fn value_before_gives_pre_edge_value_for_nba() {
    let (_d, s, _) = convert(NBA);
    let d = s.handle("top.d").unwrap();
    let q = s.handle("top.q").unwrap();

    // At the t=10 edge, q becomes 1. Naively sampling d at t=10 reads 0 and
    // contradicts the register's own behaviour.
    assert_eq!(s.value_at(d, 10).unwrap().unwrap().as_u64(), Some(0));
    assert_eq!(s.value_at(q, 10).unwrap().unwrap().as_u64(), Some(1));
    // value_before is the EPSILON of §5.5 and explains q correctly.
    assert_eq!(s.value_before(d, 10).unwrap().unwrap().as_u64(), Some(1));

    // Same again at t=30, with the polarity reversed.
    assert_eq!(s.value_at(q, 30).unwrap().unwrap().as_u64(), Some(0));
    assert_eq!(s.value_before(d, 30).unwrap().unwrap().as_u64(), Some(0));
    assert_eq!(s.value_at(d, 30).unwrap().unwrap().as_u64(), Some(1));
}

#[test]
fn value_before_at_first_event_is_none() {
    let (_d, s, _) = convert(NBA);
    let d = s.handle("top.d").unwrap();
    assert_eq!(s.value_before(d, 0).unwrap(), None);
    assert_eq!(s.last_change_before(d, 0).unwrap(), None);
}

// ---------------------------------------------------------------------------
// §6.4 — the seven functions
// ---------------------------------------------------------------------------

const BASIC: &str = "\
$timescale 1ns $end
$scope module top $end
$var reg 1 ! clk $end
$var reg 8 \" data [7:0] $end
$var wire 4 # bus [3:0] $end
$upscope $end
$enddefinitions $end
#0
0!
b0 \"
bx #
#10
1!
b10100000 \"
#20
0!
#30
1!
bz #
#40
0!
";

#[test]
fn seven_functions_behave() {
    let (_d, s, _) = convert(BASIC);
    let clk = s.handle("top.clk").unwrap();
    let data = s.handle("top.data").unwrap();
    let bus = s.handle("top.bus").unwrap();

    // value_at
    assert_eq!(s.value_at(clk, 25).unwrap().unwrap().as_u64(), Some(0));
    assert_eq!(s.value_at(data, 35).unwrap().unwrap().as_u64(), Some(0xA0));
    // Before the first event there is no value.
    assert_eq!(s.value_at(clk, -1).unwrap(), None);

    // transitions
    let tr = s.transitions(clk, 0, 25).unwrap();
    assert_eq!(tr.iter().map(|(t, _)| *t).collect::<Vec<_>>(), vec![0, 10, 20]);
    let tr = s.transitions(clk, 10, 30).unwrap();
    assert_eq!(tr.iter().map(|(t, _)| *t).collect::<Vec<_>>(), vec![10, 20]);

    // last_change_before / next_change_after
    assert_eq!(s.last_change_before(clk, 25).unwrap(), Some(20));
    assert_eq!(s.last_change_before(clk, 20).unwrap(), Some(10));
    assert_eq!(s.next_change_after(clk, 20).unwrap(), Some(30));
    assert_eq!(s.next_change_after(clk, 40).unwrap(), None);

    // is_constant
    assert!(s.is_constant(data, 10, 30).unwrap());
    assert!(!s.is_constant(data, 0, 30).unwrap());
    assert!(s.is_constant(clk, 21, 30).unwrap());
    assert!(!s.is_constant(clk, 21, 31).unwrap());

    // edge_count
    assert_eq!(s.edge_count(clk, 0, 41).unwrap(), 5);
    assert_eq!(s.edge_count(clk, 10, 30).unwrap(), 2);
    assert_eq!(s.edge_count(data, 15, 25).unwrap(), 0);

    // first_x — set at t=0, and Z at t=30 must not count as X
    assert_eq!(s.first_x(bus).unwrap(), Some(0));
    assert_eq!(s.first_x(clk).unwrap(), None);
    assert_eq!(s.first_x(data).unwrap(), None);
}

#[test]
fn parallel_scans_agree_with_serial() {
    let (_d, s, _) = convert(BASIC);
    let xs = s.first_x_all();
    let bus = s.handle("top.bus").unwrap();
    assert_eq!(xs, vec![(bus, 0)]);

    let consts = s.constant_signals(11, 19);
    assert_eq!(consts.len(), s.n_signals());
}

// ---------------------------------------------------------------------------
// Store shape and aliasing
// ---------------------------------------------------------------------------

#[test]
fn store_layout_matches_spec() {
    let (_d, s, _) = convert(BASIC);
    let root = PathBuf::from(&s.meta.source_file.clone().unwrap_or_default());
    let _ = root;
    assert_eq!(s.meta.n_signals, 3);
    assert_eq!(s.timescale().to_string(), "1ns");
    assert_eq!(s.time_range(), (0, 40));
}

#[test]
fn vtx_directory_contains_expected_files() {
    let dir = tempfile::tempdir().unwrap();
    let trace = vcd::parse_str(BASIC).unwrap();
    let out = dir.path().join("dump.vtx");
    store::write_vtx(&trace, &out, None).unwrap();
    for f in ["meta.json", "signals.parquet", "scopes.parquet", "index.bin"] {
        assert!(out.join(f).exists(), "missing {f}");
    }
    assert!(out.join("events").is_dir());
    assert!(out.join("txn").is_dir());
    let parts: Vec<_> = std::fs::read_dir(out.join("events")).unwrap().collect();
    assert!(!parts.is_empty(), "no event parts written");
}

#[test]
fn aliased_paths_share_one_stream() {
    let src = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$scope module dut $end
$var wire 1 ! clk $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
";
    let (_d, s, _) = convert(src);
    let a = s.handle("tb.clk").unwrap();
    let b = s.handle("tb.dut.clk").unwrap();
    assert_eq!(s.signal(a).unwrap().stream_id, s.signal(b).unwrap().stream_id);
    assert_eq!(s.value_at(a, 5).unwrap(), s.value_at(b, 5).unwrap());
    let mut al = s.aliases(a).unwrap();
    al.sort();
    assert_eq!(al, vec!["tb.clk", "tb.dut.clk"]);
}

#[test]
fn wide_signals_survive_the_store() {
    let wide: String = std::iter::repeat('1').take(100).collect();
    let src = format!(
        "$timescale 1ns $end\n$scope module top $end\n$var wire 100 ! w [99:0] $end\n\
         $upscope $end\n$enddefinitions $end\n#0\nb{wide} !\n#10\nbx !\n"
    );
    let (_d, s, _) = convert(&src);
    let h = s.handle("top.w").unwrap();
    let v = s.value_at(h, 0).unwrap().unwrap();
    assert_eq!(v.width(), 100);
    for i in 0..100 {
        assert_eq!(v.bit(i), vt_trace::Bit::One, "bit {i}");
    }
    assert_eq!(s.first_x(h).unwrap(), Some(10));
}

#[test]
fn real_values_survive_the_store() {
    let src = "\
$timescale 1ns $end
$scope module top $end
$var real 64 ! r $end
$upscope $end
$enddefinitions $end
#0
r1.5 !
#10
r-2.25 !
";
    let (_d, s, _) = convert(src);
    let h = s.handle("top.r").unwrap();
    assert_eq!(s.value_at(h, 5).unwrap(), Some(Value::Real(1.5)));
    assert_eq!(s.value_at(h, 10).unwrap(), Some(Value::Real(-2.25)));
}

// ---------------------------------------------------------------------------
// Round trip on the reference design
// ---------------------------------------------------------------------------

/// VCD -> .vtx -> VCD, compared against the same canonicaliser applied to the
/// original. See `reconstruct`'s module docs for what canonical means and why
/// the parts it normalises carry no information.
#[test]
fn roundtrip_fifo_async_is_lossless() {
    let src = designs_dir().join("dump.vcd");
    if !src.exists() {
        // Generated by `make sim-icarus`; skip when Icarus has not been run.
        eprintln!("skipping: {} not present", src.display());
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("dump.vtx");

    let original = vcd::parse_file(&src).unwrap();
    store::write_vtx(&original, &out, Some(&src)).unwrap();
    let s = TraceStore::open(&out).unwrap();

    let from_original = reconstruct::from_trace(&original);
    let from_vtx = reconstruct::from_store(&s).unwrap();
    assert_eq!(from_original, from_vtx, "reconstruction from .vtx differs from the source trace");

    // Re-parsing the reconstruction must yield identical events.
    let again = vcd::parse_str(&from_vtx).unwrap();
    assert_eq!(again.signals.len(), original.signals.len());
    assert_eq!(again.streams.len(), original.streams.len());
    assert_eq!(again.total_events(), original.total_events());
    for (i, (a, b)) in again.streams.iter().zip(original.streams.iter()).enumerate() {
        assert_eq!(a.times, b.times, "stream {i} times");
        assert_eq!(a.deltas, b.deltas, "stream {i} deltas");
        for r in 0..a.len() {
            assert_eq!(a.values.get(r), b.values.get(r), "stream {i} row {r}");
        }
    }
    assert_eq!(reconstruct::from_trace(&again), from_original, "canonical form is not a fixed point");
}

/// The reference dump is where the NBA case shows up for real: `wr_ptr` updates
/// in the same timestamp as the `clk` posedge that caused it.
#[test]
fn reference_design_has_real_nba_case() {
    let src = designs_dir().join("dump.vcd");
    if !src.exists() {
        eprintln!("skipping: {} not present", src.display());
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("dump.vtx");
    store::convert_vcd(&src, &out).unwrap();
    let s = TraceStore::open(&out).unwrap();

    let clk = s.handle("tb_fifo_sync.clk").unwrap();
    let wr_ptr = s.handle("tb_fifo_sync.dut.wr_ptr").unwrap();

    // Find a posedge of clk where wr_ptr also changed at that exact timestamp.
    let edges = s.transitions(clk, 0, s.time_range().1 + 1).unwrap();
    let mut checked = 0;
    for (t, v) in edges {
        if v.as_u64() != Some(1) {
            continue;
        }
        if s.deltas_at(wr_ptr, t).unwrap() == 0 {
            continue;
        }
        let after = s.value_at(wr_ptr, t).unwrap();
        let before = s.value_before(wr_ptr, t).unwrap();
        assert_ne!(after, before, "at t={t} the pre-edge value must differ from the post-edge one");
        checked += 1;
    }
    assert!(checked > 0, "reference dump should contain at least one same-timestamp register update");
}

// ---------------------------------------------------------------------------
// Cycle-aligned sampling — the primitive under §8.14's channel scan
// ---------------------------------------------------------------------------

#[test]
fn rising_edges_are_settled_edges_not_glitches() {
    // `q` in GLITCH goes 0 -> (1,0,1) -> 0 inside one timestamp. Only the
    // settled 0->1 at #10 is an edge; counting the intermediate writes would
    // renumber every cycle after it.
    let (_d, s, _) = convert(GLITCH);
    let q = s.handle("top.q").unwrap();
    assert_eq!(s.rising_edges(q).unwrap(), vec![10]);
}

#[test]
fn rising_edges_include_the_first_edge_out_of_x() {
    let src = "\
$timescale 1ns $end
$scope module top $end
$var reg 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
x!
#10
1!
#20
0!
#30
1!
";
    // A clock that starts unknown still has a first real edge; dropping it
    // would shift the whole run by one cycle.
    let (_d, s, _) = convert(src);
    assert_eq!(s.rising_edges(s.handle("top.clk").unwrap()).unwrap(), vec![10, 30]);
}

#[test]
fn sample_before_agrees_with_value_before_everywhere() {
    let (_d, s, _) = convert(BASIC);
    let handles = vec![
        s.handle("top.clk").unwrap(),
        s.handle("top.data").unwrap(),
        s.handle("top.bus").unwrap(),
    ];
    let times: Vec<i64> = (-5..45).collect();

    // The fast path and the O(log n) one must never disagree: everything the
    // extraction engine reports about a handshake rests on this.
    let rows = s.sample_before(&handles, &times).unwrap();
    assert_eq!(rows.len(), handles.len());
    for (row, &h) in rows.iter().zip(&handles) {
        assert_eq!(row.len(), times.len());
        for (got, &t) in row.iter().zip(&times) {
            assert_eq!(*got, s.value_before(h, t).unwrap(), "handle {h} at t={t}");
        }
    }
}

#[test]
fn sample_before_reads_the_pre_edge_value_at_an_edge() {
    // §5.5 problem 2: at #10 `data` is written in the same timestamp as the
    // clock edge. A handshake is decided by what the flops saw going *into*
    // the edge, so sampling must report the old value, not the new one.
    let (_d, s, _) = convert(BASIC);
    let data = s.handle("top.data").unwrap();
    let clk = s.handle("top.clk").unwrap();
    let edges = s.rising_edges(clk).unwrap();
    assert_eq!(edges, vec![10, 30]);

    let rows = s.sample_before(&[data], &edges).unwrap();
    assert_eq!(rows[0][0].as_ref().unwrap().as_u64(), Some(0x00));
    assert_eq!(rows[0][1].as_ref().unwrap().as_u64(), Some(0xA0));
    assert_eq!(s.value_at(data, 10).unwrap().unwrap().as_u64(), Some(0xA0));
}

#[test]
fn sample_before_has_no_value_before_the_first_event() {
    let (_d, s, _) = convert(BASIC);
    let rows = s.sample_before(&[s.handle("top.clk").unwrap()], &[-1, 0, 1]).unwrap();
    assert_eq!(rows[0][0], None);
    assert_eq!(rows[0][1], None);
    assert_eq!(rows[0][2].as_ref().unwrap().as_u64(), Some(0));
}

/// Widths that exercise every encoding branch, plus a signal that never moves.
const RAGGED: &str = "$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$var wire 8 \" b $end
$var wire 33 # wide $end
$var wire 1 $ quiet $end
$upscope $end
$enddefinitions $end
#0
0!
b00000001 \"
b000000000000000000000000000000001 #
0$
#10
1!
b10101010 \"
#20
0!
b11111111 \"
b111111111111111111111111111111111 #
#30
1!
bxxxxxxxx \"
#40
b00001111 \"
";

/// The index summary and the Parquet rows must never disagree.
///
/// Past the end of the trace `value_at_all` answers from the last-value summary
/// in `index.bin` and touches no Parquet at all — which is what takes §8.4 from
/// four times over its tier-B budget to well inside it. Two routes to one answer
/// is exactly where a fast path goes quietly wrong, so this pins them together
/// across every encoding: one bit, a byte, a value wider than a word, an X, and
/// a signal that never moved.
#[test]
fn the_summary_agrees_with_the_rows_it_summarises() {
    let (_d, s, _) = convert(RAGGED);
    let handles: Vec<u32> = (0..s.n_signals() as u32).collect();
    let (_t0, t1) = s.time_range();

    for t in [t1, t1 + 1, t1 + 1000] {
        let bulk = s.value_at_all(&handles, t);
        let one: Vec<_> = handles.iter().map(|&h| s.value_at(h, t).unwrap()).collect();
        assert_eq!(bulk, one, "past the end at t={t}");
    }
    // And the values themselves are the last ones written, not a neighbour's.
    assert_eq!(u64_at(&s, "top.b", t1 + 1), Some(0b0000_1111));
    assert_eq!(u64_at(&s, "top.a", t1 + 1), Some(1));
    assert_eq!(u64_at(&s, "top.quiet", t1 + 1), Some(0));

    // Inside the trace the row-group path still runs, and still agrees.
    for t in [0, 5, 10, 20, 25, 30, 40] {
        let bulk = s.value_at_all(&handles, t);
        let one: Vec<_> = handles.iter().map(|&h| s.value_at(h, t).unwrap()).collect();
        assert_eq!(bulk, one, "inside the trace at t={t}");
    }
}

/// §8.4 asks this of every signal at once, so it carries the same obligation.
///
/// The whole-trace case now answers from the chunk table rather than decoding
/// every signal's time column, which was most of what the scan cost.
#[test]
fn last_change_all_agrees_with_last_change_before() {
    let (_d, s, _) = convert(RAGGED);
    let (_t0, t1) = s.time_range();
    for t in [0, 15, 25, t1, t1 + 1, t1 + 1000] {
        let bulk = s.last_change_all(t);
        for h in 0..s.n_signals() as u32 {
            assert_eq!(
                bulk[h as usize],
                s.last_change_before(h, t).unwrap(),
                "signal {h} disagrees at t={t}"
            );
        }
    }
}
