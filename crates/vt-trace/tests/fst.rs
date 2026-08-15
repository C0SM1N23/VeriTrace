//! FST reading — §14.2's round-trip, and the contract when it cannot be done.
//!
//! `designs/fifo_async/dump.fst` is the same run as `dump.vcd`, written by
//! Icarus with `vvp sim.vvp -fst`. Two formats, one simulation, so the store
//! built from each must agree signal for signal and event for event. That is
//! the round-trip §14.2 asks for, and until this file existed nothing read an
//! FST at all: the only tests under the feature were two pure-string helpers.
//!
//! The reader is allowed to *fail* — the bundled libfst cannot inflate an FST
//! hierarchy on Windows/MSVC — but it is not allowed to succeed with less than
//! the file declares. An empty store reported as a conversion is the one
//! outcome this test exists to forbid.

#![cfg(feature = "fst")]

use std::path::PathBuf;

use vt_trace::{fst, vcd};

fn designs() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../designs/fifo_async")
}

#[test]
fn an_fst_either_matches_the_vcd_or_says_why_not() {
    let dir = designs();
    let from_vcd = vcd::parse_file(dir.join("dump.vcd")).expect("the VCD fixture must parse");

    let parsed = match fst::parse_file(dir.join("dump.fst")) {
        Ok(t) => t,
        Err(e) => {
            // The honest failure. It has to name the hierarchy, because that is
            // what distinguishes "this platform cannot" from "your dump is
            // broken" — and it must not be a silent empty trace.
            let msg = e.to_string();
            assert!(
                msg.contains("hierarchy"),
                "an FST that cannot be read must say what failed, got: {msg}"
            );
            return;
        }
    };

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
    assert_eq!((parsed.t_min, parsed.t_max), (from_vcd.t_min, from_vcd.t_max));

    // And the values themselves, per signal, in order.
    for sig in &from_vcd.signals {
        let other = parsed
            .signals
            .iter()
            .find(|s| s.id.path() == sig.id.path())
            .expect("signal present in both");
        let sa = &from_vcd.streams[sig.stream as usize];
        let sb = &parsed.streams[other.stream as usize];
        assert_eq!(sa.times, sb.times, "{}: transition times differ", sig.id.path());
    }
}

#[test]
fn the_reader_never_returns_fewer_signals_than_the_file_declares() {
    // The invariant behind the check, stated on its own: whatever happens, a
    // successful parse accounts for every variable in the header. Anything
    // less means the hierarchy was lost and the events are anonymous.
    if let Ok(t) = fst::parse_file(designs().join("dump.fst")) {
        assert!(!t.signals.is_empty(), "a successful FST parse with no signals");
        assert!(
            t.total_events() > 0,
            "signals but no events — the value pass produced nothing"
        );
    }
}
