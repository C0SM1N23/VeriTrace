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
pub const VERSION: u32 = 3;
pub const HEADER_LEN: usize = 64;
pub const ENTRY_LEN: usize = 48;
/// One `(stream_id, blob_off, blob_len)` row of the last-value summary, padded
/// to a power of two so the table can be indexed without a multiply.
pub const SUMMARY_LEN: usize = 16;

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
    /// Second *distinct* timestamp in this chunk, or `Time::MAX` when every row
    /// shares one. It is what lets `is_constant` — and so the stuck detector of
    /// §8.4 — answer from the index instead of decoding the time column of
    /// every signal in the trace.
    pub t_second: Time,
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
        out.extend_from_slice(&self.t_second.to_le_bytes());
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
            t_second: i64_at(40),
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
    /// Streams carrying a settled-value summary (§8.4). Zero is legal and means
    /// the answer has to come from Parquet, as it did before format 3.
    pub n_summaries: u32,
    pub blob_len: u32,
}

/// Serialise header + entries + the last-value summary.
///
/// Entries must already be sorted by `(stream_id, t_first)`; `summaries` by
/// `stream_id`, one row per stream, holding that stream's final encoded value.
///
/// **Why the summary is here.** §8.4 runs over every signal when a session
/// opens and asks each one what it is stuck at — always at the end of the
/// trace. Answering from Parquet costs one row-group decode per frozen signal,
/// which at tier B is thirty thousand of them and four times §4.2's budget. The
/// value is a few bytes the writer already holds, so keeping it beside the
/// chunk table turns that whole scan into a binary search per signal. This is
/// the same trade `t_second` made for `is_constant`.
pub fn write(
    path: impl AsRef<Path>,
    h: &Header,
    entries: &[ChunkEntry],
    summaries: &[(u32, Vec<u8>)],
) -> Result<()> {
    let blob_len: usize = summaries.iter().map(|(_, v)| v.len()).sum();
    let mut buf = Vec::with_capacity(
        HEADER_LEN + entries.len() * ENTRY_LEN + summaries.len() * SUMMARY_LEN + blob_len,
    );
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
    buf.extend_from_slice(&(summaries.len() as u32).to_le_bytes());
    buf.extend_from_slice(&(blob_len as u32).to_le_bytes());
    buf.resize(HEADER_LEN, 0);
    for e in entries {
        e.write_to(&mut buf);
    }
    let mut off = 0u32;
    for (stream, v) in summaries {
        buf.extend_from_slice(&stream.to_le_bytes());
        buf.extend_from_slice(&off.to_le_bytes());
        buf.extend_from_slice(&(v.len() as u32).to_le_bytes());
        buf.extend_from_slice(&0u32.to_le_bytes()); // pad
        off += v.len() as u32;
    }
    for (_, v) in summaries {
        buf.extend_from_slice(v);
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
            return Err(Error::Store(format!(
                "index.bin: version {version}, expected {VERSION}"
            )));
        }
        let header = Header {
            timescale: Timescale {
                num: u32_at(8),
                unit_exp: i32_at(12),
            },
            n_signals: u32_at(16),
            n_streams: u32_at(20),
            t_min: i64_at(24),
            t_max: i64_at(32),
            n_chunks: u32_at(40),
            n_summaries: u32_at(48),
            blob_len: u32_at(52),
        };
        let need = HEADER_LEN
            + header.n_chunks as usize * ENTRY_LEN
            + header.n_summaries as usize * SUMMARY_LEN
            + header.blob_len as usize;
        if map.len() < need {
            return Err(Error::Store(format!(
                "index.bin: truncated ({} < {need})",
                map.len()
            )));
        }
        let summary_base = HEADER_LEN + header.n_chunks as usize * ENTRY_LEN;
        let mut previous: Option<u32> = None;
        for i in 0..header.n_summaries as usize {
            let o = summary_base + i * SUMMARY_LEN;
            let stream = u32::from_le_bytes(map[o..o + 4].try_into().unwrap());
            let off = u32::from_le_bytes(map[o + 4..o + 8].try_into().unwrap());
            let len = u32::from_le_bytes(map[o + 8..o + 12].try_into().unwrap());
            if stream >= header.n_streams {
                return Err(Error::Store(format!(
                    "index.bin: summary {i} references stream {stream} of {}",
                    header.n_streams
                )));
            }
            if previous.is_some_and(|p| p >= stream) {
                return Err(Error::Store(format!(
                    "index.bin: summaries are not strictly sorted at stream {stream}"
                )));
            }
            let end = off as u64 + len as u64;
            if end > header.blob_len as u64 {
                return Err(Error::Store(format!(
                    "index.bin: summary {i} points outside its value blob"
                )));
            }
            previous = Some(stream);
        }
        Ok(Index { map, header })
    }

    /// Byte offset of the summary table, i.e. just past the chunk entries.
    fn summary_base(&self) -> usize {
        HEADER_LEN + self.header.n_chunks as usize * ENTRY_LEN
    }

    /// The settled value of `stream` at the end of the trace, still encoded.
    ///
    /// `None` when this store carries no summary for it, which is what a caller
    /// falls back to Parquet for.
    pub fn last_value(&self, stream: u32) -> Option<&[u8]> {
        let n = self.header.n_summaries as usize;
        if n == 0 {
            return None;
        }
        let base = self.summary_base();
        let id_at = |i: usize| {
            let o = base + i * SUMMARY_LEN;
            u32::from_le_bytes(self.map[o..o + 4].try_into().unwrap())
        };
        // Written one row per stream in ascending order, so this is a binary
        // search over a borrowed slice with nothing decoded up front — the same
        // property the chunk table is built for.
        let (mut lo, mut hi) = (0usize, n);
        while lo < hi {
            let mid = (lo + hi) / 2;
            match id_at(mid).cmp(&stream) {
                std::cmp::Ordering::Less => lo = mid + 1,
                std::cmp::Ordering::Greater => hi = mid,
                std::cmp::Ordering::Equal => {
                    let o = base + mid * SUMMARY_LEN;
                    let off = u32::from_le_bytes(self.map[o + 4..o + 8].try_into().unwrap());
                    let len = u32::from_le_bytes(self.map[o + 8..o + 12].try_into().unwrap());
                    let blob = base + n * SUMMARY_LEN + off as usize;
                    return Some(&self.map[blob..blob + len as usize]);
                }
            }
        }
        None
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
        ChunkEntry {
            stream_id,
            part_id: 0,
            row_group: 0,
            n_rows: 4,
            row_offset,
            t_first,
            t_last,
            t_second: t_first + 1,
        }
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
        // Deliberately ragged: two streams with values of different lengths and
        // one with none, which is what the blob offsets have to survive.
        let summaries = vec![
            (0u32, vec![1u8, 2, 3]),
            (2u32, vec![9u8]),
            (5u32, vec![7u8, 7]),
        ];
        let h = Header {
            timescale: Timescale {
                num: 1,
                unit_exp: -12,
            },
            n_signals: 7,
            n_streams: 6,
            t_min: 0,
            t_max: 90,
            n_chunks: entries.len() as u32,
            n_summaries: summaries.len() as u32,
            blob_len: summaries.iter().map(|(_, v)| v.len() as u32).sum(),
        };
        write(&path, &h, &entries, &summaries).unwrap();
        let idx = Index::open(&path).unwrap();
        (dir, idx)
    }

    #[test]
    fn the_summary_gives_each_stream_its_own_last_value() {
        let (_d, idx) = sample();
        assert_eq!(idx.last_value(0), Some(&[1u8, 2, 3][..]));
        assert_eq!(idx.last_value(2), Some(&[9u8][..]));
        assert_eq!(idx.last_value(5), Some(&[7u8, 7][..]));
        // A stream with no summary is a miss, not a neighbour's value.
        assert_eq!(idx.last_value(1), None);
        assert_eq!(idx.last_value(99), None);
    }

    #[test]
    fn an_index_with_no_summary_is_still_readable() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.bin");
        let entries = vec![entry(0, 0, 30, 0)];
        let h = Header {
            timescale: Timescale {
                num: 1,
                unit_exp: -12,
            },
            n_signals: 1,
            n_streams: 1,
            t_min: 0,
            t_max: 30,
            n_chunks: 1,
            n_summaries: 0,
            blob_len: 0,
        };
        write(&path, &h, &entries, &[]).unwrap();
        let idx = Index::open(&path).unwrap();
        assert_eq!(idx.len(), 1);
        assert_eq!(idx.last_value(0), None);
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
