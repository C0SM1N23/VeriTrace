//! VeriTrace trace core: waveform parsing, the `.vtx` store, and value-at-time
//! queries.
//!
//! The layering rule from §3 applies inside this crate too — nothing here knows
//! what a why-trace or a waveform panel is. It answers questions about signal
//! values over time, and nothing else.

pub mod index;
pub mod model;
pub mod query;
pub mod reconstruct;
pub mod store;
pub mod txn;
pub mod value;
pub mod vcd;

#[cfg(feature = "fst")]
pub mod fst;

pub use model::{Kind, Scope, Signal, SignalId, Time, Trace};
pub use query::TraceStore;
pub use value::{Bit, Value};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("malformed VCD at line {line}: {msg}")]
    Vcd { line: usize, msg: String },

    #[error("malformed .vtx store: {0}")]
    Store(String),

    #[error("parquet error: {0}")]
    Parquet(#[from] parquet::errors::ParquetError),

    #[error("arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("unknown signal: {0}")]
    UnknownSignal(String),

    #[cfg(feature = "fst")]
    #[error("fst error: {0}")]
    Fst(String),
}

pub type Result<T> = std::result::Result<T, Error>;
