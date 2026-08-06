//! FST reader, built on the `fstapi` binding over GTKWave's libfst.
//!
//! The format is not reimplemented here (§4.1): libfst is the reference
//! implementation and handles block compression and its many edge cases. This
//! module only maps FST's hierarchy and value callbacks onto the same [`Trace`]
//! the VCD path produces, so everything above it is format-agnostic.
//!
//! Enabled by the `fst` Cargo feature. `fstapi` compiles libfst from C and needs
//! zlib plus libclang; on Linux that is `zlib1g-dev` and `libclang-dev`, on
//! Windows it additionally wants a vcpkg install, which is why it is off by
//! default.

use std::collections::HashMap;
use std::path::Path;

use fstapi::{var_type, Hier, Reader};

use crate::model::{EventStream, Kind, Scope, Signal, SignalId, Timescale, Trace};
use crate::value::{parse_vcd_vector, Value};
use crate::{Error, Result};

fn fe(e: fstapi::Error) -> Error {
    Error::Fst(e.to_string())
}

fn kind_of(ty: fstapi::VarType) -> Kind {
    match ty {
        t if t == var_type::VCD_REG => Kind::Reg,
        t if t == var_type::VCD_PARAMETER || t == var_type::VCD_REAL_PARAMETER => Kind::Parameter,
        t if t == var_type::VCD_INTEGER || t == var_type::SV_INT => Kind::Integer,
        t if t == var_type::VCD_REAL
            || t == var_type::VCD_REALTIME
            || t == var_type::SV_SHORTREAL =>
        {
            Kind::Real
        }
        t if t == var_type::VCD_TIME => Kind::Time,
        t if t == var_type::VCD_EVENT => Kind::Event,
        t if t == var_type::VCD_SUPPLY0 => Kind::Supply0,
        t if t == var_type::VCD_SUPPLY1 => Kind::Supply1,
        t if t == var_type::VCD_TRI => Kind::Tri,
        t if t == var_type::VCD_TRIAND => Kind::TriAnd,
        t if t == var_type::VCD_TRIOR => Kind::TriOr,
        t if t == var_type::VCD_TRIREG => Kind::TriReg,
        t if t == var_type::VCD_TRI0 => Kind::Tri0,
        t if t == var_type::VCD_TRI1 => Kind::Tri1,
        t if t == var_type::VCD_WAND => Kind::WAnd,
        t if t == var_type::VCD_WOR => Kind::WOr,
        t if t == var_type::GEN_STRING => Kind::String,
        _ => Kind::Wire,
    }
}

/// FST records the timescale as a plain power of ten; VCD units go in steps of
/// three, so 10 ps arrives as exponent -11 and becomes `10ps`.
fn timescale_from_exp(exp: i32) -> Timescale {
    let unit_exp = (exp.div_euclid(3)) * 3;
    let unit_exp = unit_exp.clamp(-15, 0);
    let mut num = 1u32;
    for _ in 0..(exp - unit_exp).clamp(0, 3) {
        num = num.saturating_mul(10);
    }
    Timescale { num, unit_exp }
}

/// FST names often carry their range, e.g. `data [7:0]`.
fn split_name_range(raw: &str) -> (String, Option<i64>, Option<i64>) {
    let raw = raw.trim();
    if let Some(pos) = raw.rfind(' ') {
        let (name, tail) = raw.split_at(pos);
        let tail = tail.trim();
        if tail.starts_with('[') && tail.ends_with(']') {
            let (msb, lsb) = crate::vcd::parse_range(tail);
            if msb.is_some() {
                return (name.trim().to_string(), msb, lsb);
            }
        }
    }
    (raw.to_string(), None, None)
}

pub fn parse_file(path: impl AsRef<Path>) -> Result<Trace> {
    let path = path.as_ref();
    let mut r = Reader::open(path).map_err(fe)?;

    let mut trace = Trace::default();
    trace.timescale = timescale_from_exp(r.timescale());
    trace.date = r.date().ok().map(|s| s.trim().to_string());
    trace.version = r.version().ok().map(|s| s.trim().to_string());

    // Hierarchy first: FST handles play the role VCD identifier codes do, and
    // aliased variables share one, so they map onto one stream.
    let mut stack: Vec<u32> = Vec::new();
    let mut handle_to_stream: HashMap<u32, u32> = HashMap::new();
    let mut stream_meta: Vec<(u32, Kind)> = Vec::new();

    for h in r.hiers() {
        match h {
            Hier::Scope(s) => {
                let name = s.name().map_err(fe)?.to_string();
                let parent = stack.last().copied();
                let existing =
                    trace.scopes.iter().position(|x| x.parent == parent && x.name == name);
                let idx = match existing {
                    Some(i) => i as u32,
                    None => {
                        trace.scopes.push(Scope { name, kind: "module".into(), parent });
                        (trace.scopes.len() - 1) as u32
                    }
                };
                stack.push(idx);
            }
            Hier::Upscope => {
                stack.pop();
            }
            Hier::Var(v) => {
                let raw = v.name().map_err(fe)?.to_string();
                let (name, msb, lsb) = split_name_range(&raw);
                let width = v.length().max(1);
                let kind = kind_of(v.ty());
                let handle = u32::from(v.handle());

                let stream = match handle_to_stream.get(&handle) {
                    Some(id) => *id,
                    None => {
                        let id = trace.streams.len() as u32;
                        trace.streams.push(EventStream::new(width, kind));
                        stream_meta.push((width, kind));
                        handle_to_stream.insert(handle, id);
                        id
                    }
                };

                let hier: Vec<String> =
                    stack.iter().map(|i| trace.scopes[*i as usize].name.clone()).collect();
                trace.signals.push(Signal {
                    id: SignalId { hier, name: name.clone() },
                    width,
                    kind,
                    stream,
                    msb,
                    lsb,
                    array_index: crate::vcd::array_index_of(&name),
                    code: handle.to_string(),
                });
            }
            _ => {}
        }
    }

    // Values. Without an explicit mask the reader emits nothing.
    r.set_mask_all();
    let mut t_min: Option<i64> = None;
    let mut t_max = 0i64;
    {
        let streams = &mut trace.streams;
        r.for_each_block(|time, handle, value, _var_len| {
            let stream = match handle_to_stream.get(&u32::from(handle)) {
                Some(s) => *s as usize,
                None => return,
            };
            let (width, kind) = stream_meta[stream];
            let t = time as i64;
            let v = match kind {
                Kind::Real => {
                    if value.len() == 8 {
                        // Native doubles, when the reader hands them over raw.
                        let mut b = [0u8; 8];
                        b.copy_from_slice(value);
                        Value::Real(f64::from_le_bytes(b))
                    } else {
                        Value::Real(
                            std::str::from_utf8(value)
                                .ok()
                                .and_then(|s| s.trim().parse().ok())
                                .unwrap_or(0.0),
                        )
                    }
                }
                Kind::String => Value::Str(String::from_utf8_lossy(value).to_string()),
                _ => match parse_vcd_vector(value, width) {
                    Some(v) => v,
                    None => return,
                },
            };
            if t_min.is_none() {
                t_min = Some(t);
            }
            if t > t_max {
                t_max = t;
            }
            streams[stream].push(t, &v);
        })
        .map_err(fe)?;
    }

    trace.t_min = t_min.unwrap_or(r.start_time() as i64);
    trace.t_max = t_max.max(trace.t_min);
    Ok(trace)
}

/// Parse an FST and write it as `.vtx`.
pub fn convert(src: impl AsRef<Path>, out: impl AsRef<Path>) -> Result<Trace> {
    let src = src.as_ref();
    let trace = parse_file(src)?;
    crate::store::write_vtx(&trace, out, Some(src))?;
    Ok(trace)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timescale_exponents_map_to_vcd_units() {
        assert_eq!(timescale_from_exp(-12).to_string(), "1ps");
        assert_eq!(timescale_from_exp(-9).to_string(), "1ns");
        assert_eq!(timescale_from_exp(-11).to_string(), "10ps");
        assert_eq!(timescale_from_exp(-10).to_string(), "100ps");
        assert_eq!(timescale_from_exp(0).to_string(), "1s");
    }

    #[test]
    fn names_split_from_ranges() {
        assert_eq!(split_name_range("clk"), ("clk".into(), None, None));
        assert_eq!(split_name_range("data [7:0]"), ("data".into(), Some(7), Some(0)));
        assert_eq!(split_name_range("mem[3] [7:0]"), ("mem[3]".into(), Some(7), Some(0)));
    }
}
