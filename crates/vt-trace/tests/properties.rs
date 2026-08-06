//! Property-based tests over synthetic traces built with deliberate delta
//! cycles and glitches (§5.5, §14).
//!
//! Each case generates a random waveform, renders it to VCD, pushes it through
//! the real conversion path, and checks the store against a reference model
//! computed straight from the generated events. The reference is intentionally
//! naive — a linear scan — so it cannot share a bug with the binary searches it
//! is checking.

use proptest::prelude::*;
use vt_trace::query::TraceStore;
use vt_trace::value::{parse_vcd_vector, Value};
use vt_trace::{reconstruct, store, vcd};

/// Widths chosen to cover scalar, sub-word, word and multi-word encodings.
const WIDTHS: [u32; 5] = [1, 3, 8, 40, 96];
const CODES: [&str; 5] = ["!", "\"", "#", "$", "%"];

/// One write: which signal, and the 4-state digits written.
type Write = (usize, String);
/// A timestamp and every write that lands on it, in order.
type Group = (i64, Vec<Write>);

fn arb_digits(width: u32) -> impl Strategy<Value = String> {
    let max = width.min(8) as usize;
    prop::collection::vec(prop::sample::select(vec!['0', '1', 'x', 'z']), 1..=max)
        .prop_map(|v| v.into_iter().collect())
}

fn arb_write() -> impl Strategy<Value = Write> {
    (0..WIDTHS.len()).prop_flat_map(|sig| arb_digits(WIDTHS[sig]).prop_map(move |d| (sig, d)))
}

/// A whole waveform: increasing timestamps, each with 1..6 writes. Repeated
/// writes to one signal inside a group are the glitches under test.
fn arb_groups() -> impl Strategy<Value = Vec<Group>> {
    prop::collection::vec((1i64..20, prop::collection::vec(arb_write(), 1..6)), 1..25).prop_map(
        |raw| {
            let mut t = 0i64;
            raw.into_iter()
                .map(|(dt, writes)| {
                    t += dt;
                    (t, writes)
                })
                .collect()
        },
    )
}

fn render_vcd(groups: &[Group]) -> String {
    let mut s = String::from("$timescale 1ns $end\n$scope module top $end\n");
    for (i, w) in WIDTHS.iter().enumerate() {
        if *w == 1 {
            s.push_str(&format!("$var wire 1 {} s{i} $end\n", CODES[i]));
        } else {
            s.push_str(&format!("$var wire {w} {} s{i} [{}:0] $end\n", CODES[i], w - 1));
        }
    }
    s.push_str("$upscope $end\n$enddefinitions $end\n");
    for (t, writes) in groups {
        s.push_str(&format!("#{t}\n"));
        for (sig, digits) in writes {
            if WIDTHS[*sig] == 1 {
                s.push_str(&format!("{digits}{}\n", CODES[*sig]));
            } else {
                s.push_str(&format!("b{digits} {}\n", CODES[*sig]));
            }
        }
    }
    s
}

/// Reference model: per signal, the events in order, as parsed values.
fn reference(groups: &[Group]) -> Vec<Vec<(i64, Value)>> {
    let mut out: Vec<Vec<(i64, Value)>> = vec![Vec::new(); WIDTHS.len()];
    for (t, writes) in groups {
        for (sig, digits) in writes {
            let v = parse_vcd_vector(digits.as_bytes(), WIDTHS[*sig]).unwrap();
            out[*sig].push((*t, v));
        }
    }
    out
}

fn ref_value_at(events: &[(i64, Value)], t: i64) -> Option<Value> {
    events.iter().filter(|(et, _)| *et <= t).next_back().map(|(_, v)| v.clone())
}

fn ref_value_before(events: &[(i64, Value)], t: i64) -> Option<Value> {
    events.iter().filter(|(et, _)| *et < t).next_back().map(|(_, v)| v.clone())
}

struct Fixture {
    _dir: tempfile::TempDir,
    store: TraceStore,
}

fn build(groups: &[Group]) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("t.vtx");
    let trace = vcd::parse_str(&render_vcd(groups)).unwrap();
    store::write_vtx(&trace, &out, None).unwrap();
    let store = TraceStore::open(&out).unwrap();
    Fixture { _dir: dir, store }
}

/// Every timestamp in the trace plus the gaps and edges around it.
fn probe_times(groups: &[Group]) -> Vec<i64> {
    let mut ts: Vec<i64> = groups.iter().flat_map(|(t, _)| [*t - 1, *t, *t + 1]).collect();
    ts.push(-1);
    ts.push(groups.last().map(|(t, _)| *t + 50).unwrap_or(0));
    ts.sort_unstable();
    ts.dedup();
    ts
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 64, ..ProptestConfig::default() })]

    /// The rule from §5.5: `value_at` is the last write at that timestamp.
    #[test]
    fn value_at_is_the_settled_value(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            for t in probe_times(&groups) {
                prop_assert_eq!(
                    f.store.value_at(h, t).unwrap(),
                    ref_value_at(events, t),
                    "value_at(s{}, {})", sig, t
                );
            }
        }
    }

    /// `value_before` is what NBA evaluation needs: strictly earlier.
    #[test]
    fn value_before_is_strictly_earlier(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            for t in probe_times(&groups) {
                prop_assert_eq!(
                    f.store.value_before(h, t).unwrap(),
                    ref_value_before(events, t),
                    "value_before(s{}, {})", sig, t
                );
            }
        }
    }

    /// Glitch accounting: every write at a timestamp is retained and reachable,
    /// and the highest delta is what `value_at` returns.
    #[test]
    fn deltas_expose_every_write(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            for t in probe_times(&groups) {
                let at_t: Vec<Value> =
                    events.iter().filter(|(et, _)| *et == t).map(|(_, v)| v.clone()).collect();
                prop_assert_eq!(f.store.deltas_at(h, t).unwrap(), at_t.len());
                for (d, expected) in at_t.iter().enumerate() {
                    let got = f.store.value_at_delta(h, t, d).unwrap();
                    prop_assert_eq!(
                        got, Some(expected.clone()),
                        "value_at_delta(s{}, {}, {})", sig, t, d
                    );
                }
                if let Some(last) = at_t.last() {
                    // Asking past the final delta clamps to the settled value.
                    let clamped = f.store.value_at_delta(h, t, at_t.len() + 3).unwrap();
                    prop_assert_eq!(clamped, Some(last.clone()));
                    let settled = f.store.value_at(h, t).unwrap();
                    prop_assert_eq!(settled, Some(last.clone()));
                }
            }
        }
    }

    /// `last_change_before` / `next_change_after` bracket a timestamp, and
    /// nothing changes strictly between them.
    #[test]
    fn change_navigation_is_consistent(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            for t in probe_times(&groups) {
                let prev = f.store.last_change_before(h, t).unwrap();
                let next = f.store.next_change_after(h, t).unwrap();
                if let Some(p) = prev {
                    prop_assert!(p < t);
                    prop_assert!(events.iter().any(|(et, _)| *et == p));
                    // Nothing between p and t.
                    prop_assert!(!events.iter().any(|(et, _)| *et > p && *et < t));
                }
                if let Some(n) = next {
                    prop_assert!(n > t);
                    prop_assert!(events.iter().any(|(et, _)| *et == n));
                    prop_assert!(!events.iter().any(|(et, _)| *et > t && *et < n));
                }
            }
        }
    }

    /// `is_constant` agrees with the transitions actually present.
    #[test]
    fn is_constant_agrees_with_transitions(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        let times = probe_times(&groups);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            for (i, a) in times.iter().enumerate() {
                for b in times.iter().skip(i + 1) {
                    let changed = events.iter().any(|(et, _)| *et > *a && *et < *b);
                    prop_assert_eq!(
                        f.store.is_constant(h, *a, *b).unwrap(),
                        !changed,
                        "is_constant(s{}, {}, {})", sig, a, b
                    );
                }
            }
        }
    }

    /// Everything written to the store reads back byte-identically.
    #[test]
    fn store_preserves_every_event(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        let end = groups.last().map(|(t, _)| *t + 1).unwrap_or(1);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            let got = f.store.transitions(h, i64::MIN / 4, end).unwrap();
            prop_assert_eq!(got.len(), events.len(), "event count for s{}", sig);
            for (a, b) in got.iter().zip(events.iter()) {
                prop_assert_eq!(a.0, b.0);
                prop_assert_eq!(&a.1, &b.1);
            }
        }
    }

    /// Reconstruction is lossless: canonical VCD reparses to the same events and
    /// re-emits byte-identically.
    #[test]
    fn reconstruction_is_a_fixed_point(groups in arb_groups()) {
        let text = render_vcd(&groups);
        let t1 = vcd::parse_str(&text).unwrap();
        let c1 = reconstruct::from_trace(&t1);
        let t2 = vcd::parse_str(&c1).unwrap();
        let c2 = reconstruct::from_trace(&t2);
        prop_assert_eq!(&c1, &c2);

        prop_assert_eq!(t1.streams.len(), t2.streams.len());
        for (a, b) in t1.streams.iter().zip(t2.streams.iter()) {
            prop_assert_eq!(&a.times, &b.times);
            prop_assert_eq!(&a.deltas, &b.deltas);
            for r in 0..a.len() {
                prop_assert_eq!(a.values.get(r), b.values.get(r));
            }
        }
    }

    /// Reconstructing from the store matches reconstructing from the trace.
    #[test]
    fn store_and_trace_reconstruct_identically(groups in arb_groups()) {
        let text = render_vcd(&groups);
        let trace = vcd::parse_str(&text).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("t.vtx");
        store::write_vtx(&trace, &out, None).unwrap();
        let s = TraceStore::open(&out).unwrap();
        prop_assert_eq!(reconstruct::from_store(&s).unwrap(), reconstruct::from_trace(&trace));
    }

    /// `first_x` finds the earliest X and never reports a Z.
    #[test]
    fn first_x_finds_earliest_x(groups in arb_groups()) {
        let f = build(&groups);
        let model = reference(&groups);
        for (sig, events) in model.iter().enumerate() {
            let h = f.store.handle(&format!("top.s{sig}")).unwrap();
            let expected = events.iter().find(|(_, v)| v.has_x()).map(|(t, _)| *t);
            prop_assert_eq!(f.store.first_x(h).unwrap(), expected, "first_x(s{})", sig);
        }
    }
}
