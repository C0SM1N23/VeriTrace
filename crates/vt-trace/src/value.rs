//! Four-state value representation.
//!
//! Every bit is held in two planes, matching the encoding table in §6.3:
//!
//! | `a` | `b` | meaning |
//! |-----|-----|---------|
//! |  0  |  0  | `0`     |
//! |  1  |  0  | `1`     |
//! |  0  |  1  | `z`     |
//! |  1  |  1  | `x`     |
//!
//! Signals that never carry X or Z leave the `b` plane empty, which is what
//! lets the store pick a 2-state encoding for them at conversion time.

use std::fmt;

/// Bit-level state, ordered so that `Zero`/`One` are the 2-state subset.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Bit {
    Zero,
    One,
    Z,
    X,
}

impl Bit {
    pub fn from_vcd_char(c: u8) -> Option<Bit> {
        match c {
            b'0' => Some(Bit::Zero),
            b'1' => Some(Bit::One),
            // Weak/pull strengths collapse to their driven level; VCD writers
            // emit these for tri-state nets with pull resistors.
            b'l' | b'L' => Some(Bit::Zero),
            b'h' | b'H' => Some(Bit::One),
            b'z' | b'Z' => Some(Bit::Z),
            b'x' | b'X' | b'u' | b'U' | b'w' | b'W' | b'-' => Some(Bit::X),
            _ => None,
        }
    }

    pub fn to_vcd_char(self) -> char {
        match self {
            Bit::Zero => '0',
            Bit::One => '1',
            Bit::Z => 'z',
            Bit::X => 'x',
        }
    }

    fn planes(self) -> (bool, bool) {
        match self {
            Bit::Zero => (false, false),
            Bit::One => (true, false),
            Bit::Z => (false, true),
            Bit::X => (true, true),
        }
    }

    fn from_planes(a: bool, b: bool) -> Bit {
        match (a, b) {
            (false, false) => Bit::Zero,
            (true, false) => Bit::One,
            (false, true) => Bit::Z,
            (true, true) => Bit::X,
        }
    }
}

/// A value carried by a signal at one point in time.
#[derive(Clone, PartialEq, Debug)]
pub enum Value {
    /// Bit vector, LSB at index 0. `b` is empty when the value is 2-state.
    Bits { width: u32, a: Vec<u64>, b: Vec<u64> },
    /// `r<double>` VCD values.
    Real(f64),
    /// `s<string>` VCD values, emitted by some tools for enums.
    Str(String),
}

pub fn words_for(width: u32) -> usize {
    ((width as usize) + 63) / 64
}

impl Value {
    pub fn width(&self) -> u32 {
        match self {
            Value::Bits { width, .. } => *width,
            Value::Real(_) => 64,
            Value::Str(_) => 0,
        }
    }

    pub fn from_bits(width: u32, bits: &[Bit]) -> Value {
        let n = words_for(width);
        let mut a = vec![0u64; n];
        let mut b = vec![0u64; n];
        for (i, bit) in bits.iter().take(width as usize).enumerate() {
            let (pa, pb) = bit.planes();
            if pa {
                a[i / 64] |= 1u64 << (i % 64);
            }
            if pb {
                b[i / 64] |= 1u64 << (i % 64);
            }
        }
        if b.iter().all(|w| *w == 0) {
            b.clear();
        }
        Value::Bits { width, a, b }
    }

    /// Bit at `i`, LSB-first. Out-of-range reads yield `Zero`.
    pub fn bit(&self, i: u32) -> Bit {
        match self {
            Value::Bits { width, a, b } => {
                if i >= *width {
                    return Bit::Zero;
                }
                let (w, off) = ((i / 64) as usize, i % 64);
                let pa = a.get(w).map_or(false, |x| x >> off & 1 == 1);
                let pb = b.get(w).map_or(false, |x| x >> off & 1 == 1);
                Bit::from_planes(pa, pb)
            }
            _ => Bit::Zero,
        }
    }

    /// True when no bit is X or Z.
    pub fn is_two_state(&self) -> bool {
        match self {
            Value::Bits { b, .. } => b.iter().all(|w| *w == 0),
            _ => true,
        }
    }

    /// True when any bit is X specifically (not Z) — drives `first_x`.
    pub fn has_x(&self) -> bool {
        match self {
            Value::Bits { a, b, .. } => a.iter().zip(b.iter()).any(|(x, y)| x & y != 0),
            _ => false,
        }
    }

    /// True when any bit is Z (high impedance).
    pub fn has_z(&self) -> bool {
        match self {
            Value::Bits { a, b, .. } => a.iter().zip(b.iter()).any(|(x, y)| !x & y != 0),
            _ => false,
        }
    }

    /// Numeric value, when it fits in 64 bits and carries no X/Z.
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Value::Bits { width, a, b } if *width <= 64 && b.iter().all(|w| *w == 0) => {
                Some(a.first().copied().unwrap_or(0))
            }
            _ => None,
        }
    }

    /// Render in VCD form, MSB first, without the leading `b`.
    ///
    /// VCD writers strip redundant leading digits; this reproduces that so a
    /// reconstructed dump matches what a simulator would have written.
    pub fn to_vcd_bits(&self) -> String {
        match self {
            Value::Bits { width, .. } => {
                let w = *width;
                if w == 0 {
                    return String::new();
                }
                // Full MSB-first form, then drop only those leading digits that
                // `parse_vcd_vector`'s left-extension rule would put back. A
                // leading `0` is redundant only ahead of `0`/`1`: dropping it
                // ahead of `x`/`z` would re-extend as x/z and change the value.
                let full: Vec<char> = (0..w).rev().map(|i| self.bit(i).to_vcd_char()).collect();
                let mut start = 0usize;
                while start + 1 < full.len() {
                    let redundant = match full[start] {
                        '0' => matches!(full[start + 1], '0' | '1'),
                        'x' => full[start + 1] == 'x',
                        'z' => full[start + 1] == 'z',
                        _ => false,
                    };
                    if redundant {
                        start += 1;
                    } else {
                        break;
                    }
                }
                full[start..].iter().collect()
            }
            Value::Real(v) => format!("{v}"),
            Value::Str(s) => s.clone(),
        }
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Value::Bits { .. } => write!(f, "{}", self.to_vcd_bits()),
            Value::Real(v) => write!(f, "{v}"),
            Value::Str(s) => write!(f, "{s}"),
        }
    }
}

/// Parse the digits of a VCD vector (`b1010`), applying VCD's left-extension
/// rule: a value shorter than the signal is extended with `0` when the MSB is
/// `0`/`1`, and with the MSB itself when it is `x` or `z`.
pub fn parse_vcd_vector(digits: &[u8], width: u32) -> Option<Value> {
    if digits.is_empty() {
        return None;
    }
    let mut bits = Vec::with_capacity(width as usize);
    // Digits arrive MSB-first; store LSB-first.
    for c in digits.iter().rev() {
        bits.push(Bit::from_vcd_char(*c)?);
    }
    let msb = Bit::from_vcd_char(digits[0])?;
    let fill = match msb {
        Bit::Zero | Bit::One => Bit::Zero,
        Bit::X => Bit::X,
        Bit::Z => Bit::Z,
    };
    while bits.len() < width as usize {
        bits.push(fill);
    }
    bits.truncate(width as usize);
    Some(Value::from_bits(width, &bits))
}

/// Parse a single-character VCD scalar (`0`, `1`, `x`, `z`, ...).
pub fn parse_vcd_scalar(c: u8) -> Option<Value> {
    Bit::from_vcd_char(c).map(|b| Value::from_bits(1, &[b]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scalar_round_trip() {
        for c in [b'0', b'1', b'x', b'z'] {
            let v = parse_vcd_scalar(c).unwrap();
            assert_eq!(v.to_vcd_bits(), (c as char).to_string());
        }
    }

    #[test]
    fn vector_left_extends_with_zero() {
        // b1 on an 8-bit signal is 0000_0001.
        let v = parse_vcd_vector(b"1", 8).unwrap();
        assert_eq!(v.as_u64(), Some(1));
        assert!(v.is_two_state());
    }

    #[test]
    fn vector_left_extends_x() {
        // bx on an 8-bit signal is x across all eight bits, not just bit 0.
        let v = parse_vcd_vector(b"x", 8).unwrap();
        for i in 0..8 {
            assert_eq!(v.bit(i), Bit::X, "bit {i}");
        }
        assert!(v.has_x());
        assert!(!v.is_two_state());
    }

    #[test]
    fn vector_left_extends_z() {
        let v = parse_vcd_vector(b"z", 4).unwrap();
        for i in 0..4 {
            assert_eq!(v.bit(i), Bit::Z, "bit {i}");
        }
        // Z is not X: first_x must not trigger on a tri-stated bus.
        assert!(!v.has_x());
        assert!(!v.is_two_state());
    }

    #[test]
    fn value_bits_round_trip() {
        let v = parse_vcd_vector(b"10100000", 8).unwrap();
        assert_eq!(v.as_u64(), Some(0xA0));
        assert_eq!(v.to_vcd_bits(), "10100000");
    }

    #[test]
    fn wide_vector_beyond_64_bits() {
        let digits: Vec<u8> = std::iter::repeat(b'1').take(100).collect();
        let v = parse_vcd_vector(&digits, 100).unwrap();
        assert_eq!(v.width(), 100);
        assert!(v.is_two_state());
        for i in 0..100 {
            assert_eq!(v.bit(i), Bit::One, "bit {i}");
        }
        // Too wide for u64 extraction.
        assert_eq!(v.as_u64(), None);
    }

    #[test]
    fn mixed_xz_planes() {
        let v = parse_vcd_vector(b"01xz", 4).unwrap();
        assert_eq!(v.bit(0), Bit::Z);
        assert_eq!(v.bit(1), Bit::X);
        assert_eq!(v.bit(2), Bit::One);
        assert_eq!(v.bit(3), Bit::Zero);
        assert!(v.has_x());
        // The leading 0 is dropped: re-reading `1xz` at width 4 left-extends
        // with 0 because the emitted MSB is `1`.
        assert_eq!(v.to_vcd_bits(), "1xz");
    }

    /// Whatever `to_vcd_bits` emits must read back as the same value — this is
    /// what makes VCD reconstruction lossless.
    #[test]
    fn emitted_bits_reparse_identically() {
        let cases: &[(&str, u32)] = &[
            ("0", 1),
            ("1", 1),
            ("x", 1),
            ("z", 1),
            ("01xz", 4),
            ("0000", 4),
            ("1111", 4),
            ("xxxx", 4),
            ("zzzz", 4),
            ("0xz1", 4),
            ("z1", 8),
            ("x0", 8),
            ("10100000", 8),
            ("00000001", 8),
        ];
        for (digits, width) in cases {
            let v = parse_vcd_vector(digits.as_bytes(), *width).unwrap();
            let emitted = v.to_vcd_bits();
            let back = parse_vcd_vector(emitted.as_bytes(), *width).unwrap();
            assert_eq!(v, back, "{digits} @{width} emitted as {emitted}");
        }
    }
}
