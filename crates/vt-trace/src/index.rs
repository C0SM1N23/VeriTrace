//! `index.bin` — the only proprietary part of a `.vtx` store.
//!
//! Parquet gives scans, not `value_at(sig, t)` in O(log n) (§6.3). This is the
//! flat, mmap-able side table that does: one entry per (stream, chunk), sorted
//! by `(stream_id, t_first)`, so locating the chunk covering a timestamp is a
//! binary search over a borrowed byte slice with nothing decoded up front.

use std::fs::File;
use std::io::Write;
use std::path::Path;

use memmap2::Mmap;

use crate::model::{Time, Timescale};
use crate::{Error, Result};

pub const MAGIC: &[u8; 4] = b"VTX1";
pub const VERSION: u32 = 1;
pub const HEADER_LEN: usize = 64;
pub const ENTRY_LEN: usize = 40;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChunkEntry {
    pub stream_id: u32,
    pub part_id: u32,
    /// Parquet row group holding this chunk. Each row group belongs to exactly
    /// one stream, so a chunk can be read without filtering.
    pub row_group: u32,
    pub n_rows: u32,
    /// Row offset of this chunk within its stream.
    pub row_offset: u64,
    pub t_first: Time,
    pub t_last: Time,
}

impl ChunkEntry {
    fn write_to(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.stream_id.to_le_bytes());
        out.extend_from_slice(&self.part_id.to_le_bytes());
        out.extend_from_slice(&self.row_group.to_le_bytes());
        out.extend_from_slice(&self.n_rows.to_le_bytes());
        out.extend_from_slice(&self.row_offset.to_le_bytes());
        out.extend_from_slice(&self.t_first.to_le_bytes());
        out.extend_from_slice(&self.t_last.to_le_bytes());
    }

    fn read_from(b: &[u8]) -> ChunkEntry {
        let u32_at = |o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap());
        let u64_at = |o: usize| u64::from_le_bytes(b[o..o + 8].try_into().unwrap());
        let i64_at = |o: usize| i64::from_le_bytes(b[o..o + 8].try_into().unwrap());
        ChunkEntry {
            stream_id: u32_at(0),
            part_id: u32_at(4),
            row_group: u32_at(8),
            n_rows: u32_at(12),
            row_offset: u64_at(16),
            t_first: i64_at(24),
            t_last: i64_at(32),
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Header {
    pub timescale: Timescale,
    pub n_signals: u32,
    pub n_streams: u32,
    pub t_min: Time,
    pub t_max: Time,
    pub n_chunks: u32,
}

/// Serialise header + entries. Entries must already be sorted by
/// `(stream_id, t_first)`.
pub fn write(path: impl AsRef<Path>, h: &Header, entries: &[ChunkEntry]) -> Result<()> {
    let mut buf = Vec::with_capacity(HEADER_LEN + entries.len() * ENTRY_LEN);
    buf.extend_from_slice(MAGIC);
    buf.extend_from_slice(&VERSION.to_le_bytes());
    buf.extend_from_slice(&h.timescale.num.to_le_bytes());
    buf.extend_from_slice(&h.timescale.unit_exp.to_le_bytes());
    buf.extend_from_slice(&h.n_signals.to_le_bytes());
    buf.extend_from_slice(&h.n_streams.to_le_bytes());
    buf.extend_from_slice(&h.t_min.to_le_bytes());
    buf.extend_from_slice(&h.t_max.to_le_bytes());
    buf.extend_from_slice(&(entries.len() as u32).to_le_bytes());
    buf.extend_from_slice(&0u32.to_le_bytes()); // flags
    buf.resize(HEADER_LEN, 0);
    for e in entries {
        e.write_to(&mut buf);
    }
    let mut f = File::create(path)?;
    f.write_all(&buf)?;
    f.sync_all()?;
    Ok(())
}

/// A memory-mapped `index.bin`. Nothing is decoded until queried.
pub struct Index {
    map: Mmap,
    pub header: Header,
}

impl Index {
    pub fn open(path: impl AsRef<Path>) -> Result<Index> {
        let f = File::open(path)?;
        // SAFETY: the store is treated as immutable once written; concurrent
        // truncation would be a corrupt-store condition either way.
        let map = unsafe { Mmap::map(&f)? };
        if map.len() < HEADER_LEN || &map[0..4] != MAGIC {
            return Err(Error::Store("index.bin: bad magic".into()));
        }
        let u32_at = |o: usize| u32::from_le_bytes(map[o..o + 4].try_into().unwrap());
        let i32_at = |o: usize| i32::from_le_bytes(map[o..o + 4].try_into().unwrap());
        let i64_at = |o: usize| i64::from_le_bytes(map[o..o + 8].try_into().unwrap());
        let version = u32_at(4);
        if version != VERSION {
            return Err(Error::Store(format!("index.bin: version {version}, expected {VERSION}")));
        }
        let header = Header {
            timescale: Timescale { num: u32_at(8), unit_exp: i32_at(12) },
            n_signals: u32_at(16),
            n_streams: u32_at(20),
            t_min: i64_at(24),
            t_max: i64_at(32),
            n_chunks: u32_at(40),
        };
        let need = HEADER_LEN + header.n_chunks as usize * ENTRY_LEN;
        if map.len() < need {
            return Err(Error::Store(format!("index.bin: truncated ({} < {need})", map.len())));
        }
        Ok(Index { map, header })
    }

    pub fn len(&self) -> usize {
        self.header.n_chunks as usize
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn entry(&self, i: usize) -> ChunkEntry {
        let off = HEADER_LEN + i * ENTRY_LEN;
        ChunkEntry::read_from(&self.map[off..off + ENTRY_LEN])
    }

    /// Half-open entry range belonging to `stream`, found by binary search.
    pub fn chunks_of(&self, stream: u32) -> (usize, usize) {
        let n = self.len();
        let lo = partition_point(n, |i| self.entry(i).stream_id < stream);
        let hi = partition_point(n, |i| self.entry(i).stream_id <= stream);
        (lo, hi)
    }

    /// Index of the chunk whose time span covers `t`, or the last chunk that
    /// starts at or before `t`. `None` when `t` precedes the stream entirely.
    pub fn chunk_for_time(&self, stream: u32, t: Time) -> Option<usize> {
        let (lo, hi) = self.chunks_of(stream);
        if lo == hi {
            return None;
        }
        // Last chunk with t_first <= t.
        let k = partition_point(hi - lo, |i| self.entry(lo + i).t_first <= t);
        if k == 0 {
            None
        } else {
            Some(lo + k - 1)
        }
    }
}

/// `slice::partition_point` over an index range, for borrowed-on-demand entries.
fn partition_point(n: usize, pred: impl Fn(usize) -> bool) -> usize {
    let (mut lo, mut hi) = (0usize, n);
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if pred(mid) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(stream_id: u32, t_first: Time, t_last: Time, row_offset: u64) -> ChunkEntry {
        ChunkEntry { stream_id, part_id: 0, row_group: 0, n_rows: 4, row_offset, t_first, t_last }
    }

    fn sample() -> (tempfile::TempDir, Index) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.bin");
        let entries = vec![
            entry(0, 0, 30, 0),
            entry(0, 40, 70, 4),
            entry(2, 10, 90, 0),
            entry(5, 5, 5, 0),
        ];
        let h = Header {
            timescale: Timescale { num: 1, unit_exp: -12 },
            n_signals: 7,
            n_streams: 6,
            t_min: 0,
            t_max: 90,
            n_chunks: entries.len() as u32,
        };
        write(&path, &h, &entries).unwrap();
        let idx = Index::open(&path).unwrap();
        (dir, idx)
    }

    #[test]
    fn header_round_trips() {
        let (_d, idx) = sample();
        assert_eq!(idx.header.n_signals, 7);
        assert_eq!(idx.header.n_streams, 6);
        assert_eq!(idx.header.t_max, 90);
        assert_eq!(idx.header.timescale.to_string(), "1ps");
        assert_eq!(idx.len(), 4);
    }

    #[test]
    fn entries_round_trip() {
        let (_d, idx) = sample();
        assert_eq!(idx.entry(1), entry(0, 40, 70, 4));
        assert_eq!(idx.entry(3), entry(5, 5, 5, 0));
    }

    #[test]
    fn chunks_of_finds_stream_ranges() {
        let (_d, idx) = sample();
        assert_eq!(idx.chunks_of(0), (0, 2));
        assert_eq!(idx.chunks_of(2), (2, 3));
        assert_eq!(idx.chunks_of(5), (3, 4));
        // Streams with no events resolve to an empty range, not a panic.
        assert_eq!(idx.chunks_of(1).0, idx.chunks_of(1).1);
        assert_eq!(idx.chunks_of(99).0, idx.chunks_of(99).1);
    }

    #[test]
    fn chunk_for_time_selects_covering_chunk() {
        let (_d, idx) = sample();
        assert_eq!(idx.chunk_for_time(0, 0), Some(0));
        assert_eq!(idx.chunk_for_time(0, 39), Some(0));
        assert_eq!(idx.chunk_for_time(0, 40), Some(1));
        assert_eq!(idx.chunk_for_time(0, 10_000), Some(1));
        // Before the stream starts.
        assert_eq!(idx.chunk_for_time(2, 0), None);
        assert_eq!(idx.chunk_for_time(1, 5), None);
    }

    #[test]
    fn rejects_bad_magic() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.bin");
        std::fs::write(&path, vec![0u8; 128]).unwrap();
        assert!(Index::open(&path).is_err());
    }
}
