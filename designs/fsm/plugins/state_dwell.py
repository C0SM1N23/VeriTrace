"""A worked plugin — §13.7, and the example `docs/PLUGINS.md` points at.

*How long did this design sit in each state?* A fair question that no built-in
check answers, because it is about this project rather than about RTL in
general — which is exactly the line §13.7 draws for what belongs in a plugin.

Written against `docs/PLUGINS.md` and nothing else. It is short on purpose: the
bar §16.2 sets for the API is that a new analysis takes under 45 minutes, and a
worked example that runs to two hundred lines argues the opposite.
"""

from veritrace.plugin import Analysis, register


@register
class StateDwell(Analysis):
    name = "state_dwell"
    needs = ["trace", "graph"]
    description = "Cycles spent in each state of every extracted FSM"

    def run(self, ctx):
        from veritrace.analysis import fsm

        rows = []
        # `ctx.elaboration`, not `None`: without it the extractor cannot resolve
        # `S_IDLE` to a value and finds no machines at all.
        for machine in fsm.extract(ctx.graph, ctx.elaboration, ctx.store):
            fsm.overlay(machine, ctx.store, ctx.graph, ctx.clock)
            total = sum(machine.cycles_in.values()) or 1
            for state in machine.states:
                cycles = machine.cycles_in.get(state.value, 0)
                rows.append(
                    [
                        machine.signal,
                        state.name,
                        cycles,
                        f"{100 * cycles / total:.1f}",
                        machine.visits.get(state.value, 0),
                    ]
                )
                # A state the run never entered is worth one line in Checks, not
                # only a zero in a table: it is the same fact §8.8's coverage
                # overlay draws, and somebody reading Checks should see it too.
                if not machine.visits.get(state.value, 0):
                    yield ctx.finding(
                        f"{machine.signal.rsplit('.', 1)[-1]} never entered {state.name}",
                        severity="info",
                        signal=machine.signal,
                        detail="The stimulus did not reach this state, so nothing in "
                        "the run says whether it works.",
                    )

        yield ctx.table(
            "State dwell",
            ["machine", "state", "cycles", "%", "visits"],
            rows,
        )
