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

use fstapi::{scope_type, var_type, Hier, Reader};

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

fn scope_kind(ty: fstapi::ScopeType) -> &'static str {
    match ty {
        t if t == scope_type::VCD_MODULE => "module",
        t if t == scope_type::VCD_TASK => "task",
        t if t == scope_type::VCD_FUNCTION => "function",
        t if t == scope_type::VCD_BEGIN => "begin",
        t if t == scope_type::VCD_FORK => "fork",
        t if t == scope_type::VCD_GENERATE => "generate",
        t if t == scope_type::VCD_CLASS => "class",
        t if t == scope_type::VCD_INTERFACE => "interface",
        t if t == scope_type::VCD_PACKAGE => "package",
        t if t == scope_type::VCD_PROGRAM => "program",
        t if t == scope_type::VCD_STRUCT => "struct",
        t if t == scope_type::VCD_UNION => "union",
        _ => "scope",
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
///
/// The leading backslash of an escaped identifier is dropped here for the same
/// reason the VCD reader drops it: Icarus writes every dumped array word as
/// `\mem[0]`, and a signal that arrives as `mem[0]` from one format and
/// `\mem[0]` from the other correlates against the RTL in only one of them.
fn split_name_range(raw: &str) -> (String, Option<i64>, Option<i64>) {
    let raw = crate::vcd::unescape_name(raw.trim());
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
    let timescale_exp = r.timescale();
    if !(-15..=2).contains(&timescale_exp) {
        return Err(Error::Fst(format!(
            "unsupported timescale exponent {timescale_exp}; VeriTrace represents femtoseconds through hundreds of seconds"
        )));
    }
    trace.timescale = timescale_from_exp(timescale_exp);
    trace.date = Some(r.date().map_err(fe)?.trim().to_string());
    trace.version = Some(r.version().map_err(fe)?.trim().to_string());

    // Hierarchy first: FST handles play the role VCD identifier codes do, and
    // aliased variables share one, so they map onto one stream.
    let mut stack: Vec<u32> = Vec::new();
    let mut handle_to_stream: HashMap<u32, u32> = HashMap::new();
    let mut stream_meta: Vec<(u32, Kind)> = Vec::new();

    for h in r.hiers() {
        match h {
            Hier::Scope(s) => {
                let name = s.name().map_err(fe)?.to_string();
                let kind = scope_kind(s.ty()).to_string();
                let parent = stack.last().copied();
                let existing = trace
                    .scopes
                    .iter()
                    .position(|x| x.parent == parent && x.name == name && x.kind == kind);
                let idx = match existing {
                    Some(i) => i as u32,
                    None => {
                        trace.scopes.push(Scope { name, kind, parent });
                        (trace.scopes.len() - 1) as u32
                    }
                };
                stack.push(idx);
            }
            Hier::Upscope => {
                if stack.pop().is_none() {
                    return Err(Error::Fst("hierarchy contains an unmatched upscope".into()));
                }
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

                let hier: Vec<String> = stack
                    .iter()
                    .map(|i| trace.scopes[*i as usize].name.clone())
                    .collect();
                trace.signals.push(Signal {
                    id: SignalId {
                        hier,
                        name: name.clone(),
                    },
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

    if !stack.is_empty() {
        return Err(Error::Fst(format!(
            "hierarchy ended with {} unclosed scope(s)",
            stack.len()
        )));
    }

    // The header says how many variables the file declares. If the hierarchy
    // iterator produced fewer, libfst could not inflate the hierarchy block —
    // and the values still arrive, so the result would be a store full of
    // events belonging to signals with no names. That is worse than no answer:
    // it converts, it reports success, and everything above it is nonsense.
    //
    // Seen on Windows/MSVC: `fstReaderRecreateHierFile` duplicates the file
    // descriptor and hands it to `gzdopen`, having "flushed" an input stream —
    // undefined behaviour that glibc tolerates and the MSVC CRT does not. The
    // scratch file it writes beside the dump comes out zero bytes long.
    let declared = usize::try_from(r.var_count())
        .map_err(|_| Error::Fst("variable count does not fit this platform".into()))?;
    if trace.signals.len() != declared {
        return Err(Error::Fst(format!(
            "the FST hierarchy could not be read: the file declares {declared} variable(s) \
             and libfst returned {}. The value data is intact, so this is a limitation of \
             the bundled libfst on this platform, not a corrupt dump — convert the same \
             run to VCD instead.",
            trace.signals.len()
        )));
    }

    // Values. Without an explicit mask the reader emits nothing.
    r.set_mask_all();
    // This makes real callbacks unambiguous.  Without it an eight-character
    // ASCII real is indistinguishable by length from the native payload and
    // was previously decoded as arbitrary binary data.
    r.set_native_doubles_on_callback(true);
    let mut t_min: Option<i64> = None;
    let mut t_max = 0i64;
    let mut value_error: Option<Error> = None;
    {
        let streams = &mut trace.streams;
        r.for_each_block(|time, handle, value, _var_len| {
            if value_error.is_some() {
                return;
            }
            let stream = match handle_to_stream.get(&u32::from(handle)) {
                Some(s) => *s as usize,
                None => {
                    value_error = Some(Error::Fst(format!(
                        "value block references undeclared handle {}",
                        u32::from(handle)
                    )));
                    return;
                }
            };
            let (width, kind) = stream_meta[stream];
            let t = match i64::try_from(time) {
                Ok(t) => t,
                Err(_) => {
                    value_error = Some(Error::Fst(format!(
                        "timestamp {time} exceeds VeriTrace's signed time range"
                    )));
                    return;
                }
            };
            let v = match kind {
                Kind::Real => {
                    if value.len() != 8 {
                        value_error = Some(Error::Fst(format!(
                            "real value for handle {} has {} bytes, expected 8",
                            u32::from(handle),
                            value.len()
                        )));
                        return;
                    }
                    let mut b = [0u8; 8];
                    b.copy_from_slice(value);
                    Value::Real(f64::from_ne_bytes(b))
                }
                Kind::String => match std::str::from_utf8(value) {
                    Ok(s) => Value::Str(s.to_string()),
                    Err(e) => {
                        value_error = Some(Error::Fst(format!(
                            "string value for handle {} is not UTF-8: {e}",
                            u32::from(handle)
                        )));
                        return;
                    }
                },
                _ => match parse_vcd_vector(value, width) {
                    Some(v) => v,
                    None => {
                        value_error = Some(Error::Fst(format!(
                            "invalid bit-vector value for handle {} at time {time}",
                            u32::from(handle)
                        )));
                        return;
                    }
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
    if let Some(e) = value_error {
        return Err(e);
    }

    let header_start = i64::try_from(r.start_time())
        .map_err(|_| Error::Fst("start timestamp exceeds VeriTrace's signed time range".into()))?;
    let header_end = i64::try_from(r.end_time())
        .map_err(|_| Error::Fst("end timestamp exceeds VeriTrace's signed time range".into()))?;
    trace.t_min = t_min.map_or(header_start, |t| t.min(header_start));
    trace.t_max = t_max.max(header_end).max(trace.t_min);
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
        assert_eq!(
            split_name_range("data [7:0]"),
            ("data".into(), Some(7), Some(0))
        );
        assert_eq!(
            split_name_range("mem[3] [7:0]"),
            ("mem[3]".into(), Some(7), Some(0))
        );
    }

    #[test]
    fn an_escaped_identifier_is_named_the_way_the_vcd_path_names_it() {
        // Icarus writes every dumped array word as an escaped identifier. The
        // two readers disagreeing here means the same signal correlates under
        // one format and not the other — which is exactly what CI caught.
        assert_eq!(
            split_name_range(r"\mem[0] [7:0]"),
            ("mem[0]".into(), Some(7), Some(0))
        );
        assert_eq!(split_name_range(r"\my$sig"), ("my$sig".into(), None, None));
    }
}
