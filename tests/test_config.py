"""The project configuration is an executable contract, not decoration."""

from __future__ import annotations

from click.testing import CliRunner
import pytest

from veritrace import config
from veritrace.cli import main


def _write(root, text: str) -> None:
    (root / ".veritrace.toml").write_text(text, encoding="utf-8")


def test_an_omitted_reset_polarity_is_inferred_instead_of_forced_low(tmp_path):
    _write(tmp_path, '[reset]\nsignal = "top.rst"\n')
    got = config.load(tmp_path)
    assert got is not None
    assert got.reset_active is None


@pytest.mark.parametrize(
    "body, message",
    [
        ('[design]\nrtl = "rtl/*.sv"\n', "design.rtl must be an array of strings"),
        ('[reset]\nactive = "falling"\n', "reset.active must be 'low' or 'high'"),
        ('[ui]\nrow_height = "wide"\n', "ui.row_height must be"),
        ('[ui.radix]\n"*_state" = "oct"\n', "ui.radix values must be"),
        ('[checks]\nstuck_cycles = 0\n', "checks.stuck_cycles must be a positive"),
        ('[triage.patterns]\nbad = "["\n', "is not a valid regex"),
    ],
)
def test_invalid_options_are_rejected_at_the_config_boundary(tmp_path, body, message):
    _write(tmp_path, body)
    with pytest.raises(config.ConfigError, match=message):
        config.load(tmp_path)


def test_cli_surfaces_a_bad_config_without_a_traceback(tmp_path, monkeypatch):
    _write(tmp_path, '[reset]\nactive = "falling"\n')
    monkeypatch.chdir(tmp_path)
    got = CliRunner().invoke(main, ["signals"])
    assert got.exit_code == 1
    assert "Error: reset.active must be 'low' or 'high'" in got.output
    assert "Traceback" not in got.output
