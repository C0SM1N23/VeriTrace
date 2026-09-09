"""Coverage — §8.21 functional, §8.12 code, TAB 7.

The second acceptance criterion of Prompt 12 is the first section: the
functional matrix shows the *right* empty cells on the AXI4-Lite design from
the earlier stages. "Right" is checkable here because designs/axi_lite is
small enough to read: its master drives `wstrb = 4'hF` on every write, uses
only word-aligned addresses, and strictly alternates a write and a read. So
`partial_write`, `unaligned`, `WRITE -> WRITE` and `READ -> READ` are holes as
a matter of fact about the source, not as a matter of what the tool happened to
report.

The code-coverage half is exercised against fixtures written the way Verilator
and xcrg write them. The inputs are synthetic; what is being tested is the
reader, and a fixture is the only way to test one without installing both
tools.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from veritrace import TraceStore, clocks
from veritrace._native import convert
from veritrace.analysis import vtq
from veritrace.config import Config
from veritrace.correlate.resolver import correlate
from veritrace.coverage import code, functional, query as cov_query, report as cov_report
from veritrace.coverage.code import CoverageError
from veritrace.graph.elaborate import discover, elaborate
from veritrace.protocol import engine

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


@pytest.fixture(scope="module")
def axi_lite(tmp_path_factory):
    out = tmp_path_factory.mktemp("cov") / "dump.vtx"
    convert(str(DESIGNS / "axi_lite" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    analysis = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    return store, clock, analysis


@pytest.fixture(scope="module")
def cpu(axi_lite):
    store, clock, analysis = axi_lite
    ex = next(e for e in analysis.extractions if e.interface.name == "cpu")
    return functional.measure(store, ex, clock)


def _point(cov, name):
    return next(p for p in cov.points if p.name == name)


def _holes(cov, name):
    return {" ".join(k) for k in _point(cov, name).holes()}


# --- the acceptance criterion ----------------------------------------------


def test_the_matrix_is_measured_from_the_declared_bins(cpu):
    """Not "the values that occurred" — the pack's domain, so a hole exists."""
    assert not cpu.automatic
    assert cpu.n_transactions == 24
    assert {p.name for p in cpu.points} >= {
        "bresp", "rresp", "kind", "unaligned", "partial_write", "error_response"
    }


def test_the_response_bins_show_the_two_that_never_happened(cpu):
    """The slave answers OKAY and SLVERR; AXI's other two codes are holes."""
    assert _holes(cpu, "bresp") == {"EXOKAY", "DECERR"}
    assert _holes(cpu, "rresp") == {"EXOKAY", "DECERR"}
    assert _point(cpu, "bresp").covered == 2


def test_the_sequence_matrix_shows_the_pairs_the_test_never_did(cpu):
    """axil_master alternates WRITE then READ, so two of the four cells are
    empty — and both of them are the ones read-after-write bugs live in."""
    assert _holes(cpu, "kind") == {"READ READ", "WRITE WRITE"}


def test_the_corners_the_design_never_reaches_are_holes(cpu):
    """`wstrb` is tied to 4'hF and every address is word-aligned, both visible
    in axil_master.sv — so these two corners cannot have been hit."""
    assert _point(cpu, "partial_write").covered == 0
    assert _point(cpu, "unaligned").covered == 0


def test_a_corner_the_design_does_reach_is_not_a_hole(cpu):
    """One transaction is aimed at an unmapped address, so SLVERR happens —
    the control that keeps the two assertions above from passing vacuously."""
    assert _point(cpu, "error_response").covered == 1


def test_every_cell_of_the_grid_exists_even_when_empty(cpu):
    """The point of the feature: a cell that is missing is a hole nobody sees."""
    kind = _point(cpu, "kind")
    assert kind.total == 4
    assert len(kind.cells) == 4
    assert kind.labels == (("READ", "WRITE"), ("READ", "WRITE"))


# --- automatic bins, for a pack that declares none --------------------------


def test_a_pack_with_no_cover_block_still_produces_a_matrix(axi_lite):
    """§8.21 has to apply to every pack, not only the ones updated for it."""
    store, clock, analysis = axi_lite
    ex = next(e for e in analysis.extractions if e.interface.name == "cpu")
    original = ex.interface.pack
    ex.interface.pack = replace(original, cover=())
    try:
        got = functional.measure(store, ex, clock)
    finally:
        ex.interface.pack = original
    assert got.automatic
    assert got.points, "automatic bins produced no points at all"
    # The transaction kind is always an axis: it needs no declaration to exist.
    assert any(p.name == "kind" and p.total == 4 for p in got.points)
    # And the fields the transactions carry become points of their own.
    assert any(p.name == "bresp" for p in got.points)


def test_a_narrow_field_enumerates_its_whole_domain(cpu):
    """A 2-bit response has four possible values whether or not they occurred;
    that is what makes the unreached two visible."""
    assert _point(cpu, "bresp").total == 4


# --- code coverage: both sources (§8.12) ------------------------------------


VERILATOR_DAT = (
    "# SystemC::Coverage-3\n"
    "C '\x01f\x02fifo.sv\x01l\x0242\x01n\x02body\x01page\x02v_line/top' 12\n"
    "C '\x01f\x02fifo.sv\x01l\x0249\x01n\x02else if\x01page\x02v_branch/top' 0\n"
    "C '\x01f\x02fifo.sv\x01l\x0242\x01n\x02body\x01page\x02v_line/top' 3\n"
    "C '\x01f\x02other.sv\x01l\x027\x01n\x02t\x01page\x02v_toggle/top' 5\n"
)

XCRG_XML = """<?xml version="1.0"?>
<coverage>
  <module name="fifo">
    <statement file="fifo.sv" line="42" count="12"/>
    <branch file="fifo.sv" line="49" count="0" type="branch"/>
  </module>
</coverage>
"""


@pytest.fixture
def verilator_db(tmp_path):
    p = tmp_path / "coverage.dat"
    p.write_text(VERILATOR_DAT, encoding="utf-8")
    return p


@pytest.fixture
def vivado_db(tmp_path):
    p = tmp_path / "dashboard.xml"
    p.write_text(XCRG_XML, encoding="utf-8")
    return p


def test_verilator_records_are_read_with_their_kind(verilator_db):
    got = code.read(verilator_db)
    assert got.source == "verilator"
    kinds = {p.kind for f in got.files for p in f.points}
    assert kinds == {"line", "branch", "toggle"}


def test_records_for_the_same_point_are_summed(verilator_db):
    """Verilator emits one record per instance; a module instantiated twice is
    not half-covered."""
    got = code.read(verilator_db)
    fifo = next(f for f in got.files if f.file == "fifo.sv")
    line42 = next(p for p in fifo.points if p.line == 42)
    assert line42.count == 15
    assert len(fifo.points) == 2


def test_the_uncovered_branch_is_the_only_hole(verilator_db):
    got = code.read(verilator_db)
    assert [(p.file, p.line) for p in got.uncovered()] == [("fifo.sv", 49)]
    assert got.covered == 2 and got.total == 3


def test_xcrg_xml_is_read_from_its_attributes(vivado_db):
    """Element names have moved between Vivado releases; the attributes have
    not, so that is what the reader keys on."""
    got = code.read(vivado_db)
    assert got.source == "vivado"
    assert got.total == 2
    assert [(p.line, p.covered) for f in got.files for p in f.points] == [(42, True), (49, False)]


def test_the_source_is_detected_from_the_content(tmp_path):
    """`coverage.dat` is a name Verilator chose and nothing enforces it."""
    p = tmp_path / "cov.txt"
    p.write_text(XCRG_XML, encoding="utf-8")
    assert code.read(p).source == "vivado"


def test_the_simple_csv_of_the_spec_is_accepted(tmp_path):
    p = tmp_path / "coverage.dat"
    p.write_text("v_line,fifo.sv,42,12\nv_branch,fifo.sv,49,0\n", encoding="utf-8")
    got = code.read(p)
    assert got.total == 2
    assert [p.kind for f in got.files for p in f.points] == ["line", "branch"]


def test_a_file_with_no_records_says_so_rather_than_reporting_100_percent(tmp_path):
    p = tmp_path / "coverage.dat"
    p.write_text("# SystemC::Coverage-3\n", encoding="utf-8")
    got = code.read(p)
    assert got.error
    assert got.score is None


def test_an_unreadable_database_raises_with_the_path(tmp_path):
    with pytest.raises(CoverageError, match="cannot read"):
        code.read(tmp_path / "nope.dat")


def test_a_database_beside_the_project_is_found_without_being_named(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "coverage.dat").write_text(VERILATOR_DAT, encoding="utf-8")
    assert code.discover(tmp_path) == tmp_path / "logs" / "coverage.dat"
    # The database's own directory works too — `logs/` is Verilator's default,
    # not a requirement.
    assert code.discover(tmp_path / "logs") == tmp_path / "logs" / "coverage.dat"
    (tmp_path / "empty").mkdir()
    assert code.discover(tmp_path / "empty") is None


# --- §8.12's derivation: an uncovered line into the conditions that close it -


@pytest.fixture(scope="module")
def fifo_buggy(tmp_path_factory):
    out = tmp_path_factory.mktemp("holes") / "dump.vtx"
    convert(str(DESIGNS / "fifo_buggy" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    el = elaborate(discover(DESIGNS / "fifo_buggy"))
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    return store, clock, el.graph


@pytest.fixture
def uncovered_read_branch(tmp_path, fifo_buggy):
    """Line 49 of fifo_buggy.sv — `else if (rd_en && !empty)`, which the
    injected bug makes unreachable because `rd_rst_n` is tied low."""
    store, clock, graph = fifo_buggy
    db = tmp_path / "coverage.dat"
    db.write_text(
        "C '\x01f\x02fifo_buggy.sv\x01l\x0249\x01n\x02else if"
        "\x01page\x02v_branch/fifo' 0\n",
        encoding="utf-8",
    )
    return cov_report.build(
        None, store, clock, graph, coverage_path=db, project_root=DESIGNS / "fifo_buggy"
    )


def test_the_conditions_come_from_the_guard_at_that_line(uncovered_read_branch):
    hole = uncovered_read_branch.holes[0]
    assert (hole.file, hole.line) == ("fifo_buggy.sv", 49)
    assert hole.signal.endswith("rd_ptr")
    assert {c.text for c in hole.conditions} == {"rd_rst_n", "rd_en", "!empty"}


def test_each_condition_says_how_often_it_actually_held(uncovered_read_branch):
    """§8.12's "in prezent: 1 in 94% din cicluri" — measured, not guessed."""
    conds = {c.text: c for c in uncovered_read_branch.holes[0].conditions}
    assert conds["rd_rst_n"].held == 0
    assert conds["rd_en"].held > 0
    assert conds["rd_en"].sampled == conds["rd_rst_n"].sampled > 0


def test_the_condition_that_never_held_carries_how_it_is_produced(uncovered_read_branch):
    """The last block of §8.12's example: read off the graph, not suggested."""
    conds = {c.text: c for c in uncovered_read_branch.holes[0].conditions}
    assert conds["rd_rst_n"].produced_by == ("rd_rst_n <= 0",)
    # A condition that *is* met needs no explanation of how to meet it.
    assert conds["rd_en"].produced_by == ()


def test_the_source_line_travels_with_the_hole(uncovered_read_branch):
    assert "rd_en && !empty" in uncovered_read_branch.holes[0].text


def test_a_line_with_no_assignment_says_so_instead_of_inventing_one(tmp_path, fifo_buggy):
    store, clock, graph = fifo_buggy
    db = tmp_path / "coverage.dat"
    db.write_text("v_line,fifo_buggy.sv,3,0\n", encoding="utf-8")
    got = cov_report.build(
        None, store, clock, graph, coverage_path=db, project_root=DESIGNS / "fifo_buggy"
    )
    assert got.holes[0].conditions == []
    assert "no assignment" in got.holes[0].note


def test_without_rtl_the_holes_are_listed_and_the_reason_is_stated(tmp_path, verilator_db):
    got = cov_report.build(None, None, None, None, coverage_path=verilator_db)
    assert [h.line for h in got.holes] == [49]
    assert "no RTL" in got.holes[0].note


# --- the report, and the §10.1 commands -------------------------------------


def test_both_halves_are_optional_and_the_missing_one_says_why(axi_lite):
    store, clock, analysis = axi_lite
    got = cov_report.build(analysis, store, clock)
    assert got.functional
    assert got.code is None
    assert "coverage database" in got.skipped["code"]


def test_no_interfaces_is_a_stated_result_not_an_empty_page(fifo_buggy, verilator_db):
    store, clock, graph = fifo_buggy
    got = cov_report.build(None, store, clock, graph, coverage_path=verilator_db)
    assert got.functional == []
    assert "no protocol interface" in got.skipped["functional"]
    assert got.code is not None


def test_the_fcov_command_returns_one_interface(axi_lite):
    store, clock, analysis = axi_lite
    report = cov_report.build(analysis, store, clock)
    got = cov_query.run(None, report, vtq.parse_pipeline("fcov(cpu)"))
    assert [f["iface"] for f in got["functional"]] == ["cpu"]


def test_an_unknown_interface_lists_the_ones_measured(axi_lite):
    store, clock, analysis = axi_lite
    report = cov_report.build(analysis, store, clock)
    with pytest.raises(vtq.QueryError, match="measured:"):
        cov_query.run(None, report, vtq.parse_pipeline("fcov(nope)"))


def test_the_uncovered_command_carries_the_conditions(uncovered_read_branch):
    got = cov_query.run(None, uncovered_read_branch, vtq.parse_pipeline("uncovered()"))
    assert got["holes"][0]["conditions"]
    assert got["code"]["source"] == "verilator"


def test_a_command_on_a_session_that_never_measured_says_so():
    with pytest.raises(vtq.QueryError, match="did not run"):
        cov_query.run(None, None, vtq.parse_pipeline("fcov()"))


def test_a_cached_extraction_measures_the_same_coverage(tmp_path):
    """P1: the same trace must not report two different matrices depending on
    whether a previous command happened to warm the cache.

    A corner like `bresp != 0` reads a response-channel payload, and the cached
    transaction table has no per-beat events — so the values are lifted onto the
    transaction at assembly, where both paths can see them.
    """
    out = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "axi_lite" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)

    fresh = engine.extract(store, out, clock, Config.empty(), use_cache=False)
    cached = engine.extract(store, out, clock, Config.empty(), use_cache=True)
    assert any(ex.parquet for ex in cached.extractions)

    def matrix(analysis):
        ex = next(e for e in analysis.extractions if e.interface.name == "cpu")
        got = functional.measure(store, ex, clock)
        return {p.name: (p.covered, p.total) for p in got.points}

    assert matrix(cached) == matrix(fresh)
    assert matrix(fresh)["error_response"] == (1, 1)


def test_the_exported_table_carries_the_response_a_reader_would_want(tmp_path):
    """§6.3's promise is that the Parquet table *is* the export. A table with
    the address and no response cannot answer "which writes were refused"."""
    from veritrace.protocol import persist

    out = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "axi_lite" / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    clock = clocks.resolve(store)
    engine.extract(store, out, clock, Config.empty(), use_cache=False)
    cols = persist.read(persist.table_path(out, "cpu"))
    assert "bresp" in cols
    assert any(v == 2 for v in cols["bresp"] if v is not None), "the SLVERR is not in the table"
