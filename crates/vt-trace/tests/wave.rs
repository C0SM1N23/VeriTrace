//! Tests for the rendering-window reduction of §10.2.
//!
//! The rule under test is a hard invariant, not a heuristic: whatever the
//! client asks for, the number of entries returned never exceeds the pixel
//! width it asked for.

use vt_trace::query::{TraceStore, WaveMode, FLAG_X, FLAG_Z, MAX_PX};
use vt_trace::{store, vcd};

fn build(src: &str) -> (tempfile::TempDir, TraceStore) {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("t.vtx");
    let trace = vcd::parse_str(src).unwrap();
    store::write_vtx(&trace, &out, None).unwrap();
    let s = TraceStore::open(&out).unwrap();
    (dir, s)
}

/// One signal with 1e6 transitions, plus a slow one for contrast.
fn big_trace(n: usize) -> String {
    let mut s = String::with_capacity(n * 12 + 256);
    s.push_str(
        "$timescale 1ns $end\n$scope module top $end\n\
         $var wire 1 ! fast $end\n$var wire 8 \" slow [7:0] $end\n\
         $upscope $end\n$enddefinitions $end\n",
    );
    for i in 0..n {
        s.push_str(&format!("#{}\n{}!\n", i + 1, i % 2));
        if i % 100_000 == 0 {
            s.push_str(&format!("b{:b} \"\n", (i / 100_000) & 0xff));
        }
    }
    s
}

#[test]
fn never_returns_more_entries_than_pixels() {
    // The explicit case from the spec: 10^6 transitions asked for at 1200 px.
    let n = 1_000_000;
    let (_d, s) = build(&big_trace(n));
    let fast = s.handle("top.fast").unwrap();

    let w = s.wave(fast, 0, n as i64 + 1, 1200).unwrap();
    assert_eq!(
        w.mode,
        WaveMode::MinMax,
        "a million transitions must be reduced"
    );
    assert!(w.len() <= 1200, "returned {} entries for 1200 px", w.len());
    // The reduction must still cover the window, not truncate it.
    assert!(
        w.len() > 1000,
        "reduction collapsed too far: {} entries",
        w.len()
    );

    // Total transitions accounted for, none dropped.
    let counted: u32 = w.buckets.iter().map(|b| b.n).sum();
    assert_eq!(
        counted as usize, n,
        "every transition must land in some bucket"
    );
}

#[test]
fn invariant_holds_across_pixel_widths_and_windows() {
    let n = 200_000;
    let (_d, s) = build(&big_trace(n));
    let fast = s.handle("top.fast").unwrap();

    for px in [1usize, 2, 7, 100, 1200, 1920, 4096] {
        for (t0, t1) in [
            (0i64, n as i64),
            (0, 1000),
            (n as i64 / 2, n as i64),
            (500, 600),
        ] {
            let w = s.wave(fast, t0, t1, px).unwrap();
            assert!(
                w.len() <= px,
                "px={px} window={t0}..{t1} returned {} entries",
                w.len()
            );
        }
    }
}

#[test]
fn absurd_pixel_width_is_clamped() {
    let n = 100_000;
    let (_d, s) = build(&big_trace(n));
    let fast = s.handle("top.fast").unwrap();
    let w = s.wave(fast, 0, n as i64, 100_000_000).unwrap();
    assert!(w.len() <= MAX_PX, "clamp failed: {} entries", w.len());
}

#[test]
fn sparse_signal_is_returned_exactly() {
    let (_d, s) = build(
        "$timescale 1ns $end\n$scope module top $end\n$var wire 1 ! a $end\n\
         $upscope $end\n$enddefinitions $end\n#0\n0!\n#10\n1!\n#20\n0!\n",
    );
    let h = s.handle("top.a").unwrap();
    let w = s.wave(h, 0, 100, 1200).unwrap();
    // Three transitions into 1200 pixels: no reduction, exact values.
    assert_eq!(w.mode, WaveMode::Exact);
    assert_eq!(w.points.len(), 3);
    assert_eq!(w.points[0], (0, "0".to_string()));
    assert_eq!(w.points[1], (10, "1".to_string()));
    assert_eq!(w.points[2], (20, "0".to_string()));
}

#[test]
fn initial_value_covers_the_leading_edge() {
    let (_d, s) = build(
        "$timescale 1ns $end\n$scope module top $end\n$var wire 4 ! d [3:0] $end\n\
         $upscope $end\n$enddefinitions $end\n#0\nb1010 !\n#100\nb1 !\n",
    );
    let h = s.handle("top.d").unwrap();
    // Window starting after the last change: the client still needs to know
    // what the signal is holding.
    let w = s.wave(h, 50, 60, 100).unwrap();
    assert_eq!(w.initial.as_deref(), Some("1010"));
    assert!(w.is_empty());

    // Window before any event at all.
    let w = s.wave(h, -100, -50, 100).unwrap();
    assert_eq!(w.initial, None);
}

#[test]
fn buckets_report_value_range_and_unknowns() {
    // Many changes inside one pixel: the bucket must expose min, max and the
    // presence of X/Z rather than an arbitrary sample.
    let mut src = String::from(
        "$timescale 1ns $end\n$scope module top $end\n$var wire 8 ! d [7:0] $end\n\
         $upscope $end\n$enddefinitions $end\n",
    );
    for i in 0..100 {
        src.push_str(&format!("#{}\nb{:b} !\n", i + 1, i));
    }
    src.push_str("#101\nbx !\n#102\nbz !\n");
    let (_d, s) = build(&src);
    let h = s.handle("top.d").unwrap();

    let w = s.wave(h, 0, 103, 1).unwrap();
    assert_eq!(w.mode, WaveMode::MinMax);
    assert_eq!(w.buckets.len(), 1);
    let b = &w.buckets[0];
    assert_eq!(b.min, "0");
    assert_eq!(b.max, "1100011"); // 99
    assert_eq!(b.n, 102);
    assert_eq!(b.flags & FLAG_X, FLAG_X);
    assert_eq!(b.flags & FLAG_Z, FLAG_Z);
}

#[test]
fn clean_signal_reports_no_unknown_flags() {
    let (_d, s) = build(&big_trace(5000));
    let h = s.handle("top.fast").unwrap();
    let w = s.wave(h, 0, 5001, 50).unwrap();
    assert!(w.buckets.iter().all(|b| b.flags == 0));
    // A toggling 1-bit signal collapses to a 0..1 band.
    assert!(w.buckets.iter().all(|b| b.min == "0" && b.max == "1"));
}

#[test]
fn degenerate_window_is_not_an_error() {
    let (_d, s) = build(&big_trace(100));
    let h = s.handle("top.fast").unwrap();
    assert!(s.wave(h, 50, 50, 100).unwrap().is_empty());
    assert!(s.wave(h, 60, 50, 100).unwrap().is_empty());
    assert_eq!(
        s.wave(h, 0, 10, 0).unwrap().len(),
        1.min(s.wave(h, 0, 10, 1).unwrap().len())
    );
}
