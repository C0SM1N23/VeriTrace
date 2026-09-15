"""`veritrace.tools.run_capture` — the one place an external tool is started.

Every simulator and synthesiser this project runs goes through here, and the
reason it exists rather than `subprocess.run(capture_output=True, timeout=...)`
is that the stock call cannot reliably stop one. Installing Icarus on the
Windows CI runner turned that from theory into a three-hour job that ended with
"the hosted runner lost communication with the server": a testbench with no
`$finish`, a three-second timeout, and a simulation that went on writing its
waveform long after Python had stopped waiting for it.

The tests below use Python itself as the tool, so the behaviour is proved on
every platform the package is built for rather than only where Icarus is.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from veritrace.tools import run_capture


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_output_and_status_come_back_like_subprocess_run():
    got = run_capture(
        _python("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
    )
    assert got.returncode == 3
    assert "out" in got.stdout
    assert "err" in got.stderr


def test_a_timeout_raises_with_what_the_tool_had_already_printed():
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_capture(
            _python("import sys, time; print('started'); sys.stdout.flush(); time.sleep(30)"),
            timeout=2,
        )
    # The point of collecting through files rather than pipes: the output is
    # still there after the kill, so the caller can say what the tool was doing.
    assert "started" in (raised.value.output or "")


def test_a_timeout_ends_the_whole_tree_not_just_the_process_python_started(tmp_path: Path):
    """The CI failure, in miniature.

    `iverilog` drives `ivlpp` and `ivl`, and a package manager may install a
    launcher that runs the real binary as a child. `Popen.kill()` reaches
    neither — so the grandchild here keeps appending to `heartbeat` until
    something kills it too.
    """
    beat = tmp_path / "heartbeat"
    inner = tmp_path / "grandchild.py"
    inner.write_text(
        "import time\n"
        "while True:\n"
        "    open({path!r}, 'a').write('.')\n"
        "    time.sleep(0.05)\n".format(path=str(beat)),
        encoding="utf-8",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture(
            _python(
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, {path!r}]); "
                "time.sleep(60)".format(path=str(inner))
            ),
            timeout=5,
        )

    assert beat.exists(), "the grandchild never started, so nothing was proved"
    settled = beat.stat().st_size
    time.sleep(1.0)
    assert beat.stat().st_size == settled, "the grandchild outlived the timeout"


def test_the_tool_gets_no_stdin_to_wait_on():
    """`vvp` opens a prompt on `$stop` and reads from it. A tool that inherits a
    console nobody will type into hangs with no timeout attached at all."""
    got = run_capture(_python("import sys; print(len(sys.stdin.read()))"), timeout=30)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "0"


def test_a_missing_program_raises_oserror_rather_than_reporting_a_failure():
    with pytest.raises(OSError):
        run_capture(["veritrace-no-such-tool-4d7f"], timeout=10)
