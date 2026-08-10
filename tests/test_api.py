"""Tests for the FastAPI server: REST routes, the waveform WebSocket, and the
two acceptance criteria of this stage.

The downsampling rule of §10.2 is tested through the real WebSocket, not just
against the underlying primitive, because the rule is a promise about what goes
on the wire.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import msgpack
import pytest
from fastapi.testclient import TestClient

from veritrace import convert
from veritrace.api import create_app
from veritrace.api.sessions import Session, SessionRegistry, session_id_for

DESIGNS = Path(__file__).resolve().parents[1] / "designs"

SMALL_VCD = """\
$timescale 1ns $end
$scope module tb $end
$var reg 1 ! clk $end
$var reg 8 " data [7:0] $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 4 # state [3:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0 "
bx #
#10
1!
b10100000 "
#20
0!
b1 #
#30
1!
"""


def make_vtx(tmp_path: Path, text: str = SMALL_VCD, name: str = "dump") -> Path:
    src = tmp_path / f"{name}.vcd"
    src.write_text(text)
    out = tmp_path / f"{name}.vtx"
    convert(str(src), str(out))
    return out


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
    assert body["session_id"]
    # tb.clk, tb.data, tb.dut.clk, tb.dut.state — clk is aliased into dut, so it
    # is two signals over one stream.
    assert body["n_signals"] == 4
    assert body["phase"] == "ready"


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


def test_search_is_deterministic(client, session_id):
    first = client.get(f"/session/{session_id}/signals", params={"q": "c"}).json()
    for _ in range(3):
        assert client.get(f"/session/{session_id}/signals", params={"q": "c"}).json() == first


def test_source_returns_404_for_now(client, session_id):
    r = client.get(f"/session/{session_id}/source/rtl/fifo.sv")
    assert r.status_code == 404
    assert "RTL" in r.json()["detail"]


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
    }
    r = client.put(f"/session/{session_id}/layout", json=layout)
    assert r.status_code == 200
    got = client.get(f"/session/{session_id}/layout").json()
    for k, v in layout.items():
        assert got[k] == v


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


def test_corrupt_layout_file_does_not_break_the_session(vtx):
    sidecar = vtx.with_name(vtx.name + ".session.json")
    sidecar.write_text("{ this is not json")
    with TestClient(create_app(default_trace=vtx)) as c:
        sid = c.get("/").json()["default_session"]
        # Falls back to defaults rather than refusing to open the trace (P7).
        assert c.get(f"/session/{sid}/layout").json()["signals"] == []


def test_session_id_is_stable_and_path_based(tmp_path, vtx):
    assert session_id_for(vtx) == session_id_for(vtx)
    assert session_id_for(vtx) != session_id_for(tmp_path)


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
    assert {v["constraint"] for v in iface["violations"]} == {"tRCD", "tRP", "tRFC", "tFAW"}
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
    assert len(memory) == 4
    assert memory[0]["check"] == "memory_timing"


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
