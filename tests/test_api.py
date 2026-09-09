"""Tests for the FastAPI server: REST routes, the waveform WebSocket, and the
two acceptance criteria of this stage.

The downsampling rule of §10.2 is tested through the real WebSocket, not just
against the underlying primitive, because the rule is a promise about what goes
on the wire.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import threading
import time
from pathlib import Path

import msgpack
import pytest
from fastapi.testclient import TestClient

from conftest import SMALL_VCD, design_store, make_vtx
from veritrace import convert, regress
from veritrace.api import create_app
from veritrace.api.sessions import LayoutFile, Session, SessionRegistry, session_id_for

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

@pytest.fixture
def vtx(tmp_path: Path) -> Path:
    return make_vtx(tmp_path)


@pytest.fixture
def client(vtx: Path):
    with TestClient(create_app(default_trace=vtx)) as c:
        yield c


@pytest.fixture
def session_id(client) -> str:
    return client.get("/").json()["default_session"]


# ---------------------------------------------------------------------------
# §10.1 REST
# ---------------------------------------------------------------------------


def test_root_reports_default_session(client):
    body = client.get("/").json()
    assert body["name"] == "veritrace"
    assert body["default_session"]
    assert body["max_px"] >= 1200


def test_create_session(client, tmp_path):
    other = make_vtx(tmp_path, name="other")
    r = client.post("/session", json={"trace_path": str(other)})
    assert r.status_code == 200
    body = r.json()
    sid = body["session_id"]
    assert sid
    deadline = time.monotonic() + 5
    while body["phase"] != "ready" and time.monotonic() < deadline:
        time.sleep(0.01)
        body = client.get(f"/session/{sid}/status").json()
    assert body["phase"] == "ready", body
    # tb.clk, tb.data, tb.dut.clk, tb.dut.state — clk is aliased into dut, so it
    # is two signals over one stream.
    assert body["n_signals"] == 4
    assert body["phase"] == "ready"


def test_create_session_exposes_real_progress_before_it_is_ready(client, tmp_path, monkeypatch):
    other = make_vtx(tmp_path, name="slow")
    gate = threading.Event()
    original = Session.open.__func__

    def slow_open(cls, trace_path, rtl_paths=None, top=None, progress=None):
        if progress:
            progress("converting", 0.1)
        gate.wait(timeout=5)
        return original(cls, trace_path, rtl_paths, top, progress)

    monkeypatch.setattr(Session, "open", classmethod(slow_open))
    posted = client.post("/session", json={"trace_path": str(other)}).json()
    sid = posted["session_id"]
    status = client.get(f"/session/{sid}/status").json()
    assert status["phase"] in {"queued", "converting"}
    assert status["progress"] < 1.0
    blocked = client.get(f"/session/{sid}/signals")
    assert blocked.status_code == 409
    assert "still" in blocked.json()["detail"]

    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/session/{sid}/status").json()
        if status["phase"] == "ready":
            break
        time.sleep(0.01)
    assert status["phase"] == "ready", status
    assert status["progress"] == 1.0


def test_background_session_failure_cannot_become_ready(client, tmp_path):
    broken = tmp_path / "broken.vcd"
    broken.write_text("this is not a waveform", encoding="utf-8")
    posted = client.post("/session", json={"trace_path": str(broken)}).json()
    sid = posted["session_id"]
    deadline = time.monotonic() + 5
    status = posted
    while time.monotonic() < deadline:
        status = client.get(f"/session/{sid}/status").json()
        if status["phase"] == "error":
            break
        time.sleep(0.01)
    assert status["phase"] == "error", status
    assert status["error"]
    assert client.get(f"/session/{sid}/signals").status_code == 409


def test_create_session_is_idempotent_per_trace(client, vtx, session_id):
    # A stable id is what lets a client come back to its layout later.
    r = client.post("/session", json={"trace_path": str(vtx)})
    assert r.json()["session_id"] == session_id


def test_create_session_missing_trace(client):
    r = client.post("/session", json={"trace_path": "does/not/exist.vtx"})
    assert r.status_code == 404


def test_status(client, session_id):
    body = client.get(f"/session/{session_id}/status").json()
    assert body["phase"] == "ready"
    assert body["progress"] == 1.0
    # No RTL yet, so there is genuinely nothing to report here.
    assert body["correlation_rate"] is None
    assert body["t0"] == 0
    assert body["t1"] == 30
    assert body["timescale"] == "1ns"
    assert body["capture"] is False


def test_unknown_session_is_404(client):
    assert client.get("/session/deadbeef/status").status_code == 404
    assert client.get("/session/deadbeef/layout").status_code == 404


def test_hierarchy_is_lazy_per_level(client, session_id):
    top = client.get(f"/session/{session_id}/hierarchy").json()
    assert [s["name"] for s in top["scopes"]] == ["tb"]
    assert top["signals"] == []

    tb = client.get(f"/session/{session_id}/hierarchy", params={"path": "tb"}).json()
    assert [s["name"] for s in tb["scopes"]] == ["dut"]
    assert [s["name"] for s in tb["signals"]] == ["clk", "data"]
    assert tb["scopes"][0]["n_children"] == 0

    dut = client.get(f"/session/{session_id}/hierarchy", params={"path": "tb.dut"}).json()
    assert [s["name"] for s in dut["signals"]] == ["clk", "state"]


def test_signal_search(client, session_id):
    r = client.get(f"/session/{session_id}/signals", params={"q": "state"}).json()
    assert r["count"] == 1
    assert r["signals"][0]["path"] == "tb.dut.state"

    # Fuzzy, not just substring.
    r = client.get(f"/session/{session_id}/signals", params={"q": "dtstate"}).json()
    assert "tb.dut.state" in [s["path"] for s in r["signals"]]

    # No query lists everything, bounded.
    r = client.get(f"/session/{session_id}/signals").json()
    assert r["total"] == 4

    # A query matching nothing is an empty result, not an error.
    r = client.get(f"/session/{session_id}/signals", params={"q": "zzzz"}).json()
    assert r["count"] == 0


def test_exact_values_endpoint_reads_the_store_not_a_wave_bucket(client, session_id):
    data = client.get(f"/session/{session_id}/signals", params={"q": "tb.data"}).json()["signals"][0]
    got = client.post(
        f"/session/{session_id}/values",
        json={"handles": [data["handle"]], "time": 15},
    )
    assert got.status_code == 200, got.text
    assert got.json() == {
        "time": 15,
        "values": [{"handle": data["handle"], "value": "10100000"}],
    }


def test_search_is_deterministic(client, session_id):
    first = client.get(f"/session/{session_id}/signals", params={"q": "c"}).json()
    for _ in range(3):
        assert client.get(f"/session/{session_id}/signals", params={"q": "c"}).json() == first


def test_source_returns_404_for_now(client, session_id):
    r = client.get(f"/session/{session_id}/source/rtl/fifo.sv")
    assert r.status_code == 404
    assert "RTL" in r.json()["detail"]


def test_source_keeps_duplicate_basenames_and_included_headers_distinct(vtx, tmp_path):
    a, b = tmp_path / "a" / "part.sv", tmp_path / "b" / "part.sv"
    for path, module, signal in [(a, "first", "left"), (b, "second", "right")]:
        path.parent.mkdir()
        path.write_text(f"module {module};\nlogic {signal};\nassign {signal} = 1'b1;\nendmodule\n")
    header = tmp_path / "inside.vh"
    header.write_text("logic from_header;\n")
    top = tmp_path / "top.sv"
    top.write_text('module top;\n`include "inside.vh"\nfirst x(); second y();\nendmodule\n')
    with TestClient(create_app(default_trace=vtx, rtl=[str(p) for p in [top, a, b]],
                               top="top")) as c:
        sid = c.get("/").json()["default_session"]
        graph = c.app.state.registry.get(sid).graph
        assert graph.get("top.x.left").decl_loc.file == a.resolve().as_posix()
        assert graph.get("top.y.right").decl_loc.file == b.resolve().as_posix()
        assert c.get(f"/session/{sid}/source/part.sv").status_code == 409
        for path, signal in [(a, "top.x.left"), (b, "top.y.right")]:
            response = c.get(f"/session/{sid}/source/{path.resolve().as_posix()}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["file"] == path.resolve().as_posix()
            assert body["text"] == path.read_text()
            assert {s["path"] for rows in body["signals"].values() for s in rows} == {signal}
        included = c.get(f"/session/{sid}/source/inside.vh")
        assert included.status_code == 200, included.text
        assert included.json()["signals"]["1"][0]["path"] == "top.from_header"
        outside = tmp_path / "not_loaded.sv"
        outside.write_text("not loaded by slang")
        assert c.get(f"/session/{sid}/source/{outside.as_posix()}").status_code == 404


# ---------------------------------------------------------------------------
# Layout persistence (P5)
# ---------------------------------------------------------------------------


def test_layout_defaults_are_empty(client, session_id):
    body = client.get(f"/session/{session_id}/layout").json()
    assert body["signals"] == []
    assert body["radix"] == {}
    assert body["version"] == 1


def test_layout_round_trips(client, session_id):
    layout = {
        "signals": [0, 3, 1],
        "groups": [{"name": "fifo", "signals": [3, 1]}],
        "radix": {"tb.data": "hex"},
        "bookmarks": [{"t": 20, "label": "first write"}],
        "cursors": [10],
        "zoom": {"t0": 0, "t1": 30},
        "queryHistory": ["find(clk)", "why(tb.dut.state @ 20)"],
        "savedQueries": {"clock": "find(clk)"},
    }
    r = client.put(f"/session/{session_id}/layout", json=layout)
    assert r.status_code == 200
    got = client.get(f"/session/{session_id}/layout").json()
    for k, v in layout.items():
        assert got[k] == v


def test_concurrent_layout_saves_are_serialized(tmp_path, monkeypatch):
    """Parallel browser PUTs cannot collide on the atomic-save temporary file."""
    from concurrent.futures import ThreadPoolExecutor

    target = tmp_path / "dump.vtx.session.json"
    layout_file = LayoutFile(target)
    original = Path.write_text
    active = 0
    maximum = 0
    guard = threading.Lock()

    def observed_write(path, *args, **kwargs):
        nonlocal active, maximum
        if path == target.with_suffix(target.suffix + ".tmp"):
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            try:
                return original(path, *args, **kwargs)
            finally:
                with guard:
                    active -= 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", observed_write)
    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(lambda n: layout_file.save({"signals": [n]}), range(16)))

    assert len(saved) == 16
    assert maximum == 1
    assert json.loads(target.read_text(encoding="utf-8"))["signals"][0] in range(16)


def test_layout_is_written_beside_the_store(client, session_id, vtx):
    client.put(f"/session/{session_id}/layout", json={"signals": [1]})
    sidecar = vtx.with_name(vtx.name + ".session.json")
    # Beside, never inside: converting again wipes the .vtx directory.
    assert sidecar.exists()
    assert sidecar.parent == vtx.parent
    assert json.loads(sidecar.read_text())["signals"] == [1]


def test_layout_survives_a_server_restart(vtx):
    """Acceptance criterion: the saved layout persists across restarts."""
    layout = {"signals": [2, 0], "radix": {"tb.data": "bin"}, "zoom": {"t0": 5, "t1": 25}}

    with TestClient(create_app(default_trace=vtx)) as c1:
        sid = c1.get("/").json()["default_session"]
        c1.put(f"/session/{sid}/layout", json=layout)

    # Brand new app object, new registry, new store — nothing shared in memory.
    with TestClient(create_app(default_trace=vtx)) as c2:
        sid2 = c2.get("/").json()["default_session"]
        assert sid2 == sid, "session id must be stable for the same trace"
        got = c2.get(f"/session/{sid2}/layout").json()
        assert got["signals"] == [2, 0]
        assert got["radix"] == {"tb.data": "bin"}
        assert got["zoom"] == {"t0": 5, "t1": 25}


def test_configured_row_height_and_radix_are_real_layout_defaults(tmp_path, monkeypatch):
    vtx = make_vtx(tmp_path)
    (tmp_path / ".veritrace.toml").write_text(
        '[ui]\nrow_height = "comfortable"\n\n'
        '[ui.radix]\n"*state" = "enum"\n"*data" = "dec"\n'
    )
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(default_trace=vtx)) as c:
        sid = c.get("/").json()["default_session"]
        layout = c.get(f"/session/{sid}/layout").json()
        assert layout["rowH"] == 28
        assert layout["radix"] == {"tb.data": "dec", "tb.dut.state": "enum"}


def test_server_elaboration_receives_config_include_dirs_and_defines(tmp_path, monkeypatch):
    vtx = make_vtx(tmp_path)
    rtl = tmp_path / "rtl"
    inc = tmp_path / "include"
    rtl.mkdir()
    inc.mkdir()
    (inc / "width.svh").write_text("`define VT_WIDTH 8\n")
    source = rtl / "tb.sv"
    source.write_text(
        '`include "width.svh"\nmodule tb;\nlogic [`VT_WIDTH-1:0] data;\n'
        "`ifdef FEATURE\nlogic enabled;\n`endif\nendmodule\n"
    )
    (tmp_path / ".veritrace.toml").write_text(
        '[design]\ntop = "tb"\nincdirs = ["include"]\n\n'
        '[design.defines]\nFEATURE = 1\n'
    )
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(default_trace=vtx, rtl=[str(source)])) as c:
        sid = c.get("/").json()["default_session"]
        session = c.app.state.registry.get(sid)
        assert session.graph is not None, session.rtl_error
        assert session.graph.get("tb.data").width == 8
        assert session.graph.get("tb.enabled") is not None


def test_corrupt_layout_file_does_not_break_the_session(vtx):
    sidecar = vtx.with_name(vtx.name + ".session.json")
    sidecar.write_text("{ this is not json")
    with TestClient(create_app(default_trace=vtx)) as c:
        sid = c.get("/").json()["default_session"]
        # Falls back to defaults rather than refusing to open the trace (P7).
        assert c.get(f"/session/{sid}/layout").json()["signals"] == []
        status = c.get(f"/session/{sid}/status").json()
        assert "could not read saved layout" in status["layout_error"]
        # Merely opening/reading must not replace the bytes that might be
        # repaired by hand.
        assert sidecar.read_text() == "{ this is not json"
        c.put(f"/session/{sid}/layout", json={"signals": [0]})
        assert json.loads(sidecar.read_text())["signals"] == [0]
        backups = list(sidecar.parent.glob(sidecar.name + ".corrupt*"))
        assert len(backups) == 1 and backups[0].read_text() == "{ this is not json"


def test_unwritable_layout_reports_the_real_error_and_retry_preserves_changes(vtx):
    app = create_app(default_trace=vtx)
    with TestClient(app, raise_server_exceptions=False) as client:
        sid = client.get("/").json()["default_session"]
        endpoint = f"/session/{sid}/layout"
        initial = client.put(endpoint, json={"rowH": 20, "rulerMode": "time"})
        assert initial.status_code == 200, initial.text
        sidecar = app.state.registry.get(sid).layout_file.path
        original = sidecar.read_bytes()
        # A real filesystem refusal on the production atomic-write path, not a
        # mocked LayoutFile or a successful fixture response.
        temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
        temporary.mkdir()
        failed = client.put(endpoint, json={"rowH": 28, "rulerMode": "cycle"})
        assert failed.status_code == 503, failed.text
        assert "Could not save layout" in failed.json()["detail"]
        assert "retry" in failed.json()["detail"]
        assert sidecar.read_bytes() == original
        temporary.rmdir()
        saved = client.put(endpoint, json={"rowH": 28, "rulerMode": "cycle"})
        assert saved.status_code == 200, saved.text
        assert client.get(endpoint).json()["rulerMode"] == "cycle"
    with TestClient(create_app(default_trace=vtx)) as reopened:
        assert reopened.get(endpoint).json()["rowH"] == 28


def test_session_id_is_stable_and_path_based(tmp_path, vtx):
    assert session_id_for(vtx) == session_id_for(vtx)
    assert session_id_for(vtx) != session_id_for(tmp_path)


def test_reopening_same_trace_with_empty_rtl_clears_dependent_state(tmp_path):
    design = tmp_path / "rtl"
    design.mkdir()
    (design / "tb.sv").write_text(
        "module tb; logic clk; logic [7:0] data; dut u(.clk(clk)); endmodule\n"
        "module dut(input logic clk); logic [3:0] state; always_ff @(posedge clk) state <= 1; endmodule\n"
    )
    trace = make_vtx(tmp_path)
    registry = SessionRegistry()
    loaded = registry.open(trace, [str(design)], "tb")
    assert loaded.graph is not None
    loaded._why[("stale", 0)] = object()  # type: ignore[assignment]
    loaded._why_not[("stale", 0, "1")] = object()

    waveform_only = registry.open(trace, [], None)
    assert waveform_only is loaded
    assert waveform_only.graph is None
    assert waveform_only.rtl_paths == []
    assert waveform_only._why == {}
    assert waveform_only._why_not == {}
    assert waveform_only.status()["has_rtl"] is False


def test_configured_rtl_is_used_on_first_open_but_explicit_empty_still_disables_it(tmp_path, monkeypatch):
    design = tmp_path / "tb.sv"
    design.write_text("module tb; logic clk; logic [7:0] data; endmodule\n")
    (tmp_path / ".veritrace.toml").write_text('[design]\ntop="tb"\nrtl=["tb.sv"]\n')
    trace = make_vtx(tmp_path)
    caller = tmp_path / "elsewhere"
    caller.mkdir()
    monkeypatch.chdir(caller)
    for explicit in (None, []):
        with TestClient(create_app(default_trace=trace, rtl=explicit)) as client:
            sid = client.get("/").json()["default_session"]
            status = client.get(f"/session/{sid}/status").json()
            assert status["has_rtl"] is (explicit is None), status
            assert not status["rtl_error"]
    with TestClient(create_app()) as client:
        response = client.post("/session", json={"trace_path": str(trace)})
        sid = response.json()["session_id"]
        status = _wait_session_ready(client, sid)
        assert status["has_rtl"] and status["top"] == "tb", status
        client.post("/session", json={"trace_path": str(trace), "rtl_paths": []})
        assert not _wait_session_ready(client, sid)["has_rtl"]


def test_reopening_replaces_a_session_when_store_provenance_changes(vtx):
    registry = SessionRegistry()
    first = registry.open(vtx)
    meta_path = vtx / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["source_sha256"] = "f" * 64
    meta_path.write_text(json.dumps(meta))

    second = registry.open(vtx)
    assert second is not first
    assert second.session_id == first.session_id
    assert second.store.source_sha256 == "f" * 64


def _wait_session_ready(client, sid):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = client.get(f"/session/{sid}/status").json()
        assert status.get("phase") != "error", status
        if status.get("phase") == "ready":
            return status
        time.sleep(0.01)
    pytest.fail(f"session did not become ready: {status}")


def test_post_session_reopens_changed_top_and_rtl_in_production(tmp_path):
    rtl = tmp_path / "tops.sv"
    rtl.write_text("module tb; logic clk; endmodule\nmodule alternate; logic other; endmodule\n")
    trace = make_vtx(tmp_path)
    with TestClient(create_app(default_trace=trace, rtl=[str(rtl)], top="tb")) as c:
        sid = c.get("/").json()["default_session"]
        r = c.post("/session", json={"trace_path": str(trace), "top": "alternate"})
        assert r.status_code == 200 and r.json()["session_id"] == sid
        _wait_session_ready(c, sid)
        selected = c.app.state.registry.get(sid)
        assert selected.top == "alternate"
        assert selected.graph.get("alternate.other") is not None
        assert selected.graph.get("tb.clk") is None

        c.post("/session", json={"trace_path": str(trace), "rtl_paths": []})
        status = _wait_session_ready(c, sid)
        assert status["has_rtl"] is False


def test_post_session_rerun_uses_new_waveform_and_preserves_layout(tmp_path):
    raw = tmp_path / "rerun.vcd"
    raw.write_text(SMALL_VCD)
    with TestClient(create_app(default_trace=raw)) as c:
        sid = c.get("/").json()["default_session"]
        first = c.app.state.registry.get(sid)
        c.put(f"/session/{sid}/layout", json={"signals": [1], "query": "find(data)"})
        # The old native store stays open, exactly as when a user reruns while
        # the UI still has the earlier simulation loaded on Windows.
        raw.write_text(SMALL_VCD.replace("#30", "#40"))
        posted = c.post("/session", json={"trace_path": str(raw)})
        assert posted.json()["session_id"] == sid
        status = _wait_session_ready(c, sid)
        assert status["t1"] == 40
        second = c.app.state.registry.get(sid)
        assert first.store.time_range[1] == 30
        assert second.trace_path != first.trace_path
        assert c.get(f"/session/{sid}/layout").json()["query"] == "find(data)"


def test_session_can_retry_a_failed_open_after_the_dump_is_repaired(tmp_path):
    raw = tmp_path / "repair.vcd"
    raw.write_text("broken waveform")
    with TestClient(create_app()) as c:
        sid = c.post("/session", json={"trace_path": str(raw)}).json()["session_id"]
        deadline = time.monotonic() + 5
        while c.get(f"/session/{sid}/status").json()["phase"] != "error":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        raw.write_text(SMALL_VCD)
        c.post("/session", json={"trace_path": str(raw)})
        assert _wait_session_ready(c, sid)["n_signals"] == 4


def test_cached_session_keeps_bandwidth_events_and_export(tmp_path):
    raw = tmp_path / "bus.vcd"
    shutil.copy2(DESIGNS / "deadlock" / "dump_ok.vcd", raw)
    responses = []
    for _ in range(2):
        with TestClient(create_app(default_trace=raw)) as c:
            sid = c.get("/").json()["default_session"]
            perf = c.get(f"/session/{sid}/performance").json()
            assert all(row["bytes_moved"] > 0 for row in perf["interfaces"])
            iface = perf["interfaces"][0]["iface"]
            exported = c.post(f"/session/{sid}/export", json={"kind": "csv", "target": iface})
            assert exported.status_code == 200
            rows = list(csv.DictReader(io.StringIO(exported.text)))
            assert all(int(row["n_beats"]) > 0 for row in rows)
            responses.append((perf["interfaces"], rows))
    assert responses[0] == responses[1]


def test_performance_window_recalculates_all_metrics_without_changing_the_run(tmp_path):
    raw = tmp_path / "bus.vcd"
    shutil.copy2(DESIGNS / "deadlock" / "dump_ok.vcd", raw)
    with TestClient(create_app(default_trace=raw)) as c:
        sid = c.get("/").json()["default_session"]
        session = c.app.state.registry.get(sid)
        full = c.get(f"/session/{sid}/performance").json()
        ex = session.protocol.extractions[0]
        transfer_channels = ex.interface.pack.perf.channels(ex.interface.pack)
        at = next(e.time for t in ex.transactions for e in t.events if e.channel in transfer_channels)
        response = c.get(f"/session/{sid}/performance", params={"t0": at, "t1": at})
        assert response.status_code == 200, response.text
        selected = response.json()
        assert selected["window"] == {"t0": at, "t1": at}
        assert sum(i["bytes_moved"] for i in selected["interfaces"]) > 0
        for before, after in zip(full["interfaces"], selected["interfaces"], strict=True):
            assert after["stalls"]["total"] == 1
            assert after["bytes_moved"] < before["bytes_moved"]
            assert after["latency"]["n"] < before["latency"]["n"]
            assert len(after["outstanding"]["points"]) == 1
        assert selected["fairness"] != full["fairness"]
        assert c.get(f"/session/{sid}/performance").json() == full
        assert c.get(f"/session/{sid}/performance", params={"t0": 10}).status_code == 400
        assert c.get(f"/session/{sid}/performance", params={"t0": 20, "t1": 10}).status_code == 400


# ---------------------------------------------------------------------------
# §10.2 WebSocket
# ---------------------------------------------------------------------------


def ws_roundtrip(client, session_id, request, expect=1):
    """Send one message, collect `expect` wave_chunks (ignoring progress)."""
    chunks = []
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(msgpack.packb(request))
        while len(chunks) < expect:
            msg = msgpack.unpackb(ws.receive_bytes(), raw=False)
            if msg["op"] == "wave_chunk":
                chunks.append(msg)
            elif msg["op"] == "error":
                pytest.fail(f"server error: {msg}")
    return chunks


def test_ws_ping(client, session_id):
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(msgpack.packb({"op": "ping"}))
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "pong"


def test_ws_runs_generic_queries_and_finishes(client, session_id):
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(msgpack.packb({"op": "query", "vtq": "find(state)"}))
        partial = msgpack.unpackb(ws.receive_bytes(), raw=False)
        done = msgpack.unpackb(ws.receive_bytes(), raw=False)
    assert partial["op"] == "partial"
    assert partial["result"]["signals"][0]["path"] == "tb.dut.state"
    assert done["op"] == "done" and done["stats"]["nodes"] == 0


def test_ws_reports_progress_when_a_generic_query_crosses_200ms(
    client, session_id, monkeypatch
):
    import veritrace.api.app as app_mod

    original = app_mod._run_pipeline

    def slow(session, pipeline, registry=None):
        time.sleep(0.3)
        return original(session, pipeline, registry)

    monkeypatch.setattr(app_mod, "_run_pipeline", slow)
    messages = []
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(msgpack.packb({"op": "query", "vtq": "find(state)"}))
        while True:
            message = msgpack.unpackb(ws.receive_bytes(), raw=False)
            messages.append(message)
            if message["op"] == "done":
                break
    ops = [message["op"] for message in messages]
    assert "progress" in ops
    assert ops.index("progress") < ops.index("partial")
    progress = next(message for message in messages if message["op"] == "progress")
    assert progress["phase"] == "find"
    # Verify real progress and its ordering, not Windows timer granularity.
    assert progress["elapsed_ms"] > 0
    assert messages[-1]["stats"]["ms"] >= progress["elapsed_ms"]


def test_ws_causal_query_streams_nodes_before_done(checks_client):
    client, sid = checks_client
    with client.websocket_connect(f"/session/{sid}/ws") as ws:
        ws.send_bytes(msgpack.packb({"op": "query", "vtq": "why(u_dut.lock_r)"}))
        partials = []
        while True:
            msg = msgpack.unpackb(ws.receive_bytes(), raw=False)
            if msg["op"] == "partial":
                partials.append(msg["node"])
            elif msg["op"] == "done":
                done = msg
                break
            elif msg["op"] == "error":
                pytest.fail(msg)
    assert partials
    assert partials[-1]["signal"].endswith("u_dut.lock_r")
    assert done["stats"]["nodes"] == len(partials)
    assert done["result"]["root"]["signal"].endswith("u_dut.lock_r")


def test_ws_streams_wave_chunks(client, session_id):
    clk = client.get(f"/session/{session_id}/signals", params={"q": "tb.clk"}).json()["signals"][0]
    chunks = ws_roundtrip(
        client, session_id, {"op": "wave", "signals": [clk["handle"]], "t0": 0, "t1": 31, "px_width": 100}
    )
    chunk = chunks[0]
    assert chunk["h"] == clk["handle"]
    assert chunk["done"] is True
    assert chunk["mode"] == "exact"
    assert [t for t, _ in chunk["transitions"]] == [0, 10, 20, 30]


def test_ws_marks_done_only_on_the_last_signal(client, session_id):
    handles = [s["handle"] for s in client.get(f"/session/{session_id}/signals").json()["signals"]]
    chunks = ws_roundtrip(
        client,
        session_id,
        {"op": "wave", "signals": handles, "t0": 0, "t1": 31, "px_width": 100},
        expect=len(handles),
    )
    assert [c["done"] for c in chunks] == [False] * (len(handles) - 1) + [True]
    assert [c["h"] for c in chunks] == handles


def test_ws_reports_initial_value(client, session_id):
    data = client.get(f"/session/{session_id}/signals", params={"q": "tb.data"}).json()["signals"][0]
    chunks = ws_roundtrip(
        client,
        session_id,
        {"op": "wave", "signals": [data["handle"]], "t0": 15, "t1": 20, "px_width": 100},
    )
    # Nothing changes in this window; the client still needs the held value.
    assert chunks[0]["initial"] == "10100000"
    assert chunks[0]["transitions"] == []


def test_ws_rejects_bad_input(client, session_id):
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(b"\xc1not msgpack at all")
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "error"

        ws.send_bytes(msgpack.packb({"op": "nonsense"}))
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "error"

        ws.send_bytes(msgpack.packb({"op": "wave", "signals": [0], "px_width": 0}))
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "error"


def test_ws_bad_handle_does_not_kill_the_connection(client, session_id):
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(msgpack.packb({"op": "wave", "signals": [99999], "px_width": 10}))
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "error"
        # Still usable afterwards.
        ws.send_bytes(msgpack.packb({"op": "ping"}))
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "pong"


def test_undumped_signal_is_reconstructed_over_the_real_wave_window(tmp_path):
    """§7.3 is a waveform, not a single value copied from a causal card.

    ``tmp`` is deliberately absent from the dump while both operands are real
    trace streams. The WebSocket must evaluate the combinational RTL expression
    at their transition times, label the local negative handle, and obey the
    same pixel bound as an observed signal.
    """
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        "module top(input logic a, b, output logic y);\n"
        "  logic tmp;\n"
        "  assign tmp = a & b;\n"
        "  assign y = tmp;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    vcd = tmp_path / "dump.vcd"
    vcd.write_text(
        "$timescale 1ns $end\n"
        "$scope module top $end\n"
        "$var wire 1 ! a $end\n"
        "$var wire 1 \" b $end\n"
        "$var wire 1 # y $end\n"
        "$upscope $end\n$enddefinitions $end\n"
        "#0\n0!\n0\"\n0#\n"
        "#10\n1!\n"
        "#20\n1\"\n1#\n"
        "#30\n0!\n0#\n",
        encoding="utf-8",
    )
    out = tmp_path / "dump.vtx"
    convert(str(vcd), str(out))
    app = create_app(default_trace=out, rtl=[str(rtl)], top="top")
    with TestClient(app) as c:
        sid = c.get("/").json()["default_session"]
        session = app.state.registry.get(sid)
        derived = session.graph.get("top.tmp")
        assert derived is not None and derived.is_reconstructible
        handle = -17

        exact = ws_roundtrip(
            c,
            sid,
            {
                "op": "wave",
                "signals": [handle],
                "derived": {str(handle): derived.path},
                "t0": 0,
                "t1": 31,
                "px_width": 100,
            },
        )[0]
        assert exact["h"] == handle
        assert exact["mode"] == "exact"
        assert exact["initial"] == "0"
        assert exact["transitions"] == [[20, "1"], [30, "0"]]

        reduced = ws_roundtrip(
            c,
            sid,
            {
                "op": "wave",
                "signals": [handle],
                "derived": {str(handle): derived.path},
                "t0": 0,
                "t1": 31,
                "px_width": 1,
            },
        )[0]
        assert reduced["mode"] == "minmax"
        assert len(reduced["transitions"]) <= 1

        at = c.post(
            f"/session/{sid}/values",
            json={"handles": [handle], "time": 25, "derived": {handle: derived.path}},
        )
        assert at.status_code == 200, at.text
        assert at.json()["values"][0]["value"] == "1"


def test_undumped_memory_is_not_fabricated_as_a_derived_wave():
    """§5.6: current write data is not the historical contents of an array."""
    app = create_app(
        default_trace=design_store("lanes"), rtl=[str(DESIGNS / "lanes")], top="tb_lanes"
    )
    with TestClient(app) as c:
        sid = c.get("/").json()["default_session"]
        session = app.state.registry.get(sid)
        memory = next(sig for sig in session.graph if sig.kind.value == "mem")
        assert not memory.is_reconstructible
        with c.websocket_connect(f"/session/{sid}/ws") as ws:
            ws.send_bytes(
                msgpack.packb(
                    {
                        "op": "wave",
                        "signals": [-17],
                        "derived": {"-17": memory.path},
                        "t0": 0,
                        "t1": 300_000,
                        "px_width": 100,
                    }
                )
            )
            message = msgpack.unpackb(ws.receive_bytes(), raw=False)
        assert message["op"] == "error"
        assert "not an undumped reconstructible signal" in message["message"]


def test_negative_wave_handle_without_a_derived_path_is_rejected(client, session_id):
    with client.websocket_connect(f"/session/{session_id}/ws") as ws:
        ws.send_bytes(msgpack.packb({"op": "wave", "signals": [-1], "px_width": 10}))
        message = msgpack.unpackb(ws.receive_bytes(), raw=False)
        assert message["op"] == "error"
        assert "derived signal path" in message["message"]


def test_ws_unknown_session(client):
    with client.websocket_connect("/session/deadbeef/ws") as ws:
        assert msgpack.unpackb(ws.receive_bytes(), raw=False)["op"] == "error"


# ---------------------------------------------------------------------------
# The mandatory downsampling rule (§10.2)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def big_vtx(tmp_path_factory) -> Path:
    """A signal with 10^6 transitions — the case the rule exists for."""
    tmp = tmp_path_factory.mktemp("big")
    src = tmp / "big.vcd"
    n = 1_000_000
    parts = [
        "$timescale 1ns $end\n$scope module top $end\n",
        "$var wire 1 ! fast $end\n$var wire 8 \" slow [7:0] $end\n",
        "$upscope $end\n$enddefinitions $end\n",
    ]
    parts.extend(f"#{i + 1}\n{i % 2}!\n" for i in range(n))
    src.write_text("".join(parts))
    out = tmp / "big.vtx"
    convert(str(src), str(out))
    return out


def test_million_transitions_at_1200px_never_exceeds_the_pixel_width(big_vtx):
    """Acceptance criterion, stated explicitly in the prompt.

    10^6 transitions requested at a 1200 px window must come back as at most
    1200 points. Without this the UI dies on the first serious dump.
    """
    with TestClient(create_app(default_trace=big_vtx)) as client:
        sid = client.get("/").json()["default_session"]
        fast = client.get(f"/session/{sid}/signals", params={"q": "top.fast"}).json()["signals"][0]
        assert fast["n_events"] == 1_000_000

        chunks = ws_roundtrip(
            client,
            sid,
            {"op": "wave", "signals": [fast["handle"]], "t0": 0, "t1": 1_000_001, "px_width": 1200},
        )
        chunk = chunks[0]
        assert chunk["mode"] == "minmax"
        assert len(chunk["transitions"]) <= 1200, (
            f"server sent {len(chunk['transitions'])} points for a 1200px window"
        )
        # Reduced, but still covering the window rather than truncated.
        assert len(chunk["transitions"]) > 1000
        # Every transition is accounted for in some bucket.
        assert sum(row[3] for row in chunk["transitions"]) == 1_000_000
        # A toggling 1-bit signal collapses to a 0..1 band per pixel.
        assert all(row[1] == "0" and row[2] == "1" for row in chunk["transitions"])


@pytest.mark.parametrize("px", [1, 13, 320, 1200, 1920, 3840])
def test_pixel_cap_holds_for_any_width(big_vtx, px):
    with TestClient(create_app(default_trace=big_vtx)) as client:
        sid = client.get("/").json()["default_session"]
        fast = client.get(f"/session/{sid}/signals", params={"q": "top.fast"}).json()["signals"][0]
        chunks = ws_roundtrip(
            client,
            sid,
            {"op": "wave", "signals": [fast["handle"]], "t0": 0, "t1": 1_000_001, "px_width": px},
        )
        assert len(chunks[0]["transitions"]) <= px


def test_absurd_pixel_width_is_clamped(big_vtx):
    from veritrace import MAX_PX

    with TestClient(create_app(default_trace=big_vtx)) as client:
        sid = client.get("/").json()["default_session"]
        fast = client.get(f"/session/{sid}/signals", params={"q": "top.fast"}).json()["signals"][0]
        chunks = ws_roundtrip(
            client,
            sid,
            {
                "op": "wave",
                "signals": [fast["handle"]],
                "t0": 0,
                "t1": 1_000_001,
                "px_width": 50_000_000,
            },
        )
        assert len(chunks[0]["transitions"]) <= MAX_PX


# --- TAB 6, Checks (§11.4) -------------------------------------------------


@pytest.fixture
def checks_client(tmp_path):
    """A server on the checks design, with RTL — the real end-to-end shape.

    Converted into a copy rather than pointed at `designs/checks/dump.vtx`: a
    store is a build product and is not committed, so the old form only ever
    worked on a machine where an earlier run had left one lying about. It
    passed locally and could not pass on a clean checkout, which is the worst
    shape a test can be in.

    The copy also isolates the sidecar these tests write — suppressions and
    layout — so one of them cannot decide what the next one sees.
    """
    design = tmp_path / "checks"
    shutil.copytree(DESIGNS / "checks", design)
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    app = create_app(design / "dump.vtx", rtl=[str(design)])
    with TestClient(app) as c:
        yield c, app.state.default_session_id


def test_checks_are_ready_when_the_session_opens(checks_client):
    """§1.4: the findings are there before anything is asked for."""
    client, sid = checks_client
    status = client.get(f"/session/{sid}/status").json()
    assert status["n_findings"] > 0
    # §11.4b: and the tool opens on them rather than on an empty canvas.
    assert status["default_tab"] == "checks"
    assert status["clock"] is not None and status["n_cycles"] > 100


def test_checks_route_returns_grouped_findings(checks_client):
    client, sid = checks_client
    body = client.get(f"/session/{sid}/checks").json()
    assert set(body["groups"]) <= {"stuck", "x_sources", "lint", "parameters"}
    assert body["findings"]
    first = body["findings"][0]
    assert {"id", "group", "severity", "check", "title", "loc", "why"} <= set(first)
    assert body["parameters"]["path"] == "tb_checks"


def test_suppression_needs_a_reason_and_persists(checks_client, tmp_path):
    client, sid = checks_client
    findings = client.get(f"/session/{sid}/checks").json()["findings"]
    target = findings[0]["id"]
    before = len(findings)

    # §11.4: no reason, no suppression — enforced at the API, not just the UI.
    assert client.post(f"/session/{sid}/checks/{target}/suppress", json={"reason": ""}).status_code == 422

    r = client.post(
        f"/session/{sid}/checks/{target}/suppress", json={"reason": "known, TICKET-12"}
    )
    assert r.status_code == 200
    body = client.get(f"/session/{sid}/checks").json()
    assert len(body["findings"]) == before - 1
    assert body["suppressed"][target] == "known, TICKET-12"

    client.delete(f"/session/{sid}/checks/{target}/suppress")
    assert len(client.get(f"/session/{sid}/checks").json()["findings"]) == before


def test_a_layout_save_does_not_wipe_suppressions(checks_client):
    """P5 cuts both ways: the wave layout must not clobber the checks state."""
    client, sid = checks_client
    target = client.get(f"/session/{sid}/checks").json()["findings"][0]["id"]
    client.post(f"/session/{sid}/checks/{target}/suppress", json={"reason": "expected"})
    client.put(f"/session/{sid}/layout", json={"signals": [], "radix": {}, "cursors": []})
    assert client.get(f"/session/{sid}/checks").json()["suppressed"][target] == "expected"
    client.delete(f"/session/{sid}/checks/{target}/suppress")


def test_cone_route(checks_client):
    client, sid = checks_client
    body = client.get(
        f"/session/{sid}/cone",
        params={"signal": "tb_checks.dut.u_dut.status", "depth": 3},
    ).json()
    assert body["root"] == "tb_checks.dut.u_dut.status"
    assert any(n["path"].endswith("lock_r") for n in body["nodes"])
    assert client.get(f"/session/{sid}/cone", params={"signal": "nope"}).status_code == 404


def test_cone_needs_rtl(client, session_id):
    assert client.get(f"/session/{session_id}/cone", params={"signal": "tb.clk"}).status_code == 409


def test_stuck_route_re_runs_at_another_threshold(checks_client):
    """§8.4's window, tried without editing a file and restarting the server.

    The right value depends on the run, so it has to be cheap enough to turn.
    Only the stuck scan re-runs; the rest of the report is untouched.
    """
    client, sid = checks_client
    wide = client.get(f"/session/{sid}/stuck", params={"cycles": 1_000_000}).json()
    assert wide["findings"] == []
    # And it says why it is empty, rather than reading as a clean design.
    assert "1000000 cycles" in wide["unreachable"]

    tight = client.get(f"/session/{sid}/stuck", params={"cycles": 20}).json()
    assert tight["unreachable"] is None
    assert len(tight["findings"]) > len(wide["findings"])
    assert all(f["group"] == "stuck" for f in tight["findings"])
    assert client.get(f"/session/{sid}/stuck", params={"cycles": 0}).status_code == 422


def test_stuck_route_honours_suppressions(checks_client):
    """A finding hidden in the Checks tab must not reappear at another window."""
    client, sid = checks_client
    target = client.get(f"/session/{sid}/stuck", params={"cycles": 20}).json()["findings"][0]
    client.post(f"/session/{sid}/checks/{target['id']}/suppress", json={"reason": "known"})
    try:
        again = client.get(f"/session/{sid}/stuck", params={"cycles": 20}).json()
        assert target["id"] not in [f["id"] for f in again["findings"]]
    finally:
        client.delete(f"/session/{sid}/checks/{target['id']}/suppress")


def test_the_query_bar_takes_a_partial_name_like_the_terminal_does(checks_client):
    """§7.2's suffix rule, applied to what a person typed rather than to a dump.

    A `why(dut.full)` that worked in the terminal and not in the query bar
    would be a rule the user has to remember rather than one the tool has.
    """
    client, sid = checks_client
    body = client.post(f"/session/{sid}/query", json={"vtq": "why(u_dut.lock_r)"})
    assert body.status_code == 200, body.text
    assert body.json()["signal"].endswith(".u_dut.lock_r")

    # And ambiguity is refused with the candidates, never resolved by guessing.
    bad = client.post(f"/session/{sid}/query", json={"vtq": "why(nope_at_all)"})
    assert bad.status_code == 404
    assert "unknown signal" in bad.json()["detail"]


def test_the_query_bar_runs_the_rest_of_the_vtq_table(checks_client):
    """§10.1 lists eighteen signal-level commands and calls VTQ the query
    language. The parser accepted all of them from the start; the executor
    answered one, so `cone(...)` typed into the spine of the tool (§11.3) came
    back "not supported" for an analysis the same session had a button for."""
    client, sid = checks_client

    def run(vtq: str):
        r = client.post(f"/session/{sid}/query", json={"vtq": vtq})
        return r.status_code, r.json()

    code, body = run("cone(u_dut.lock_r, depth=2)")
    assert code == 200, body
    assert body["kind"] == "cone" and body["nodes"]
    # A partial name resolves here the way it does in the terminal (§7.2).
    assert body["signal"].endswith(".u_dut.lock_r")

    assert run("fanout(u_dut.lock_r, depth=2)")[1]["direction"] == "fanout"
    assert run("find(lock)")[1]["signals"]
    assert run("lint(tb_checks)")[1]["findings"]
    assert run("xtrace()")[1]["findings"]

    code, body = run("stuck(min_duration=c50)")
    assert code == 200 and body["cycles"] == 50
    assert all(f["group"] == "stuck" for f in body["findings"])

    # `hold` and `edges` are about one signal in a window, so both have to
    # respect the window rather than answer about the whole run.
    edges = run("edges(tb_checks.dut.u_dut.lock_r, c0:c400)")[1]["edges"]
    assert edges and all(e["cycle"] is not None for e in edges)
    spans = run("hold(tb_checks.dut.u_dut.lock_r, c10:c200)")[1]["spans"]
    assert spans and spans[0]["from"] < spans[-1]["to"]


def test_stuck_after_really_narrows_the_observation_window(checks_client):
    """Both arguments in §9.2 must affect the detector, not merely parse."""
    client, sid = checks_client
    whole = client.post(
        f"/session/{sid}/query", json={"vtq": "stuck(min_duration=c50)"}
    ).json()
    narrowed = client.post(
        f"/session/{sid}/query",
        json={"vtq": "stuck(after=c100, min_duration=c50)"},
    )
    assert narrowed.status_code == 200, narrowed.text
    body = narrowed.json()
    assert body["after"] > 0
    assert len(body["findings"]) <= len(whole["findings"])

    # There are fewer than 700 cycles in the fixture. A late 100-cycle window
    # cannot produce a finding inherited from before its requested start.
    late = client.post(
        f"/session/{sid}/query",
        json={"vtq": "stuck(after=c600, min_duration=c100)"},
    )
    assert late.status_code == 200, late.text
    assert late.json()["findings"] == []
    assert "requested window" in late.json()["unreachable"]


def test_a_vtq_command_that_needs_rtl_says_which_half_is_missing(client, session_id):
    """§7.4: a dump with no RTL is a supported mode, not a stack trace."""
    r = client.post(f"/session/{session_id}/query", json={"vtq": "cone(tb.clk)"})
    assert r.status_code == 400
    assert "needs the RTL" in r.json()["detail"]
    # And one that does not need it still works.
    ok = client.post(f"/session/{session_id}/query", json={"vtq": "find(clk)"})
    assert ok.status_code == 200 and ok.json()["signals"]


def test_the_hierarchy_is_a_tree_that_can_be_walked_one_level_at_a_time(checks_client):
    """§11.3's left column, and §10.1's lazy-per-level rule behind it.

    The endpoint existed from the start and no frontend code called it, so the
    only way to put a signal on screen was one at a time — which is what makes
    a viewer unusable next to `add wave -r`.
    """
    client, sid = checks_client
    root = client.get(f"/session/{sid}/hierarchy").json()
    assert [s["name"] for s in root["scopes"]] == ["tb_checks"]
    top = root["scopes"][0]
    # A collapsed node says what it is hiding, so "add everything here" can be
    # a decision rather than a surprise.
    assert top["n_signals"] > top["n_children"] > 0

    level = client.get(f"/session/{sid}/hierarchy", params={"path": "tb_checks"}).json()
    assert level["path"] == "tb_checks"
    assert level["scopes"], "tb_checks has sub-scopes"
    # Lazy: this level carries its own signals, not the subtree's.
    assert len(level["signals"]) < top["n_signals"]
    assert all(s["scope"] == "tb_checks" for s in level["signals"])


def test_add_wave_recursive_returns_the_whole_subtree(checks_client):
    """The one action that separates a usable viewer from a signal-at-a-time one."""
    client, sid = checks_client
    top = client.get(f"/session/{sid}/hierarchy").json()["scopes"][0]

    everything = client.get(f"/session/{sid}/hierarchy/signals").json()
    assert everything["count"] == top["n_signals"], "the count on the node is what you get"

    part = client.get(
        f"/session/{sid}/hierarchy/signals", params={"path": "tb_checks.dut"}
    ).json()
    assert 0 < part["count"] < everything["count"]
    assert all(
        s["path"].startswith("tb_checks.dut.") or s["scope"] == "tb_checks.dut"
        for s in part["signals"]
    )
    # Hierarchy order, not alphabetical: the wave should read like the design.
    scopes = [s["scope"] for s in part["signals"]]
    assert scopes == sorted(scopes)

    # A limit is honoured — a 50k-signal design must not be able to ask for one
    # response with everything in it by accident.
    assert client.get(
        f"/session/{sid}/hierarchy/signals", params={"limit": 5}
    ).json()["count"] == 5


def test_correlation_route_carries_the_names_behind_the_rate(checks_client):
    """§7.2's rate is only actionable next to the list it summarises."""
    client, sid = checks_client
    body = client.get(f"/session/{sid}/correlation").json()
    assert body["matched"] <= body["total"]
    assert body["summary"].endswith(f"({body['percent']}%)")
    assert body["n_unmatched"] == len(body["unmatched"])
    assert "exact" in body["by_method"]


def test_correlation_route_is_404_without_rtl(client, session_id):
    """§7.4: a fabricated 0% would be worse than saying there is nothing."""
    r = client.get(f"/session/{session_id}/correlation")
    assert r.status_code == 404
    assert "no RTL" in r.json()["detail"]


def test_checks_without_rtl_still_runs_stuck_and_says_what_it_skipped(client, session_id):
    """§7.4: a dump with no RTL is a supported mode, not a degraded one."""
    body = client.get(f"/session/{session_id}/checks").json()
    assert "lint" in body["skipped"] and "no RTL" in body["skipped"]["lint"]
    assert body["parameters"] is None


# --- packaging: the app itself is served (§13.4) ---------------------------


def test_root_negotiates_between_the_app_and_the_api(client, monkeypatch, tmp_path):
    """§10.1 fixes the API paths, and a packaged install must answer a bare URL
    with something a person can use. `fetch` gets JSON; a browser gets the app.
    """
    body = client.get("/").json()
    assert body["name"] == "veritrace"

    # With a built UI present, a browser navigation gets index.html instead.
    ui = tmp_path / "web"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>VeriTrace</title>", encoding="utf-8")
    from veritrace.api import app as app_mod

    monkeypatch.setattr(app_mod, "_ui_dir", lambda: ui)
    html = client.get("/", headers={"Accept": "text/html"})
    assert html.status_code == 200
    assert "<!doctype html>" in html.text
    # And a plain fetch still gets the JSON, so the client is unaffected.
    assert client.get("/").json()["name"] == "veritrace"

    # An explicit ask for JSON wins over `text/html`, even when both are
    # present. A browser navigation sends both, and so does `fetch` on some
    # engines — which is how the app once requested its own configuration and
    # was handed its own index page back ("Unexpected token '<'"). The answer
    # has to depend on what was asked for, not on which browser asked.
    both = client.get(
        "/", headers={"Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
    )
    assert both.json()["name"] == "veritrace"

    # Two representations behind one URL have to say so, or a browser caches
    # whichever it saw first *under the URL alone* and answers the app's own
    # fetch out of that cache — the server never asked, nothing in the access
    # log, and `Unexpected token '<'` on screen.
    assert html.headers["vary"] == "Accept"
    assert both.headers["vary"] == "Accept"
    assert "no-store" in html.headers["cache-control"]
    assert "no-store" in both.headers["cache-control"]


def test_static_files_never_shadow_an_api_route(vtx, tmp_path):
    """The catch-all is mounted after the router, so §10.1's paths always win."""
    ui = tmp_path / "web"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html>", encoding="utf-8")
    from veritrace.api import app as app_mod

    original = app_mod._ui_dir
    app_mod._ui_dir = lambda: ui
    try:
        app = app_mod.create_app(vtx)
        with TestClient(app) as c:
            sid = app.state.default_session_id
            assert c.get(f"/session/{sid}/status").json()["n_signals"] > 0
            # An unknown path falls through to the app, not to a 404.
            assert c.get("/anything/at/all", headers={"Accept": "text/html"}).status_code == 200
    finally:
        app_mod._ui_dir = original


# ---------------------------------------------------------------------------
# TAB 8 — transactions (§8.13-8.16, §11.4b)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def arb(tmp_path_factory):
    """The two-master arbiter design, served as a session would serve it."""
    work = tmp_path_factory.mktemp("api-arb")
    shutil.copytree(DESIGNS / "axi_arb", work / "axi_arb")
    design = work / "axi_arb"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    with TestClient(create_app(default_trace=design / "dump.vtx", rtl=[str(design)])) as c:
        yield c, c.get("/").json()["default_session"]


def test_status_counts_interfaces_and_opens_on_transactions(arb):
    """§11.4b: two or more interfaces means the Transactions tab."""
    client, sid = arb
    body = client.get(f"/session/{sid}/status").json()
    assert body["n_interfaces"] == 3
    assert body["n_transactions"] == 40
    assert body["default_tab"] == "transactions"
    assert body["protocol_error"] == ""


def test_transactions_endpoint_lists_interfaces_with_their_rate(arb):
    client, sid = arb
    body = client.get(f"/session/{sid}/transactions").json()
    names = {i["interface"]["name"] for i in body["interfaces"]}
    assert names == {"m0", "m1", "slv"}
    for i in body["interfaces"]:
        # §8.14's acceptance criterion, on the wire.
        assert i["correlation"] == 100.0
        assert i["interface"]["pack"] == "AXI4-Lite"
    assert body["errors"] == []


def test_transaction_query_filters_and_refines(arb):
    client, sid = arb
    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "txn(m0, type=WRITE)"})
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 10 and body["scanned"] == 10
    assert body["transactions"][0]["ref"].startswith("m0.WRITE[")

    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "txn(m0) | slowest(2)"})
    assert r.json()["n"] == 2


def test_a_bad_transaction_query_is_a_400_with_the_reason(arb):
    client, sid = arb
    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "txn(nope)"})
    assert r.status_code == 400
    assert "detected: m0, m1, slv" in r.json()["detail"]


def test_global_query_endpoint_runs_transaction_commands_too(arb):
    client, sid = arb
    r = client.post(f"/session/{sid}/query", json={"vtq": "txn(m0) | slowest(2)"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "txn"
    assert r.json()["n"] == 2


def test_why_at_transaction_level_crosses_into_the_other_master(arb):
    """§8.16, end to end over the API."""
    client, sid = arb
    r = client.post(f"/session/{sid}/query", json={"vtq": "why(txn.m0.WRITE[0].not_issued)"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "WRITE#0" in body["headline"]

    def walk(n):
        yield n
        for c in n["children"]:
            yield from walk(c)

    links = [n for n in walk(body["root"]) if n["kind"] == "txn_link"]
    assert links, "the chain never left signal level"
    assert any(n["txn"].startswith("m1.") for n in links)


def test_a_repeated_node_is_sent_once_not_re_expanded(arb):
    """The answer is a DAG; serialising it as a tree is how a 90-node chain
    becomes a megabyte of JSON."""
    client, sid = arb
    body = client.post(
        f"/session/{sid}/query", json={"vtq": "why(txn.m0.WRITE[0].not_issued)"}
    ).json()

    def walk(n):
        yield n
        for c in n["children"]:
            yield from walk(c)

    nodes = list(walk(body["root"]))
    assert any(n["repeated"] for n in nodes), "no sharing at all in this design?"
    assert all(not n["children"] for n in nodes if n["repeated"])


def test_a_naming_mistake_in_a_transaction_query_is_a_404(arb):
    client, sid = arb
    r = client.post(f"/session/{sid}/query", json={"vtq": "why(txn.nope.WRITE[0].not_issued)"})
    assert r.status_code == 404
    assert "no interface named" in r.json()["detail"]


def test_a_design_with_no_bus_reports_no_interfaces_rather_than_failing(client, session_id):
    body = client.get(f"/session/{session_id}/transactions").json()
    assert body["interfaces"] == []
    assert client.get(f"/session/{session_id}/status").json()["n_interfaces"] == 0


# ---------------------------------------------------------------------------
# TAB 9 — Performance (§8.17, §8.18)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def deadlock_api(tmp_path_factory):
    """The design with the injected deadlock, served as a session serves it."""
    work = tmp_path_factory.mktemp("api-deadlock")
    shutil.copytree(DESIGNS / "deadlock", work / "deadlock")
    design = work / "deadlock"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    with TestClient(create_app(default_trace=design / "dump.vtx", rtl=[str(design)])) as c:
        yield c, c.get("/").json()["default_session"]


def test_performance_endpoint_carries_the_whole_tab(arb):
    client, sid = arb
    body = client.get(f"/session/{sid}/performance").json()
    assert {i["iface"] for i in body["interfaces"]} == {"m0", "m1", "slv"}
    for i in body["interfaces"]:
        prof = i["stalls"]
        # §8.17's claim has to survive the round trip to JSON.
        assert sum(prof["counts"].values()) == prof["total"]
        assert sum(prof["shares"].values()) == 100.0
        assert prof["series"]
    assert body["fairness"]["index"] > 0
    assert body["errors"] == []


def test_transaction_exports_are_the_real_csv_and_parquet_tables(arb, tmp_path):
    """TAB 8 exports the production extraction, not a browser-side mock."""
    from veritrace._native import read_txn_table

    client, sid = arb
    csv_response = client.post(
        f"/session/{sid}/export", json={"kind": "csv", "target": "txn(m0)"}
    )
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.headers["content-disposition"] == 'attachment; filename="m0.csv"'
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(rows) == 10
    assert {"kind", "start_time", "start_cycle", "addr", "latency"} <= set(rows[0])
    assert {row["kind"] for row in rows} == {"WRITE"}

    parquet_response = client.post(
        f"/session/{sid}/export", json={"kind": "parquet", "target": "m0"}
    )
    assert parquet_response.status_code == 200, parquet_response.text
    assert "m0.parquet" in parquet_response.headers["content-disposition"]
    exported = tmp_path / "m0.parquet"
    exported.write_bytes(parquet_response.content)
    columns = dict(read_txn_table(str(exported)))
    assert len(columns["index"]) == 10
    assert columns["kind"] == ["WRITE"] * 10
    assert columns["start_cycle"] != columns["start_time"]


def test_transaction_export_refuses_ambiguous_or_unknown_targets(arb):
    client, sid = arb
    assert client.post(f"/session/{sid}/export", json={"kind": "csv"}).status_code == 400
    unknown = client.post(
        f"/session/{sid}/export", json={"kind": "csv", "target": "txn(nope)"}
    )
    assert unknown.status_code == 404
    assert "detected: m0, m1, slv" in unknown.json()["detail"]
    filtered = client.post(
        f"/session/{sid}/export",
        json={"kind": "csv", "target": "txn(m0, type=WRITE)"},
    )
    assert filtered.status_code == 400


def test_performance_history_endpoint_reads_the_record_database(tmp_path, monkeypatch):
    """UI history and `veritrace history` consume the same DuckDB rows."""
    monkeypatch.chdir(tmp_path)
    vtx = make_vtx(tmp_path, name="history")
    db_path = tmp_path / regress.DEFAULT_DB
    con = regress.connect(db_path)
    for commit, p99 in (("before", 8.0), ("regression", 13.0), ("after", 14.0)):
        run = regress.Run(
            seed=7,
            simulator="icarus",
            simulator_version="12.0",
            rtl_sha256="a" * 64,
            commit_sha=commit,
            command="iverilog -o sim.vvp tb.v",
        )
        run.txn.append({"iface": "dma0", "pack": "AXI4", "n": 3, "p99": p99})
        regress.record(con, run)
    con.close()

    with TestClient(create_app(default_trace=vtx)) as c:
        sid = c.get("/").json()["default_session"]
        r = c.get(
            f"/session/{sid}/performance/history",
            params={"iface": "dma0", "metric": "p99_latency"},
        )
        assert r.status_code == 200
        body = r.json()
        assert [p["commit"] for p in body["points"]] == ["before", "regression", "after"]
        assert body["regression_run_id"] == body["points"][1]["run_id"]
        assert body["delta"] == 1.0

        bad = c.get(
            f"/session/{sid}/performance/history",
            params={"iface": "dma0", "metric": "fabricated"},
        )
        assert bad.status_code == 400
        assert "unknown performance metric" in bad.json()["detail"]


def test_the_injected_deadlock_is_on_the_wire_when_the_session_opens(deadlock_api):
    """§8.18: the scan runs on open, so this is a read and not a request to
    compute."""
    client, sid = deadlock_api
    body = client.get(f"/session/{sid}/performance").json()
    dl = body["liveness"]["deadlocks"]
    assert len(dl) == 1
    assert len(dl[0]["agents"]) == 2
    for e in dl[0]["edges"]:
        assert e["holder"] and e["resource"].endswith("awready")
        assert e["why"].startswith("why(")


def test_the_deadlock_is_also_a_finding_in_checks(deadlock_api):
    """§11.4: it is not a separate kind of news."""
    client, sid = deadlock_api
    body = client.get(f"/session/{sid}/checks").json()
    found = [f for f in body["findings"] if f["group"] == "liveness"]
    assert found and found[0]["check"] == "deadlock"
    assert found[0]["severity"] == "error"


def test_performance_commands_share_the_transaction_query_endpoint(arb):
    """§10.1 is one language: `txn(m0)` and `stalls(m0)` differ in what they
    return, not in how they are written."""
    client, sid = arb
    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "stalls(m0)"})
    assert r.status_code == 200 and r.json()["kind"] == "stalls"

    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "txn(m0)"})
    assert r.status_code == 200 and r.json()["kind"] == "txn"

    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "deadlock()"})
    assert r.status_code == 200 and r.json()["deadlocks"] == []


def test_a_bad_performance_query_is_a_400_with_the_reason(arb):
    client, sid = arb
    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "stalls(nope)"})
    assert r.status_code == 400
    assert "m0" in r.json()["detail"]


# ---------------------------------------------------------------------------
# TAB 10 — Memory (§8.20)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sdram_api(tmp_path_factory):
    """The SDRAM design with four injected timing violations."""
    work = tmp_path_factory.mktemp("api-sdram")
    shutil.copytree(DESIGNS / "sdram", work / "sdram")
    design = work / "sdram"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    with TestClient(create_app(default_trace=design / "dump.vtx")) as c:
        yield c, c.get("/").json()["default_session"]


def test_a_memory_design_opens_on_the_memory_tab(sdram_api):
    """§11.4b, decided when the session is created."""
    client, sid = sdram_api
    body = client.get(f"/session/{sid}/status").json()
    assert body["default_tab"] == "memory"
    assert body["n_memory_interfaces"] == 1
    assert body["memory_error"] == ""


def test_the_memory_endpoint_carries_the_whole_tab(sdram_api):
    client, sid = sdram_api
    body = client.get(f"/session/{sid}/memory").json()
    assert len(body["interfaces"]) == 1
    iface = body["interfaces"][0]
    assert iface["chip"] == "mt48lc16m16a2"
    assert iface["n_banks"] == 4
    assert {v["constraint"] for v in iface["violations"]} == {"tRCD", "tRP", "tRFC"}
    assert iface["segments"]
    # The address map arrives as resolved bit ranges, so the browser does not
    # need an expression evaluator to decompose an address (§8.20).
    assert iface["address_map"]["bank"] == [11, 10]
    assert body["errors"] == []


def test_memory_commands_share_the_query_endpoint(sdram_api):
    """§10.1 is one language: `cmds(x)` is written like `txn(x)`."""
    client, sid = sdram_api
    for query, key in (
        ("cmds(ctrl)", "commands"),
        ("banks(ctrl)", "segments"),
        ("timing(ctrl)", "violations"),
        ("rowhits(ctrl)", "efficiency"),
    ):
        r = client.post(f"/session/{sid}/transactions/query", json={"vtq": query})
        assert r.status_code == 200, (query, r.text)
        assert key in r.json()


def test_a_bad_memory_query_is_a_400_with_the_reason(sdram_api):
    client, sid = sdram_api
    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "cmds(nope)"})
    assert r.status_code == 400
    assert "ctrl" in r.json()["detail"]


def test_timing_violations_are_findings_in_checks(sdram_api):
    client, sid = sdram_api
    body = client.get(f"/session/{sid}/checks").json()
    memory = [f for f in body["findings"] if f["group"] == "memory"]
    assert len(memory) == 3
    assert memory[0]["check"] == "memory_timing"


def test_memory_chip_selection_recomputes_all_consumers_and_survives_reopen(tmp_path):
    from veritrace.memory import timing

    dump = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "sdram" / "dump.vcd"), str(dump))
    custom = timing.find("mt48lc16m16a2").path.read_text().replace("tRCD  = 20", "tRCD  = 5")
    custom = custom.replace('name = "MT48LC16M16A2"', 'name = "Audited custom"')
    with TestClient(create_app(default_trace=dump)) as client:
        sid = client.get("/").json()["default_session"]
        base = f"/session/{sid}"
        before = client.get(base + "/memory").json()
        iface = before["interfaces"][0]["iface"]
        assert before["chips"][0]["slug"] == "mt48lc16m16a2"
        selected = client.post(base + "/memory/timing", json={"iface": iface, "toml": custom})
        assert selected.status_code == 200, selected.text
        report = selected.json()["interfaces"][0]
        assert report["chip"] == "audited_custom"
        assert {v["constraint"] for v in report["violations"]} == {"tRP", "tRFC"}
        query = client.post(base + "/query", json={"vtq": f"timing({iface})"}).json()
        assert query["violations"] == report["violations"]
        findings = client.get(base + "/checks").json()["findings"]
        assert len([f for f in findings if f["group"] == "memory"]) == 2
        # A normal Wave save does not own or erase the chosen memory timings.
        assert client.put(base + "/layout", json={"signals": []}).status_code == 200
        assert client.get(base + "/layout").json()["memoryTiming"][iface] == {"toml": custom}
        from click.testing import CliRunner
        from veritrace.cli import main

        cli = CliRunner().invoke(main, ["memory", str(dump), "--json"])
        assert cli.exit_code == 0, cli.output
        assert len(json.loads(cli.stdout)[0]["violations"]) == 2
        for invalid in ({}, {"chip": "missing"}, {"toml": "tRCD = nan"},
                        {"chip": "mt48lc16m16a2", "toml": custom}):
            response = client.post(base + "/memory/timing", json={"iface": iface, **invalid})
            assert response.status_code == 400, response.text
            assert client.get(base + "/memory").json()["interfaces"][0]["chip"] == "audited_custom"
    with TestClient(create_app(default_trace=dump)) as reopened:
        report = reopened.get(base + "/memory").json()["interfaces"][0]
        assert report["chip"] == "audited_custom"
        assert len(report["violations"]) == 2
        reset = reopened.post(base + "/memory/timing", json={"iface": iface, "chip": "mt48lc16m16a2"})
        assert reset.status_code == 200, reset.text
        assert len(reset.json()["interfaces"][0]["violations"]) == 3


def test_invalid_timing_catalog_entry_does_not_hide_usable_chips(tmp_path):
    dump = tmp_path / "dump.vtx"
    convert(str(DESIGNS / "sdram" / "dump.vcd"), str(dump))
    (tmp_path / ".veritrace.toml").write_text("[design]\n")
    (tmp_path / "timing").mkdir()
    (tmp_path / "timing" / "broken.toml").write_text("tRCD = nan")
    with TestClient(create_app(default_trace=dump)) as client:
        sid = client.get("/").json()["default_session"]
        response = client.get(f"/session/{sid}/memory")
        assert response.status_code == 200
        assert len(response.json()["chips"]) == 1
        assert "missing timing parameter" in response.json()["timing_errors"][0]


# ---------------------------------------------------------------------------
# TAB 7 — Coverage, and §8.19's scoreboard (Prompt 12)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dma_api(tmp_path_factory):
    """The DMA path with the injected byte-enable bug, served as a session
    serves it — both new tabs read from one open."""
    work = tmp_path_factory.mktemp("api-dma")
    shutil.copytree(DESIGNS / "dma", work / "dma")
    design = work / "dma"
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    with TestClient(create_app(default_trace=design / "dump.vtx", rtl=[str(design)])) as c:
        yield c, c.get("/").json()["default_session"]


def test_the_scoreboard_is_on_the_wire_when_the_session_opens(dma_api):
    """§8.19 runs on open like every other scan, so this is a read."""
    client, sid = dma_api
    body = client.get(f"/session/{sid}/integrity").json()
    assert len(body["mismatches"]) == 16
    assert {m["lane_text"] for m in body["mismatches"]} == {"0-1"}
    # What was compared, so "found nothing" could not be confused with
    # "compared nothing".
    assert body["compared_paths"] == [{"a": "dma.s_axi", "b": "mem", "writes": 8}]
    assert body["errors"] == []


def test_the_corruption_is_also_a_finding_in_checks(dma_api):
    """§11.4: corrupted data is not a separate kind of news."""
    client, sid = dma_api
    body = client.get(f"/session/{sid}/checks").json()
    found = [f for f in body["findings"] if f["group"] == "integrity"]
    assert len(found) == 16
    assert found[0]["check"] == "data_mismatch"
    assert found[0]["severity"] == "error"


def test_coverage_endpoint_carries_both_sections(dma_api):
    client, sid = dma_api
    body = client.get(f"/session/{sid}/coverage").json()
    assert {f["iface"] for f in body["functional"]} == {"dma.s_axi", "mem"}
    # No coverage database in this design, and the tab is told why rather than
    # being handed an empty section.
    assert body["code"] is None
    assert "coverage database" in body["skipped"]["code"]
    assert body["errors"] == []


def test_the_functional_matrix_reaches_the_wire_with_its_empty_cells(dma_api):
    """The cell that was never hit has to survive JSON, or the tab cannot draw
    the hole."""
    client, sid = dma_api
    body = client.get(f"/session/{sid}/coverage").json()
    cpu = next(f for f in body["functional"] if f["iface"] == "dma.s_axi")
    kind = next(p for p in cpu["points"] if p["name"] == "kind")
    assert kind["labels"] == [["READ", "WRITE"], ["READ", "WRITE"]]
    assert len(kind["cells"]) == 4
    assert kind["covered"] < kind["total"]
    partial = next(p for p in cpu["points"] if p["name"] == "partial_write")
    assert partial["covered"] == 0


def test_the_status_says_whether_the_coverage_tab_has_anything(dma_api):
    client, sid = dma_api
    assert client.get(f"/session/{sid}/status").json()["has_coverage"] is True


def test_the_new_commands_share_the_query_endpoint(dma_api):
    """§10.1 is one language: `track(...)` is written like `txn(...)`."""
    client, sid = dma_api
    for query, key in (
        ("track(addr=0x0)", "steps"),
        ("track(data=0xdeadbeef)", "steps"),
        ("scoreboard(mem)", "mismatches"),
        ("fcov(mem)", "functional"),
        ("uncovered()", "holes"),
    ):
        r = client.post(f"/session/{sid}/transactions/query", json={"vtq": query})
        assert r.status_code == 200, (query, r.text)
        assert key in r.json()


def test_a_bad_track_query_is_a_400_with_the_reason(dma_api):
    client, sid = dma_api
    r = client.post(f"/session/{sid}/transactions/query", json={"vtq": "scoreboard(nope)"})
    assert r.status_code == 400
    assert "mem" in r.json()["detail"]


def test_the_query_bar_completes_the_signal_level_table(checks_client):
    """The three §10.1 commands the dispatch table still had no line for.

    Each already had an implementation somewhere — `changed` in the store's edge
    counts, `handshake` in the extraction, `uncovered` in §8.12's holes — so the
    gap was the name, not the analysis.
    """
    client, sid = checks_client

    def run(vtq: str):
        r = client.post(f"/session/{sid}/query", json={"vtq": vtq})
        return r.status_code, r.json()

    code, body = run("changed(tb_checks.dut, c0:c40)")
    assert code == 200, body
    assert body["kind"] == "changed"
    assert body["signals"] and all(s["n_edges"] > 0 for s in body["signals"])
    # Busiest first, so the answer to "what moved" starts with what moved most.
    counts = [s["n_edges"] for s in body["signals"]]
    assert counts == sorted(counts, reverse=True)

    # The window is the point: a narrow one has to say less than a wide one,
    # which is what a range silently parsed as `None` could never do.
    narrow = run("changed(tb_checks.dut, c0:c2)")[1]["signals"]
    wide = run("changed(tb_checks.dut, c0:c300)")[1]["signals"]
    assert sum(s["n_edges"] for s in narrow) < sum(s["n_edges"] for s in wide)
    # And a scope is a scope: nothing outside it is reported.
    assert all(s["path"].startswith("tb_checks.dut") for s in wide)

    # This design has no bus, and saying so beats an empty list (P7).
    code, body = run("handshake()")
    assert code == 400 and "no protocol interface" in body["detail"]

    code, body = run("uncovered(checks_dut.sv)")
    assert code == 200, body
    assert body["kind"] == "uncovered"


def test_uncovered_query_uses_the_renderable_coverage_shape_for_fsm_edges(tmp_path):
    """An untaken diagram edge must reach TAB 7 with its real guard."""
    design = tmp_path / "fsm"
    shutil.copytree(DESIGNS / "fsm", design, ignore=shutil.ignore_patterns("*.vtx*", "sim.vvp"))
    convert(str(design / "dump.vcd"), str(design / "dump.vtx"))
    with TestClient(create_app(design / "dump.vtx", rtl=[str(design)])) as client:
        sid = client.get("/").json()["default_session"]
        response = client.post(
            f"/session/{sid}/query", json={"vtq": "uncovered(fsm_dut.sv)"}
        )
        assert response.status_code == 200, response.text
        holes = response.json()["holes"]
        transitions = [hole for hole in holes if hole["kind"] == "fsm-transition"]
        assert transitions
        first = transitions[0]
        assert first["label"] and first["text"]
        assert first["conditions"]
        assert all(condition["sampled"] > 0 for condition in first["conditions"])
        assert any(condition["ever"] is False for hole in transitions for condition in hole["conditions"])
        # The ordinary Coverage tab receives the same measured conditions even
        # before a diagram click or an uncovered() query has been made.
        reported = client.get(f"/session/{sid}/coverage").json()["holes"]
        assert first in reported


def test_a_pipeline_narrows_each_stage_to_the_one_before_it(checks_client):
    """§9.3, with the spec's own two examples.

    Every stage used to be refused outright, so the piping half of the language
    did not exist. What matters is not that a pipeline parses but that it
    *narrows*: a stage that quietly ignored its input would read the same and
    answer about the whole design.
    """
    client, sid = checks_client

    def run(vtq: str):
        r = client.post(f"/session/{sid}/query", json={"vtq": vtq})
        return r.status_code, r.json()

    everything = run("stuck(min_duration=c50)")[1]["findings"]
    code, body = run("find(*lock_r*) | stuck(min_duration=c50)")
    assert code == 200, body
    assert body["kind"] == "stuck"
    piped = body["findings"]
    assert piped, "the pipeline filtered everything away"
    assert len(piped) < len(everything), "the stage ignored its input"
    assert all("lock_r" in f["signal"] for f in piped)

    # §9.3's other example: a cone, narrowed to what actually moved (§8.6).
    code, body = run("cone(u_dut.lock_r, 3) | changed(c0:c400)")
    assert code == 200, body
    assert body["kind"] == "changed"
    cone = {n["path"] for n in run("cone(u_dut.lock_r, 3)")[1]["nodes"]}
    assert {s["path"] for s in body["signals"]} <= cone

    # A command that needs a signal of its own cannot be a stage, and says so.
    code, body = run("find(lock) | cone(x)")
    assert code == 400 and "cannot follow a `|`" in body["detail"]

    # A typed but empty SignalSet remains a valid input to the next stage.
    code, body = run("find(no_signal_can_match_this) | stuck(min_duration=c1)")
    assert code == 200, body
    assert body["kind"] == "stuck" and body["findings"] == []


def test_positional_cone_depth_affects_the_real_query(checks_client):
    """§9.3's `cone(ready, 4)` form must not silently use a default depth."""
    client, sid = checks_client

    def paths(depth: int) -> set[str]:
        response = client.post(
            f"/session/{sid}/query", json={"vtq": f"cone(u_dut.lock_r, {depth})"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["depth"] == depth
        return {node["path"] for node in body["nodes"]}

    shallow = paths(1)
    deep = paths(4)
    assert shallow < deep


def test_a_glob_in_find_is_a_glob(checks_client):
    """§9.1 makes a path segment a glob and §9.3's example is `find(*fifo*.full)`.

    The fuzzy ranking treated the star as a character to look for, so the one
    syntax the spec writes its examples in matched nothing, silently.
    """
    client, sid = checks_client

    def run(vtq: str):
        return client.post(f"/session/{sid}/query", json={"vtq": vtq}).json()

    starred = {s["path"] for s in run("find(*lock_r*)")["signals"]}
    assert starred, "a glob matched nothing"
    assert all("lock_r" in p for p in starred)
    # An anchored glob is anchored: this one has a prefix that cannot match.
    assert run("find(nosuchscope.*)")["signals"] == []


def test_a_cycle_range_parses_as_a_range():
    """§9.1's `time := INT | '@' INT | 't' INT | 'c' INT`, in a `lo:hi`.

    `c1200` is hex as far as a character class is concerned, so the range
    pattern matched it and then `int("c1200", 0)` raised and was turned into
    `None`. Every cycle range in the language therefore meant "the whole trace",
    silently — `edges(sig, c0:c400)` included.
    """
    from veritrace.analysis import vtq

    def args(q: str):
        return vtq.parse_pipeline(q).source.args

    assert args("edges(a.b, c0:c400)") == ("a.b", ("c0", "c400"))
    assert args("edges(a.b, t10:t99)") == ("a.b", ("t10", "t99"))
    assert args("edges(a.b, @10:@99)") == ("a.b", ("@10", "@99"))
    # Plain and hex endpoints still arrive as numbers, which is what an address
    # range like §10.1's `addr=0x4000:0x5000` depends on.
    assert args("edges(a.b, 10:400)") == ("a.b", (10, 400))
    assert vtq.parse_pipeline("txn(m0, addr=0x4000:0x5000)").source.kwargs == {
        "addr": (0x4000, 0x5000)
    }


def test_vtq_list_arguments_are_values_not_bracketed_check_names(checks_client):
    """The exact §9.2 `checks=[cdc,latch]` form filters the real lint path."""
    from veritrace.analysis import vtq

    parsed = vtq.parse_pipeline("lint(tb_checks.dut, checks=[cdc,latch])")
    assert parsed.source.kwargs["checks"] == ["cdc", "latch"]

    client, sid = checks_client
    response = client.post(
        f"/session/{sid}/query",
        json={"vtq": "lint(tb_checks.dut, checks=[cdc,latch])"},
    )
    assert response.status_code == 200, response.text
    findings = response.json()["findings"]
    assert findings, "the documented list syntax silently filtered every check"
    from veritrace.analysis.checks import expand_checks

    assert {finding["check"] for finding in findings} <= expand_checks(["cdc", "latch"])
