"""A simulation's build inputs must travel with its exact native waveform."""

import json
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest

from conftest import make_vtx
from veritrace import TraceStore, build
from veritrace.api import create_app
from veritrace.cli import main
from veritrace.config import Config, ConfigError


@pytest.fixture
def recorded(tmp_path):
    trace = make_vtx(tmp_path)
    source = tmp_path / "tb.sv"
    source.write_text("module tb; reg clk; reg [7:0] data; endmodule\n")
    build.record(trace, [source], ["headers"], ["CHOICE=1", "CHOICE=2"], "tb", tmp_path)
    return trace, source


def test_a_manifest_restores_exact_ordered_inputs_without_mutating_config(recorded):
    trace, source = recorded
    original = Config.empty(trace.parent)
    original.defines = ["CHOICE=0"]
    restored = build.config_for(trace, TraceStore(str(trace)), original)
    assert restored is not original
    assert restored.rtl_files() == [source]
    assert restored.defines == ["CHOICE=1", "CHOICE=2"]
    assert restored.incdirs == [str(trace.parent / "headers")]
    assert original.defines == ["CHOICE=0"] and original.rtl == []
    data = json.loads((trace / build.MANIFEST).read_text())
    assert data["sources"] == ["../tb.sv"]
    assert data["incdirs"] == ["../headers"]


def test_elaboration_uses_the_last_macro_definition_like_icarus(tmp_path):
    from veritrace.graph.elaborate import elaborate

    source = tmp_path / "tb.sv"
    source.write_text("module tb; wire [`WIDTH-1:0] data; endmodule\n")
    result = elaborate([source], defines=["WIDTH=4", "WIDTH=8"], top="tb")
    assert result.graph.get("tb.data").width == 8


def test_explicit_waveform_only_and_different_rtl_are_not_overridden(recorded):
    trace, _source = recorded
    original = Config.empty(trace.parent)
    store = TraceStore(str(trace))
    assert build.config_for(trace, store, original, []) is original
    assert build.config_for(trace, store, original, [trace.parent / "other.sv"]) is original
    with TestClient(create_app(trace, rtl=[])) as client:
        sid = client.get("/").json()["default_session"]
        assert not client.get(f"/session/{sid}/status").json()["has_rtl"]


@pytest.mark.parametrize("patch", [
    {"schema": True}, {"schema": 2}, {"trace_sha256": "wrong waveform"},
    {"sources": []}, {"sources": [1]}, {"incdirs": "headers"},
    {"defines": [None]}, {"top": ""}, {"top": "bad\0top"},
])
def test_invalid_or_stale_build_fails_instead_of_using_old_options(recorded, patch):
    trace, _source = recorded
    path = trace / build.MANIFEST
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, **patch}))
    with pytest.raises(ConfigError):
        create_app(trace)
    cli = CliRunner().invoke(main, ["why", str(trace), "why(tb.data @ 10)"])
    assert cli.exit_code != 0 and "simulation build" in cli.output, cli.output


def test_unreadable_manifest_is_not_silently_treated_as_absent(recorded):
    trace, _source = recorded
    (trace / build.MANIFEST).write_text("not json")
    with pytest.raises(ConfigError, match="could not read simulation build"):
        create_app(trace)


def test_failed_atomic_replacement_preserves_the_previous_build(recorded, monkeypatch):
    trace, source = recorded
    before = (trace / build.MANIFEST).read_bytes()

    def denied(_self, _target):
        raise PermissionError("cannot replace build manifest")

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError):
        build.record(trace, [source], [], ["CHOICE=3"], "tb", trace.parent)
    assert (trace / build.MANIFEST).read_bytes() == before
    assert list(trace.glob(".build-*.tmp")) == []
