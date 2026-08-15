//! What libfst sees in an FST file — a debugging aid for the reader.
//!
//!   cargo run -p vt-trace --features fst --example fstinfo -- dump.fst
//!
//! Prints the header, the first few hierarchy entries and the first value
//! callbacks. Exists because "0 signals, 0 events" from the converter says
//! nothing about *which* of those two steps came back empty.

#[cfg(not(feature = "fst"))]
fn main() {
    eprintln!("build with --features fst");
    std::process::exit(2);
}

#[cfg(feature = "fst")]
fn main() {
    use fstapi::{Hier, Reader};

    let path = std::env::args().nth(1).expect("usage: fstinfo <file.fst>");
    let mut r = Reader::open(&path).expect("open");

    println!("timescale exp   {}", r.timescale());
    println!("start .. end    {} .. {}", r.start_time(), r.end_time());
    println!("var count       {:?}", r.var_count());
    println!("scope count     {:?}", r.scope_count());
    println!("alias count     {:?}", r.alias_count());

    let mut scopes = 0;
    let mut vars = 0;
    for h in r.hiers() {
        match h {
            Hier::Scope(s) => {
                if scopes < 4 {
                    println!("  scope  {}", s.name().unwrap_or_default());
                }
                scopes += 1;
            }
            Hier::Var(v) => {
                if vars < 6 {
                    println!(
                        "  var    {:<22} len={} handle={}",
                        v.name().unwrap_or_default(),
                        v.length(),
                        u32::from(v.handle())
                    );
                }
                vars += 1;
            }
            _ => {}
        }
    }
    println!("hierarchy       {scopes} scope(s), {vars} var(s)");

    r.set_mask_all();
    let mut n = 0usize;
    r.for_each_block(|time, handle, value, _| {
        if n < 6 {
            println!(
                "  value  t={time:<8} handle={:<4} {:?}",
                u32::from(handle),
                String::from_utf8_lossy(value)
            );
        }
        n += 1;
    })
    .expect("for_each_block");
    println!("values          {n}");
}
