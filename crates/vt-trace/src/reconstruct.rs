//! Rebuilding a VCD from a trace or a `.vtx` store.
//!
//! Output is *canonical*: identifier codes are reassigned from stream order and
//! headers use one fixed layout. Two things are therefore deliberately not
//! preserved from the source file — the original identifier codes, and the
//! relative order of writes to *different* signals inside one timestamp (the
//! `.vtx` schema of §6.3 records a per-signal delta, not a global sequence).
//!
//! Neither carries meaning: a VCD's codes are private to the file, and
//! cross-signal order within a timestamp is simulator scheduling noise. Passing
//! the original through the same canonicaliser makes the comparison exact for
//! everything that does carry meaning — hierarchy, widths, timestamps,
//! per-signal delta ordering and every value.

use std::collections::HashMap;
use std::fmt::Write as _;

use crate::model::{Kind, Time, Timescale, Trace};
use crate::query::TraceStore;
use crate::value::Value;
use crate::Result;

/// Deterministic base-94 identifier code over printable ASCII.
fn code_for(mut n: u32) -> String {
    let mut s = String::new();
    loop {
        s.push((33 + (n % 94) as u8) as char);
        n /= 94;
        if n == 0 {
            break;
        }
    }
    s
}

struct Decl {
    path: Vec<String>,
    name: String,
    width: u32,
    kind: Kind,
    msb: Option<i64>,
    lsb: Option<i64>,
    stream: u32,
}

fn format_value(v: &Value, width: u32, kind: Kind, code: &str) -> String {
    match (kind, v) {
        (Kind::Real, Value::Real(x)) => format!("r{x} {code}"),
        (Kind::String, Value::Str(s)) => format!("s{s} {code}"),
        _ => {
            let bits = v.to_vcd_bits();
            if width == 1 {
                format!("{bits}{code}")
            } else {
                format!("b{bits} {code}")
            }
        }
    }
}

/// Emit the declaration section and value changes.
///
/// `events` must be sorted by `(time, stream, row)`; `values[stream][row]` holds
/// the corresponding value.
fn emit(
    timescale: Timescale,
    date: Option<&str>,
    version: Option<&str>,
    decls: &[Decl],
    events: &[(Time, u32, u32)],
    values: &HashMap<u32, Vec<Value>>,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "$date\n\t{}\n$end", date.unwrap_or("").trim());
    let _ = writeln!(out, "$version\n\t{}\n$end", version.unwrap_or("VeriTrace").trim());
    let _ = writeln!(out, "$timescale\n\t{timescale}\n$end");

    // Codes are per stream, so aliased paths keep sharing one code.
    let mut codes: HashMap<u32, String> = HashMap::new();
    let mut next = 0u32;
    for d in decls {
        codes.entry(d.stream).or_insert_with(|| {
            let c = code_for(next);
            next += 1;
            c
        });
    }

    // Walk declarations in order, opening and closing scopes as the path
    // changes, so the hierarchy comes out as a properly nested tree.
    let mut cur: Vec<String> = Vec::new();
    for d in decls {
        let common = cur.iter().zip(d.path.iter()).take_while(|(a, b)| a == b).count();
        for _ in common..cur.len() {
            let _ = writeln!(out, "$upscope $end");
        }
        cur.truncate(common);
        for s in &d.path[common..] {
            let _ = writeln!(out, "$scope module {s} $end");
            cur.push(s.clone());
        }
        let range = match (d.msb, d.lsb) {
            (Some(m), Some(l)) if d.width > 1 || m != l => format!(" [{m}:{l}]"),
            _ => String::new(),
        };
        let code = &codes[&d.stream];
        let _ = writeln!(
            out,
            "$var {} {} {} {}{} $end",
            d.kind.as_vcd(),
            d.width,
            code,
            d.name,
            range
        );
    }
    for _ in 0..cur.len() {
        let _ = writeln!(out, "$upscope $end");
    }
    let _ = writeln!(out, "$enddefinitions $end");

    let widths: HashMap<u32, (u32, Kind)> =
        decls.iter().map(|d| (d.stream, (d.width, d.kind))).collect();

    let mut last_time: Option<Time> = None;
    for (t, stream, row) in events {
        if last_time != Some(*t) {
            let _ = writeln!(out, "#{t}");
            last_time = Some(*t);
        }
        let (w, k) = widths[stream];
        let v = &values[stream][*row as usize];
        let _ = writeln!(out, "{}", format_value(v, w, k, &codes[stream]));
    }
    out
}

/// Canonical VCD for an in-memory trace.
pub fn from_trace(trace: &Trace) -> String {
    let decls: Vec<Decl> = trace
        .signals
        .iter()
        .map(|s| Decl {
            path: s.id.hier.clone(),
            name: s.id.name.clone(),
            width: s.width,
            kind: s.kind,
            msb: s.msb,
            lsb: s.lsb,
            stream: s.stream,
        })
        .collect();

    let mut events: Vec<(Time, u32, u32)> = Vec::new();
    let mut values: HashMap<u32, Vec<Value>> = HashMap::new();
    for (sid, s) in trace.streams.iter().enumerate() {
        let sid = sid as u32;
        let mut vs = Vec::with_capacity(s.len());
        for r in 0..s.len() {
            events.push((s.times[r], sid, r as u32));
            vs.push(s.values.get(r));
        }
        values.insert(sid, vs);
    }
    events.sort_unstable();
    emit(trace.timescale, trace.date.as_deref(), trace.version.as_deref(), &decls, &events, &values)
}

/// Canonical VCD for a `.vtx` store.
pub fn from_store(store: &TraceStore) -> Result<String> {
    let decls: Vec<Decl> = store
        .signals
        .iter()
        .map(|s| Decl {
            path: if s.scope.is_empty() {
                Vec::new()
            } else {
                s.scope.split('.').map(|x| x.to_string()).collect()
            },
            name: s.name.clone(),
            width: s.width,
            kind: s.kind,
            msb: s.msb,
            lsb: s.lsb,
            stream: s.stream_id,
        })
        .collect();

    // One representative handle per stream: aliases share the event data.
    let mut rep: HashMap<u32, u32> = HashMap::new();
    for s in &store.signals {
        rep.entry(s.stream_id).or_insert(s.signal_id);
    }

    let (t_min, t_max) = store.time_range();
    let mut events: Vec<(Time, u32, u32)> = Vec::new();
    let mut values: HashMap<u32, Vec<Value>> = HashMap::new();
    let mut streams: Vec<_> = rep.into_iter().collect();
    streams.sort();
    for (stream, handle) in streams {
        let tr = store.transitions(handle, t_min, t_max.saturating_add(1))?;
        let mut vs = Vec::with_capacity(tr.len());
        for (r, (t, v)) in tr.into_iter().enumerate() {
            events.push((t, stream, r as u32));
            vs.push(v);
        }
        values.insert(stream, vs);
    }
    events.sort_unstable();

    let ts = store.timescale();
    Ok(emit(ts, store.meta.date.as_deref(), store.meta.writer.as_deref(), &decls, &events, &values))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_unique() {
        let mut seen = std::collections::HashSet::new();
        for n in 0..5000u32 {
            assert!(seen.insert(code_for(n)), "duplicate code at {n}");
        }
    }

    #[test]
    fn canonical_form_is_stable_under_reparse() {
        let src = "\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 4 \" d [3:0] $end
$scope module sub $end
$var wire 1 ! clk $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0 \"
#5
1!
bx \"
#10
0!
b1010 \"
";
        let t1 = crate::vcd::parse_str(src).unwrap();
        let c1 = from_trace(&t1);
        let t2 = crate::vcd::parse_str(&c1).unwrap();
        let c2 = from_trace(&t2);
        assert_eq!(c1, c2, "canonical form must be a fixed point");
        // And the alias survives the trip.
        assert_eq!(t2.signals.len(), 3);
        assert_eq!(t2.streams.len(), 2);
    }

    #[test]
    fn glitches_survive_reconstruction() {
        let src = "\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! g $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
0!
";
        let t1 = crate::vcd::parse_str(src).unwrap();
        let c1 = from_trace(&t1);
        let t2 = crate::vcd::parse_str(&c1).unwrap();
        assert_eq!(t2.streams[0].times, vec![0, 5, 5]);
        assert_eq!(t2.streams[0].deltas, vec![0, 0, 1]);
        assert_eq!(from_trace(&t2), c1);
    }
}
