//! FST reading — §14.2's real round-trip through GTKWave's libfst.
//!
//! `designs/fifo_async/dump.fst` is the same run as `dump.vcd`, written by
//! Icarus with `vvp sim.vvp -fst`. Two formats, one simulation, so the store
//! built from each must agree signal for signal and event for event. That is
//! the round-trip §14.2 asks for, and until this file existed nothing read an
//! FST at all: the only tests under the feature were two pure-string helpers.
//!
//! Windows is excluded at compile time because the current libfst binding
//! cannot inflate hierarchy data with the Windows CRT.  On every platform
//! where the packaged capability says FST is supported, this test must parse
//! successfully and match the VCD; an "honest failure" is not implementation.

#![cfg(all(feature = "fst", not(windows)))]

use std::path::PathBuf;

use vt_trace::{fst, vcd};

fn designs() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../designs/fifo_async")
}

#[test]
fn an_fst_matches_the_vcd_event_for_event() {
    let dir = designs();
    let from_vcd = vcd::parse_file(dir.join("dump.vcd")).expect("the VCD fixture must parse");

    let parsed = fst::parse_file(dir.join("dump.fst"))
        .expect("a build advertising FST support must parse the reference dump");

    // Same simulation, so the same signals — names, widths and hierarchy.
    let mut a: Vec<String> = parsed.signals.iter().map(|s| s.id.path()).collect();
    let mut b: Vec<String> = from_vcd.signals.iter().map(|s| s.id.path()).collect();
    a.sort();
    b.sort();
    assert_eq!(a, b, "the two formats disagree about which signals exist");

    assert_eq!(
        parsed.total_events(),
        from_vcd.total_events(),
        "the two formats disagree about how many events there are"
    );
    assert_eq!(
        (parsed.t_min, parsed.t_max),
        (from_vcd.t_min, from_vcd.t_max)
    );
    assert_eq!(parsed.timescale, from_vcd.timescale);

    // And the values themselves, per signal, in order.
    for sig in &from_vcd.signals {
        let other = parsed
            .signals
            .iter()
            .find(|s| s.id.path() == sig.id.path())
            .expect("signal present in both");
        let sa = &from_vcd.streams[sig.stream as usize];
        let sb = &parsed.streams[other.stream as usize];
        assert_eq!(
            sa.times,
            sb.times,
            "{}: transition times differ",
            sig.id.path()
        );
        assert_eq!(
            sa.deltas,
            sb.deltas,
            "{}: delta indices differ",
            sig.id.path()
        );
        assert_eq!(sa.len(), sb.len(), "{}: event counts differ", sig.id.path());
        for row in 0..sa.len() {
            assert_eq!(
                sa.values.get(row),
                sb.values.get(row),
                "{}: value differs at event {row}",
                sig.id.path()
            );
        }
    }
}

#[test]
fn the_reader_returns_named_signals_and_real_events() {
    // The invariant behind the check, stated on its own: whatever happens, a
    // successful parse accounts for every variable in the header. Anything
    // less means the hierarchy was lost and the events are anonymous.
    let t = fst::parse_file(designs().join("dump.fst")).expect("reference FST parses");
    assert!(
        !t.signals.is_empty(),
        "a successful FST parse with no signals"
    );
    assert!(
        t.total_events() > 0,
        "signals but no events — the value pass produced nothing"
    );
}
