//! The extracted-transaction table of §8.13, as Parquet inside the store.
//!
//! Step 6 of the §8.14 algorithm writes each interface's transactions to
//! `dump.vtx/txn/<iface>.parquet`. §6.3 is explicit about why that file format
//! and not a private one: *a colleague must be able to do
//! `pl.read_parquet("dump.vtx/txn/axi_m0.parquet")` and plot their own latency
//! histogram without going through this tool, without an API and without asking
//! you.* Interoperability is the adoption argument, so the cache file and the
//! export are deliberately the same file.
//!
//! The schema is decided by the pack, not by this module: a protocol pack names
//! its own payload fields and metrics, and inventing a fixed column set here
//! would cap what a pack can express. So a table is an ordered list of named
//! columns over the two types every field reduces to — an integer or the text
//! that stands in for one when the value carries X/Z or exceeds 64 bits.

use std::fs::File;
use std::path::Path;
use std::sync::Arc;

use arrow::array::{Array, ArrayRef, Int64Array, StringArray};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::properties::WriterProperties;

use crate::{Error, Result};

/// One column of a transaction table. Nullable throughout: a field a protocol
/// only carries on some transaction types (`bresp` on writes, not on reads) is
/// missing rather than zero, and the difference matters to anyone querying it.
#[derive(Debug, Clone, PartialEq)]
pub enum Column {
    Int(Vec<Option<i64>>),
    Text(Vec<Option<String>>),
}

impl Column {
    pub fn len(&self) -> usize {
        match self {
            Column::Int(v) => v.len(),
            Column::Text(v) => v.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn field(&self, name: &str) -> Field {
        let ty = match self {
            Column::Int(_) => DataType::Int64,
            Column::Text(_) => DataType::Utf8,
        };
        Field::new(name, ty, true)
    }

    fn array(&self) -> ArrayRef {
        match self {
            Column::Int(v) => Arc::new(Int64Array::from(v.clone())) as ArrayRef,
            Column::Text(v) => Arc::new(StringArray::from(v.clone())) as ArrayRef,
        }
    }
}

fn writer_props() -> WriterProperties {
    WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::try_new(3).unwrap()))
        .build()
}

/// Write `columns` as a single Parquet file, creating parent directories.
///
/// Rejects ragged input rather than writing a file Arrow would later refuse to
/// read: a truncated column is a bug in the extractor, and failing at the write
/// keeps it attributable there.
pub fn write(path: impl AsRef<Path>, columns: &[(String, Column)]) -> Result<()> {
    let path = path.as_ref();
    let rows = columns.first().map(|(_, c)| c.len()).unwrap_or(0);
    if let Some((name, c)) = columns.iter().find(|(_, c)| c.len() != rows) {
        return Err(Error::Store(format!(
            "transaction column `{name}` has {} rows, expected {rows}",
            c.len()
        )));
    }
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }

    let schema = Arc::new(Schema::new(
        columns.iter().map(|(n, c)| c.field(n)).collect::<Vec<_>>(),
    ));
    let arrays: Vec<ArrayRef> = columns.iter().map(|(_, c)| c.array()).collect();

    let file = File::create(path)?;
    let mut w = ArrowWriter::try_new(file, schema.clone(), Some(writer_props()))?;
    // An empty table is still a valid answer — "this interface produced no
    // transactions" is information, and a missing file would be indistinguishable
    // from an extraction that never ran.
    if rows > 0 {
        w.write(&RecordBatch::try_new(schema, arrays)?)?;
    }
    w.close()?;
    Ok(())
}

/// Read back a table written by [`write`], preserving column order.
pub fn read(path: impl AsRef<Path>) -> Result<Vec<(String, Column)>> {
    let file = File::open(path.as_ref())?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;

    // The columns come from the file schema, not from the first batch: a table
    // with no transactions has no row groups at all, and reading it must still
    // report the columns rather than look like a file that was never written.
    let mut names: Vec<String> = Vec::new();
    let mut out: Vec<Column> = Vec::new();
    for f in builder.schema().fields() {
        names.push(f.name().clone());
        out.push(match f.data_type() {
            DataType::Int64 => Column::Int(Vec::new()),
            DataType::Utf8 => Column::Text(Vec::new()),
            other => {
                return Err(Error::Store(format!(
                    "transaction column `{}` has unsupported type {other}",
                    f.name()
                )))
            }
        });
    }

    for batch in builder.build()? {
        let batch = batch?;
        for (i, col) in out.iter_mut().enumerate() {
            let src = batch.column(i);
            match col {
                Column::Int(v) => {
                    let a = src
                        .as_any()
                        .downcast_ref::<Int64Array>()
                        .ok_or_else(|| Error::Store(format!("column {i} is not int64")))?;
                    v.extend((0..a.len()).map(|r| (!a.is_null(r)).then(|| a.value(r))));
                }
                Column::Text(v) => {
                    let a = src
                        .as_any()
                        .downcast_ref::<StringArray>()
                        .ok_or_else(|| Error::Store(format!("column {i} is not utf8")))?;
                    v.extend(
                        (0..a.len()).map(|r| (!a.is_null(r)).then(|| a.value(r).to_string())),
                    );
                }
            }
        }
    }
    Ok(names.into_iter().zip(out).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table() -> Vec<(String, Column)> {
        vec![
            ("type".into(), Column::Text(vec![Some("WRITE".into()), Some("READ".into())])),
            ("addr".into(), Column::Int(vec![Some(0x4000), Some(0x4004)])),
            ("resp".into(), Column::Text(vec![Some("OKAY".into()), None])),
            ("latency".into(), Column::Int(vec![Some(22), None])),
        ]
    }

    #[test]
    fn round_trips_with_nulls_and_column_order() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("txn").join("axi_m0.parquet");
        write(&p, &table()).unwrap();
        assert_eq!(read(&p).unwrap(), table());
    }

    #[test]
    fn an_empty_table_is_a_valid_file() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("empty.parquet");
        let cols = vec![("addr".to_string(), Column::Int(Vec::new()))];
        write(&p, &cols).unwrap();
        assert_eq!(read(&p).unwrap(), cols);
    }

    #[test]
    fn ragged_columns_are_rejected_at_the_write() {
        let dir = tempfile::tempdir().unwrap();
        let cols = vec![
            ("a".to_string(), Column::Int(vec![Some(1), Some(2)])),
            ("b".to_string(), Column::Int(vec![Some(1)])),
        ];
        let err = write(dir.path().join("bad.parquet"), &cols).unwrap_err();
        assert!(err.to_string().contains("`b`"), "{err}");
    }
}
