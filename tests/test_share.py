"""§13.8 — shareable sessions and annotations, and §1.8's license on output.

The team features have one hard rule behind them: *niciun feature de echipa nu
are voie sa ceara un server. Fisiere si git, nimic altceva.* So what is tested
here is mostly file behaviour — that a bundle is a plain deterministic file,
that applying one cannot silently overwrite a colleague's notes, and that a
dump which is not the shared dump is called out rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veritrace import licensing, notes, share
from veritrace.api.sessions import LayoutFile

from conftest import SMALL_VCD, make_vtx


@pytest.fixture
def vtx(tmp_path: Path) -> Path:
    return make_vtx(tmp_path)


# --- annotations ------------------------------------------------------------


def test_a_note_is_one_greppable_line():
    n = notes.Note("grant drops a cycle early", signal="top.u_dma.state", time=12500)
    assert str(n) == "top.u_dma.state @ 12500 :: grant drops a cycle early"
    assert "\n" not in str(n)


def test_both_anchor_forms_round_trip():
    src = notes.render(
        [
            notes.Note("never leaves ARB", signal="top.u_dma.state", time=12500),
            notes.Note("should test busy too", file="rtl/dma.sv", line=42),
            notes.Note("this run was with the slow SDRAM model"),
        ],
        title="dma deadlock",
    )
    got = notes.parse(src)
    assert [n.text for n in got] == [
        "never leaves ARB",
        "should test busy too",
        "this run was with the slow SDRAM model",
    ]
    assert (got[0].signal, got[0].time) == ("top.u_dma.state", 12500)
    assert (got[1].file, got[1].line) == ("rtl/dma.sv", 42)
    # No anchor is a legitimate note, not a parse failure.
    assert got[2].anchor == ""


def test_scope_syntax_in_the_text_is_not_read_as_a_separator():
    (note,) = notes.parse("rtl/x.sv:9 :: uvm_pkg::config_db never set")
    assert note.text == "uvm_pkg::config_db never set"
    assert (note.file, note.line) == ("rtl/x.sv", 9)


def test_a_typo_keeps_the_note_rather_than_losing_it():
    # A file people edit by hand must not have a syntax error that eats a line.
    (note,) = notes.parse("top.x @ notatime :: still worth keeping")
    assert "still worth keeping" in note.text


def test_append_creates_the_file_and_keeps_what_was_there(tmp_path):
    path = tmp_path / "notes" / "dma.vtnotes"
    notes.append(path, notes.Note("first", signal="a", time=1))
    notes.append(path, notes.Note("second", file="b.sv", line=2))
    assert len(notes.parse(path.read_text(encoding="utf-8"))) == 2


def test_discover_finds_the_notes_directory(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.vtnotes").write_text("x @ 1 :: a", encoding="utf-8")
    (tmp_path / "b.vtnotes").write_text("x @ 2 :: b", encoding="utf-8")
    assert [p.name for p in notes.discover(tmp_path)] == ["a.vtnotes", "b.vtnotes"]


# --- .vtsession -------------------------------------------------------------


def _layout(vtx: Path, **over) -> dict:
    lf = LayoutFile.for_trace(vtx)
    layout = lf.load()
    layout.update(
        signals=[{"kind": "signal", "handle": 0, "path": "tb.clk"}],
        cursors=[20],
        radix={"tb.data": "hex"},
        query="why(tb.dut.state @ 20)",
        **over,
    )
    return lf.save(layout)


def test_share_carries_the_screen_and_only_a_hash_of_the_dump(vtx, tmp_path):
    layout = _layout(vtx)
    bundle = share.build(vtx, layout, tmp_path, version="0.1.0")

    assert bundle["layout"]["signals"] == layout["signals"]
    assert bundle["layout"]["cursors"] == [20]
    assert bundle["query"] == "why(tb.dut.state @ 20)"
    # The dump is referenced, never carried: a trace is gigabytes and the
    # receiver already has it.
    assert len(bundle["trace"]["sha256"]) == 64
    text = json.dumps(bundle)
    assert "$enddefinitions" not in text and "b10100000" not in text


def test_two_shares_of_the_same_screen_are_byte_identical(vtx, tmp_path):
    # P1, and the reason there is no timestamp in the file: a .vtsession is
    # meant to be committed next to the bug it explains, so it must not churn.
    layout = _layout(vtx)
    a = share.write(tmp_path / "a.vtsession", share.build(vtx, layout, tmp_path))
    b = share.write(tmp_path / "b.vtsession", share.build(vtx, layout, tmp_path))
    assert a.read_bytes() == b.read_bytes()


def test_notes_travel_with_the_session(vtx, tmp_path):
    notes.append(tmp_path / "notes" / "dma.vtnotes", notes.Note("wedges here", signal="s", time=7))
    bundle = share.build(vtx, _layout(vtx), tmp_path)
    assert bundle["notes"][0]["path"] == "notes/dma.vtnotes"
    assert "wedges here" in bundle["notes"][0]["text"]


def test_restore_installs_the_layout_and_the_notes(vtx, tmp_path):
    notes.append(tmp_path / "notes" / "dma.vtnotes", notes.Note("wedges here", signal="s", time=7))
    bundle = share.build(vtx, _layout(vtx), tmp_path)

    # The colleague: same dump, empty sidecar, no notes.
    them = tmp_path / "them"
    them.mkdir()
    theirs = make_vtx(them)
    applied = share.apply(bundle, theirs, them)

    assert LayoutFile.for_trace(theirs).load()["cursors"] == [20]
    assert (them / "notes" / "dma.vtnotes").read_text(encoding="utf-8").count("wedges here") == 1
    assert applied.query == "why(tb.dut.state @ 20)"
    assert applied.warnings == []


def test_restore_does_not_overwrite_annotations_that_differ(vtx, tmp_path):
    notes.append(tmp_path / "notes" / "dma.vtnotes", notes.Note("mine", signal="s", time=7))
    bundle = share.build(vtx, _layout(vtx), tmp_path)

    them = tmp_path / "them"
    them.mkdir()
    theirs = make_vtx(them)
    notes.append(them / "notes" / "dma.vtnotes", notes.Note("theirs", signal="s", time=9))

    applied = share.apply(bundle, theirs, them)
    kept = (them / "notes" / "dma.vtnotes").read_text(encoding="utf-8")
    assert "theirs" in kept and "mine" not in kept
    assert any("already exists and differs" in w for w in applied.warnings)

    # ...unless asked, which is what makes the refusal safe to be the default.
    share.apply(bundle, theirs, them, force=True)
    assert "mine" in (them / "notes" / "dma.vtnotes").read_text(encoding="utf-8")


def test_a_different_dump_is_applied_but_called_out(vtx, tmp_path):
    bundle = share.build(vtx, _layout(vtx), tmp_path)
    them = tmp_path / "them"
    them.mkdir()
    # Same signals, one extra event: a re-run, not the shared run.
    theirs = make_vtx(them, text=SMALL_VCD + "#40\n0!\n")
    applied = share.apply(bundle, theirs, them)

    assert LayoutFile.for_trace(theirs).load()["cursors"] == [20]  # still useful
    assert any("not the one that was shared" in w for w in applied.warnings)


def test_a_bundle_from_a_future_format_is_refused_by_name(tmp_path):
    p = tmp_path / "x.vtsession"
    p.write_text(json.dumps({"vtsession": 99, "layout": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="session format 99"):
        share.read(p)


def test_provenance_is_not_shared(vtx, tmp_path):
    # It describes the sender's machine — their RTL hash against their dump.
    # Carrying it over would corrupt the receiver's §5.7 check.
    layout = _layout(vtx, provenance={"trace_sha256": "a" * 64, "rtl_sha256": "b" * 64})
    assert "provenance" not in share.build(vtx, layout, tmp_path)["layout"]


# --- §1.8 -------------------------------------------------------------------


def test_generated_checkers_carry_their_own_license(tmp_path):
    from veritrace.export import sva
    from veritrace.protocol import pack as pack_mod

    packs = Path(__file__).resolve().parents[1] / "python" / "veritrace" / "protocol" / "packs"
    pack = pack_mod.load(packs / "axi4lite.vtp.toml")
    checker = sva.render(pack, "tb.dut", target="portable", clock="aclk", reset="aresetn")
    assert f"SPDX-License-Identifier: {licensing.SPDX}" in checker.code
    # The point of §1.8 is that the answer travels with the file, so the header
    # has to say whose terms do *not* apply, not just cite an identifier.
    assert "MIT terms do not attach" in checker.code


def test_the_repository_states_both_licenses():
    root = Path(__file__).resolve().parents[1]
    assert "MIT License" in (root / "LICENSE").read_text(encoding="utf-8")
    generated = (root / "LICENSE-GENERATED").read_text(encoding="utf-8")
    assert licensing.SPDX in generated
