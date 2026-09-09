"""FSM extraction and the static checks — §8.8.

Prompt 15's acceptance criterion is one test:
`test_an_injected_dead_state_is_found_without_a_trace`. §8.8's argument for the
whole feature is that these checks find what a simulation cannot, so that test
asserts three things together — the flaw is found, **no waveform is opened**,
and the location is the exact line the bug is on.

`designs/fsm` carries one deliberate flaw per row of §8.8's table plus a clean
control machine, the way `designs/checks` does for §8.11. The control is not
decoration: a static check that fires on correct RTL gets switched off, and then
the real findings go with it. `test_the_clean_machine_produces_no_findings` is
what keeps that honest.

The regression tests at the bottom pin four bugs this module actually had, each
of which produced confident nonsense rather than an error:

* every parameter with the right value was used as a state name, so state 0 of a
  machine was labelled `START_DELAY` after an unrelated constant;
* `Driver.reset` was read as "this register is reset", but it records the reset
  in the *block's* event list — so every transition looked like a reset edge and
  all seventeen states in the reference design were reported dead;
* a `default` arm was expanded to "from any state" rather than to the complement,
  which gave every state an exit and disabled the dead-state check;
* a guard was only searched literally, so a machine whose enable is a named wire
  — which is most real RTL, including the CPU this was found on — was missed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import design_store
from click.testing import CliRunner

from veritrace import TraceStore, clocks, convert
from veritrace.analysis import fsm, fsmchecks
from veritrace.cli import main
from veritrace.graph.elaborate import discover, elaborate

DESIGNS = Path(__file__).resolve().parents[1] / "designs"
FSM_DIR = DESIGNS / "fsm"


@pytest.fixture(scope="module")
def design():
    """The reference design, elaborated. **No trace is opened.**"""
    return elaborate(discover(FSM_DIR))


@pytest.fixture(scope="module")
def machines(design):
    return fsm.extract(design.graph, design)


def one(machines, leaf: str) -> fsm.Machine:
    got = fsm.find(machines, f"tb_fsm.{leaf}.state")
    assert got is not None, [m.signal for m in machines]
    return got


def findings(design, machines) -> list:
    return list(fsmchecks.scan(design.graph, design))


# --- the acceptance criterion ----------------------------------------------


def test_an_injected_dead_state_is_found_without_a_trace(design, machines):
    """**Prompt 15's acceptance criterion.**

    `fsm_dead` traps in `S_TRAP` and never leaves. The testbench beside it never
    asserts `err`, so no simulation ever goes there and the run passes — which is
    exactly §8.8's point. Nothing in this test opens a waveform.
    """
    found = [f for f in findings(design, machines) if f.check == "fsm_dead_state"]
    trap = [f for f in found if "bad_dead" in (f.signal or "")]
    assert len(trap) == 1, [f.title for f in found]

    finding = trap[0]
    assert "S_TRAP" in finding.title
    assert finding.severity.label == "error"
    assert finding.signal == "tb_fsm.bad_dead.state"

    # The exact location: the arm that traps, not the module and not the reset.
    assert finding.loc is not None
    assert finding.loc.file == "fsm_dut.sv"
    line = FSM_DIR.joinpath("fsm_dut.sv").read_text(encoding="utf-8").splitlines()[
        finding.loc.line - 1
    ]
    assert "S_TRAP:" in line and "state <= S_TRAP" in line, line


def test_the_dead_state_is_one_a_passing_simulation_never_entered(tmp_path):
    """The other half of the claim: the dump agrees the state was never reached.

    If the stimulus *had* gone there, the finding would be unremarkable. This
    pins that it did not — so the check is genuinely seeing something no test
    could have.
    """
    from veritrace.correlate.resolver import correlate

    out = tmp_path / "fsm.vtx"
    convert(str(FSM_DIR / "dump.vcd"), str(out))
    store = TraceStore(str(out))
    el = elaborate(discover(FSM_DIR))
    # The overlay reads `trace_handle`, which correlation is what fills in.
    correlate(el.graph, {s.path: s.handle for s in store.signals()}, el.aliases)
    machine = one(fsm.extract(el.graph, el, store), "bad_dead")
    fsm.overlay(machine, store, el.graph, clocks.resolve(store, el.graph))

    trap = next(s for s in machine.states if s.name == "S_TRAP")
    assert machine.visits.get(trap.value, 0) == 0, "the stimulus did reach it after all"
    assert machine.visits.get(machine.reset_state, 0) > 0, "the machine did run"


# --- the rest of §8.8's table ----------------------------------------------


def test_the_clean_machine_produces_no_findings(design, machines):
    """A false positive is what gets a static check switched off."""
    noise = [f for f in findings(design, machines) if ".good." in (f.signal or "")]
    assert noise == [], [f"{f.check}: {f.title}" for f in noise]


def test_an_unreachable_state_is_found(design, machines):
    got = [f for f in findings(design, machines) if f.check == "fsm_unreachable_state"]
    assert len(got) == 1, [f.title for f in got]
    assert "S_ORPHAN" in got[0].title
    assert "bad_unreach" in got[0].signal


def test_a_contradictory_guard_is_found(design, machines):
    got = [f for f in findings(design, machines) if f.check == "fsm_impossible_transition"]
    assert len(got) == 1, [f.title for f in got]
    assert "bad_typo" in got[0].signal
    assert "S_WAIT" in got[0].title


def test_a_state_with_one_exit_and_no_else_is_noted(design, machines):
    got = [f for f in findings(design, machines) if f.check == "fsm_incomplete_guards"]
    assert len(got) == 1, [f.title for f in got]
    assert "bad_partial" in got[0].signal
    # `info`, because an idle state waiting for work has the same shape and is
    # correct — the two are not distinguishable from the RTL alone.
    assert got[0].severity.label == "info"


def test_a_register_left_out_of_the_reset_is_found(design, machines):
    got = [f for f in findings(design, machines) if f.check == "fsm_incomplete_reset"]
    assert len(got) == 1, [f.title for f in got]
    assert "bad_reset" in got[0].signal
    assert "count" in got[0].title
    assert "count" in " ".join(got[0].related)


def test_every_flaw_is_reported_exactly_once(design, machines):
    """Six machines, five injected flaws, and one consequence of the typo.

    The dead state on `bad_typo` is real: its only exit is the contradictory
    guard, so the state genuinely cannot be left. Two findings for one bug is
    right when both statements are true and each points somewhere useful.
    """
    got = findings(design, machines)
    assert len(got) == 6, [f"{f.check}: {f.signal}" for f in got]
    assert {f.check for f in got} == set(fsmchecks.CHECKS)


# --- extraction (§8.8 steps 1-3) -------------------------------------------


def test_a_counter_is_not_a_state_machine():
    """§8.8 step 1 is structural. `sdram_ctrl.step` is incremented, not switched
    on, and a detector that trusted the name would call it a machine."""
    el = elaborate(discover(DESIGNS / "sdram"))
    assert fsm.extract(el.graph, el) == []


def test_states_are_named_by_what_the_rtl_wrote(machines):
    """§8.8 step 2, and its limit.

    `bad_dead` uses `localparam S_IDLE/S_WORK/S_TRAP`, so the states carry those
    names. A machine that writes `2'd0` gets `2'd0` — inventing a name from any
    parameter that shares the value is how state 0 of `deadlock` was once
    labelled `START_DELAY`.
    """
    assert [s.name for s in one(machines, "bad_dead").states] == ["S_IDLE", "S_WORK", "S_TRAP"]

    el = elaborate(discover(DESIGNS / "deadlock"))
    mstate = fsm.find(fsm.extract(el.graph, el), "tb_deadlock.node0.mstate")
    assert mstate is not None
    assert [s.name for s in mstate.states] == ["2'd0", "2'd1", "2'd2", "2'd3"]


def test_a_ternary_next_state_is_two_transitions(machines):
    """`state <= err ? S_TRAP : S_IDLE` leaves S_WORK by two different edges.
    Collapsing them would invent a dead state on correct RTL."""
    machine = one(machines, "bad_dead")
    work = next(s for s in machine.states if s.name == "S_WORK")
    out = {machine.name_of(t.dst) for t in machine.transitions if t.src == work.value}
    assert out == {"S_TRAP", "S_IDLE"}


def test_the_reset_edge_is_the_one_the_reset_takes(machines):
    """Regression: `Driver.reset` records the reset in the *block's* event list,
    so every driver of an `always_ff @(posedge clk or negedge rst_n)` carries it.
    Reading that as "this is the reset branch" marked every transition in the
    design as a reset edge — and since reset edges are not escapes, all
    seventeen states in this design were reported dead."""
    machine = one(machines, "good")
    resets = [t for t in machine.transitions if t.is_reset]
    assert len(resets) == 1
    assert resets[0].src is None and resets[0].guard == "!rst_n"
    assert machine.reset_signal is not None and machine.reset_signal.endswith("rst_n")
    assert all(not t.is_reset for t in machine.transitions if t.src is not None)


def test_the_reset_term_is_not_repeated_on_every_edge(machines):
    """Every arm of the `else` branch carries `rst_n`; a diagram that shows it on
    every arrow says nothing on any of them."""
    machine = one(machines, "good")
    labels = {t.guard for t in machine.transitions if not t.is_reset}
    assert labels == {"go", "!go", "ack", "!ack", "always"}


def test_a_default_arm_becomes_the_complement_not_every_state(machines):
    """Regression: expanding `default` to "from any state" gives every state an
    outgoing edge, which silently disables the dead-state check."""
    machine = one(machines, "bad_dead")
    trap = next(s for s in machine.states if s.name == "S_TRAP")
    # The default arm covers no *known* state here — all three are labelled — so
    # it contributes nothing, and S_TRAP keeps only its self-loop.
    out = [t for t in machine.transitions if t.src == trap.value]
    assert [machine.name_of(t.dst) for t in out] == ["S_TRAP"]


def test_companions_stay_inside_their_own_instance():
    """Regression: `block_lines` are positions in a *file*, so two instances of
    one module share them exactly — matching on lines alone put `node1`'s
    registers in `node0`'s machine, and listed every one of them twice."""
    el = elaborate(discover(DESIGNS / "deadlock"))
    machine = fsm.find(fsm.extract(el.graph, el), "tb_deadlock.node0.mstate")
    assert machine is not None
    assert machine.companions == sorted(set(machine.companions))
    assert all(c.startswith("tb_deadlock.node0.") for c in machine.companions)
    assert machine.signal not in machine.companions


#: The shape almost all real RTL uses: the state test lives in a named wire, and
#: the clocked block only ever mentions the wire. Written out here rather than
#: taken from a reference design, so the test states the rule instead of relying
#: on a file that happens to have it.
_INDIRECT = """
module indirect (input clk, input rst_n, input req, input we, input ack,
                 output busy);
  localparam S_IDLE = 2'd0, S_RD = 2'd1, S_WR = 2'd2;
  reg [1:0] state_q;
  wire start = req && (state_q == S_IDLE);
  wire done  = (state_q == S_RD && ack) || (state_q == S_WR && ack);
  assign busy = (state_q != S_IDLE);
  always @(posedge clk or negedge rst_n) begin
    if (~rst_n)     state_q <= S_IDLE;
    else if (start) state_q <= we ? S_WR : S_RD;
    else if (done)  state_q <= S_IDLE;
  end
endmodule
"""


def test_a_state_test_hidden_in_a_wire_is_still_found(tmp_path):
    """Regression, and the one that mattered most.

    Real RTL writes `wire start = req && (state_q == S_IDLE);` and then
    `else if (start)`. A detector that reads the guard literally finds no machine
    at all — which is what this module did on a RISC-V CPU whose load/store unit
    is a textbook three-state FSM: zero machines, on a file with `S_IDLE` in it.
    """
    src = tmp_path / "indirect.v"
    src.write_text(_INDIRECT, encoding="utf-8")
    el = elaborate([src])
    machines = fsm.extract(el.graph, el)
    assert len(machines) == 1, [m.signal for m in machines]

    machine = machines[0]
    assert machine.signal.endswith("state_q")
    assert [s.name for s in machine.states] == ["S_IDLE", "S_RD", "S_WR"]
    edges = {(machine.name_of(t.src) if t.src is not None else "any", machine.name_of(t.dst))
             for t in machine.transitions}
    assert ("S_IDLE", "S_WR") in edges and ("S_IDLE", "S_RD") in edges

    # `done` is a disjunction where *every* branch pins a state, so the return
    # edges belong to S_RD and S_WR — not to "any state", which would be sound
    # but would draw a diagram the RTL has not got.
    assert ("S_RD", "S_IDLE") in edges and ("S_WR", "S_IDLE") in edges
    assert ("any", "S_IDLE") in edges, "the reset edge does leave every state"

    # The labels keep the wording the source used.
    assert {t.guard for t in machine.transitions if not t.is_reset} == {"(start && we)",
                                                                        "(start && !we)",
                                                                        "(!start && done)"}


def test_satisfiability_refuses_to_guess():
    """`None` means "cannot tell", and callers treat it as satisfiable. A static
    checker that invents a bug gets switched off, and the real findings go with
    it (P1)."""
    from veritrace.graph.model import Binary, Const, Ref, SignalId, Unary

    a = Ref(SignalId.parse("top.a"))
    assert fsmchecks.satisfiable(Binary("&&", a, Unary("!", a))) is False
    assert fsmchecks.satisfiable(Const(0, 1)) is False
    assert (
        fsmchecks.satisfiable(
            Binary("&&", Binary("==", a, Const(1, 2)), Binary("==", a, Const(2, 2)))
        )
        is False
    )
    assert fsmchecks.satisfiable(Binary("&&", a, Ref(SignalId.parse("top.b")))) is not False
    assert fsmchecks.satisfiable(None) is True


# --- the CLI and the route --------------------------------------------------


def test_the_command_works_with_no_trace_at_all():
    """§8.8's claim in the terminal: RTL is enough."""
    got = CliRunner().invoke(main, ["fsm", "--rtl", str(FSM_DIR)])
    assert got.exit_code == 0, got.output
    assert "S_TRAP is a dead state" in got.output
    assert "from the RTL alone" in got.output


def test_the_command_reports_the_overlay_when_there_is_a_dump():
    got = CliRunner().invoke(
        main,
        ["fsm", str(FSM_DIR / "dump.vcd"), "tb_fsm.bad_dead.state", "--rtl", str(FSM_DIR)],
    )
    assert got.exit_code == 0, got.output
    assert "NEVER VISITED" in got.output, "the overlay should say S_TRAP was not reached"
    assert "never taken" in got.output


def test_a_signal_that_is_not_a_machine_says_what_is():
    got = CliRunner().invoke(main, ["fsm", "--rtl", str(FSM_DIR), "nope"])
    assert got.exit_code != 0
    assert "no state machine on" in got.output
    assert "tb_fsm.good.state" in got.output


def test_svg_export_is_well_formed(tmp_path):
    import xml.dom.minidom

    out = tmp_path / "d.svg"
    got = CliRunner().invoke(
        main, ["fsm", "--rtl", str(FSM_DIR), "tb_fsm.bad_dead.state", "--svg", str(out)]
    )
    assert got.exit_code == 0, got.output
    text = out.read_text(encoding="utf-8")
    xml.dom.minidom.parseString(text)  # raises if it is not well formed
    assert text.count("<circle") == 3
    assert "S_TRAP" in text


def test_the_checks_appear_in_the_checks_tab():
    """§8.8: "apar direct in tab-ul Checks". Through `run_all`, like every other
    detector, so `--fail-on fsm` and `checks.disable` work on them too."""
    from veritrace.analysis import checks as checks_mod
    from veritrace.analysis.findings import Group

    el = elaborate(discover(FSM_DIR))
    report = checks_mod.run_all(store=None, graph=el.graph, elaboration=el)
    fsm_findings = [f for f in report if f.group is Group.FSM]
    assert len(fsm_findings) == 6
    assert "fsm" in checks_mod.GROUP_ALIASES
    assert checks_mod.expand_checks(["fsm"]) == set(fsmchecks.CHECKS)
    assert all(c in checks_mod.ALL_CHECKS for c in fsmchecks.CHECKS)


def test_the_fsm_route_serves_machines_and_a_diagram():
    from fastapi.testclient import TestClient

    from veritrace.api import create_app

    client = TestClient(create_app(str(design_store("fsm")), [str(FSM_DIR)]))
    sid = client.get("/", headers={"Accept": "application/json"}).json()["default_session"]

    got = client.get(f"/session/{sid}/fsm")
    assert got.status_code == 200, got.text
    machines = got.json()["machines"]
    assert len(machines) == 6
    dead = next(m for m in machines if m["signal"].endswith("bad_dead.state"))
    assert [s["name"] for s in dead["states"]] == ["S_IDLE", "S_WORK", "S_TRAP"]
    # The overlay travels with it (§8.8 step 5).
    assert dead["visits"] and "2" not in dead["visits"]

    one = client.get(f"/session/{sid}/fsm/tb_fsm.bad_dead.state")
    assert one.status_code == 200, one.text
    assert one.json()["signal"] == "tb_fsm.bad_dead.state"
    assert one.json()["transitions"] == dead["transitions"]

    svg = client.get(f"/session/{sid}/fsm/tb_fsm.bad_dead.state/svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert svg.text.startswith("<svg")
