//! `.vtx` writer: Parquet event data plus the binary index.
//!
//! Layout follows §6.3 exactly. The choice of Parquet over a private container
//! is deliberate: anyone can open `dump.vtx/events/*.parquet` in polars, pandas
//! or DuckDB without going through this crate at all.
//!
//! Note on the `signal_id` column in `events/`: it identifies the *event
//! stream*. When several hierarchical paths share one VCD identifier code they
//! share a stream, and `signals.parquet` maps each path to its `stream_id`.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use arrow::array::{
    ArrayRef, BinaryBuilder, Int64Builder, RecordBatch, StringBuilder, UInt32Builder,
    UInt64Builder, UInt8Builder,
};
use arrow::datatypes::{DataType, Field, Schema};
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, Encoding as ParquetEncoding, ZstdLevel};
use parquet::file::properties::WriterProperties;
use parquet::schema::types::ColumnPath;
use rayon::prelude::*;
use sha2::{Digest, Sha256};

use crate::index::{self, ChunkEntry};
use crate::model::{Trace, ValueColumn};
use crate::value::words_for;
use crate::Result;

/// Events per Parquet row group, matching the 64k chunking of §6.2.
pub const CHUNK_ROWS: usize = 65_536;

/// Per-stream value encoding, chosen at conversion time from width and whether
/// the stream ever carries X/Z (§6.3).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Encoding {
    /// <=64 bits, 2-state: one little-endian u64.
    U64_2S,
    /// <=64 bits, 4-state: two little-endian u64 (value plane, then X/Z plane).
    U64_4S,
    /// >64 bits, 2-state: ceil(width/8) little-endian bytes.
    Bits2S,
    /// >64 bits, 4-state: value plane then X/Z plane, ceil(width/8) bytes each.
    Bits4S,
    Real,
    Str,
}

impl Encoding {
    pub fn as_str(self) -> &'static str {
        match self {
            Encoding::U64_2S => "u64_2s",
            Encoding::U64_4S => "u64_4s",
            Encoding::Bits2S => "bits_2s",
            Encoding::Bits4S => "bits_4s",
            Encoding::Real => "real",
            Encoding::Str => "string",
        }
    }

    pub fn parse(s: &str) -> Option<Encoding> {
        Some(match s {
            "u64_2s" => Encoding::U64_2S,
            "u64_4s" => Encoding::U64_4S,
            "bits_2s" => Encoding::Bits2S,
            "bits_4s" => Encoding::Bits4S,
            "real" => Encoding::Real,
            "string" => Encoding::Str,
            _ => return None,
        })
    }

    pub fn is_four_state(self) -> bool {
        matches!(self, Encoding::U64_4S | Encoding::Bits4S)
    }

    pub fn for_column(col: &ValueColumn) -> Encoding {
        match col {
            ValueColumn::Real(_) => Encoding::Real,
            ValueColumn::Str(_) => Encoding::Str,
            ValueColumn::Bits { width, .. } => match (*width <= 64, col.is_two_state()) {
                (true, true) => Encoding::U64_2S,
                (true, false) => Encoding::U64_4S,
                (false, true) => Encoding::Bits2S,
                (false, false) => Encoding::Bits4S,
            },
        }
    }
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct Meta {
    pub version: u32,
    pub timescale_num: u32,
    pub timescale_unit_exp: i32,
    pub t_min: i64,
    pub t_max: i64,
    pub n_signals: u32,
    pub n_streams: u32,
    pub n_events: u64,
    pub n_parts: u32,
    #[serde(default)]
    pub date: Option<String>,
    #[serde(default)]
    pub writer: Option<String>,
    #[serde(default)]
    pub source_file: Option<String>,
    /// Hash of the source dump. §5.7 wants provenance so a stale trace can be
    /// detected rather than silently believed.
    #[serde(default)]
    pub source_sha256: Option<String>,
    /// Size of the source dump, in bytes.
    ///
    /// The hash answers §5.7's question but costs a full read, which the < 1 s
    /// reopen budget of §4.2 cannot pay on every open. The size is O(1) from
    /// the directory entry and catches a regenerated dump whose timestamp was
    /// preserved — a `git checkout`, a `cp -p`, an extracted archive — which
    /// an mtime comparison alone reads as fresh.
    #[serde(default)]
    pub source_bytes: Option<u64>,
    /// Cheap, precise source identity recorded with the hash.
    ///
    /// Size + mtime is insufficient when a dump is rewritten and its mtime is
    /// restored. `change_token` includes inode/ctime on Unix and the NTFS file
    /// id/ChangeTime on Windows, so that case is detected without hashing a
    /// gigabyte on every ordinary reopen. A changed token is still followed by
    /// a hash comparison at the Python boundary: metadata-only touches do not
    /// force a needless conversion.
    #[serde(default)]
    pub source_identity: Option<SourceIdentity>,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct SourceIdentity {
    pub bytes: u64,
    /// Opaque decimal nanoseconds since the Unix epoch. A string avoids JSON's
    /// integer portability limit and also represents pre-epoch timestamps.
    pub mtime_ns: String,
    /// Platform-specific stable file id plus change time.
    pub change_token: String,
}

fn system_time_token(t: std::time::SystemTime) -> String {
    match t.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => format!("{}{:09}", d.as_secs(), d.subsec_nanos()),
        Err(e) => {
            let d = e.duration();
            format!("-{}{:09}", d.as_secs(), d.subsec_nanos())
        }
    }
}

/// Identity for cache freshness, read from one open handle to avoid mixing
/// metadata from two generations when a simulator replaces the dump.
pub(crate) fn source_identity(path: &Path) -> Result<SourceIdentity> {
    let file = fs::File::open(path)?;
    let meta = file.metadata()?;
    let mtime_ns = system_time_token(meta.modified()?);
    let change_token = platform_change_token(&file, &meta)?;
    Ok(SourceIdentity {
        bytes: meta.len(),
        mtime_ns,
        change_token,
    })
}

#[cfg(unix)]
fn platform_change_token(_file: &fs::File, meta: &fs::Metadata) -> Result<String> {
    use std::os::unix::fs::MetadataExt;
    Ok(format!(
        "unix:{}:{}:{}:{}",
        meta.dev(),
        meta.ino(),
        meta.ctime(),
        meta.ctime_nsec()
    ))
}

#[cfg(windows)]
fn platform_change_token(file: &fs::File, _meta: &fs::Metadata) -> Result<String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FileBasicInfo, FileIdInfo, GetFileInformationByHandleEx, FILE_BASIC_INFO, FILE_ID_INFO,
    };

    let handle = file.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE;
    let mut basic = FILE_BASIC_INFO::default();
    let ok = unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileBasicInfo,
            (&mut basic as *mut FILE_BASIC_INFO).cast(),
            std::mem::size_of::<FILE_BASIC_INFO>() as u32,
        )
    };
    if ok == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut id = FILE_ID_INFO::default();
    let ok = unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileIdInfo,
            (&mut id as *mut FILE_ID_INFO).cast(),
            std::mem::size_of::<FILE_ID_INFO>() as u32,
        )
    };
    if ok == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let file_id: String = id
        .FileId
        .Identifier
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok(format!(
        "windows:{}:{}:{}",
        id.VolumeSerialNumber, file_id, basic.ChangeTime
    ))
}

#[cfg(not(any(unix, windows)))]
fn platform_change_token(_file: &fs::File, meta: &fs::Metadata) -> Result<String> {
    // No stronger portable change marker exists. Keeping this equal to mtime
    // makes callers treat an uncertain identity as a hash-required case.
    Ok(format!("portable:{}", system_time_token(meta.modified()?)))
}

fn bytes_per_plane(width: u32) -> usize {
    (width as usize).div_ceil(8)
}

/// Serialise one row of a value column per the encoding table in §6.3.
pub fn encode_row(col: &ValueColumn, row: usize, enc: Encoding, out: &mut Vec<u8>) {
    out.clear();
    match col {
        ValueColumn::Real(v) => out.extend_from_slice(&v[row].to_le_bytes()),
        ValueColumn::Str(v) => out.extend_from_slice(v[row].as_bytes()),
        ValueColumn::Bits { width, words, a, b } => {
            let off = row * words;
            let nbytes = bytes_per_plane(*width);
            let plane = |src: &[u64], out: &mut Vec<u8>| {
                for i in 0..nbytes {
                    let w = src.get(off + i / 8).copied().unwrap_or(0);
                    out.push((w >> ((i % 8) * 8)) as u8);
                }
            };
            plane(a, out);
            if enc.is_four_state() {
                if b.is_empty() {
                    out.resize(out.len() + nbytes, 0);
                } else {
                    plane(b, out);
                }
            }
        }
    }
    let _ = words_for(0);
}

/// Decode a row previously written by [`encode_row`].
pub fn decode_row(bytes: &[u8], width: u32, enc: Encoding) -> (Vec<u64>, Vec<u64>) {
    let nbytes = bytes_per_plane(width);
    let nwords = words_for(width).max(1);
    let mut a = vec![0u64; nwords];
    let mut b = Vec::new();
    for i in 0..nbytes.min(bytes.len()) {
        a[i / 8] |= (bytes[i] as u64) << ((i % 8) * 8);
    }
    if enc.is_four_state() && bytes.len() >= nbytes * 2 {
        let mut bb = vec![0u64; nwords];
        for i in 0..nbytes {
            bb[i / 8] |= (bytes[nbytes + i] as u64) << ((i % 8) * 8);
        }
        if bb.iter().any(|w| *w != 0) {
            b = bb;
        }
    }
    (a, b)
}

fn events_schema() -> Arc<Schema> {
    Arc::new(Schema::new(vec![
        Field::new("signal_id", DataType::UInt32, false),
        Field::new("time", DataType::Int64, false),
        Field::new("delta", DataType::UInt8, false),
        Field::new("value", DataType::Binary, false),
    ]))
}

fn writer_props() -> WriterProperties {
    WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::try_new(3).unwrap()))
        // §6.2 requires delta encoding for monotonically increasing times.
        // Keeping the logical Arrow column as Int64 preserves the public
        // Parquet schema while DELTA_BINARY_PACKED supplies the on-disk varint
        // representation the compression section calls for.
        .set_column_dictionary_enabled(ColumnPath::from("time"), false)
        .set_column_encoding(
            ColumnPath::from("time"),
            ParquetEncoding::DELTA_BINARY_PACKED,
        )
        .build()
}

/// Split streams into contiguous groups of roughly equal event count so the
/// parts can be encoded and written in parallel.
fn partition_streams(trace: &Trace, n_parts: usize) -> Vec<Vec<u32>> {
    let total: usize = trace.total_events().max(1);
    let target = total.div_ceil(n_parts.max(1));
    let mut parts: Vec<Vec<u32>> = Vec::new();
    let mut cur: Vec<u32> = Vec::new();
    let mut acc = 0usize;
    for (i, s) in trace.streams.iter().enumerate() {
        cur.push(i as u32);
        acc += s.len();
        if acc >= target && parts.len() + 1 < n_parts {
            parts.push(std::mem::take(&mut cur));
            acc = 0;
        }
    }
    if !cur.is_empty() || parts.is_empty() {
        parts.push(cur);
    }
    parts
}

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

pub(crate) fn temporary_sibling(path: &Path, role: &str) -> Result<PathBuf> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .ok_or_else(|| crate::Error::Store(format!("invalid store path {}", path.display())))?
        .to_string_lossy();
    for _ in 0..100 {
        let n = NEXT_TEMP.fetch_add(1, Ordering::Relaxed);
        let candidate = parent.join(format!(".{name}.{role}-{}-{n}", std::process::id()));
        if !candidate.exists() {
            return Ok(candidate);
        }
    }
    Err(crate::Error::Store(format!(
        "could not allocate a temporary path beside {}",
        path.display()
    )))
}

fn remove_path(path: &Path) -> std::io::Result<()> {
    if path.is_dir() {
        fs::remove_dir_all(path)
    } else {
        fs::remove_file(path)
    }
}

fn is_vtx_store(path: &Path) -> bool {
    if !path.is_dir() || fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        return false;
    }
    let Ok(mut index) = fs::File::open(path.join("index.bin")) else {
        return false;
    };
    let mut magic = [0u8; 4];
    index.read_exact(&mut magic).is_ok()
        && &magic == index::MAGIC
        && path.join("meta.json").is_file()
        && path.join("signals.parquet").is_file()
        && path.join("scopes.parquet").is_file()
        && path.join("events").is_dir()
}

/// Persist refreshed provenance after a changed file identity was proven by
/// hash to contain the same bytes. This avoids re-hashing a large, merely
/// touched dump on every later reopen.
pub(crate) fn write_meta(dir: &Path, meta: &Meta) -> Result<()> {
    let path = dir.join("meta.json");
    let stage = temporary_sibling(&path, "tmp")?;
    let mut f = fs::File::create(&stage)?;
    f.write_all(&serde_json::to_vec_pretty(meta)?)?;
    f.sync_all()?;

    #[cfg(not(windows))]
    {
        if let Err(e) = fs::rename(&stage, &path) {
            let _ = fs::remove_file(&stage);
            return Err(e.into());
        }
    }
    #[cfg(windows)]
    {
        // std::fs::rename does not replace an existing file on Windows.
        // Readers close meta.json immediately after parsing it, so moving the
        // old file aside is safe; a failed install is rolled back.
        let backup = temporary_sibling(&path, "old")?;
        if let Err(e) = fs::rename(&path, &backup) {
            let _ = fs::remove_file(&stage);
            return Err(e.into());
        }
        if let Err(e) = fs::rename(&stage, &path) {
            let _ = fs::rename(&backup, &path);
            let _ = fs::remove_file(&stage);
            return Err(e.into());
        }
        fs::remove_file(backup)?;
    }
    Ok(())
}

/// Write `trace` as a `.vtx` directory at `out_dir`.
///
/// The new store is completed beside the destination and only then installed.
/// A failed Parquet/index/hash write therefore cannot destroy a previously
/// valid cache or leave a half-written directory that looks current.
pub fn write_vtx(trace: &Trace, out_dir: impl AsRef<Path>, source: Option<&Path>) -> Result<()> {
    let out_dir = out_dir.as_ref();
    // Validate the in-memory identity table before creating a staging
    // directory. Parsers normally guarantee this, but other importers and
    // callers can construct `Trace` directly. Publishing a store that only
    // fails when it is reopened turns a failed conversion into a stale-looking
    // success artifact.
    let mut paths = HashSet::with_capacity(trace.signals.len());
    for signal in &trace.signals {
        let path = signal.path();
        if !paths.insert(path.clone()) {
            return Err(crate::Error::Store(format!(
                "refusing to write duplicate signal path `{path}`"
            )));
        }
        if signal.stream as usize >= trace.streams.len() {
            return Err(crate::Error::Store(format!(
                "signal `{path}` refers to missing stream {}",
                signal.stream
            )));
        }
    }
    if out_dir.exists() && !is_vtx_store(out_dir) {
        return Err(crate::Error::Store(format!(
            "refusing to replace `{}` because it is not a VeriTrace store",
            out_dir.display()
        )));
    }
    let stage = temporary_sibling(out_dir, "tmp")?;
    let built = write_vtx_inner(trace, &stage, source);
    if let Err(e) = built {
        if stage.exists() {
            let _ = remove_path(&stage);
        }
        return Err(e);
    }

    let backup = temporary_sibling(out_dir, "old")?;
    let had_old = out_dir.exists();
    if had_old {
        if let Err(e) = fs::rename(out_dir, &backup) {
            let _ = remove_path(&stage);
            return Err(e.into());
        }
    }
    if let Err(e) = fs::rename(&stage, out_dir) {
        if had_old {
            let _ = fs::rename(&backup, out_dir);
        }
        let _ = remove_path(&stage);
        return Err(e.into());
    }
    if had_old {
        remove_path(&backup)?;
    }
    Ok(())
}

fn write_vtx_inner(trace: &Trace, out_dir: &Path, source: Option<&Path>) -> Result<()> {
    fs::create_dir_all(out_dir.join("events"))?;
    fs::create_dir_all(out_dir.join("txn"))?;

    let encodings: Vec<Encoding> = trace
        .streams
        .iter()
        .map(|s| Encoding::for_column(&s.values))
        .collect();

    let n_parts = rayon::current_num_threads()
        .clamp(1, 32)
        .min(trace.streams.len().max(1));
    let parts = partition_streams(trace, n_parts);

    // Each part is an independent file, so encoding and writing run fully in
    // parallel across signal groups.
    let per_part: Vec<Result<PartWrite>> = parts
        .par_iter()
        .enumerate()
        .map(|(part_id, streams)| write_part(trace, &encodings, out_dir, part_id as u32, streams))
        .collect();

    let mut entries: Vec<ChunkEntry> = Vec::new();
    let mut summaries: Vec<(u32, Vec<u8>)> = Vec::new();
    for r in per_part {
        let (e, s) = r?;
        entries.extend(e);
        summaries.extend(s);
    }
    entries.sort_by_key(|e| (e.stream_id, e.t_first));
    // Both tables are binary-searched by stream, so both are written sorted.
    summaries.sort_by_key(|(sid, _)| *sid);

    index::write(
        out_dir.join("index.bin"),
        &index::Header {
            timescale: trace.timescale,
            n_signals: trace.signals.len() as u32,
            n_streams: trace.streams.len() as u32,
            t_min: trace.t_min,
            t_max: trace.t_max,
            n_chunks: entries.len() as u32,
            n_summaries: summaries.len() as u32,
            blob_len: summaries.iter().map(|(_, v)| v.len() as u32).sum(),
        },
        &entries,
        &summaries,
    )?;

    write_signals(trace, &encodings, out_dir)?;
    write_scopes(trace, out_dir)?;

    let (source_sha256, source_bytes, recorded_identity) = match source {
        Some(path) => {
            let before = source_identity(path)?;
            let hash = sha256_file(path)?;
            let after = source_identity(path)?;
            if before != after {
                return Err(crate::Error::Store(format!(
                    "source dump `{}` changed while it was being converted",
                    path.display()
                )));
            }
            (Some(hash), Some(after.bytes), Some(after))
        }
        None => (None, None, None),
    };
    let meta = Meta {
        version: index::VERSION,
        timescale_num: trace.timescale.num,
        timescale_unit_exp: trace.timescale.unit_exp,
        t_min: trace.t_min,
        t_max: trace.t_max,
        n_signals: trace.signals.len() as u32,
        n_streams: trace.streams.len() as u32,
        n_events: trace.total_events() as u64,
        n_parts: parts.len() as u32,
        date: trace.date.clone(),
        writer: trace.version.clone(),
        source_file: source.map(|p| p.display().to_string()),
        source_sha256,
        source_bytes,
        source_identity: recorded_identity,
    };
    fs::write(out_dir.join("meta.json"), serde_json::to_vec_pretty(&meta)?)?;
    Ok(())
}

type PartWrite = (Vec<ChunkEntry>, Vec<(u32, Vec<u8>)>);

fn write_part(
    trace: &Trace,
    encodings: &[Encoding],
    out_dir: &Path,
    part_id: u32,
    streams: &[u32],
) -> Result<PartWrite> {
    let path = out_dir
        .join("events")
        .join(format!("part-{part_id:03}.parquet"));
    let schema = events_schema();
    let file = fs::File::create(&path)?;
    let mut w = ArrowWriter::try_new(file, schema.clone(), Some(writer_props()))?;

    let mut entries = Vec::new();
    // §8.4's settled value per stream, encoded exactly as the Parquet row is.
    // Collected here because the writer already holds the final row; the reader
    // would have to decode a row group per signal to recover it.
    let mut summaries: Vec<(u32, Vec<u8>)> = Vec::new();
    let mut row_group = 0u32;
    let mut scratch = Vec::with_capacity(32);

    for &sid in streams {
        let s = &trace.streams[sid as usize];
        if s.is_empty() {
            continue;
        }
        let enc = encodings[sid as usize];
        let mut start = 0usize;
        while start < s.len() {
            let end = (start + CHUNK_ROWS).min(s.len());
            let mut ids = UInt32Builder::with_capacity(end - start);
            let mut times = Int64Builder::with_capacity(end - start);
            let mut deltas = UInt8Builder::with_capacity(end - start);
            let mut vals = BinaryBuilder::new();
            for r in start..end {
                ids.append_value(sid);
                times.append_value(s.times[r]);
                deltas.append_value(s.deltas[r]);
                encode_row(&s.values, r, enc, &mut scratch);
                vals.append_value(&scratch);
            }
            let batch = RecordBatch::try_new(
                schema.clone(),
                vec![
                    Arc::new(ids.finish()) as ArrayRef,
                    Arc::new(times.finish()) as ArrayRef,
                    Arc::new(deltas.finish()) as ArrayRef,
                    Arc::new(vals.finish()) as ArrayRef,
                ],
            )?;
            w.write(&batch)?;
            // One row group per chunk, so a chunk can be read back on its own.
            w.flush()?;
            entries.push(ChunkEntry {
                stream_id: sid,
                part_id,
                row_group,
                n_rows: (end - start) as u32,
                row_offset: start as u64,
                t_first: s.times[start],
                t_last: s.times[end - 1],
                // The first timestamp that is not the chunk's first. Written
                // here because the writer already has the times in hand; the
                // reader would have to decode the whole column to learn it.
                t_second: s.times[start..end]
                    .iter()
                    .copied()
                    .find(|&t| t != s.times[start])
                    .unwrap_or(crate::model::Time::MAX),
            });
            row_group += 1;
            start = end;
        }
        // The stream's settled value: the last row, encoded the same way.
        encode_row(&s.values, s.len() - 1, enc, &mut scratch);
        summaries.push((sid, scratch.clone()));
    }
    w.close()?;
    Ok((entries, summaries))
}

fn write_signals(trace: &Trace, encodings: &[Encoding], out_dir: &Path) -> Result<()> {
    let schema = Arc::new(Schema::new(vec![
        Field::new("signal_id", DataType::UInt32, false),
        Field::new("path", DataType::Utf8, false),
        Field::new("name", DataType::Utf8, false),
        Field::new("scope", DataType::Utf8, false),
        Field::new("width", DataType::UInt32, false),
        Field::new("kind", DataType::Utf8, false),
        Field::new("stream_id", DataType::UInt32, false),
        Field::new("msb", DataType::Int64, true),
        Field::new("lsb", DataType::Int64, true),
        Field::new("array_index", DataType::Int64, true),
        Field::new("code", DataType::Utf8, false),
        Field::new("n_events", DataType::UInt64, false),
        Field::new("encoding", DataType::Utf8, false),
    ]));

    let n = trace.signals.len();
    let mut ids = UInt32Builder::with_capacity(n);
    let mut paths = StringBuilder::new();
    let mut names = StringBuilder::new();
    let mut scopes = StringBuilder::new();
    let mut widths = UInt32Builder::with_capacity(n);
    let mut kinds = StringBuilder::new();
    let mut streams = UInt32Builder::with_capacity(n);
    let mut msbs = Int64Builder::with_capacity(n);
    let mut lsbs = Int64Builder::with_capacity(n);
    let mut arr = Int64Builder::with_capacity(n);
    let mut codes = StringBuilder::new();
    let mut nev = UInt64Builder::with_capacity(n);
    let mut encs = StringBuilder::new();

    for (i, s) in trace.signals.iter().enumerate() {
        ids.append_value(i as u32);
        paths.append_value(s.path());
        names.append_value(&s.id.name);
        scopes.append_value(s.id.hier.join("."));
        widths.append_value(s.width);
        kinds.append_value(s.kind.as_vcd());
        streams.append_value(s.stream);
        msbs.append_option(s.msb);
        lsbs.append_option(s.lsb);
        arr.append_option(s.array_index);
        codes.append_value(&s.code);
        nev.append_value(trace.streams[s.stream as usize].len() as u64);
        encs.append_value(encodings[s.stream as usize].as_str());
    }

    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![
            Arc::new(ids.finish()) as ArrayRef,
            Arc::new(paths.finish()) as ArrayRef,
            Arc::new(names.finish()) as ArrayRef,
            Arc::new(scopes.finish()) as ArrayRef,
            Arc::new(widths.finish()) as ArrayRef,
            Arc::new(kinds.finish()) as ArrayRef,
            Arc::new(streams.finish()) as ArrayRef,
            Arc::new(msbs.finish()) as ArrayRef,
            Arc::new(lsbs.finish()) as ArrayRef,
            Arc::new(arr.finish()) as ArrayRef,
            Arc::new(codes.finish()) as ArrayRef,
            Arc::new(nev.finish()) as ArrayRef,
            Arc::new(encs.finish()) as ArrayRef,
        ],
    )?;
    write_single(out_dir.join("signals.parquet"), schema, batch)
}

fn write_scopes(trace: &Trace, out_dir: &Path) -> Result<()> {
    let schema = Arc::new(Schema::new(vec![
        Field::new("scope_id", DataType::UInt32, false),
        Field::new("name", DataType::Utf8, false),
        Field::new("kind", DataType::Utf8, false),
        Field::new("parent", DataType::UInt32, true),
        Field::new("path", DataType::Utf8, false),
    ]));

    // Resolve full paths once, parents always precede children.
    let mut full: Vec<String> = Vec::with_capacity(trace.scopes.len());
    for s in &trace.scopes {
        let p = match s.parent {
            Some(pi) => format!("{}.{}", full[pi as usize], s.name),
            None => s.name.clone(),
        };
        full.push(p);
    }

    let mut ids = UInt32Builder::new();
    let mut names = StringBuilder::new();
    let mut kinds = StringBuilder::new();
    let mut parents = UInt32Builder::new();
    let mut paths = StringBuilder::new();
    for (i, s) in trace.scopes.iter().enumerate() {
        ids.append_value(i as u32);
        names.append_value(&s.name);
        kinds.append_value(&s.kind);
        parents.append_option(s.parent);
        paths.append_value(&full[i]);
    }

    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![
            Arc::new(ids.finish()) as ArrayRef,
            Arc::new(names.finish()) as ArrayRef,
            Arc::new(kinds.finish()) as ArrayRef,
            Arc::new(parents.finish()) as ArrayRef,
            Arc::new(paths.finish()) as ArrayRef,
        ],
    )?;
    write_single(out_dir.join("scopes.parquet"), schema, batch)
}

fn write_single(path: PathBuf, schema: Arc<Schema>, batch: RecordBatch) -> Result<()> {
    let file = fs::File::create(path)?;
    let mut w = ArrowWriter::try_new(file, schema, Some(writer_props()))?;
    w.write(&batch)?;
    w.close()?;
    Ok(())
}

pub(crate) fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut f = fs::File::open(path)?;
    let mut h = Sha256::new();
    std::io::copy(&mut f, &mut h)?;
    Ok(format!("{:x}", h.finalize()))
}

/// Convenience: parse a VCD and write it as `.vtx` in one step.
pub fn convert_vcd(src: impl AsRef<Path>, out: impl AsRef<Path>) -> Result<Trace> {
    let src = src.as_ref();
    let trace = crate::vcd::parse_file(src)?;
    write_vtx(&trace, out, Some(src))?;
    Ok(trace)
}

/// Map of stream id -> encoding, as recorded in `signals.parquet`.
pub type EncodingMap = HashMap<u32, Encoding>;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{EventStream, Kind};
    use crate::value::parse_vcd_vector;

    #[test]
    fn encoding_choice_follows_width_and_state() {
        let mut two = EventStream::new(8, Kind::Wire);
        two.push(0, &parse_vcd_vector(b"1", 8).unwrap());
        assert_eq!(Encoding::for_column(&two.values), Encoding::U64_2S);

        let mut four = EventStream::new(8, Kind::Wire);
        four.push(0, &parse_vcd_vector(b"x", 8).unwrap());
        assert_eq!(Encoding::for_column(&four.values), Encoding::U64_4S);

        let mut wide = EventStream::new(100, Kind::Wire);
        wide.push(0, &parse_vcd_vector(b"1", 100).unwrap());
        assert_eq!(Encoding::for_column(&wide.values), Encoding::Bits2S);

        let mut wide4 = EventStream::new(100, Kind::Wire);
        wide4.push(0, &parse_vcd_vector(b"x", 100).unwrap());
        assert_eq!(Encoding::for_column(&wide4.values), Encoding::Bits4S);
    }

    fn round_trip(width: u32, digits: &[u8]) {
        let mut s = EventStream::new(width, Kind::Wire);
        let v = parse_vcd_vector(digits, width).unwrap();
        s.push(0, &v);
        let enc = Encoding::for_column(&s.values);
        let mut buf = Vec::new();
        encode_row(&s.values, 0, enc, &mut buf);
        let (a, b) = decode_row(&buf, width, enc);
        assert_eq!(
            crate::value::Value::Bits { width, a, b },
            v,
            "{:?} @{width}",
            String::from_utf8_lossy(digits)
        );
    }

    #[test]
    fn value_bytes_round_trip_across_encodings() {
        round_trip(1, b"1");
        round_trip(1, b"x");
        round_trip(8, b"10100000");
        round_trip(8, b"x");
        round_trip(8, b"01xz");
        round_trip(64, b"1111000011110000");
        round_trip(100, b"1");
        round_trip(100, b"x");
        round_trip(128, b"1010101010101010101010101010101010");
    }

    #[test]
    fn partitioning_covers_every_stream_once() {
        let mut trace = Trace::default();
        for i in 0..10 {
            let mut s = EventStream::new(8, Kind::Wire);
            for t in 0..(i + 1) {
                s.push(t as i64, &parse_vcd_vector(b"1", 8).unwrap());
            }
            trace.streams.push(s);
        }
        for n in 1..=12 {
            let parts = partition_streams(&trace, n);
            let mut seen: Vec<u32> = parts.iter().flatten().copied().collect();
            seen.sort();
            assert_eq!(seen, (0..10).collect::<Vec<u32>>(), "n_parts={n}");
            assert!(
                parts.len() <= n.max(1),
                "n_parts={n} produced {}",
                parts.len()
            );
        }
    }
}
