//! Performance check against the budgets in §4.2.
//!
//!   cargo run --release --example bench -- [target_mb] [n_signals]
//!
//! Generates a synthetic VCD of roughly `target_mb` megabytes over `n_signals`
//! signals, converts it, and times `value_at`. Exits non-zero if a threshold is
//! missed, so it can gate a build the way §4.2 asks.
//!
//! Both tiers of §4.2 are reachable from here:
//!
//!   bench -- 100 5000      tier A, "typical"  — the budgets below
//!   bench -- 1000 50000    tier B, "stress"   — convert < 45 s, open < 2 s
//!
//! The thresholds enforced are tier A's; tier B is a validation run whose
//! numbers are read rather than gated, exactly as the spec frames it.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::time::Instant;

use vt_trace::query::TraceStore;
use vt_trace::{store, vcd};

/// §4.2, tier A.
const BUDGET_CONVERT_S: f64 = 6.0;
const BUDGET_VALUE_AT_US: f64 = 5.0;

/// Deterministic PRNG so runs are comparable.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n.max(1)
    }
}

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

/// Build a VCD of about `target_bytes`, shaped like a real design: a mix of
/// scalars and buses, most signals quiet, a few very active.
///
/// Written straight to `path` instead of built in memory: the tier-B input of
/// §4.2 is a gigabyte, and holding that as a `String` needs the whole thing
/// resident before a single byte is parsed. Streaming it makes the stress tier
/// runnable at all, and the machine measures conversion rather than its own
/// allocator.
fn generate(path: &Path, target_bytes: usize, n_signals: usize) -> u64 {
    let mut rng = Rng(0x1234_5678_9abc_def0);
    let widths: Vec<u32> =
        (0..n_signals).map(|i| if i % 3 == 0 { 1 } else { [4, 8, 16, 32][i % 4] }).collect();
    let codes: Vec<String> = (0..n_signals).map(|i| code_for(i as u32)).collect();

    let file = File::create(path).unwrap();
    let mut w = BufWriter::with_capacity(1 << 20, file);
    let mut written: u64 = 0;
    // A small reusable buffer keeps the flush cadence independent of the
    // record size, so the writer never sees a partial line.
    let mut s = String::with_capacity(1 << 16);
    s.push_str("$timescale 1ps $end\n$scope module top $end\n");
    for i in 0..n_signals {
        if widths[i] == 1 {
            s.push_str(&format!("$var wire 1 {} s{i} $end\n", codes[i]));
        } else {
            s.push_str(&format!(
                "$var wire {} {} s{i} [{}:0] $end\n",
                widths[i],
                codes[i],
                widths[i] - 1
            ));
        }
    }
    s.push_str("$upscope $end\n$enddefinitions $end\n");
    written += s.len() as u64;
    w.write_all(s.as_bytes()).unwrap();
    s.clear();

    let mut t: u64 = 0;
    while written < target_bytes as u64 {
        t += 1000;
        s.push_str(&format!("#{t}\n"));
        // A clock-like signal toggles every step; the rest are sparse.
        let n_changes = 8 + rng.below(24) as usize;
        for _ in 0..n_changes {
            let i = rng.below(n_signals as u64) as usize;
            let w = widths[i];
            if w == 1 {
                let bit = if rng.next() & 1 == 0 { '0' } else { '1' };
                s.push_str(&format!("{bit}{}\n", codes[i]));
            } else {
                let v = rng.next() & ((1u64 << w.min(63)) - 1);
                s.push_str(&format!("b{:b} {}\n", v, codes[i]));
            }
        }
        // Occasional delta-cycle glitch, so the settled-value path is exercised.
        if rng.below(50) == 0 {
            let i = rng.below(n_signals as u64) as usize;
            if widths[i] == 1 {
                s.push_str(&format!("1{}\n0{}\n", codes[i], codes[i]));
            }
        }
        written += s.len() as u64;
        w.write_all(s.as_bytes()).unwrap();
        s.clear();
    }
    w.flush().unwrap();
    written
}

fn main() {
    // `bench [MB] [signals]` — both tiers of §4.2 from one harness. Tier A is
    // 100 MB over 5k signals, tier B 1000 MB over 50k, and neither was
    // reachable while the signal count was a constant.
    let mut args = std::env::args().skip(1);
    let target_mb: usize = args.next().and_then(|s| s.parse().ok()).unwrap_or(50);
    let n_signals: usize = args.next().and_then(|s| s.parse().ok()).unwrap_or(5_000);

    eprintln!("generating ~{target_mb} MB VCD over {n_signals} signals...");
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("bench.vcd");
    let bytes = generate(&src, target_mb * 1024 * 1024, n_signals);
    let mb = bytes as f64 / (1024.0 * 1024.0);

    // --- conversion -------------------------------------------------------
    let t0 = Instant::now();
    let trace = vcd::parse_file(&src).unwrap();
    let parse_s = t0.elapsed().as_secs_f64();
    let n_events = trace.total_events();

    let out = dir.path().join("bench.vtx");
    let t1 = Instant::now();
    store::write_vtx(&trace, &out, Some(&src)).unwrap();
    let write_s = t1.elapsed().as_secs_f64();
    let convert_s = parse_s + write_s;
    drop(trace);

    // --- open -------------------------------------------------------------
    let t2 = Instant::now();
    let s = TraceStore::open(&out).unwrap();
    let open_s = t2.elapsed().as_secs_f64();

    // --- value_at ---------------------------------------------------------
    let (t_min, t_max) = s.time_range();
    let span = (t_max - t_min).max(1) as u64;
    let mut rng = Rng(0xdead_beef_cafe_1234);

    // Warm the working set first: the budget describes steady-state querying,
    // not the one-off decode of a signal's first access.
    let probe: Vec<u32> = (0..200.min(s.n_signals())).map(|i| i as u32).collect();
    for h in &probe {
        let _ = s.value_at(*h, t_min).unwrap();
    }

    let n_queries = 200_000;
    let t3 = Instant::now();
    let mut sink = 0u64;
    for _ in 0..n_queries {
        let h = probe[rng.below(probe.len() as u64) as usize];
        let t = t_min + rng.below(span) as i64;
        if let Some(v) = s.value_at(h, t).unwrap() {
            sink = sink.wrapping_add(v.as_u64().unwrap_or(1));
        }
    }
    let value_at_us = t3.elapsed().as_secs_f64() * 1e6 / n_queries as f64;
    std::hint::black_box(sink);

    // Cold cost, for honesty: a signal touched for the first time.
    let cold_start = probe.len() as u32;
    let mut cold_us = f64::NAN;
    if (cold_start as usize) < s.n_signals() {
        let t4 = Instant::now();
        let _ = s.value_at(cold_start, t_min + (span / 2) as i64).unwrap();
        cold_us = t4.elapsed().as_secs_f64() * 1e6;
    }

    // --- whole-trace scan --------------------------------------------------
    let t5 = Instant::now();
    let consts = s.constant_signals(t_min, t_max);
    let scan_ms = t5.elapsed().as_secs_f64() * 1e3;

    // §4.2 has two tiers with different numbers, and comparing a stress run
    // against the typical budget is how a passing gate reads as a failure.
    let stress = mb > 500.0 || s.n_signals() > 20_000;
    let (tier, convert_budget, open_budget, scan_budget) = if stress {
        ("tier B, stress", 45.0, 2.0, 3_000.0)
    } else {
        ("tier A, typical", BUDGET_CONVERT_S, 1.0, 400.0)
    };

    println!();
    println!("  source VCD          {mb:>10.1} MB");
    println!("  signals             {:>10}", s.n_signals());
    println!("  events              {n_events:>10}");
    println!("  ---");
    println!("  parse                {parse_s:>9.2} s");
    println!("  write .vtx           {write_s:>9.2} s");
    // Each line prints the budget for the tier that was actually run. Printing
    // the tier-A constant beside a tier-B run is how a failing gate reads as a
    // pass, and vice versa.
    println!("  convert total        {convert_s:>9.2} s   (budget {convert_budget:.0} s)");
    println!("  open store           {open_s:>9.3} s   (budget {open_budget:.0} s)");
    println!("  value_at (warm)      {value_at_us:>9.3} us  (budget {BUDGET_VALUE_AT_US:.0} us)");
    println!("  value_at (cold)      {cold_us:>9.1} us  (first touch of a signal)");
    // §4.2 budgets *the stuck detector* at 400 ms / 3 s, not this. This is the
    // Rust half of it — the whole-trace constant scan — and labelling it with
    // the detector's number read as if §8.4 were being measured here. It is
    // not: the detector is Python, and `bench/pybench.py` measures it.
    println!(
        "  constant_signals     {scan_ms:>9.1} ms  ({} constant)  [the Rust half of §8.4; \
         see bench/pybench.py for the detector]",
        consts.len()
    );
    println!();

    let mut failed = false;
    if convert_s > convert_budget {
        eprintln!("FAIL: convert {convert_s:.2}s exceeds {convert_budget:.0}s ({tier})");
        failed = true;
    }
    if value_at_us > BUDGET_VALUE_AT_US {
        eprintln!("FAIL: value_at {value_at_us:.3}us exceeds {BUDGET_VALUE_AT_US:.0}us");
        failed = true;
    }
    if open_s > open_budget {
        eprintln!("FAIL: open {open_s:.3}s exceeds {open_budget:.0}s ({tier})");
        failed = true;
    }
    if scan_ms > scan_budget {
        // Reported, not fatal: a regression here is worth seeing without
        // blocking a build on a machine slower than the one the numbers were
        // taken on. The threshold is the stuck detector's, used as a ceiling
        // for its Rust half rather than as a claim that §8.4 was measured.
        eprintln!("OVER: constant_signals {scan_ms:.0}ms exceeds {scan_budget:.0}ms ({tier})");
    }
    if failed {
        std::process::exit(1);
    }
    println!("within budget ({tier})");
}
