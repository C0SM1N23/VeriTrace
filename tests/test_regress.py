"""§13.6 the regression database, §8.36 the scorecard, §8.37 the test plan.

Three features whose value is entirely in what they *refuse* to claim, so that
is what most of this checks: a run that cannot be reproduced is not recorded, a
reproduction against changed RTL is not called identical, and no scorecard row
says a thing is verified without the qualifier that makes it true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritrace import plan as plan_mod
from veritrace import regress
from veritrace.regress import db
from veritrace.report import scorecard as card

DESIGNS = Path(__file__).resolve().parents[1] / "designs"


@pytest.fixture()
def con(tmp_path):
    c = regress.connect(tmp_path / "r.duckdb")
    yield c
    c.close()


def _run(**over) -> regress.Run:
    base = dict(
        seed=3910182, simulator="icarus", simulator_version="12.0",
        rtl_sha256="a" * 64, command="iverilog -g2012 -o sim.vvp *.sv",
        commit_sha="abc123", work_dir="/p", top="tb",
    )
    return regress.Run(**(base | over))


# --- §13.6 ------------------------------------------------------------------


def test_a_run_that_cannot_be_reproduced_is_not_recorded(con):
    """The seed, the simulator and the RTL hash are mandatory, and §13.6 says
    why: without them a nightly failure is an anecdote."""
    for missing in ("seed", "simulator", "simulator_version", "rtl_sha256"):
        with pytest.raises(ValueError, match=missing):
            regress.record(con, _run(**{missing: None if missing == "seed" else ""}))


def test_a_recorded_run_keeps_everything_reproduce_needs(con):
    run_id = regress.record(con, _run())
    row = regress.get(con, run_id)
    assert row["seed"] == 3910182
    assert row["simulator"] == "icarus" and row["simulator_version"] == "12.0"
    assert row["rtl_sha256"] == "a" * 64
    assert row["command"].startswith("iverilog")
    assert row["host"] and row["veritrace_version"], "who and what ran it"


def test_metrics_land_in_tables_the_spec_queries(con):
    """§13.6's own example is `SELECT ... FROM txn_metrics WHERE iface='dma0'`."""
    run = _run()
    run.txn.append({"iface": "dma0", "pack": "AXI4", "n": 200, "p50": 12.0,
                    "p95": 40.0, "p99": 61.0, "max": 90.0, "throughput": 0.4,
                    "outstanding": 6, "violations": 0})
    run.findings.append(("stuck", "warn", 3))
    run.coverage.append(("line", None, 94, 100, 0.94))
    regress.record(con, run)

    cols, rows = regress.query(
        con, "SELECT p99_latency FROM txn_metrics WHERE iface = 'dma0'"
    )
    assert rows == [(61.0,)]
    assert regress.query(con, "SELECT n FROM findings WHERE grp='stuck'")[1] == [(3,)]
    assert regress.query(con, "SELECT score FROM coverage WHERE kind='line'")[1] == [(0.94,)]


def test_history_is_machine_readable(tmp_path):
    """§13.6's whole point is a trend across runs, which is read by a script.

    DuckDB hands back `datetime` for the timestamp column, which `json.dumps`
    refuses — so the default query, the one `veritrace history` runs with no
    SQL, is exactly the one that would have failed.
    """
    import json

    from click.testing import CliRunner

    from veritrace.cli import main

    path = tmp_path / "r.duckdb"
    con = regress.connect(path)
    run = _run()
    run.findings.append(("stuck", "warn", 3))
    regress.record(con, run)
    con.close()

    r = CliRunner().invoke(main, ["history", "--db", str(path), "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert "ts" in doc["columns"] and "seed" in doc["columns"]
    assert len(doc["rows"]) == 1
    assert doc["rows"][0][doc["columns"].index("seed")] == 3910182

    q = CliRunner().invoke(
        main, ["history", "--db", str(path), "--json", "SELECT n FROM findings WHERE grp='stuck'"]
    )
    assert json.loads(q.output)["rows"] == [[3]]


def test_the_rtl_hash_changes_when_the_rtl_does(tmp_path):
    a, b = tmp_path / "a.sv", tmp_path / "b.sv"
    a.write_text("module a; endmodule\n", encoding="utf-8")
    b.write_text("module b; endmodule\n", encoding="utf-8")
    first = db.sha256_of([a, b])
    assert db.sha256_of([b, a]) == first, "order must not matter"
    b.write_text("module b; wire x; endmodule\n", encoding="utf-8")
    assert db.sha256_of([a, b]) != first


def test_reproduce_rebuilds_the_command_and_the_seed(con, tmp_path):
    src = tmp_path / "a.sv"
    src.write_text("module a; endmodule\n", encoding="utf-8")
    run_id = regress.record(con, _run(rtl_sha256=db.sha256_of([src])))

    out = regress.plan(con, run_id, [src])
    assert out.seed == 3910182
    assert out.command == "iverilog -g2012 -o sim.vvp *.sv"
    assert out.simulator == "icarus" and out.simulator_version == "12.0"
    assert out.identical and not out.caveat


def test_reproduce_refuses_to_claim_identical_when_the_rtl_moved(con, tmp_path):
    """§13.6's honesty rule, and the same one §5.7 applies to a stale trace."""
    src = tmp_path / "a.sv"
    src.write_text("module a; endmodule\n", encoding="utf-8")
    run_id = regress.record(con, _run(rtl_sha256=db.sha256_of([src])))
    src.write_text("module a; wire changed; endmodule\n", encoding="utf-8")

    out = regress.plan(con, run_id, [src])
    assert not out.identical
    assert "not guaranteed to be identical" in out.caveat


def test_without_the_current_rtl_nothing_is_claimed(con):
    run_id = regress.record(con, _run())
    out = regress.plan(con, run_id, None)
    assert not out.identical
    assert "nothing can be said" in out.caveat


def test_an_unknown_run_is_an_error_not_an_empty_answer(con):
    regress.record(con, _run())
    with pytest.raises(KeyError):
        regress.plan(con, 999)


# --- §8.36 ------------------------------------------------------------------


def test_an_absent_input_reads_as_absent_not_as_zero():
    """0% is a claim, and a pessimistic one (P1)."""
    out = card.build()
    assert len(out.rows) == len(card.CATEGORIES)
    assert out.covered == []
    assert all(r.value == "—" for r in out.rows)
    assert all(r.detail for r in out.rows), "every row says why it is empty"


def test_no_formal_row_says_proved_without_a_depth():
    formal = [card.Loaded({
        "iface": "m_axi",
        "properties": [
            {"id": "A", "verdict": "held", "depth": 20},
            {"id": "B", "verdict": "held", "depth": 20},
            {"id": "C", "verdict": "unknown", "depth": 20},
        ],
    })]
    row = card.build(formal=formal).rows[3]
    assert row.value == "2/3"
    assert "bounded depth 20" in row.detail
    assert "never proved unconditionally" in row.detail
    assert "proved" not in row.value


def test_a_broken_property_makes_the_row_bad():
    formal = [card.Loaded({"properties": [{"id": "A", "verdict": "failed", "depth": 4}]})]
    row = card.build(formal=formal).rows[3]
    assert row.status == card.BAD and "counterexample" in row.detail


def test_json_from_the_other_commands_reads_like_a_live_object():
    """§8.36 aggregates what other jobs computed, and they emit JSON."""
    mutation = card.Loaded({"score": 0.71, "killed": 142, "scored": 200,
                            "seed": 7, "invalid": []})
    row = card.build(mutation=mutation, targets={"mutation": 70.0}).rows[2]
    assert row.value == "71%" and row.status == card.OK
    assert "142/200" in row.detail and "seed 7" in row.detail


def test_a_target_that_is_missed_is_marked_bad():
    mutation = card.Loaded({"score": 0.4, "killed": 8, "scored": 20, "seed": 1, "invalid": []})
    out = card.build(mutation=mutation, targets={"mutation": 70.0})
    assert out.failing and out.failing[0].category == "mutation score"


# --- §8.37 ------------------------------------------------------------------


def test_the_reference_plan_loads(tmp_path):
    p = plan_mod.load(DESIGNS / "axi_lite" / "testplan.toml")
    assert not p.errors
    assert {i.id for i in p.items} >= {"AXI-01", "AXI-02", "AXI-05"}


def test_a_malformed_reference_is_reported(tmp_path):
    f = tmp_path / "testplan.toml"
    f.write_text(
        '[[item]]\nid = "X"\ncovered_by = ["nonsense"]\n'
        '[[item]]\nid = "Y"\nstatus = "maybe"\n',
        encoding="utf-8",
    )
    p = plan_mod.load(f)
    assert any("kind:reference" in e for e in p.errors)
    assert any("unknown status" in e for e in p.errors)


def test_evidence_beats_what_the_file_claims(tmp_path):
    """A plan is a document and documents drift."""
    f = tmp_path / "testplan.toml"
    f.write_text('[[item]]\nid = "X"\nstatus = "covered"\ncovered_by = ["fcov:nope"]\n',
                 encoding="utf-8")
    p = plan_mod.link(plan_mod.load(f))
    assert p.items[0].status == "covered"
    assert p.items[0].evidence == "unknown", "the run does not show it"


def test_a_formal_link_resolves_to_its_qualified_verdict(tmp_path):
    f = tmp_path / "testplan.toml"
    f.write_text('[[item]]\nid = "X"\ncovered_by = ["formal:A"]\n', encoding="utf-8")
    formal = [card.Loaded({
        "iface": "m", "properties": [{"id": "A", "verdict": "held", "depth": 20,
                                      "label": "HELD (bounded, depth=20)  A"}],
    })]
    p = plan_mod.link(plan_mod.load(f), formal=formal)
    link = p.items[0].links[0]
    assert link.state == "hit" and "depth=20" in link.detail
    assert p.score == 100.0


def test_a_waived_item_is_not_scored(tmp_path):
    f = tmp_path / "testplan.toml"
    f.write_text(
        '[[item]]\nid = "X"\nstatus = "waived"\n'
        '[[item]]\nid = "Y"\ncovered_by = ["formal:A"]\n',
        encoding="utf-8",
    )
    p = plan_mod.link(plan_mod.load(f))
    assert p.items[0].evidence == "waived"
    assert p.score == 0.0, "one scored item, not covered"


def test_record_and_history_resolve_the_same_database(tmp_path, monkeypatch):
    """§13.6's two commands have to agree about where the file is.

    `record` anchored the default to the project root and the readers anchored
    it to the working directory, so recording beside a simulation and querying
    from the repository root — the sequence §13.6 documents — reported that the
    database `record` had just written did not exist.
    """
    from veritrace.cli import _regress_db

    (tmp_path / ".veritrace.toml").write_text(
        '[design]\ntop = "dut"\nrtl = ["rtl/*.sv"]\n', encoding="utf-8"
    )
    sub = tmp_path / "sim" / "deep"
    sub.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    at_root = _regress_db(None)
    monkeypatch.chdir(sub)
    from_below = _regress_db(None)

    assert at_root == from_below == tmp_path / "regressions.duckdb"


def test_without_a_project_the_working_directory_is_the_anchor(tmp_path, monkeypatch):
    """No config means no project, and then there is nothing else to anchor to."""
    from veritrace.cli import _regress_db

    monkeypatch.chdir(tmp_path)
    assert _regress_db(None) == tmp_path / "regressions.duckdb"


def test_an_explicit_db_wins(tmp_path, monkeypatch):
    from veritrace.cli import _regress_db

    monkeypatch.chdir(tmp_path)
    assert _regress_db(tmp_path / "elsewhere.duckdb") == tmp_path / "elsewhere.duckdb"
