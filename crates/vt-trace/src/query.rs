//! Reading a `.vtx` store: the store API of §6.4 plus the time-model additions
//! §5.5 requires.
//!
//! Opening a store reads only `meta.json`, the two small metadata tables and
//! the mmap'd index — event data is materialised per stream on first touch and
//! then cached, so the common pattern of many queries over a small working set
//! stays an in-RAM binary search.
//!
//! ## The rule that matters
//!
//! Several writes can land on one signal at one timestamp (§5.5, problem 1).
//! `value_at` always returns the **last** of them — the settled value — never an
//! intermediate glitch. `value_at_delta`/`deltas_at` exist to inspect the
//! glitches deliberately, and `value_before` gives the value strictly before a
//! timestamp, which is what NBA semantics need at a clock edge (problem 2).

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use arrow::array::{
    Array, BinaryArray, Int64Array, StringArray, UInt32Array, UInt64Array, UInt8Array,
};
use parquet::arrow::arrow_reader::{
    ArrowReaderMetadata, ArrowReaderOptions, ParquetRecordBatchReaderBuilder,
};
use parquet::arrow::ProjectionMask;
use rayon::prelude::*;

use crate::index::Index;
use crate::model::{Kind, Time, Timescale, ValueColumn};
use crate::store::{Encoding, Meta};
use crate::value::{words_for, Value};
use crate::{Error, Result};

/// Index into [`TraceStore::signals`].
pub type Handle = u32;

/// Upper bound on a client-requested pixel width, so a bad request cannot make
/// the server allocate without limit.
pub const MAX_PX: usize = 8192;

/// Bucket contained at least one X bit.
pub const FLAG_X: u8 = 1;
/// Bucket contained at least one Z bit.
pub const FLAG_Z: u8 = 2;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum WaveMode {
    /// Every transition in the window; the window held no more than `px_width`.
    Exact,
    /// One entry per pixel column that had activity, carrying its value range.
    MinMax,
}

#[derive(Clone, Debug, PartialEq)]
pub struct WaveBucket {
    /// Time of the first transition in this pixel column.
    pub t: Time,
    /// Canonical bits of the numerically smallest value in the column.
    pub min: String,
    pub max: String,
    /// How many transitions the column collapsed.
    pub n: u32,
    /// [`FLAG_X`] / [`FLAG_Z`].
    pub flags: u8,
}

/// A rendering window. Never more than `px_width` entries in total.
#[derive(Clone, Debug)]
pub struct Wave {
    /// Settled value at `t0`, so the client can draw the leading edge.
    pub initial: Option<String>,
    pub mode: WaveMode,
    /// Populated when `mode` is [`WaveMode::Exact`].
    pub points: Vec<(Time, String)>,
    /// Populated when `mode` is [`WaveMode::MinMax`].
    pub buckets: Vec<WaveBucket>,
}

impl Wave {
    /// Total entries sent for this signal — the quantity §10.2 caps.
    pub fn len(&self) -> usize {
        self.points.len() + self.buckets.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[derive(Clone)]
struct Acc {
    t: Time,
    min_key: Option<u64>,
    min_bits: String,
    max_key: Option<u64>,
    max_bits: String,
    first_bits: String,
    n: u32,
    flags: u8,
}

#[derive(Clone, Debug)]
pub struct SignalMeta {
    pub signal_id: u32,
    pub path: String,
    pub name: String,
    pub scope: String,
    pub width: u32,
    pub kind: Kind,
    pub stream_id: u32,
    pub msb: Option<i64>,
    pub lsb: Option<i64>,
    pub array_index: Option<i64>,
    pub code: String,
    pub n_events: u64,
    pub encoding: Encoding,
}

#[derive(Clone, Debug)]
pub struct ScopeMeta {
    pub scope_id: u32,
    pub name: String,
    pub kind: String,
    pub parent: Option<u32>,
    pub path: String,
}

/// Materialised events of one stream.
///
/// `times` is shared with the times-only cache: a scan that only asks *when* a
/// signal changed never pays to decode the values, and if the full stream is
/// loaded later the two views stay one allocation.
struct StreamData {
    times: Arc<Vec<Time>>,
    deltas: Vec<u8>,
    values: ValueColumn,
}

impl StreamData {
    fn len(&self) -> usize {
        self.times.len()
    }

    /// Last row at or before `t`; `None` when `t` precedes the first event.
    fn idx_at(&self, t: Time) -> Option<usize> {
        let k = self.times.partition_point(|&x| x <= t);
        if k == 0 {
            None
        } else {
            Some(k - 1)
        }
    }

    /// Last row strictly before `t`.
    fn idx_before(&self, t: Time) -> Option<usize> {
        let k = self.times.partition_point(|&x| x < t);
        if k == 0 {
            None
        } else {
            Some(k - 1)
        }
    }

    /// Half-open row range of the events at exactly `t`.
    fn rows_at(&self, t: Time) -> (usize, usize) {
        let lo = self.times.partition_point(|&x| x < t);
        let hi = self.times.partition_point(|&x| x <= t);
        (lo, hi)
    }
}

pub struct TraceStore {
    dir: PathBuf,
    pub meta: Meta,
    index: Index,
    pub signals: Vec<SignalMeta>,
    pub scopes: Vec<ScopeMeta>,
    by_path: HashMap<String, Handle>,
    /// One representative handle per event stream. Width and encoding belong to
    /// the stream, so a bulk read that works stream by stream (§7.1 puts many
    /// names on one) needs a way back to them that is not a linear search.
    by_stream: HashMap<u32, Handle>,
    cache: RwLock<HashMap<u32, Arc<StreamData>>>,
    times_cache: RwLock<HashMap<u32, Arc<Vec<Time>>>>,
    /// Parquet footers, keyed by part file. Without this every signal would
    /// re-parse its part's footer, which dominates any scan that touches many
    /// signals once.
    part_meta: RwLock<HashMap<u32, ArrowReaderMetadata>>,
}

impl TraceStore {
    pub fn open(dir: impl AsRef<Path>) -> Result<TraceStore> {
        let dir = dir.as_ref().to_path_buf();
        let meta: Meta = serde_json::from_slice(&std::fs::read(dir.join("meta.json"))?)?;
        let index = Index::open(dir.join("index.bin"))?;
        let signals = read_signals(&dir.join("signals.parquet"))?;
        let scopes = read_scopes(&dir.join("scopes.parquet"))?;
        validate_store(&dir, &meta, &index, &signals, &scopes)?;
        let mut by_path = HashMap::with_capacity(signals.len());
        for s in &signals {
            if by_path.insert(s.path.clone(), s.signal_id).is_some() {
                return Err(Error::Store(format!("duplicate signal path `{}`", s.path)));
            }
        }
        let by_stream = signals
            .iter()
            .map(|s| (s.stream_id, s.signal_id))
            .collect::<HashMap<_, _>>();
        Ok(TraceStore {
            dir,
            meta,
            index,
            signals,
            scopes,
            by_path,
            by_stream,
            cache: RwLock::new(HashMap::new()),
            times_cache: RwLock::new(HashMap::new()),
            part_meta: RwLock::new(HashMap::new()),
        })
    }

    /// Open a part file with its footer taken from cache.
    fn part_reader(&self, part_id: u32) -> Result<ParquetRecordBatchReaderBuilder<File>> {
        let path = self
            .dir
            .join("events")
            .join(format!("part-{part_id:03}.parquet"));
        let file = File::open(&path)?;
        if let Some(m) = self.part_meta.read().unwrap().get(&part_id) {
            return Ok(ParquetRecordBatchReaderBuilder::new_with_metadata(
                file,
                m.clone(),
            ));
        }
        let m = ArrowReaderMetadata::load(&file, ArrowReaderOptions::default())?;
        self.part_meta.write().unwrap().insert(part_id, m.clone());
        Ok(ParquetRecordBatchReaderBuilder::new_with_metadata(file, m))
    }

    pub fn timescale(&self) -> Timescale {
        Timescale {
            num: self.meta.timescale_num,
            unit_exp: self.meta.timescale_unit_exp,
        }
    }

    pub fn time_range(&self) -> (Time, Time) {
        (self.meta.t_min, self.meta.t_max)
    }

    /// Compare the source dump's current O(1) identity with the one recorded
    /// at conversion. `None` means this is a legacy store with no identity and
    /// must be rebuilt (or verified by hashing) before it can be trusted.
    pub fn source_identity_matches(&self, path: impl AsRef<Path>) -> Result<Option<bool>> {
        let Some(recorded) = &self.meta.source_identity else {
            return Ok(None);
        };
        Ok(Some(
            crate::store::source_identity(path.as_ref())? == *recorded,
        ))
    }

    /// Prove that `path` is still this store's source.
    ///
    /// The normal path is metadata-only. If identity changed (including a
    /// same-size rewrite with restored mtime), hash once. Equal bytes refresh
    /// the recorded identity so subsequent opens return to O(1); unequal bytes
    /// return false and the caller reconverts.
    pub fn verify_source(&mut self, path: impl AsRef<Path>) -> Result<bool> {
        let path = path.as_ref();
        let before = crate::store::source_identity(path)?;
        if self.meta.source_identity.as_ref() == Some(&before) {
            return Ok(true);
        }
        if self.meta.source_bytes.is_some_and(|n| n != before.bytes) {
            return Ok(false);
        }
        let Some(recorded_hash) = &self.meta.source_sha256 else {
            return Ok(false);
        };
        let current_hash = crate::store::sha256_file(path)?;
        let after = crate::store::source_identity(path)?;
        if before != after || &current_hash != recorded_hash {
            return Ok(false);
        }
        self.meta.source_bytes = Some(after.bytes);
        self.meta.source_identity = Some(after);
        crate::store::write_meta(&self.dir, &self.meta)?;
        Ok(true)
    }

    pub fn n_signals(&self) -> usize {
        self.signals.len()
    }

    pub fn find(&self, path: &str) -> Option<Handle> {
        self.by_path.get(path).copied()
    }

    pub fn handle(&self, path: &str) -> Result<Handle> {
        self.find(path)
            .ok_or_else(|| Error::UnknownSignal(path.to_string()))
    }

    pub fn signal(&self, h: Handle) -> Result<&SignalMeta> {
        self.signals
            .get(h as usize)
            .ok_or_else(|| Error::UnknownSignal(format!("handle {h}")))
    }

    /// All paths that share a signal's event stream (§7 alias resolution).
    pub fn aliases(&self, h: Handle) -> Result<Vec<&str>> {
        let stream = self.signal(h)?.stream_id;
        Ok(self
            .signals
            .iter()
            .filter(|s| s.stream_id == stream)
            .map(|s| s.path.as_str())
            .collect())
    }

    fn data(&self, h: Handle) -> Result<Arc<StreamData>> {
        let sig = self.signal(h)?;
        let stream = sig.stream_id;
        if let Some(d) = self.cache.read().unwrap().get(&stream) {
            return Ok(d.clone());
        }
        let d = Arc::new(self.load_stream(stream, sig.width, sig.encoding)?);
        self.cache.write().unwrap().insert(stream, d.clone());
        self.times_cache
            .write()
            .unwrap()
            .insert(stream, d.times.clone());
        Ok(d)
    }

    /// Timestamps only, reading just the `time` column.
    ///
    /// Whole-trace scans that ask *when* a signal changed — stuck detection,
    /// activity counts — go through here, so they never decode values. This is
    /// the column pruning §6.3 counts as a reason to store events in Parquet.
    fn times(&self, h: Handle) -> Result<Arc<Vec<Time>>> {
        let stream = self.signal(h)?.stream_id;
        if let Some(t) = self.times_cache.read().unwrap().get(&stream) {
            return Ok(t.clone());
        }
        let t = Arc::new(self.load_times(stream)?);
        self.times_cache.write().unwrap().insert(stream, t.clone());
        Ok(t)
    }

    fn load_times(&self, stream: u32) -> Result<Vec<Time>> {
        let mut times = Vec::new();
        for (part_id, groups) in self.chunk_groups(stream) {
            let builder = self.part_reader(part_id)?;
            // Column 1 is `time`; the value payload is never touched.
            let mask = ProjectionMask::roots(builder.parquet_schema(), [1]);
            let reader = builder
                .with_row_groups(groups)
                .with_projection(mask)
                .build()?;
            for batch in reader {
                let batch = batch?;
                let ts = col::<Int64Array>(&batch, 0, "time")?;
                times.extend((0..batch.num_rows()).map(|r| ts.value(r)));
            }
        }
        Ok(times)
    }

    /// This stream's Parquet row groups, grouped by part file and in row order.
    fn chunk_groups(&self, stream: u32) -> Vec<(u32, Vec<usize>)> {
        let (lo, hi) = self.index.chunks_of(stream);
        let mut by_part: HashMap<u32, Vec<usize>> = HashMap::new();
        for i in lo..hi {
            by_part
                .entry(self.index.entry(i).part_id)
                .or_default()
                .push(i);
        }
        let mut parts: Vec<_> = by_part
            .into_iter()
            .map(|(p, mut idxs)| {
                idxs.sort_by_key(|i| self.index.entry(*i).row_offset);
                (
                    p,
                    idxs.iter()
                        .map(|i| self.index.entry(*i).row_group as usize)
                        .collect(),
                )
            })
            .collect();
        parts.sort_by_key(|(p, _)| *p);
        parts
    }

    fn load_stream(&self, stream: u32, width: u32, enc: Encoding) -> Result<StreamData> {
        let words = words_for(width).max(1);
        let mut times = Vec::new();
        let mut deltas = Vec::new();
        let (mut a, mut b) = (Vec::new(), Vec::new());
        let mut reals: Vec<f64> = Vec::new();
        let mut strs: Vec<String> = Vec::new();

        for (part_id, groups) in self.chunk_groups(stream) {
            let builder = self.part_reader(part_id)?;
            let reader = builder.with_row_groups(groups).build()?;
            for batch in reader {
                let batch = batch?;
                let ids = col::<UInt32Array>(&batch, 0, "signal_id")?;
                let ts = col::<Int64Array>(&batch, 1, "time")?;
                let ds = col::<UInt8Array>(&batch, 2, "delta")?;
                let vs = col::<BinaryArray>(&batch, 3, "value")?;
                for r in 0..batch.num_rows() {
                    // Row groups hold a single stream, but stay defensive so a
                    // hand-edited store cannot silently corrupt a query.
                    if ids.value(r) != stream {
                        continue;
                    }
                    times.push(ts.value(r));
                    deltas.push(ds.value(r));
                    let bytes = vs.value(r);
                    match enc {
                        Encoding::Real => {
                            let mut buf = [0u8; 8];
                            buf.copy_from_slice(&bytes[..8.min(bytes.len())]);
                            reals.push(f64::from_le_bytes(buf));
                        }
                        Encoding::Str => strs.push(String::from_utf8_lossy(bytes).to_string()),
                        _ => append_planes(bytes, width, enc, words, &mut a, &mut b),
                    }
                }
            }
        }

        let values = match enc {
            Encoding::Real => ValueColumn::Real(reals),
            Encoding::Str => ValueColumn::Str(strs),
            _ => ValueColumn::Bits { width, words, a, b },
        };
        Ok(StreamData {
            times: Arc::new(times),
            deltas,
            values,
        })
    }

    // ---- §6.4 API -------------------------------------------------------

    /// Settled value at `t`: the **last** write at that timestamp (§5.5).
    pub fn value_at(&self, h: Handle, t: Time) -> Result<Option<Value>> {
        let d = self.data(h)?;
        Ok(d.idx_at(t).map(|i| d.values.get(i)))
    }

    /// Every transition in `[start, end)`, glitches included.
    ///
    /// This is the raw event list, so a timestamp with delta cycles yields more
    /// than one entry. Callers that want one value per timestamp should take
    /// the last entry of each timestamp, which is what `value_at` returns.
    pub fn transitions(&self, h: Handle, start: Time, end: Time) -> Result<Vec<(Time, Value)>> {
        let d = self.data(h)?;
        let lo = d.times.partition_point(|&x| x < start);
        let hi = d.times.partition_point(|&x| x < end);
        Ok((lo..hi).map(|i| (d.times[i], d.values.get(i))).collect())
    }

    pub fn last_change_before(&self, h: Handle, t: Time) -> Result<Option<Time>> {
        let times = self.times(h)?;
        let k = times.partition_point(|&x| x < t);
        Ok(if k == 0 { None } else { Some(times[k - 1]) })
    }

    pub fn next_change_after(&self, h: Handle, t: Time) -> Result<Option<Time>> {
        let times = self.times(h)?;
        let k = times.partition_point(|&x| x <= t);
        Ok(times.get(k).copied())
    }

    /// True when the value never changes across `[start, end)`.
    ///
    /// A write landing exactly on `start` is what establishes the value for the
    /// range, so it does not make the range non-constant.
    pub fn is_constant(&self, h: Handle, start: Time, end: Time) -> Result<bool> {
        // Answered from the index whenever the window opens at or before the
        // stream's first event, which is what the whole-trace scan of §8.4
        // always asks. That is the difference between reading the index and
        // decoding the time column of every signal in the trace: 7.6 s of the
        // tier-B budget went into the latter.
        if let Some((first, second)) = self.bounds(h) {
            if start < first {
                // The window opens before the stream does, so the first event
                // is itself a transition inside it.
                return Ok(first >= end);
            }
            if start < second {
                // Every event at `first` is behind the window; the next one to
                // fall inside it is the second distinct timestamp.
                return Ok(second >= end);
            }
        }
        let times = self.times(h)?;
        let k = times.partition_point(|&x| x <= start);
        Ok(k >= times.len() || times[k] >= end)
    }

    /// `(first event, second distinct timestamp)` for a signal's stream, from
    /// the index alone. `None` when the stream has no events at all.
    fn bounds(&self, h: Handle) -> Option<(Time, Time)> {
        let stream = self.signals.get(h as usize)?.stream_id;
        let (lo, hi) = self.index.chunks_of(stream);
        if lo >= hi {
            return None;
        }
        let mut first = Time::MAX;
        let mut second = Time::MAX;
        for i in lo..hi {
            let e = self.index.entry(i);
            if e.n_rows == 0 {
                continue;
            }
            // Chunks of one stream are stored in time order, but a fold that
            // does not assume it costs nothing and cannot be wrong.
            if e.t_first < first {
                second = second.min(first);
                first = e.t_first;
            } else if e.t_first < second {
                second = e.t_first;
            }
            second = second.min(e.t_second);
        }
        if first == Time::MAX {
            None
        } else {
            Some((first, second))
        }
    }

    /// Settled transitions in `[start, end)`.
    ///
    /// Counted on settled values, so a combinational 0→1→0 inside one timestamp
    /// contributes no edges. Use `deltas_at` to find those glitches.
    pub fn edge_count(&self, h: Handle, start: Time, end: Time) -> Result<usize> {
        let d = self.data(h)?;
        if d.len() == 0 {
            return Ok(0);
        }
        let lo = d.times.partition_point(|&x| x < start);
        let hi = d.times.partition_point(|&x| x < end);
        let mut count = 0usize;
        let mut i = lo;
        while i < hi {
            // Advance to the settled row of this timestamp.
            let t = d.times[i];
            let mut last = i;
            while last + 1 < d.len() && d.times[last + 1] == t {
                last += 1;
            }
            // Compare against the settled value in force just before it.
            if let Some(prev) = d.idx_before(t) {
                if !d.values.rows_equal(prev, last) {
                    count += 1;
                }
            } else {
                count += 1;
            }
            i = last + 1;
        }
        Ok(count)
    }

    /// First time the signal carries an X bit, if ever.
    pub fn first_x(&self, h: Handle) -> Result<Option<Time>> {
        self.first_x_from(h, Time::MIN)
    }

    /// First time at or after `t` that the signal carries an X bit.
    ///
    /// §8.5 needs this rather than `first_x`: every register is X before its
    /// first write, so a scan anchored at zero reports the reset window and
    /// nothing else. Anchoring it after reset separates "the design was
    /// initialising" from "this is still broken" — and an X that clears and
    /// comes back (a driver conflict, a bad index) is only visible this way.
    pub fn first_x_from(&self, h: Handle, t: Time) -> Result<Option<Time>> {
        let d = self.data(h)?;
        if d.values.is_two_state() {
            return Ok(None);
        }
        // The value settled *at* `t` counts even when it was written earlier.
        let start = d.idx_at(t).unwrap_or(0);
        for i in start..d.len() {
            if d.values.row_has_x(i) {
                return Ok(Some(d.times[i].max(t)));
            }
        }
        Ok(None)
    }

    // ---- §5.5 time-model additions --------------------------------------

    /// Settled value strictly before `t`.
    ///
    /// This is the `EPSILON` of §5.5: for `always_ff @(posedge clk) q <= d`, the
    /// value of `d` that determined `q` at an edge is `value_before(d, t_edge)`,
    /// not `value_at`, because `d` may itself change at that same timestamp.
    pub fn value_before(&self, h: Handle, t: Time) -> Result<Option<Value>> {
        let d = self.data(h)?;
        Ok(d.idx_before(t).map(|i| d.values.get(i)))
    }

    /// Number of writes at exactly `t`. Greater than one means a combinational
    /// glitch settled through delta cycles.
    pub fn deltas_at(&self, h: Handle, t: Time) -> Result<usize> {
        let times = self.times(h)?;
        let lo = times.partition_point(|&x| x < t);
        let hi = times.partition_point(|&x| x <= t);
        Ok(hi - lo)
    }

    /// Value at delta `delta` of timestamp `t`, for inspecting a glitch.
    ///
    /// Resolves to the last write whose delta is `<= delta`, so asking beyond
    /// the final delta yields the settled value. Returns `None` when nothing was
    /// written at `t` at all.
    pub fn value_at_delta(&self, h: Handle, t: Time, delta: usize) -> Result<Option<Value>> {
        let d = self.data(h)?;
        let (lo, hi) = d.rows_at(t);
        if lo == hi {
            return Ok(None);
        }
        let k = d.deltas[lo..hi].partition_point(|&x| (x as usize) <= delta);
        Ok(Some(d.values.get(lo + k.saturating_sub(1))))
    }

    /// Delta index of every write at `t`, in arrival order.
    pub fn delta_indices_at(&self, h: Handle, t: Time) -> Result<Vec<u8>> {
        let d = self.data(h)?;
        let (lo, hi) = d.rows_at(t);
        Ok(d.deltas[lo..hi].to_vec())
    }

    // ---- rendering windows (§10.2) --------------------------------------

    /// A window of this signal, reduced to at most `px_width` entries.
    ///
    /// §10.2 makes this mandatory: the server must never hand a client more
    /// points than it has pixels, or the UI dies on the first serious dump. The
    /// reduction is min/max per pixel column, so a bucket still reports the full
    /// range of values it covers rather than an arbitrary sample.
    ///
    /// Runs here rather than in the API layer because the scan is over every
    /// transition in the window — up to millions — while the result is bounded
    /// by `px_width`. Crossing the language boundary before reducing would cost
    /// far more than the reduction itself.
    pub fn wave(&self, h: Handle, t0: Time, t1: Time, px_width: usize) -> Result<Wave> {
        let px = px_width.clamp(1, MAX_PX);
        let d = self.data(h)?;
        let initial = d.idx_at(t0).map(|i| d.values.get(i).to_vcd_bits());

        if t1 <= t0 {
            return Ok(Wave {
                initial,
                mode: WaveMode::Exact,
                points: Vec::new(),
                buckets: Vec::new(),
            });
        }
        let lo = d.times.partition_point(|&x| x < t0);
        let hi = d.times.partition_point(|&x| x < t1);

        // Fewer transitions than pixels: nothing to gain from bucketing, and the
        // client gets an exact picture.
        if hi - lo <= px {
            let points = (lo..hi)
                .map(|i| (d.times[i], d.values.get(i).to_vcd_bits()))
                .collect::<Vec<_>>();
            return Ok(Wave {
                initial,
                mode: WaveMode::Exact,
                points,
                buckets: Vec::new(),
            });
        }

        let span = (t1 - t0) as i128;
        let mut acc: Vec<Option<Acc>> = vec![None; px];
        for i in lo..hi {
            let b = ((((d.times[i] - t0) as i128) * px as i128) / span) as usize;
            let b = b.min(px - 1);
            let v = d.values.get(i);
            let slot = &mut acc[b];
            let entry = slot.get_or_insert_with(|| Acc {
                t: d.times[i],
                min_key: None,
                min_bits: String::new(),
                max_key: None,
                max_bits: String::new(),
                first_bits: String::new(),
                n: 0,
                flags: 0,
            });
            entry.n += 1;
            if v.has_x() {
                entry.flags |= FLAG_X;
            }
            if v.has_z() {
                entry.flags |= FLAG_Z;
            }
            let bits = v.to_vcd_bits();
            if entry.first_bits.is_empty() {
                entry.first_bits = bits.clone();
            }
            // Numeric min/max only means something for known values that fit in
            // 64 bits; anything else is represented by the flags plus a
            // first-seen fallback.
            if let Some(k) = v.as_u64() {
                if entry.min_key.is_none_or(|m| k < m) {
                    entry.min_key = Some(k);
                    entry.min_bits = bits.clone();
                }
                if entry.max_key.is_none_or(|m| k > m) {
                    entry.max_key = Some(k);
                    entry.max_bits = bits;
                }
            }
        }

        let buckets = acc
            .into_iter()
            .flatten()
            .map(|mut a| {
                if a.min_key.is_none() {
                    a.min_bits = a.first_bits.clone();
                    a.max_bits = a.first_bits.clone();
                }
                WaveBucket {
                    t: a.t,
                    min: a.min_bits,
                    max: a.max_bits,
                    n: a.n,
                    flags: a.flags,
                }
            })
            .collect::<Vec<_>>();

        debug_assert!(buckets.len() <= px);
        Ok(Wave {
            initial,
            mode: WaveMode::MinMax,
            points: Vec::new(),
            buckets,
        })
    }

    // ---- whole-trace scans (rayon, per §4.1) -----------------------------

    /// `first_x` for every signal, in parallel.
    pub fn first_x_all(&self) -> Result<Vec<(Handle, Time)>> {
        let rows: Result<Vec<Option<(Handle, Time)>>> = (0..self.signals.len() as Handle)
            .into_par_iter()
            .map(|h| self.first_x(h).map(|t| t.map(|t| (h, t))))
            .collect();
        Ok(rows?.into_iter().flatten().collect())
    }

    /// Signals that never change over `[start, end)` — the basis of the stuck
    /// detector in §8.
    pub fn constant_signals(&self, start: Time, end: Time) -> Result<Vec<Handle>> {
        let rows: Result<Vec<Option<Handle>>> = (0..self.signals.len() as Handle)
            .into_par_iter()
            .map(|h| self.is_constant(h, start, end).map(|yes| yes.then_some(h)))
            .collect();
        Ok(rows?.into_iter().flatten().collect())
    }

    /// `last_change_before(t)` for every signal at once — the scan §8.4 runs on
    /// every session open.
    ///
    /// One entry per handle, in handle order, so the caller indexes rather than
    /// searches. `None` means the signal never transitions before `t` at all,
    /// which is a different finding from "froze late" and must stay
    /// distinguishable.
    ///
    /// Parallel because it touches every signal's time column: the work is one
    /// Parquet decode per signal and embarrassingly parallel, and it warms the
    /// time cache that the rest of the session then reads for free.
    pub fn last_change_all(&self, t: Time) -> Result<Vec<Option<Time>>> {
        (0..self.signals.len() as Handle)
            .into_par_iter()
            .map(|h| {
                // §8.4 asks this about the end of the trace, and there the
                // answer is already in the index: the last chunk's `t_last`.
                // `last_change_before` would decode the whole time column of
                // every signal in the design to rediscover it, which was most
                // of what put the stuck detector over §4.2's budget.
                let sig = self.signal(h)?;
                let (lo, hi) = self.index.chunks_of(sig.stream_id);
                if hi > lo {
                    let last = self.index.entry(hi - 1);
                    if t > last.t_last {
                        return Ok(Some(last.t_last));
                    }
                }
                self.last_change_before(h, t)
            })
            .collect()
    }

    /// `value_at(t)` for many signals, in one parallel pass.
    ///
    /// The companion to `last_change_all`, and it exists for the same caller:
    /// §8.4's stuck detector asks *when* every signal last moved and then *what*
    /// each frozen one is stuck at. The first half was already one rayon pass;
    /// the second was a Python loop paying a cold Parquet decode per signal,
    /// which measured at 74% of the whole scan and put it three times over
    /// §4.2's 400 ms budget.
    ///
    /// Neither `value_at` in a loop nor `sample_before`, and for the same
    /// reason: both decode a signal's *whole* event stream to answer about one
    /// timestamp. That is the right trade when a session then asks a hundred
    /// more questions of the same signal — it is what the stream cache is for —
    /// and the wrong one here, where each signal is asked exactly once and
    /// 2969 full decodes is the entire cost of the scan.
    ///
    /// So this goes through the index instead: `chunk_for_time` gives the one
    /// Parquet row group that can contain `t`, and only that row group is read.
    /// The stream cache is deliberately left untouched — a whole-trace scan
    /// that populated it would leave every signal in the design resident for a
    /// pass that never looks at them again (§4.2's tier-B RAM budget).
    /// Grouped by Parquet part, not by signal, and that is the whole point.
    /// One reader per signal meant one `File::open` per signal — 283 µs of
    /// fixed cost each, which at three thousand frozen signals *is* the scan.
    /// A store has a handful of parts, so opening each once and reading all the
    /// row groups wanted from it turns three thousand opens into sixteen.
    pub fn value_at_all(&self, handles: &[Handle], t: Time) -> Result<Vec<Option<Value>>> {
        // Stream → the one chunk that can contain `t`, from the index. Several
        // handles can share a stream (§7.1 aliases), so the work is per stream
        // and the answer is fanned back out per handle at the end.
        let mut by_part: HashMap<u32, Vec<(u32, usize, bool)>> = HashMap::new();
        let mut cached: HashMap<u32, Option<Value>> = HashMap::new();
        let mut seen: HashSet<u32> = HashSet::new();
        for &h in handles {
            let sig = self.signal(h)?;
            let stream = sig.stream_id;
            if !seen.insert(stream) {
                continue;
            }
            if let Some(d) = self.cache.read().unwrap().get(&stream) {
                // Already decoded by something else this session: reading the
                // file again would be slower than the answer already in hand.
                cached.insert(stream, d.idx_at(t).map(|i| d.values.get(i)));
                continue;
            }
            match self.index.chunk_for_time(stream, t) {
                Some(ci) => {
                    let e = self.index.entry(ci);
                    // Past the end of this chunk the answer is simply its last
                    // row — and §8.4, the caller this exists for, always asks
                    // about the end of the trace. The index carries that value
                    // (format 3), so the whole scan answers from a binary
                    // search and touches no Parquet at all. A store written
                    // before format 3 has no summary and falls back below.
                    let past_end = t >= e.t_last;
                    if past_end && ci + 1 == self.index.chunks_of(stream).1 {
                        if let Some(v) = self
                            .index
                            .last_value(stream)
                            .and_then(|b| self.decode_for(stream, b))
                        {
                            cached.insert(stream, Some(v));
                            continue;
                        }
                    }
                    by_part.entry(e.part_id).or_default().push((
                        stream,
                        e.row_group as usize,
                        past_end,
                    ));
                }
                None => {
                    cached.insert(stream, None);
                }
            }
        }

        let parts: Vec<_> = by_part.into_iter().collect();
        let decoded_parts: Vec<Result<HashMap<u32, Option<Value>>>> = parts
            .par_iter()
            .map(|(part, wanted)| self.values_from_part(*part, wanted, t))
            .collect();
        let mut decoded: HashMap<u32, Option<Value>> = HashMap::new();
        for part in decoded_parts {
            decoded.extend(part?);
        }

        handles
            .iter()
            .map(|&h| {
                let stream = self.signal(h)?.stream_id;
                Ok(cached
                    .get(&stream)
                    .or_else(|| decoded.get(&stream))
                    .cloned()
                    .flatten())
            })
            .collect()
    }

    /// The settled value at `t` of every stream named in `wanted`, from one part.
    ///
    /// `wanted` is `(stream, row_group)`; each Parquet row group holds exactly
    /// one stream, so the rows can be routed by their `signal_id` column with no
    /// bookkeeping about which group they came from.
    fn values_from_part(
        &self,
        part: u32,
        wanted: &[(u32, usize, bool)],
        t: Time,
    ) -> Result<HashMap<u32, Option<Value>>> {
        let mut groups: Vec<usize> = wanted.iter().map(|(_, g, _)| *g).collect();
        groups.sort_unstable();
        groups.dedup();
        // When `t` is past the end of every chunk wanted here — which is what
        // §8.4 asks, once per session, about the end of the trace — the answer
        // is each stream's last row and the time column has nothing left to
        // decide. Dropping it is a third of the bytes this read touches.
        let past_end = wanted.iter().all(|(_, _, p)| *p);
        // `delta` is never read either way, and skipping it is the same column
        // pruning §6.3 counts as a reason to be in Parquet at all.
        let builder = self.part_reader(part)?;
        let cols: &[usize] = if past_end { &[0, 3] } else { &[0, 1, 3] };
        let mask = ProjectionMask::roots(builder.parquet_schema(), cols.iter().copied());
        let reader = builder
            .with_row_groups(groups)
            .with_projection(mask)
            .build()?;

        // Last row at or before `t` wins: rows inside a chunk are in time order
        // and a timestamp repeats across delta cycles (§5.5), so the settled
        // value is the last match, not the first.
        let mut best: HashMap<u32, Vec<u8>> = HashMap::new();
        let mut last_row: HashMap<u32, usize> = HashMap::new();
        for batch in reader {
            let batch = batch?;
            // The projection decides the layout: `signal_id` is always first,
            // and `value` is second or third depending on whether `time` came.
            let ids = col::<UInt32Array>(&batch, 0, "signal_id")?;
            let vs = col::<BinaryArray>(&batch, if past_end { 1 } else { 2 }, "value")?;
            // Two passes over the batch, deliberately. Copying the value of
            // every candidate row and overwriting it with the next is one heap
            // allocation per event for an answer that keeps one per stream; the
            // row indices are integers and cost nothing to overwrite.
            last_row.clear();
            if past_end {
                for r in 0..batch.num_rows() {
                    last_row.insert(ids.value(r), r);
                }
            } else {
                let ts = col::<Int64Array>(&batch, 1, "time")?;
                for r in 0..batch.num_rows() {
                    if ts.value(r) <= t {
                        last_row.insert(ids.value(r), r);
                    }
                }
            }
            for (&stream, &r) in &last_row {
                best.insert(stream, vs.value(r).to_vec());
            }
        }

        let mut out = HashMap::with_capacity(wanted.len());
        for &(stream, _, _) in wanted {
            // Width and encoding belong to the stream, so any handle on it will
            // do; `by_stream` is built once at open rather than searched here.
            let value = best
                .get(&stream)
                .and_then(|bytes| self.decode_for(stream, bytes));
            out.insert(stream, value);
        }
        Ok(out)
    }

    /// Decode one encoded row using the width and encoding of `stream`.
    ///
    /// Both of those belong to the stream rather than to a handle, so any
    /// handle on it will do; `by_stream` is built once at open rather than
    /// searched here. Shared by the Parquet path and the index summary so the
    /// two can never disagree about how a value is read back.
    fn decode_for(&self, stream: u32, bytes: &[u8]) -> Option<Value> {
        let &h = self.by_stream.get(&stream)?;
        let s = &self.signals[h as usize];
        Some(decode_one(
            bytes,
            s.width,
            s.encoding,
            words_for(s.width).max(1),
        ))
    }

    // ---- cycle-aligned sampling (§5.5, §8.14) ----------------------------

    /// Timestamps where the settled value of a signal becomes 1.
    ///
    /// Settled rather than raw: a combinational 0→1→0 inside one timestamp is a
    /// delta glitch, not a clock edge, and counting it would shift every cycle
    /// number after it. Built here rather than in Python because the caller only
    /// wants the times, and going through `transitions` would materialise a
    /// value object per event to throw it away.
    ///
    /// An X→1 transition counts: a clock that starts unknown still has a first
    /// real edge, and dropping it would renumber the whole run.
    pub fn rising_edges(&self, h: Handle) -> Result<Vec<Time>> {
        let d = self.data(h)?;
        let mut out = Vec::new();
        let mut prev: Option<u64> = None;
        let mut i = 0usize;
        while i < d.len() {
            let t = d.times[i];
            let mut last = i;
            while last + 1 < d.len() && d.times[last + 1] == t {
                last += 1;
            }
            let v = d.values.get(last).as_u64();
            if v == Some(1) && prev != Some(1) {
                out.push(t);
            }
            prev = v;
            i = last + 1;
        }
        Ok(out)
    }

    /// `value_before` for many signals at many timestamps, in one pass each.
    ///
    /// This is step 2 of the §8.14 extraction algorithm: a channel scan samples
    /// every clock edge, and doing that with one binary search per (signal,
    /// edge) is O(edges · log events) per signal. `times` is required to be
    /// ascending, which turns each row into a linear merge — O(events + edges)
    /// — so the whole scan stays linear in the trace no matter how many cycles
    /// it covers.
    ///
    /// `value_before` and not `value_at`: at a rising edge the trace already
    /// carries the *results* of that edge, and a handshake is decided by what
    /// the flops saw going into it (§5.5, problem 2).
    ///
    /// One row per handle in the order given; `None` where the signal has no
    /// event before that time yet.
    pub fn sample_before(
        &self,
        handles: &[Handle],
        times: &[Time],
    ) -> Result<Vec<Vec<Option<Value>>>> {
        if !times.windows(2).all(|w| w[0] <= w[1]) {
            return Err(Error::Store("sample timestamps must be ascending".into()));
        }
        handles
            .par_iter()
            .map(|&h| {
                let d = self.data(h)?;
                let mut out = Vec::with_capacity(times.len());
                let mut i = 0usize;
                for &t in times {
                    while i < d.len() && d.times[i] < t {
                        i += 1;
                    }
                    out.push(if i == 0 {
                        None
                    } else {
                        Some(d.values.get(i - 1))
                    });
                }
                Ok(out)
            })
            .collect()
    }
}

/// Validate the small, eagerly-read part of a store before it can answer a
/// query. A truncated event part can still fail lazily when touched, but a
/// contradictory index/metadata table must never open successfully and then
/// turn into a plausible empty answer in a whole-trace scan.
fn validate_store(
    dir: &Path,
    meta: &Meta,
    index: &Index,
    signals: &[SignalMeta],
    scopes: &[ScopeMeta],
) -> Result<()> {
    let bad = |msg: String| Error::Store(msg);
    if meta.version != crate::index::VERSION {
        return Err(bad(format!(
            "meta.json: version {}, expected {}",
            meta.version,
            crate::index::VERSION
        )));
    }
    if meta.n_signals as usize != signals.len() || index.header.n_signals != meta.n_signals {
        return Err(bad(format!(
            "signal count mismatch: meta={}, index={}, table={}",
            meta.n_signals,
            index.header.n_signals,
            signals.len()
        )));
    }
    if index.header.n_streams != meta.n_streams {
        return Err(bad(format!(
            "stream count mismatch: meta={}, index={}",
            meta.n_streams, index.header.n_streams
        )));
    }
    if (index.header.t_min, index.header.t_max) != (meta.t_min, meta.t_max) {
        return Err(bad(
            "time range differs between meta.json and index.bin".into()
        ));
    }
    if index.header.timescale
        != (Timescale {
            num: meta.timescale_num,
            unit_exp: meta.timescale_unit_exp,
        })
    {
        return Err(bad(
            "timescale differs between meta.json and index.bin".into()
        ));
    }
    if meta.n_parts == 0 {
        return Err(bad("meta.json declares no event parts".into()));
    }
    for part in 0..meta.n_parts {
        let path = dir.join("events").join(format!("part-{part:03}.parquet"));
        if !path.is_file() {
            return Err(bad(format!("missing event part {}", path.display())));
        }
    }

    for (i, scope) in scopes.iter().enumerate() {
        if scope.scope_id as usize != i {
            return Err(bad(format!(
                "scopes.parquet: row {i} has scope_id {}",
                scope.scope_id
            )));
        }
        if scope.parent.is_some_and(|p| p as usize >= i) {
            return Err(bad(format!(
                "scopes.parquet: scope {i} has invalid parent {:?}",
                scope.parent
            )));
        }
    }

    let mut stream_meta: Vec<Option<(u32, Encoding, u64)>> = vec![None; meta.n_streams as usize];
    let mut paths = HashSet::with_capacity(signals.len());
    for (i, signal) in signals.iter().enumerate() {
        if signal.signal_id as usize != i {
            return Err(bad(format!(
                "signals.parquet: row {i} has signal_id {}",
                signal.signal_id
            )));
        }
        if signal.stream_id >= meta.n_streams {
            return Err(bad(format!(
                "signals.parquet: signal `{}` references stream {} of {}",
                signal.path, signal.stream_id, meta.n_streams
            )));
        }
        if !paths.insert(signal.path.as_str()) {
            return Err(bad(format!("duplicate signal path `{}`", signal.path)));
        }
        let this = (signal.width, signal.encoding, signal.n_events);
        match &mut stream_meta[signal.stream_id as usize] {
            slot @ None => *slot = Some(this),
            Some(previous) if *previous != this => {
                return Err(bad(format!(
                    "aliases of stream {} disagree about width, encoding, or event count",
                    signal.stream_id
                )))
            }
            _ => {}
        }
    }
    if let Some(missing) = stream_meta.iter().position(Option::is_none) {
        return Err(bad(format!("stream {missing} has no signal declaration")));
    }
    let table_events: u64 = stream_meta.into_iter().flatten().map(|x| x.2).sum();
    if table_events != meta.n_events {
        return Err(bad(format!(
            "event count mismatch: meta={}, signals={table_events}",
            meta.n_events
        )));
    }

    let mut next_offset = vec![0u64; meta.n_streams as usize];
    let mut indexed_events = 0u64;
    for i in 0..index.len() {
        let entry = index.entry(i);
        if entry.stream_id >= meta.n_streams {
            return Err(bad(format!(
                "index entry {i} references invalid stream {}",
                entry.stream_id
            )));
        }
        if entry.part_id >= meta.n_parts {
            return Err(bad(format!(
                "index entry {i} references invalid part {}",
                entry.part_id
            )));
        }
        if entry.n_rows == 0 || entry.t_first > entry.t_last {
            return Err(bad(format!(
                "index entry {i} has an invalid row/time range"
            )));
        }
        let expected = &mut next_offset[entry.stream_id as usize];
        if entry.row_offset != *expected {
            return Err(bad(format!(
                "index entry {i} starts at row {}, expected {}",
                entry.row_offset, *expected
            )));
        }
        *expected += entry.n_rows as u64;
        indexed_events += entry.n_rows as u64;
    }
    if indexed_events != meta.n_events {
        return Err(bad(format!(
            "event count mismatch: meta={}, index={indexed_events}",
            meta.n_events
        )));
    }
    for (stream, expected) in next_offset.into_iter().enumerate() {
        let declared = signals
            .iter()
            .find(|s| s.stream_id as usize == stream)
            .map(|s| s.n_events)
            .unwrap_or(0);
        if expected != declared {
            return Err(bad(format!(
                "stream {stream} has {expected} indexed rows, signals table declares {declared}"
            )));
        }
    }
    Ok(())
}

/// One serialised row back into a [`Value`], without building a column for it.
///
/// The single-row twin of what `load_stream` does in bulk, reusing the same
/// `append_planes` so the two cannot decode a row differently — which is the
/// only thing that would make a scoped read disagree with a cached one.
fn decode_one(bytes: &[u8], width: u32, enc: Encoding, words: usize) -> Value {
    match enc {
        Encoding::Real => {
            let mut buf = [0u8; 8];
            buf.copy_from_slice(&bytes[..8.min(bytes.len())]);
            Value::Real(f64::from_le_bytes(buf))
        }
        Encoding::Str => Value::Str(String::from_utf8_lossy(bytes).to_string()),
        _ => {
            let (mut a, mut b) = (Vec::new(), Vec::new());
            append_planes(bytes, width, enc, words, &mut a, &mut b);
            ValueColumn::Bits { width, words, a, b }.get(0)
        }
    }
}

fn append_planes(
    bytes: &[u8],
    width: u32,
    enc: Encoding,
    words: usize,
    a: &mut Vec<u64>,
    b: &mut Vec<u64>,
) {
    let nbytes = (width as usize).div_ceil(8);
    let base = a.len();
    a.resize(base + words, 0);
    for i in 0..nbytes.min(bytes.len()) {
        a[base + i / 8] |= (bytes[i] as u64) << ((i % 8) * 8);
    }
    if enc.is_four_state() {
        b.resize(base + words, 0);
        if bytes.len() >= nbytes * 2 {
            for i in 0..nbytes {
                b[base + i / 8] |= (bytes[nbytes + i] as u64) << ((i % 8) * 8);
            }
        }
    }
}

fn col<'a, T: Array + 'static>(
    batch: &'a arrow::record_batch::RecordBatch,
    i: usize,
    name: &str,
) -> Result<&'a T> {
    batch
        .column(i)
        .as_any()
        .downcast_ref::<T>()
        .ok_or_else(|| Error::Store(format!("column {name} has unexpected type")))
}

fn read_batches(path: &Path) -> Result<Vec<arrow::record_batch::RecordBatch>> {
    let file = File::open(path)?;
    let reader = ParquetRecordBatchReaderBuilder::try_new(file)?.build()?;
    let mut out = Vec::new();
    for b in reader {
        out.push(b?);
    }
    Ok(out)
}

fn read_signals(path: &Path) -> Result<Vec<SignalMeta>> {
    let mut out = Vec::new();
    for batch in read_batches(path)? {
        let id = col::<UInt32Array>(&batch, 0, "signal_id")?;
        let p = col::<StringArray>(&batch, 1, "path")?;
        let name = col::<StringArray>(&batch, 2, "name")?;
        let scope = col::<StringArray>(&batch, 3, "scope")?;
        let width = col::<UInt32Array>(&batch, 4, "width")?;
        let kind = col::<StringArray>(&batch, 5, "kind")?;
        let stream = col::<UInt32Array>(&batch, 6, "stream_id")?;
        let msb = col::<Int64Array>(&batch, 7, "msb")?;
        let lsb = col::<Int64Array>(&batch, 8, "lsb")?;
        let ai = col::<Int64Array>(&batch, 9, "array_index")?;
        let code = col::<StringArray>(&batch, 10, "code")?;
        let nev = col::<UInt64Array>(&batch, 11, "n_events")?;
        let enc = col::<StringArray>(&batch, 12, "encoding")?;
        let opt = |arr: &Int64Array, r: usize| {
            if arr.is_null(r) {
                None
            } else {
                Some(arr.value(r))
            }
        };
        for r in 0..batch.num_rows() {
            out.push(SignalMeta {
                signal_id: id.value(r),
                path: p.value(r).to_string(),
                name: name.value(r).to_string(),
                scope: scope.value(r).to_string(),
                width: width.value(r),
                kind: Kind::from_vcd(kind.value(r)),
                stream_id: stream.value(r),
                msb: opt(msb, r),
                lsb: opt(lsb, r),
                array_index: opt(ai, r),
                code: code.value(r).to_string(),
                n_events: nev.value(r),
                encoding: Encoding::parse(enc.value(r))
                    .ok_or_else(|| Error::Store(format!("unknown encoding {}", enc.value(r))))?,
            });
        }
    }
    Ok(out)
}

/// The Python binding exposes `TraceStore` as a sendable pyclass because ASGI
/// servers query it from threadpool threads. Enforce that here rather than
/// discovering it as a panic at runtime.
const _: () = {
    const fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<TraceStore>();
};

fn read_scopes(path: &Path) -> Result<Vec<ScopeMeta>> {
    let mut out = Vec::new();
    for batch in read_batches(path)? {
        let id = col::<UInt32Array>(&batch, 0, "scope_id")?;
        let name = col::<StringArray>(&batch, 1, "name")?;
        let kind = col::<StringArray>(&batch, 2, "kind")?;
        let parent = col::<UInt32Array>(&batch, 3, "parent")?;
        let path_c = col::<StringArray>(&batch, 4, "path")?;
        for r in 0..batch.num_rows() {
            out.push(ScopeMeta {
                scope_id: id.value(r),
                name: name.value(r).to_string(),
                kind: kind.value(r).to_string(),
                parent: if parent.is_null(r) {
                    None
                } else {
                    Some(parent.value(r))
                },
                path: path_c.value(r).to_string(),
            });
        }
    }
    Ok(out)
}
