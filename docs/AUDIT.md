# Functional audit checkpoint

Source of truth: [SPEC.md](SPEC.md). This checkpoint records the testing and
debugging overhaul, not a declaration that every specification requirement is
complete. Passing the existing suite does not certify untested requirements.
The external Siemens project was not modified.

## Runtime paths repaired and verified

| Requirement / boundary | Production path and observable behavior | Verification |
| --- | --- | --- |
| §4.0, §13.4b: build and simulate | `cli.run` → `simulate.icarus` → real `iverilog` / `vvp` → generated dump → native conversion. Source order, filelists, includes, defines, top and working directory reach the process. Compile failures, assertion failures and hangs are not successful runs. | `tests/test_run.py`: real compiler, missing includes, intentional errors, timeout, stale output, spaces in paths and JSON runs. |
| §4.3: reusable project configuration | A newly configured JSON run saves the exact source list and build options. Later CLI analysis and a fresh API session use them; omitted RTL selects configured sources while explicit `[]` retains waveform-only mode. | `test_json_run_preserves_exact_build_for_cli_rerun_and_api_elaboration`; `test_configured_rtl_is_used_on_first_open_but_explicit_empty_still_disables_it`. |
| §4.2, §6: Python/Rust and store ownership | The application loads the native extension. Source identity invalidates stale stores, live readers retain immutable generations, malformed data raises errors, and wide values survive persistence. | Rust store/property tests; `tests/test_store.py`, `tests/test_protocol.py`; installed-wheel smoke test. |
| §5, §11.6: graph/source correlation | Elaborated source identities distinguish same-named files; exact source paths and values reach Source and Inspector. | `tests/test_api.py`, `tests/test_whytrace.py`, `web/tests/causal.spec.ts`. |
| §8.7: exact divergence detection | `cli.diff` / REST / VTQ → shared alignment → native transitions → exact rational event positions. Sub-cycle pulses and the tail after the final anchor are compared, not collapsed away. Empty comparisons are errors, not equivalence. | `tests/test_diff.py`: known-time pulses, different timescales, missing signals and CLI/API/VTQ agreement. |
| §8.7: anchor matching | Exact LCS replaces approximate and positional fallbacks. Explicit manual marks are checked for count, order and observed range. | Exhaustive small LCS cases, 20,002-anchor missing-event case, real CLI/API/VTQ manual-anchor tests. |
| §11.4 Diff: connected visualization | Diff selection → API focus → both causal trees and aligned event segments → overlaid SVG with divergent regions hatched. Export contains that actual report. B-side navigation opens B's session and native timestamp. | `web/tests/diff.spec.ts`: real backend comparison, keyboard navigation, export download, wave jump and cross-session source link. |
| §11.4 Diff: error/state consistency | A failed focus request leaves the previous signal, wave and causal explanation together; retry uses the backend again. | Browser test injects only an HTTP failure between successful real comparisons. It does not substitute successful analysis data. |
| P5: state persistence | Diff anchors/options, query history, wave layout and memory timing choices survive reload. Layout writes and suppression updates are serialized within the process. | Browser reload tests; API layout concurrency and suppression tests. |
| §8.8: FSM time ownership | FSM overlays use their own clock, preserve unknown intervals and the final observed stay, and feed the canonical coverage result. | Real gated-clock tests; `tests/test_fsm.py`; `web/tests/fsm.spec.ts`. |
| §8.13–8.14: protocol extraction/cache | Interface clocks and reset masks control actual sampling. All-reset traces do not fabricate transactions. Cache schema 8 preserves channel events, payloads, missing values and wide integers. | Protocol roundtrip tests; real reset-held Icarus → fresh/cached API tests. |
| §8.17: selected-window metrics | Selected endpoints reach the backend measurement window; stale responses are discarded. Whole-run liveness is identified separately. | `tests/test_perf.py`; `web/tests/performance.spec.ts`. |
| §8.20: timing semantics | tFAW checks the fifth ACT, tREFI is a maximum, fractional minimum limits round up, and gated clocks retain real event ordinals. Unobserved checks are not reported as conformant. | `tests/test_memory.py`; real Icarus gated-clock and refresh-only CLI/API tests. |
| §8.20 / §11.4 Memory: timing selection | Built-in or uploaded TOML → validation → recomputed memory findings → Checks/VTQ/CLI → persisted session choice. Refresh rectangles use actual command intervals and navigate to Wave. | API chip selection/reopen tests; `web/tests/memory.spec.ts`, including fresh HDL compiled by the public CLI. |
| §8.20: address inspection | BigInt bit extraction preserves low bits beyond Number precision. Invalid addresses are rejected. Sequence classification is explicitly hypothetical open-page traffic, not simulated measurements. | Address unit tests and browser checks against the SDRAM pack's actual `[9:1]` column mapping. |
| §9–10: command reachability | Query bar, REST and WebSocket use the production dispatcher for signal, transaction, memory, performance, coverage and counterfactual queries. | `tests/test_api.py`; keyboard, causal and domain browser suites. |
| §12–13: exports, extensions and regression state | Reports, probes, plugin tables/findings and stored regression metrics consume actual analysis results; missing data is surfaced. | Export/plugin/regression/repro Python tests and browser plugin/replay/share tests. |
| Installed product | Built wheel contains its native extension, current web assets, protocol packs, timing tables and report templates. Fresh HDL runs through the installed CLI and API; a subsequent syntax error fails. | `tests/wheel_smoke.py`, run outside the editable installation. |

## Significant ghost/fake-success paths removed

- JSON runs no longer omit configuration needed by the next process; API
  configuration no longer stops at a parsed but unused RTL list.
- Cycle-rounded Diff events no longer hide real short pulses. The displayed
  value comes from the divergence time, not the beginning of its cycle.
- Large anchor sets no longer silently switch to incorrect positional matching.
- Changing the selected divergence no longer leaves the first signal's causal
  explanation on screen. A request failure cannot relabel an old answer.
- Reset-held interfaces no longer extract plausible-looking traffic, and cache
  loading no longer loses exact protocol events or payload semantics.
- Four legal activates no longer produce a false tFAW violation. Absence of a
  qualifying timing observation is not evidence of compliance.
- Native conversion, simulation and exported verification preserve failures
  instead of substituting empty results or successful exit codes.

## Remaining limits and follow-up scope

These items are **not certified complete** by this overhaul:

- Existing project config plus per-invocation `-D` / include overrides still needs
  a per-run build manifest so later elaboration cannot return to older options.
- Memory checks without observed DQ cannot establish full CL/CWL/write-recovery
  semantics; automatic precharge is qualified rather than fully modeled.
  Command occupancy must not be mistaken for measured data-bus utilization.
- Coverage module treemap/drilldown and cumulative-view controls still need
  complete UI traceability. Memory metric-series presentation is also partial.
- Diff transaction payload completeness, simultaneous handshake anchors and
  one-sided interfaces need additional semantic verification.
- Layout coordination is in-process, not a multi-process lock. Autosave error
  presentation and cache filename collisions remain follow-up items.
- Native FST is unavailable in the Windows wheel and is reported as such;
  Linux/macOS use the platform-gated reader. This checkpoint's commands were
  run on Windows, not every supported simulator/platform combination.
- The exhaustive requirement-by-requirement matrix for the entire specification
  is unfinished. No overall completion percentage is claimed.

## Reproducible checks

From the repository root, unless stated otherwise:

- `uv run --no-sync pytest -q -W error --junitxml=.veritrace/audit-python.xml`
  — 946 passed, no skips.
- `cargo test --workspace` — 88 passed; Windows has no native FST test cases.
- `cargo fmt --all -- --check` and
  `cargo clippy --workspace --all-targets -- -D warnings` — passed.
- `uvx ruff check python tests --select F` — passed (the repository's CI gate).
  Broader inherited Ruff rules also report style/annotation warnings; this is
  not a claim that every optional Ruff rule is clean, and no blanket suppression
  was added to make that claim.
- In `web/`: `npm run build` — strict TypeScript and production build passed;
  `npm test` — 48 passed.
- Backend: `uv run --no-sync veritrace serve designs/fifo_buggy/dump.vtx --rtl designs/fifo_buggy --port 8765 --no-browser`.
  In `web/`, set `VERITRACE_TEST_ORIGIN=http://127.0.0.1:8765`, then run
  `npx playwright test --grep-invert "sustains 60fps" --max-failures=3`
  — 142 functional browser tests passed. The 60-fps threshold is excluded at
  the user's request; functional performance-analysis tests remain enabled.
- `uv build --wheel --out-dir dist` — passed.
- `uv run --isolated --no-project --with K:/VeriTrace/dist/veritrace-0.1.0-cp312-cp312-win_amd64.whl --with httpx2 python tests/wheel_smoke.py`
  — passed using the installed `.pyd`, real Icarus output and bundled assets.

Keep subsequent changes bounded: reproduce a defect, fix that path, add a
regression test, and rerun affected plus product-level checks before expanding
the audit. These results close this implementation batch, not all open items.
