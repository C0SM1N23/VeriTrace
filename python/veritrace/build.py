"""The elaboration inputs of one actual simulation, attached to its native store.

Project configuration describes the next build. This manifest describes the
build that produced a particular waveform, including transient CLI overrides.
It never rewrites project configuration or guesses options from a shell command.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from veritrace.config import Config, ConfigError

if TYPE_CHECKING:
    from veritrace import TraceStore

MANIFEST = "build.json"
SCHEMA = 1


def record(trace: Path, sources: list[Path], incdirs: list[str], defines: list[str],
           top: str, run_dir: Path) -> None:
    """Atomically record the ordered inputs next to the exact converted data."""
    from veritrace import TraceStore

    trace = trace.resolve()

    def relative(path: Path) -> str:
        try:
            return Path(os.path.relpath(path.resolve(), trace)).as_posix()
        except ValueError:  # Different Windows volumes cannot be made relative.
            return str(path.resolve())

    data = {
        "schema": SCHEMA,
        "trace_sha256": TraceStore(str(trace)).source_sha256,
        "sources": [relative(p) for p in sources],
        "incdirs": [relative(run_dir / d) for d in incdirs],
        "defines": list(defines),
        "top": top,
    }
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=trace,
                                prefix=".build-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write("\n")
        temporary.replace(trace / MANIFEST)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def config_for(trace: Path, store: TraceStore, config: Config,
               requested: list[Path] | None = None) -> Config:
    """Overlay a matching build without mutating config or unrelated RTL choices.

    ``None`` permits automatic source selection; ``[]`` explicitly disables RTL.
    Paths are store-relative so moving a complete project does not strand its
    manifest at the original developer's absolute path.
    """
    if requested == []:
        return config
    path = trace / MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config  # External waveforms legitimately have no build manifest.
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read simulation build {path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA:
        raise ConfigError(f"unsupported simulation build manifest: {path}")
    if data.get("trace_sha256") != store.source_sha256:
        raise ConfigError(f"simulation build manifest does not match this waveform: {path}")
    for key in ("sources", "incdirs", "defines"):
        values = data.get(key)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v or "\0" in v for v in values):
            raise ConfigError(f"simulation build {key} must be a list of nonempty strings: {path}")
    if not data["sources"] or not isinstance(data.get("top"), str) or not data["top"] or "\0" in data["top"]:
        raise ConfigError(f"simulation build must name its sources and top: {path}")
    sources = [(trace / value).resolve() for value in data["sources"]]
    if requested is not None and not set(sources).issubset({p.resolve() for p in requested}):
        return config  # An explicit different/narrower design is not this build.
    return replace(config, rtl=[str(p) for p in sources],
                   incdirs=[str((trace / value).resolve()) for value in data["incdirs"]],
                   defines=list(data["defines"]), top=data["top"])
