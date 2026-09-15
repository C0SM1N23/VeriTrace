"""Real CLI -> Icarus -> native capture -> session -> memory UI data contract."""
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from veritrace import simulate
from veritrace.api.app import create_app
from veritrace.cli import main

DESIGN = Path(__file__).resolve().parents[1] / "designs/sram_dualport/tb.sv"
pytestmark = pytest.mark.skipif(simulate.find_iverilog() is None, reason="Icarus is not installed")


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("own_dump", [False, True])
def test_real_dual_port_array_contents_and_missing_capture(tmp_path, capture, own_dump):
    project = tmp_path / "SRAM with spaces"
    project.mkdir()
    source = project / "tb.sv"
    shutil.copy2(DESIGN, source)
    if own_dump:
        source.write_text(source.read_text().replace("module tb;", 'module tb;\n initial begin $dumpfile("own.vcd"); $dumpvars(0,tb); end'))
    before = source.read_bytes()
    args = ["run", str(source), "--top", "tb", "--json"]
    if capture:
        args += ["--dump-memory", "tb.dut.mem"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert source.read_bytes() == before
    with TestClient(create_app(default_trace=Path(json.loads(result.stdout)["dump"]))) as client:
        sid = client.get("/").json()["default_session"]
        report = client.get(f"/session/{sid}/memory").json()
        assert report["interfaces"] == []  # SRAM is not a DRAM command bus.
        mem = next(a for a in report["arrays"] if a["path"] == "tb.dut.mem")
        assert (mem["width"], mem["depth"], mem["left"], mem["right"]) == (32, 256, 0, 255)
        assert mem["captured"] == (256 if capture else 0)

        def words(t, offset=0):
            r = client.get(f"/session/{sid}/memory/array", params={"path": mem["path"], "time": t, "offset": offset})
            assert r.status_code == 200, r.text
            return {w["index"]: w for w in r.json()["words"]}

        if capture:
            assert "x" in words(0)[3]["bits"]  # uninitialised is not fabricated zero
            assert int(words(6000)[3]["bits"], 2) == 0x11223344
            assert int(words(16000)[3]["bits"], 2) == 0x11BB33DD
            assert "x" in words(16000)[4]["bits"]  # untouched location remains unknown
            assert int(words(16000, 192)[255]["bits"], 2) == 0xDEADBEEF
            store = client.app.state.registry.get(sid).store
            assert store.value_at(store.find("tb.dut.a_rdata_o"), 21000).to_int() == 0xDEADBEEF
        else:
            assert words(16000)[3]["bits"] is None
            assert words(16000)[3]["captured"] is False
        for bad, status in [({"path": "missing"}, 404), ({"offset": 256}, 400),
                            ({"time": 999999}, 400), ({"offset": -1}, 422), ({"count": 100000}, 422)]:
            r = client.get(f"/session/{sid}/memory/array", params={"path": mem["path"], "time": 0, **bad})
            assert r.status_code == status, r.text


@pytest.mark.parametrize("bounds,first,last", [("4:7", 4, 7), ("7:4", 4, 7), ("-2:1", -2, 1)])
def test_declared_nonzero_and_descending_bounds_are_real_indices(tmp_path, bounds, first, last):
    source = tmp_path / "tb.sv"
    source.write_text(f"module tb; reg [7:0] mem [{bounds}]; initial begin mem[{first}]=8'h12; #5 mem[{last}]=8'h34; #5 $finish; end endmodule")
    result = CliRunner().invoke(main, ["run", str(source), "--top", "tb", "--dump-memory", "*", "--json"])
    assert result.exit_code == 0, result.output
    with TestClient(create_app(default_trace=Path(json.loads(result.stdout)["dump"]))) as client:
        sid = client.get("/").json()["default_session"]
        r = client.get(f"/session/{sid}/memory/array", params={"path": "tb.mem", "time": 10})
        assert r.status_code == 200, r.text
        words = r.json()["words"]
        assert [w["index"] for w in words] == list(range(first, last + 1))
        assert int(words[0]["bits"], 2) == 0x12
        assert int(words[-1]["bits"], 2) == 0x34


def test_capture_typos_and_unsupported_arrays_fail_before_simulation(tmp_path):
    source = tmp_path / "tb.sv"
    source.write_text("module tb; reg [7:0] mem[0:3][0:3]; initial #1 $finish; endmodule")
    for pattern, message in [("tb.missing", "No RTL memory matches"), ("*", "one-dimensional fixed array")]:
        result = CliRunner().invoke(main, ["run", str(source), "--top", "tb", "--dump-memory", pattern])
        assert result.exit_code != 0
        assert message in result.output
        assert not (tmp_path / ".veritrace/sim.vvp").exists()
