//! Core data model: hierarchy, signals, and event streams.
//!
//! One detail drives the whole shape of this module: a VCD identifier code can
//! be shared by several hierarchical paths. In the reference `fifo_async` dump,
//! code `&` is both `tb_fifo_sync.clk` and `tb_fifo_sync.dut.clk`. Those are two
//! *signals* backed by one *stream* of events. Keeping the two concepts apart
//! avoids storing the same transitions twice and gives the correlation layer
//! (§7) the alias information it needs later.

use crate::value::{words_for, Bit, Value};

pub type Time = i64;

/// Index into [`Trace::signals`].
pub type Handle = u32;

/// Index into [`Trace::streams`].
pub type StreamId = u32;

#[derive(Clone, Copy, PartialEq, Eq, Debug, serde::Serialize, serde::Deserialize)]
pub enum Kind {
    Wire,
    Reg,
    Parameter,
    Integer,
    Real,
    Time,
    Event,
    Supply0,
    Supply1,
    Tri,
    TriAnd,
    TriOr,
    TriReg,
    Tri0,
    Tri1,
    WAnd,
    WOr,
    String,
    Other,
}

impl Kind {
    pub fn from_vcd(s: &str) -> Kind {
        match s {
            "wire" => Kind::Wire,
            "reg" => Kind::Reg,
            "parameter" => Kind::Parameter,
            "integer" => Kind::Integer,
            "real" | "realtime" => Kind::Real,
            "time" => Kind::Time,
            "event" => Kind::Event,
            "supply0" => Kind::Supply0,
            "supply1" => Kind::Supply1,
            "tri" => Kind::Tri,
            "triand" => Kind::TriAnd,
            "trior" => Kind::TriOr,
            "trireg" => Kind::TriReg,
            "tri0" => Kind::Tri0,
            "tri1" => Kind::Tri1,
            "wand" => Kind::WAnd,
            "wor" => Kind::WOr,
            "string" => Kind::String,
            _ => Kind::Other,
        }
    }

    pub fn as_vcd(self) -> &'static str {
        match self {
            Kind::Wire => "wire",
            Kind::Reg => "reg",
            Kind::Parameter => "parameter",
            Kind::Integer => "integer",
            Kind::Real => "real",
            Kind::Time => "time",
            Kind::Event => "event",
            Kind::Supply0 => "supply0",
            Kind::Supply1 => "supply1",
            Kind::Tri => "tri",
            Kind::TriAnd => "triand",
            Kind::TriOr => "trior",
            Kind::TriReg => "trireg",
            Kind::Tri0 => "tri0",
            Kind::Tri1 => "tri1",
            Kind::WAnd => "wand",
            Kind::WOr => "wor",
            Kind::String => "string",
            Kind::Other => "wire",
        }
    }
}

/// Hierarchical identity of a signal, as in §5.1.
#[derive(Clone, PartialEq, Eq, Hash, Debug, serde::Serialize, serde::Deserialize)]
pub struct SignalId {
    pub hier: Vec<String>,
    pub name: String,
}

impl SignalId {
    pub fn path(&self) -> String {
        if self.hier.is_empty() {
            return self.name.clone();
        }
        let mut s = String::with_capacity(self.hier.iter().map(|h| h.len() + 1).sum::<usize>() + self.name.len());
        for h in &self.hier {
            s.push_str(h);
            s.push('.');
        }
        s.push_str(&self.name);
        s
    }
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct Scope {
    pub name: String,
    pub kind: String,
    pub parent: Option<u32>,
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct Signal {
    pub id: SignalId,
    pub width: u32,
    pub kind: Kind,
    pub stream: StreamId,
    /// Declared bit range, e.g. `[7:0]`, preserved for reconstruction.
    pub msb: Option<i64>,
    pub lsb: Option<i64>,
    /// Element index when the signal is one word of an unpacked array, e.g.
    /// `mem[3]`. §5.6 needs this to resolve dynamically indexed reads.
    pub array_index: Option<i64>,
    /// VCD identifier code this signal was declared with.
    pub code: String,
}

impl Signal {
    pub fn path(&self) -> String {
        self.id.path()
    }
}

/// Append-only columnar buffer of values.
///
/// Holding values column-wise rather than as a `Vec<Value>` keeps conversion
/// allocation-free per event and lets `value_at` be an indexed read instead of
/// a pointer chase — which is what the <5 µs budget in §4.2 needs.
#[derive(Clone, Debug)]
pub enum ValueColumn {
    /// `b` stays empty until the first X/Z is seen, which is how the store
    /// decides between the 2-state and 4-state encodings of §6.3.
    Bits { width: u32, words: usize, a: Vec<u64>, b: Vec<u64> },
    Real(Vec<f64>),
    Str(Vec<String>),
}

impl ValueColumn {
    pub fn new_bits(width: u32) -> ValueColumn {
        ValueColumn::Bits { width, words: words_for(width).max(1), a: Vec::new(), b: Vec::new() }
    }

    pub fn len(&self) -> usize {
        match self {
            ValueColumn::Bits { words, a, .. } => a.len() / words.max(&1),
            ValueColumn::Real(v) => v.len(),
            ValueColumn::Str(v) => v.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// True when no row carries X or Z.
    pub fn is_two_state(&self) -> bool {
        match self {
            ValueColumn::Bits { b, .. } => b.is_empty(),
            _ => true,
        }
    }

    pub fn push(&mut self, v: &Value) {
        match (self, v) {
            (ValueColumn::Bits { width, words, a, b }, Value::Bits { a: va, b: vb, .. }) => {
                let rows = a.len() / *words;
                for w in 0..*words {
                    a.push(va.get(w).copied().unwrap_or(0));
                }
                if !vb.is_empty() {
                    // First X/Z on this signal: backfill the plane we skipped.
                    if b.is_empty() {
                        b.resize(rows * *words, 0);
                    }
                    for w in 0..*words {
                        b.push(vb.get(w).copied().unwrap_or(0));
                    }
                } else if !b.is_empty() {
                    b.resize(b.len() + *words, 0);
                }
                let _ = width;
            }
            (ValueColumn::Real(col), Value::Real(x)) => col.push(*x),
            (ValueColumn::Str(col), Value::Str(s)) => col.push(s.clone()),
            // Mismatched pushes keep the column length consistent rather than
            // silently desynchronising it from `times`.
            (ValueColumn::Bits { words, a, b, .. }, _) => {
                a.resize(a.len() + *words, 0);
                if !b.is_empty() {
                    b.resize(b.len() + *words, 0);
                }
            }
            (ValueColumn::Real(col), _) => col.push(f64::NAN),
            (ValueColumn::Str(col), _) => col.push(String::new()),
        }
    }

    pub fn get(&self, row: usize) -> Value {
        match self {
            ValueColumn::Bits { width, words, a, b } => {
                let off = row * words;
                let av = a.get(off..off + words).unwrap_or(&[]).to_vec();
                let bv = if b.is_empty() {
                    Vec::new()
                } else {
                    let s = b.get(off..off + words).unwrap_or(&[]).to_vec();
                    if s.iter().all(|w| *w == 0) {
                        Vec::new()
                    } else {
                        s
                    }
                };
                Value::Bits { width: *width, a: av, b: bv }
            }
            ValueColumn::Real(col) => Value::Real(col.get(row).copied().unwrap_or(0.0)),
            ValueColumn::Str(col) => Value::Str(col.get(row).cloned().unwrap_or_default()),
        }
    }

    /// Whether row `row` contains an X bit, without materialising a [`Value`].
    pub fn row_has_x(&self, row: usize) -> bool {
        match self {
            ValueColumn::Bits { words, a, b, .. } if !b.is_empty() => {
                let off = row * words;
                (0..*words).any(|w| {
                    let av = a.get(off + w).copied().unwrap_or(0);
                    let bv = b.get(off + w).copied().unwrap_or(0);
                    av & bv != 0
                })
            }
            _ => false,
        }
    }

    /// Compare two rows without materialising values.
    pub fn rows_equal(&self, x: usize, y: usize) -> bool {
        match self {
            ValueColumn::Bits { words, a, b, .. } => {
                let (ox, oy) = (x * words, y * words);
                let same_a = (0..*words).all(|w| a.get(ox + w) == a.get(oy + w));
                if !same_a {
                    return false;
                }
                if b.is_empty() {
                    return true;
                }
                (0..*words).all(|w| b.get(ox + w) == b.get(oy + w))
            }
            ValueColumn::Real(col) => col.get(x) == col.get(y),
            ValueColumn::Str(col) => col.get(x) == col.get(y),
        }
    }

    /// Bit `i` of row `row`.
    pub fn row_bit(&self, row: usize, i: u32) -> Bit {
        match self {
            ValueColumn::Bits { width, words, a, b } => {
                if i >= *width {
                    return Bit::Zero;
                }
                let off = row * words + (i / 64) as usize;
                let sh = i % 64;
                let pa = a.get(off).map_or(false, |w| w >> sh & 1 == 1);
                let pb = b.get(off).map_or(false, |w| w >> sh & 1 == 1);
                match (pa, pb) {
                    (false, false) => Bit::Zero,
                    (true, false) => Bit::One,
                    (false, true) => Bit::Z,
                    (true, true) => Bit::X,
                }
            }
            _ => Bit::Zero,
        }
    }
}

/// The transitions of one VCD identifier code.
///
/// `times` is non-decreasing. Several entries may share a timestamp: those are
/// the delta cycles of §5.5, and `deltas` records their arrival order so the
/// settled value (highest delta) can be told apart from transient glitches.
#[derive(Clone, Debug)]
pub struct EventStream {
    pub times: Vec<Time>,
    pub deltas: Vec<u8>,
    pub values: ValueColumn,
}

impl EventStream {
    pub fn new(width: u32, kind: Kind) -> EventStream {
        let values = match kind {
            Kind::Real => ValueColumn::Real(Vec::new()),
            Kind::String => ValueColumn::Str(Vec::new()),
            _ => ValueColumn::new_bits(width),
        };
        EventStream { times: Vec::new(), deltas: Vec::new(), values }
    }

    pub fn len(&self) -> usize {
        self.times.len()
    }

    pub fn is_empty(&self) -> bool {
        self.times.is_empty()
    }

    /// Append a transition, assigning its delta index within `t`.
    pub fn push(&mut self, t: Time, v: &Value) {
        let delta = match self.times.last() {
            Some(&last) if last == t => self.deltas.last().map_or(0, |d| d.saturating_add(1)),
            _ => 0,
        };
        self.times.push(t);
        self.deltas.push(delta);
        self.values.push(v);
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Timescale {
    pub num: u32,
    /// Power-of-ten exponent of the unit, in seconds: ps is -12.
    pub unit_exp: i32,
}

impl Default for Timescale {
    fn default() -> Self {
        Timescale { num: 1, unit_exp: -9 }
    }
}

impl Timescale {
    pub fn unit_str(&self) -> &'static str {
        match self.unit_exp {
            0 => "s",
            -3 => "ms",
            -6 => "us",
            -9 => "ns",
            -12 => "ps",
            -15 => "fs",
            _ => "ns",
        }
    }

    pub fn parse_unit(s: &str) -> Option<i32> {
        match s {
            "s" => Some(0),
            "ms" => Some(-3),
            "us" => Some(-6),
            "ns" => Some(-9),
            "ps" => Some(-12),
            "fs" => Some(-15),
            _ => None,
        }
    }
}

impl std::fmt::Display for Timescale {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}{}", self.num, self.unit_str())
    }
}

/// A fully parsed waveform, before it is written to a `.vtx` store.
#[derive(Clone, Debug)]
pub struct Trace {
    pub timescale: Timescale,
    pub date: Option<String>,
    pub version: Option<String>,
    pub scopes: Vec<Scope>,
    pub signals: Vec<Signal>,
    pub streams: Vec<EventStream>,
    pub t_min: Time,
    pub t_max: Time,
}

impl Default for Trace {
    fn default() -> Self {
        Trace {
            timescale: Timescale::default(),
            date: None,
            version: None,
            scopes: Vec::new(),
            signals: Vec::new(),
            streams: Vec::new(),
            t_min: 0,
            t_max: 0,
        }
    }
}

impl Trace {
    pub fn find(&self, path: &str) -> Option<Handle> {
        self.signals.iter().position(|s| s.path() == path).map(|i| i as Handle)
    }

    pub fn total_events(&self) -> usize {
        self.streams.iter().map(|s| s.len()).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::parse_vcd_vector;

    #[test]
    fn delta_indices_increment_within_a_timestamp() {
        let mut s = EventStream::new(4, Kind::Wire);
        s.push(10, &parse_vcd_vector(b"1", 4).unwrap());
        s.push(20, &parse_vcd_vector(b"10", 4).unwrap());
        s.push(20, &parse_vcd_vector(b"11", 4).unwrap());
        s.push(20, &parse_vcd_vector(b"100", 4).unwrap());
        s.push(30, &parse_vcd_vector(b"0", 4).unwrap());
        assert_eq!(s.times, vec![10, 20, 20, 20, 30]);
        assert_eq!(s.deltas, vec![0, 0, 1, 2, 0]);
    }

    #[test]
    fn xz_plane_backfills_on_first_unknown() {
        let mut s = EventStream::new(4, Kind::Wire);
        s.push(0, &parse_vcd_vector(b"1", 4).unwrap());
        s.push(1, &parse_vcd_vector(b"10", 4).unwrap());
        assert!(s.values.is_two_state());
        s.push(2, &parse_vcd_vector(b"x", 4).unwrap());
        assert!(!s.values.is_two_state());
        // Earlier rows must still read back as 2-state, not as X.
        assert!(!s.values.row_has_x(0));
        assert!(!s.values.row_has_x(1));
        assert!(s.values.row_has_x(2));
        assert_eq!(s.values.get(0).as_u64(), Some(1));
        assert_eq!(s.values.get(1).as_u64(), Some(2));
    }

    #[test]
    fn path_joins_hierarchy() {
        let id = SignalId { hier: vec!["tb".into(), "dut".into()], name: "clk".into() };
        assert_eq!(id.path(), "tb.dut.clk");
    }

    #[test]
    fn wide_column_round_trips() {
        let mut s = EventStream::new(100, Kind::Wire);
        let digits: Vec<u8> = std::iter::repeat(b'1').take(100).collect();
        s.push(0, &parse_vcd_vector(&digits, 100).unwrap());
        s.push(1, &parse_vcd_vector(b"0", 100).unwrap());
        assert_eq!(s.values.len(), 2);
        assert_eq!(s.values.get(0).bit(99), Bit::One);
        assert_eq!(s.values.get(1).bit(99), Bit::Zero);
        assert!(!s.values.rows_equal(0, 1));
    }
}
