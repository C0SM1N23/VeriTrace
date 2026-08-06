//! Streaming VCD parser.
//!
//! Two behaviours here are load-bearing for correctness downstream:
//!
//! 1. **Events are kept verbatim, in file order.** When a signal is written
//!    several times at one timestamp, every write is retained with its delta
//!    index (§5.5, problem 1). Collapsing them to the final value would destroy
//!    the glitch information `deltas_at`/`value_at_delta` exist to expose, and
//!    would break byte-exact reconstruction.
//! 2. **Identifier codes are shared.** Several hierarchical paths can map to one
//!    code; they become distinct signals over a single event stream.

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::Path;

use crate::model::{EventStream, Kind, Scope, Signal, SignalId, Time, Timescale, Trace};
use crate::value::{parse_vcd_scalar, parse_vcd_vector, Value};
use crate::{Error, Result};

/// Whitespace-delimited tokenizer over a reader.
///
/// Tokens are copied into a reused buffer, so a token that straddles a refill
/// boundary costs nothing extra and the parser never sees a truncated token.
struct Lexer<R: Read> {
    r: R,
    buf: Vec<u8>,
    pos: usize,
    filled: usize,
    tok: Vec<u8>,
    line: usize,
    eof: bool,
}

impl<R: Read> Lexer<R> {
    fn new(r: R) -> Lexer<R> {
        Lexer { r, buf: vec![0; 1 << 20], pos: 0, filled: 0, tok: Vec::with_capacity(64), line: 1, eof: false }
    }

    fn fill(&mut self) -> io::Result<()> {
        if self.pos > 0 {
            self.buf.copy_within(self.pos..self.filled, 0);
            self.filled -= self.pos;
            self.pos = 0;
        }
        if self.filled == self.buf.len() {
            let n = self.buf.len();
            self.buf.resize(n * 2, 0);
        }
        let n = self.r.read(&mut self.buf[self.filled..])?;
        if n == 0 {
            self.eof = true;
        } else {
            self.filled += n;
        }
        Ok(())
    }

    /// Advance to the next token. Returns false at end of input.
    fn next(&mut self) -> io::Result<bool> {
        self.tok.clear();
        loop {
            while self.pos < self.filled {
                let c = self.buf[self.pos];
                if !c.is_ascii_whitespace() {
                    break;
                }
                if c == b'\n' {
                    self.line += 1;
                }
                self.pos += 1;
            }
            if self.pos < self.filled {
                break;
            }
            if self.eof {
                return Ok(false);
            }
            self.fill()?;
        }
        loop {
            while self.pos < self.filled {
                let c = self.buf[self.pos];
                if c.is_ascii_whitespace() {
                    return Ok(true);
                }
                self.tok.push(c);
                self.pos += 1;
            }
            if self.eof {
                return Ok(!self.tok.is_empty());
            }
            self.fill()?;
        }
    }
}

/// Identifier codes are short printable ASCII; packing them into a `u64` keeps
/// the per-event lookup allocation-free. Longer codes fall back to a map.
#[derive(Default)]
struct CodeTable {
    short: HashMap<u64, u32>,
    long: HashMap<Vec<u8>, u32>,
}

fn pack(code: &[u8]) -> Option<u64> {
    if code.len() > 8 {
        return None;
    }
    let mut k = 0u64;
    for (i, c) in code.iter().enumerate() {
        k |= (*c as u64) << (i * 8);
    }
    Some(k)
}

impl CodeTable {
    fn get(&self, code: &[u8]) -> Option<u32> {
        match pack(code) {
            Some(k) => self.short.get(&k).copied(),
            None => self.long.get(code).copied(),
        }
    }

    fn insert(&mut self, code: &[u8], id: u32) {
        match pack(code) {
            Some(k) => {
                self.short.insert(k, id);
            }
            None => {
                self.long.insert(code.to_vec(), id);
            }
        }
    }
}

fn err(line: usize, msg: impl Into<String>) -> Error {
    Error::Vcd { line, msg: msg.into() }
}

/// Split a declared range suffix such as `[7:0]` or `[3]`.
pub(crate) fn parse_range(s: &str) -> (Option<i64>, Option<i64>) {
    let inner = match s.strip_prefix('[').and_then(|x| x.strip_suffix(']')) {
        Some(v) => v,
        None => return (None, None),
    };
    match inner.split_once(':') {
        Some((a, b)) => (a.trim().parse().ok(), b.trim().parse().ok()),
        None => {
            let v: Option<i64> = inner.trim().parse().ok();
            (v, v)
        }
    }
}

/// Trailing `[n]` on a name marks one element of an unpacked array (§5.6).
pub(crate) fn array_index_of(name: &str) -> Option<i64> {
    let inner = name.strip_suffix(']')?;
    let idx = inner.rfind('[')?;
    inner[idx + 1..].parse().ok()
}

pub fn parse_file(path: impl AsRef<Path>) -> Result<Trace> {
    let f = File::open(path)?;
    parse_reader(BufReader::with_capacity(1 << 20, f))
}

pub fn parse_str(s: &str) -> Result<Trace> {
    parse_reader(io::Cursor::new(s.as_bytes()))
}

pub fn parse_reader<R: Read>(r: R) -> Result<Trace> {
    let mut lx = Lexer::new(r);
    let mut trace = Trace::default();
    let mut codes = CodeTable::default();
    // Hierarchy walked during declarations. Scopes can be re-entered (Icarus
    // reopens them to declare array words), so entries are found-or-created.
    let mut stack: Vec<u32> = Vec::new();
    let mut stream_width: Vec<u32> = Vec::new();
    let mut t: Time = 0;
    let mut saw_time = false;
    let mut first_time: Option<Time> = None;

    while lx.next()? {
        let line = lx.line;
        if lx.tok.is_empty() {
            continue;
        }
        match lx.tok[0] {
            b'$' => {
                let kw = String::from_utf8_lossy(&lx.tok[1..]).to_string();
                match kw.as_str() {
                    "date" => trace.date = Some(read_text(&mut lx, line)?),
                    "version" => trace.version = Some(read_text(&mut lx, line)?),
                    "comment" => {
                        read_text(&mut lx, line)?;
                    }
                    "timescale" => {
                        let text = read_text(&mut lx, line)?;
                        trace.timescale = parse_timescale(&text)
                            .ok_or_else(|| err(line, format!("bad timescale {text:?}")))?;
                    }
                    "scope" => {
                        let parts = read_tokens(&mut lx, line)?;
                        let kind = parts.first().cloned().unwrap_or_else(|| "module".into());
                        let name = parts.get(1).cloned().unwrap_or_default();
                        let parent = stack.last().copied();
                        let existing = trace
                            .scopes
                            .iter()
                            .position(|s| s.parent == parent && s.name == name);
                        let idx = match existing {
                            Some(i) => i as u32,
                            None => {
                                trace.scopes.push(Scope { name, kind, parent });
                                (trace.scopes.len() - 1) as u32
                            }
                        };
                        stack.push(idx);
                    }
                    "upscope" => {
                        read_tokens(&mut lx, line)?;
                        stack.pop();
                    }
                    "var" => {
                        let parts = read_tokens(&mut lx, line)?;
                        if parts.len() < 4 {
                            return Err(err(line, "$var needs type, width, code and name"));
                        }
                        let kind = Kind::from_vcd(&parts[0]);
                        let width: u32 = parts[1]
                            .parse()
                            .map_err(|_| err(line, format!("bad width {:?}", parts[1])))?;
                        let code = parts[2].clone();
                        // An escaped identifier keeps its text but drops the
                        // leading backslash, so paths read the way users type
                        // them: tb.dut.mem[0].
                        let raw_name = parts[3].trim_start_matches('\\').to_string();
                        let suffix = parts.get(4).cloned().unwrap_or_default();
                        let (msb, lsb) = parse_range(&suffix);

                        let stream = match codes.get(code.as_bytes()) {
                            Some(id) => id,
                            None => {
                                let id = trace.streams.len() as u32;
                                trace.streams.push(EventStream::new(width.max(1), kind));
                                stream_width.push(width.max(1));
                                codes.insert(code.as_bytes(), id);
                                id
                            }
                        };

                        let hier: Vec<String> =
                            stack.iter().map(|i| trace.scopes[*i as usize].name.clone()).collect();
                        trace.signals.push(Signal {
                            id: SignalId { hier, name: raw_name.clone() },
                            width: width.max(1),
                            kind,
                            stream,
                            msb,
                            lsb,
                            array_index: array_index_of(&raw_name),
                            code,
                        });
                    }
                    "enddefinitions" => {
                        read_tokens(&mut lx, line)?;
                    }
                    // Value-carrying sections. Their contents are ordinary value
                    // changes, handled by the main loop; the closing $end is a
                    // no-op below.
                    "dumpall" | "dumpvars" | "dumpon" | "dumpoff" | "end" => {}
                    _ => {
                        // Unknown section: skip to its $end if it has one.
                        read_text(&mut lx, line)?;
                    }
                }
            }
            b'#' => {
                let s = std::str::from_utf8(&lx.tok[1..]).map_err(|_| err(line, "bad time"))?;
                t = s.parse().map_err(|_| err(line, format!("bad time {s:?}")))?;
                saw_time = true;
                if first_time.is_none() {
                    first_time = Some(t);
                }
                if t > trace.t_max {
                    trace.t_max = t;
                }
            }
            b'b' | b'B' | b'r' | b'R' | b's' | b'S' => {
                let lead = lx.tok[0];
                let digits = lx.tok[1..].to_vec();
                if !lx.next()? {
                    return Err(err(line, "value change without identifier"));
                }
                let stream = match codes.get(&lx.tok) {
                    Some(id) => id as usize,
                    // Unknown codes appear when a dump is truncated or a tool
                    // emits changes for undeclared vars; skipping beats failing
                    // the whole conversion.
                    None => continue,
                };
                let width = stream_width[stream];
                let v = match lead {
                    b'b' | b'B' => parse_vcd_vector(&digits, width)
                        .ok_or_else(|| err(line, format!("bad vector b{}", String::from_utf8_lossy(&digits))))?,
                    b'r' | b'R' => {
                        let s = String::from_utf8_lossy(&digits);
                        Value::Real(s.parse().map_err(|_| err(line, format!("bad real {s:?}")))?)
                    }
                    _ => Value::Str(String::from_utf8_lossy(&digits).to_string()),
                };
                trace.streams[stream].push(t, &v);
            }
            _ => {
                // Scalar change: value character followed by the code with no
                // separator, e.g. `0!` or `x"`.
                let lead = lx.tok[0];
                let v = match parse_vcd_scalar(lead) {
                    Some(v) => v,
                    None => continue,
                };
                let code = &lx.tok[1..];
                if code.is_empty() {
                    continue;
                }
                if let Some(stream) = codes.get(code) {
                    trace.streams[stream as usize].push(t, &v);
                }
            }
        }
    }

    trace.t_min = first_time.unwrap_or(0);
    if !saw_time {
        trace.t_max = trace.t_min;
    }
    Ok(trace)
}

/// Read tokens until `$end`, returning them joined by single spaces.
fn read_text<R: Read>(lx: &mut Lexer<R>, line: usize) -> Result<String> {
    let parts = read_tokens(lx, line)?;
    Ok(parts.join(" "))
}

fn read_tokens<R: Read>(lx: &mut Lexer<R>, line: usize) -> Result<Vec<String>> {
    let mut out = Vec::new();
    loop {
        if !lx.next()? {
            // Tolerate a missing final $end rather than discarding the parse.
            return Ok(out);
        }
        if lx.tok == b"$end" {
            return Ok(out);
        }
        out.push(String::from_utf8_lossy(&lx.tok).to_string());
        if out.len() > 1_000_000 {
            return Err(err(line, "unterminated section"));
        }
    }
}

fn parse_timescale(s: &str) -> Option<Timescale> {
    let s = s.replace(' ', "");
    let split = s.find(|c: char| c.is_ascii_alphabetic())?;
    let (num, unit) = s.split_at(split);
    let num: u32 = if num.is_empty() { 1 } else { num.parse().ok()? };
    Some(Timescale { num, unit_exp: Timescale::parse_unit(unit)? })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SIMPLE: &str = r#"
$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 4 " data $end
$upscope $end
$enddefinitions $end
#0
0!
b0 "
#5
1!
b1010 "
#10
0!
"#;

    #[test]
    fn parses_declarations_and_values() {
        let t = parse_str(SIMPLE).unwrap();
        assert_eq!(t.timescale.to_string(), "1ns");
        assert_eq!(t.signals.len(), 2);
        assert_eq!(t.signals[0].path(), "top.clk");
        assert_eq!(t.signals[1].path(), "top.data");
        assert_eq!(t.signals[1].width, 4);
        assert_eq!(t.t_min, 0);
        assert_eq!(t.t_max, 10);

        let clk = &t.streams[t.signals[0].stream as usize];
        assert_eq!(clk.times, vec![0, 5, 10]);
        let data = &t.streams[t.signals[1].stream as usize];
        assert_eq!(data.times, vec![0, 5]);
        assert_eq!(data.values.get(1).as_u64(), Some(0b1010));
    }

    #[test]
    fn shared_code_becomes_one_stream_two_signals() {
        // The reference dump aliases `clk` into the DUT scope this way.
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
        let t = parse_str(src).unwrap();
        assert_eq!(t.signals.len(), 2);
        assert_eq!(t.signals[0].path(), "tb.clk");
        assert_eq!(t.signals[1].path(), "tb.dut.clk");
        // One stream, not two copies of the same transitions.
        assert_eq!(t.streams.len(), 1);
        assert_eq!(t.signals[0].stream, t.signals[1].stream);
        assert_eq!(t.total_events(), 2);
    }

    #[test]
    fn keeps_every_write_within_a_timestamp() {
        // A combinational settle: 0 -> 1 -> 0 all at t=5. The intermediate 1 is
        // a glitch; the settled value is the last write.
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
        let t = parse_str(src).unwrap();
        let s = &t.streams[0];
        assert_eq!(s.times, vec![0, 5, 5]);
        assert_eq!(s.deltas, vec![0, 0, 1]);
        assert_eq!(s.values.get(1).as_u64(), Some(1));
        assert_eq!(s.values.get(2).as_u64(), Some(0));
    }

    #[test]
    fn escaped_identifier_and_array_index() {
        let src = "\
$timescale 1ps $end
$scope module tb $end
$scope module dut $end
$var reg 8 4 \\mem[3] [7:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b1010 4
";
        let t = parse_str(src).unwrap();
        assert_eq!(t.signals.len(), 1);
        let s = &t.signals[0];
        assert_eq!(s.path(), "tb.dut.mem[3]");
        assert_eq!(s.array_index, Some(3));
        assert_eq!((s.msb, s.lsb), (Some(7), Some(0)));
    }

    #[test]
    fn reentrant_scopes_do_not_duplicate_hierarchy() {
        // Icarus reopens tb/dut for each array word.
        let src = "\
$timescale 1ps $end
$scope module tb $end
$scope module dut $end
$var reg 8 4 \\mem[0] [7:0] $end
$upscope $end
$upscope $end
$scope module tb $end
$scope module dut $end
$var reg 8 5 \\mem[1] [7:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
";
        let t = parse_str(src).unwrap();
        // tb and dut each declared once, not twice.
        assert_eq!(t.scopes.len(), 2);
        assert_eq!(t.signals[0].path(), "tb.dut.mem[0]");
        assert_eq!(t.signals[1].path(), "tb.dut.mem[1]");
    }

    #[test]
    fn real_and_string_values() {
        let src = "\
$timescale 1ns $end
$scope module top $end
$var real 64 ! rv $end
$upscope $end
$enddefinitions $end
#0
r3.5 !
#5
r-1.25 !
";
        let t = parse_str(src).unwrap();
        assert_eq!(t.streams[0].values.get(0), Value::Real(3.5));
        assert_eq!(t.streams[0].values.get(1), Value::Real(-1.25));
    }

    #[test]
    fn unknown_code_is_skipped_not_fatal() {
        let src = "\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$upscope $end
$enddefinitions $end
#0
0!
1?
#5
1!
";
        let t = parse_str(src).unwrap();
        assert_eq!(t.streams[0].times, vec![0, 5]);
    }

    #[test]
    fn timescale_variants() {
        assert_eq!(parse_timescale("1ps").unwrap().to_string(), "1ps");
        assert_eq!(parse_timescale("10 ns").unwrap().to_string(), "10ns");
        assert_eq!(parse_timescale("100us").unwrap().to_string(), "100us");
    }
}
