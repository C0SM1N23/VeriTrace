"""Installed-product smoke test; run in an isolated environment with the wheel.

uv run --isolated --no-project --with <wheel> --with httpx2 python tests/wheel_smoke.py
This must not import the editable checkout. It builds fresh HDL via the public
CLI, then opens its generated waveform through the installed HTTP application.
"""

from __future__ import annotations

import json
from contextlib import chdir
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from click.testing import CliRunner
from fastapi.testclient import TestClient

import veritrace
from veritrace import _native
from veritrace.api import create_app
from veritrace.cli import main


def main_check() -> None:
    checkout = Path(__file__).resolve().parents[1]
    package = Path(veritrace.__file__).resolve().parent
    assert not package.is_relative_to(checkout / "python"), package
    assert Path(_native.__file__).suffix in (".pyd", ".so"), _native.__file__
    index = (package / "web" / "index.html").read_bytes()
    assert index == (checkout / "python/veritrace/web/index.html").read_bytes(), "stale wheel UI"
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.decode())
    assert assets
    for asset in assets:
        assert (package / "web" / asset.lstrip("/")).is_file(), asset
    assert (package / "protocol/packs/sdram.vtp.toml").is_file()
    assert (package / "protocol/packs/timing/mt48lc16m16a2.toml").is_file()
    assert (package / "export/templates/report.html.j2").is_file()

    original = Path.cwd()
    try:
        with TemporaryDirectory(prefix="veritrace wheel ") as temporary, chdir(temporary):
            work = Path(temporary)
            os.chdir(work)
            (work / "headers").mkdir()
            (work / "headers/width.vh").write_text("`define WIDTH 8\n", encoding="utf-8")
            config = work / ".veritrace.toml"
            config.write_text('[design]\ntop="tb"\nrtl=["tb.sv"]\n[design.defines]\nINITIAL=1\n', encoding="utf-8")
            configured = config.read_bytes()
            source = work / "tb.sv"
            source.write_text("""`timescale 1ns/1ps
`ifdef WIDE
  `include "width.vh"
`else
  `define WIDTH 4
`endif
module tb;
  reg clk=0;
  always #5 clk=~clk;
  reg [`WIDTH-1:0] data=0;
  initial begin #7 data=`INITIAL; #20 $finish; end
endmodule
""", encoding="utf-8")
            runner = CliRunner()
            run = runner.invoke(main, ["run", "--incdir", "headers", "-D", "WIDE", "-D", "INITIAL=42", "--json"])
            assert run.exit_code == 0, run.output
            result = json.loads(run.stdout)
            assert config.read_bytes() == configured, "run rewrote the user's configuration"
            dump = Path(result["dump"])
            assert dump.is_file()
            with TestClient(create_app(default_trace=dump)) as client:
                sid = client.get("/").json()["default_session"]
                status = client.get(f"/session/{sid}/status").json()
                assert status["has_rtl"] and not status["rtl_error"], status
                # The actual app must serve the same files that were packaged.
                page = client.get("/", headers={"Accept": "text/html"})
                assert page.status_code == 200 and page.content == index
                for asset in assets:
                    assert client.get(asset).status_code == 200, asset
                # Real simulator values, through the public query dispatcher.
                assert status["timescale"] == "1ps", status
                response = client.post(f"/session/{sid}/query", json={"vtq": "why(tb.data @ 8000)"})
                assert response.status_code == 200, response.text
                answer = response.json()
                assert answer["root"]["signal"] == "tb.data", answer
                assert answer["root"]["value"] == "00101010" and answer["root"]["width"] == 8, answer
            # A syntax error must not reuse the success artifact or return 0.
            source.write_text("module tb; not valid HDL ! endmodule", encoding="utf-8")
            failed = runner.invoke(main, ["run", str(source), "--top", "tb", "--json"])
            assert failed.exit_code != 0, failed.output
            assert "compile" in failed.output.lower() or "syntax" in failed.output.lower(), failed.output
            print(f"Installed wheel OK: {package}; native: {_native.__file__}")
            print("Fresh Icarus -> native waveform -> installed API/why -> bundled web; compile error rejected.")
    finally:
        os.chdir(original)


if __name__ == "__main__":
    main_check()
