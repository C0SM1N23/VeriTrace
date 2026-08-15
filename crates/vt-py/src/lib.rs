//! Python bindings for the VeriTrace trace core.
//!
//! The surface mirrors the Rust store API one-for-one (§6.4 plus the §5.5 time
//! model additions) so behaviour cannot drift between the two languages.

use pyo3::exceptions::{PyIOError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyType};

use vt_trace::query::TraceStore as RsStore;
use vt_trace::txn::Column as RsColumn;
use vt_trace::value::Value as RsValue;

fn map_err(e: vt_trace::Error) -> PyErr {
    match e {
        vt_trace::Error::UnknownSignal(s) => PyKeyError::new_err(s),
        vt_trace::Error::Io(e) => PyIOError::new_err(e.to_string()),
        other => PyValueError::new_err(other.to_string()),
    }
}

/// A four-state value.
///
/// `bits` is the canonical VCD form (MSB first, redundant leading digits
/// dropped), so `x`, `z` and multi-bit vectors all round-trip as text.
#[pyclass(module = "veritrace._native", frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Value {
    inner: RsValue,
}

#[pymethods]
impl Value {
    #[getter]
    fn bits(&self) -> String {
        self.inner.to_vcd_bits()
    }

    #[getter]
    fn width(&self) -> u32 {
        self.inner.width()
    }

    /// Integer value, or `None` when the value is wider than 64 bits or carries
    /// X/Z. Never guesses — an unknown bit has no integer meaning.
    fn to_int(&self) -> Option<u64> {
        self.inner.as_u64()
    }

    /// `True` when no bit is X or Z.
    fn is_two_state(&self) -> bool {
        self.inner.is_two_state()
    }

    /// `True` when any bit is X. Z alone does not count.
    fn has_x(&self) -> bool {
        self.inner.has_x()
    }

    /// Real value, for `real`/`realtime` signals.
    fn to_float(&self) -> Option<f64> {
        match &self.inner {
            RsValue::Real(v) => Some(*v),
            _ => None,
        }
    }

    fn __str__(&self) -> String {
        self.inner.to_vcd_bits()
    }

    fn __repr__(&self) -> String {
        format!("Value('{}', width={})", self.inner.to_vcd_bits(), self.inner.width())
    }

    fn __eq__(&self, other: &Value) -> bool {
        self.inner == other.inner
    }

    fn __hash__(&self) -> u64 {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut h = DefaultHasher::new();
        self.inner.to_vcd_bits().hash(&mut h);
        h.finish()
    }
}

/// Metadata for one declared signal.
#[pyclass(module = "veritrace._native", frozen, get_all, skip_from_py_object)]
#[derive(Clone)]
pub struct Signal {
    pub handle: u32,
    pub path: String,
    pub name: String,
    pub scope: String,
    pub width: u32,
    pub kind: String,
    pub stream_id: u32,
    pub msb: Option<i64>,
    pub lsb: Option<i64>,
    pub array_index: Option<i64>,
    pub n_events: u64,
    pub encoding: String,
}

#[pymethods]
impl Signal {
    fn __repr__(&self) -> String {
        format!("Signal('{}', width={}, kind='{}')", self.path, self.width, self.kind)
    }
}

/// One scope in the design hierarchy.
#[pyclass(module = "veritrace._native", frozen, get_all, skip_from_py_object)]
#[derive(Clone)]
pub struct Scope {
    pub scope_id: u32,
    pub name: String,
    pub kind: String,
    pub parent: Option<u32>,
    pub path: String,
}

#[pymethods]
impl Scope {
    fn __repr__(&self) -> String {
        format!("Scope('{}')", self.path)
    }
}

/// A `.vtx` trace store.
///
/// Opening reads only metadata and the mmap'd index; event data is loaded per
/// signal on first access and cached.
///
/// Deliberately *not* `unsendable`: an ASGI server runs sync handlers on a
/// threadpool, so a store opened on one thread is routinely queried from
/// another. The Rust store guards its caches with `RwLock` and is `Send + Sync`,
/// so this is sound — marking it unsendable would panic under any real server.
#[pyclass(module = "veritrace._native")]
pub struct TraceStore {
    inner: RsStore,
}

#[pymethods]
impl TraceStore {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        Ok(TraceStore { inner: RsStore::open(path).map_err(map_err)? })
    }

    /// Convert a VCD (or FST, when built with the `fst` feature) to `.vtx` and
    /// open it.
    #[classmethod]
    fn convert(_cls: &Bound<'_, PyType>, src: &str, out: &str) -> PyResult<Self> {
        convert(src, out)?;
        Ok(TraceStore { inner: RsStore::open(out).map_err(map_err)? })
    }

    #[getter]
    fn n_signals(&self) -> usize {
        self.inner.n_signals()
    }

    #[getter]
    fn time_range(&self) -> (i64, i64) {
        self.inner.time_range()
    }

    #[getter]
    fn timescale(&self) -> String {
        self.inner.timescale().to_string()
    }

    #[getter]
    fn n_events(&self) -> u64 {
        self.inner.meta.n_events
    }

    /// sha256 of the source dump, for the provenance check of §5.7.
    #[getter]
    fn source_sha256(&self) -> Option<String> {
        self.inner.meta.source_sha256.clone()
    }

    /// Size of the source dump when it was converted, in bytes.
    ///
    /// The O(1) half of the staleness check: `store.ensure` compares it before
    /// reusing a store, so a dump regenerated without a newer timestamp is
    /// reconverted instead of silently believed (§5.7).
    #[getter]
    fn source_bytes(&self) -> Option<u64> {
        self.inner.meta.source_bytes
    }

    fn signals(&self) -> Vec<Signal> {
        self.inner.signals.iter().map(to_py_signal).collect()
    }

    fn scopes(&self) -> Vec<Scope> {
        self.inner
            .scopes
            .iter()
            .map(|s| Scope {
                scope_id: s.scope_id,
                name: s.name.clone(),
                kind: s.kind.clone(),
                parent: s.parent,
                path: s.path.clone(),
            })
            .collect()
    }

    fn signal(&self, handle: u32) -> PyResult<Signal> {
        self.inner.signal(handle).map(to_py_signal).map_err(map_err)
    }

    /// Handle for a full hierarchical path, or `None`.
    fn find(&self, path: &str) -> Option<u32> {
        self.inner.find(path)
    }

    /// Like `find`, but raises `KeyError` when the path is unknown.
    fn handle(&self, path: &str) -> PyResult<u32> {
        self.inner.handle(path).map_err(map_err)
    }

    /// Every path sharing this signal's event stream.
    fn aliases(&self, handle: u32) -> PyResult<Vec<String>> {
        self.inner
            .aliases(handle)
            .map(|v| v.into_iter().map(|s| s.to_string()).collect())
            .map_err(map_err)
    }

    // ---- §6.4 ----------------------------------------------------------

    /// Settled value at `t` — the last write at that timestamp, never a
    /// mid-settle glitch.
    fn value_at(&self, handle: u32, t: i64) -> PyResult<Option<Value>> {
        self.inner.value_at(handle, t).map(wrap).map_err(map_err)
    }

    /// Every transition in `[start, end)`, glitches included.
    fn transitions(&self, handle: u32, start: i64, end: i64) -> PyResult<Vec<(i64, Value)>> {
        self.inner
            .transitions(handle, start, end)
            .map(|v| v.into_iter().map(|(t, val)| (t, Value { inner: val })).collect())
            .map_err(map_err)
    }

    fn last_change_before(&self, handle: u32, t: i64) -> PyResult<Option<i64>> {
        self.inner.last_change_before(handle, t).map_err(map_err)
    }

    fn next_change_after(&self, handle: u32, t: i64) -> PyResult<Option<i64>> {
        self.inner.next_change_after(handle, t).map_err(map_err)
    }

    fn is_constant(&self, handle: u32, start: i64, end: i64) -> PyResult<bool> {
        self.inner.is_constant(handle, start, end).map_err(map_err)
    }

    fn edge_count(&self, handle: u32, start: i64, end: i64) -> PyResult<usize> {
        self.inner.edge_count(handle, start, end).map_err(map_err)
    }

    fn first_x(&self, handle: u32) -> PyResult<Option<i64>> {
        self.inner.first_x(handle).map_err(map_err)
    }

    /// First X at or after `t` — §8.5's post-reset scan.
    fn first_x_from(&self, handle: u32, t: i64) -> PyResult<Option<i64>> {
        self.inner.first_x_from(handle, t).map_err(map_err)
    }

    // ---- §5.5 time model ------------------------------------------------

    /// Settled value strictly before `t`. This is what NBA evaluation at a
    /// clock edge must use, not `value_at`.
    fn value_before(&self, handle: u32, t: i64) -> PyResult<Option<Value>> {
        self.inner.value_before(handle, t).map(wrap).map_err(map_err)
    }

    /// Number of writes at exactly `t`; more than one means a combinational
    /// glitch settled through delta cycles.
    fn deltas_at(&self, handle: u32, t: i64) -> PyResult<usize> {
        self.inner.deltas_at(handle, t).map_err(map_err)
    }

    /// Value at a specific delta of `t`, for inspecting a glitch.
    fn value_at_delta(&self, handle: u32, t: i64, delta: usize) -> PyResult<Option<Value>> {
        self.inner.value_at_delta(handle, t, delta).map(wrap).map_err(map_err)
    }

    // ---- rendering windows (§10.2) --------------------------------------

    /// A window reduced to at most `px_width` entries, ready to put on the wire.
    ///
    /// Returns `{"mode": "exact"|"minmax", "initial": str|None, "transitions": [...]}`
    /// where an exact entry is `[t, bits]` and a min/max entry is
    /// `[t, min, max, n_changes, flags]` (flags: 1=X, 2=Z).
    ///
    /// The reduction happens in Rust: the scan is over every transition in the
    /// window, while the result is bounded by `px_width`.
    fn wave<'py>(
        &self,
        py: Python<'py>,
        handle: u32,
        t0: i64,
        t1: i64,
        px_width: usize,
    ) -> PyResult<Bound<'py, PyDict>> {
        let w = self.inner.wave(handle, t0, t1, px_width).map_err(map_err)?;
        let d = PyDict::new(py);
        d.set_item("initial", w.initial)?;
        match w.mode {
            vt_trace::query::WaveMode::Exact => {
                d.set_item("mode", "exact")?;
                d.set_item("transitions", w.points)?;
            }
            vt_trace::query::WaveMode::MinMax => {
                d.set_item("mode", "minmax")?;
                let rows: Vec<(i64, String, String, u32, u8)> =
                    w.buckets.into_iter().map(|b| (b.t, b.min, b.max, b.n, b.flags)).collect();
                d.set_item("transitions", rows)?;
            }
        }
        Ok(d)
    }

    // ---- whole-trace scans ----------------------------------------------

    fn first_x_all(&self, py: Python<'_>) -> Vec<(u32, i64)> {
        py.detach(|| self.inner.first_x_all())
    }

    fn constant_signals(&self, py: Python<'_>, start: i64, end: i64) -> Vec<u32> {
        py.detach(|| self.inner.constant_signals(start, end))
    }

    /// `last_change_before(t)` for every signal, indexed by handle.
    ///
    /// Detaches from the interpreter for the duration: the scan is pure Rust
    /// over rayon's pool, so holding the GIL would serialise the very
    /// parallelism this call exists for.
    fn last_change_all(&self, py: Python<'_>, t: i64) -> Vec<Option<i64>> {
        py.detach(|| self.inner.last_change_all(t))
    }

    /// `value_at(t)` for many signals, in one parallel pass.
    ///
    /// The scan runs detached for the same reason `last_change_all` does; only
    /// the wrapping of the results into Python objects takes the GIL back.
    fn value_at_all(&self, py: Python<'_>, handles: Vec<u32>, t: i64) -> Vec<Option<Value>> {
        let got = py.detach(|| self.inner.value_at_all(&handles, t));
        got.into_iter().map(|v| v.map(|inner| Value { inner })).collect()
    }

    /// Timestamps where this signal's settled value becomes 1.
    ///
    /// The clock edge list every cycle number is counted against (§5.5), and
    /// the sample points of a channel scan (§8.14).
    fn rising_edges(&self, py: Python<'_>, handle: u32) -> PyResult<Vec<i64>> {
        py.detach(|| self.inner.rising_edges(handle)).map_err(map_err)
    }

    /// `value_before` for many signals at many timestamps, one linear pass each.
    ///
    /// `times` must be ascending. Returns one row per handle, in the order
    /// given. This is the primitive behind step 2 of §8.14: sampling a whole
    /// interface at every clock edge stays linear in the trace instead of
    /// paying a binary search per (signal, edge).
    fn sample_before(
        &self,
        py: Python<'_>,
        handles: Vec<u32>,
        times: Vec<i64>,
    ) -> PyResult<Vec<Vec<Option<Value>>>> {
        let rows = py
            .detach(|| self.inner.sample_before(&handles, &times))
            .map_err(map_err)?;
        Ok(rows
            .into_iter()
            .map(|r| r.into_iter().map(|v| v.map(|inner| Value { inner })).collect())
            .collect())
    }

    /// Rebuild a VCD from the store.
    fn to_vcd(&self) -> PyResult<String> {
        vt_trace::reconstruct::from_store(&self.inner).map_err(map_err)
    }

    fn __len__(&self) -> usize {
        self.inner.n_signals()
    }

    fn __repr__(&self) -> String {
        let (a, b) = self.inner.time_range();
        format!("TraceStore({} signals, t={}..{})", self.inner.n_signals(), a, b)
    }
}

fn wrap(v: Option<RsValue>) -> Option<Value> {
    v.map(|inner| Value { inner })
}

fn to_py_signal(s: &vt_trace::query::SignalMeta) -> Signal {
    Signal {
        handle: s.signal_id,
        path: s.path.clone(),
        name: s.name.clone(),
        scope: s.scope.clone(),
        width: s.width,
        kind: s.kind.as_vcd().to_string(),
        stream_id: s.stream_id,
        msb: s.msb,
        lsb: s.lsb,
        array_index: s.array_index,
        n_events: s.n_events,
        encoding: s.encoding.as_str().to_string(),
    }
}

/// Convert a waveform to a `.vtx` store. Dispatches on file extension.
#[pyfunction]
fn convert(src: &str, out: &str) -> PyResult<u64> {
    let path = std::path::Path::new(src);
    let is_fst = path
        .extension()
        .map(|e| e.eq_ignore_ascii_case("fst"))
        .unwrap_or(false);
    let trace = if is_fst {
        #[cfg(feature = "fst")]
        {
            vt_trace::fst::convert(path, out).map_err(map_err)?
        }
        #[cfg(not(feature = "fst"))]
        {
            return Err(PyValueError::new_err(
                "FST support is not compiled in; rebuild with the `fst` feature \
                 (needs zlib and libclang) or convert the dump to VCD first",
            ));
        }
    } else {
        vt_trace::store::convert_vcd(path, out).map_err(map_err)?
    };
    Ok(trace.total_events() as u64)
}

/// Write a transaction table to Parquet — step 6 of §8.14.
///
/// `columns` is an ordered list of `(name, values)`, where every element of a
/// column is an `int`, a `str` or `None`. The column's type is taken from its
/// first non-null element, so a pack's own field names and metrics decide the
/// schema (§6.3: the file is the export, not just a cache).
#[pyfunction]
fn write_txn_table(path: &str, columns: Vec<(String, Vec<Option<Bound<'_, PyAny>>>)>) -> PyResult<()> {
    let mut cols: Vec<(String, RsColumn)> = Vec::with_capacity(columns.len());
    for (name, values) in columns {
        // Textual if *any* value is, not if the first one is. A read that came
        // back X part-way through a run is ordinary in a real design — the
        // memory was not ready yet — and deciding the schema from element zero
        // used to make that whole column unwritable, so the table §6.3 promises
        // as the export was silently skipped for the interface. The four-state
        // digits *are* the value, so one of them makes the column text; the
        // integers alongside render losslessly into it.
        let textual = values
            .iter()
            .flatten()
            .filter(|v| !v.is_none())
            .any(|v| v.extract::<i64>().is_err());
        let col = if textual {
            RsColumn::Text(
                values
                    .into_iter()
                    .map(|v| match v {
                        None => Ok(None),
                        Some(v) => v.extract::<String>().map(Some).or_else(|_| {
                            v.extract::<i64>().map(|i| Some(i.to_string()))
                        }),
                    })
                    .collect::<PyResult<Vec<_>>>()?,
            )
        } else {
            RsColumn::Int(
                values
                    .into_iter()
                    .map(|v| match v {
                        None => Ok(None),
                        Some(v) => v.extract::<i64>().map(Some),
                    })
                    .collect::<PyResult<Vec<_>>>()
                    .map_err(|_| {
                        PyValueError::new_err(format!("column `{name}` mixes integers and text"))
                    })?,
            )
        };
        cols.push((name, col));
    }
    vt_trace::txn::write(path, &cols).map_err(map_err)
}

/// Read back a table written by `write_txn_table`, as `{name: [values]}` plus
/// the column order.
#[pyfunction]
fn read_txn_table<'py>(py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    for (name, col) in vt_trace::txn::read(path).map_err(map_err)? {
        match col {
            RsColumn::Int(v) => out.set_item(name, v)?,
            RsColumn::Text(v) => out.set_item(name, v)?,
        }
    }
    Ok(out)
}

/// True when this build can read FST files.
#[pyfunction]
fn has_fst_support() -> bool {
    cfg!(feature = "fst")
}

#[pyfunction]
fn hello() -> String {
    "Hello from VeriTrace vt-py!".to_string()
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_function(wrap_pyfunction!(convert, m)?)?;
    m.add_function(wrap_pyfunction!(has_fst_support, m)?)?;
    m.add_function(wrap_pyfunction!(write_txn_table, m)?)?;
    m.add_function(wrap_pyfunction!(read_txn_table, m)?)?;
    m.add_class::<TraceStore>()?;
    m.add_class::<Signal>()?;
    m.add_class::<Scope>()?;
    m.add_class::<Value>()?;
    m.add("MAX_PX", vt_trace::query::MAX_PX)?;
    Ok(())
}
