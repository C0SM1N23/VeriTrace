"""Sentences for the causal chain — §11.5.

Replay mode reads the bug out loud, one subtrace event at a time. §11.5 is
specific about where the words come from: **templates keyed by `NodeKind` +
`reason`, about twelve of them, not free prose and not a language model.** The
reason is not stylistic. A generated sentence that paraphrases can drift from
the tree it describes, and the whole value of replay is that walking the chain
in chronological order is how you *check* the chain. A template can only ever
restate what the node already says.

So the table below is the complete vocabulary of the feature. Every reason in
`whytrace.Reason` has an entry; a new reason without one shows up immediately as
the fallback line rather than as a plausible-sounding invention.

The same table serves the HTML report (§12), which needs one sentence for the
symptom and one per chain step. Two renderers of one wording, rather than two
wordings.
"""

from __future__ import annotations

from typing import Any

from veritrace.repro.subtrace import Event, Subtrace

#: `{sig}` signal path, `{val}` value, `{t}` time as a cycle or timestamp,
#: `{loc}` `file:line`, `{detail}` the guard or value expression from the node.
#:
#: Keyed by `reason`, which is what actually varies — `NodeKind` and `reason`
#: agree everywhere except HOLD/CONFLICT, and those have reasons of their own.
TEMPLATES: dict[str, str] = {
    # The two structural answers of §8.1.
    "assigned": "{sig} takes {val} at {t}: a driver was enabled and assigned it.",
    "hold": "{sig} stays {val} at {t}: no driver was enabled, so it kept the value it had.",
    "conflict": "{sig} is driven by more than one source at {t}, so {val} is whichever won.",
    # Terminals that answer the question (§8.1).
    "primary_input": "{sig} is {val} because it is driven from outside the design.",
    "constant": "{sig} is {val} for the whole run — the RTL ties it off.",
    "undriven": "{sig} is {val} and nothing in the design drives it.",
    "blackbox_ip": "{sig} comes out of {detail}, which has no source to look inside.",
    # §8.5's X terminals — where an unknown was created rather than inherited.
    "uninitialized_reg": "{sig} is {val} because it has no reset and was never written.",
    "unconnected_port": "{sig} is {val} because the instance port was left unconnected.",
    "out_of_range": "{sig} is {val} because an index went outside the declared bounds.",
    "x_from_arith": "{sig} is {val} because it was computed from an unknown operand.",
    "tristate_z": "{sig} is floating at {t}: no enable was driving the net.",
    "unknown_x": "{sig} is {val} at {t}, and the trace does not say where that came from.",
    # §8.16 — the chain crossed into the transaction layer.
    "txn_open": "{detail}",
    "txn_in_flight": "{detail}",
    # Walk limits. Not answers, and they say so.
    "cycle": "{sig} feeds back into itself; the walk stopped here rather than loop.",
    "depth_limit": "The walk hit its depth limit at {sig}; the cause is further upstream.",
    "not_traced": "{sig} is neither in the trace nor derivable from the RTL.",
}

#: Used when a reason has no entry. Deliberately dull, and it names the reason
#: so the gap is obvious rather than papered over.
FALLBACK = "{sig} = {val} at {t} ({reason})."


def _time(event: Event) -> str:
    return f"c{event.cycle}" if event.cycle is not None else str(event.time)


def describe(event: Event) -> str:
    """One sentence for one subtrace event."""
    template = TEMPLATES.get(event.reason, FALLBACK)
    text = template.format(
        sig=event.signal,
        val=event.value,
        t=_time(event),
        loc=f"{event.loc['file']}:{event.loc['line']}" if event.loc else "",
        detail=event.detail or event.reason.replace("_", " "),
        reason=event.reason,
    )
    # §8.2's step 2 is a fact worth stating: a value established long before the
    # question is the shape most stuck bugs have, and the sentence above reads
    # as if it happened now.
    if event.kind == "held" and event.reason not in ("constant", "primary_input"):
        text += f" It has been {event.value} since {_time(event)}."
    return text


def narrate(sub: Subtrace) -> Subtrace:
    """Fill in `Event.text` for every event, in place, and return the subtrace."""
    for event in sub.events:
        event.text = describe(event)
    return sub


def symptom_paragraph(sub: Subtrace, query: str = "") -> str:
    """The §12 header paragraph: what broke, and what it came down to.

    Two sentences at most. A bug report that opens with a wall of text is one
    nobody reads past, and everything else is below it anyway.
    """
    symptom = sub.symptom
    if symptom is None:
        return query or "No causal chain."
    first = f"{symptom.signal} is {symptom.value} at {_time(symptom)}."
    cause = sub.root_cause
    if cause is None or cause is symptom:
        if not sub.reached_root_cause:
            return first + " The walk did not reach a terminal that explains it."
        return first
    return first + " " + describe(cause)


def headline(sub: Subtrace) -> str:
    """A one-line title for a report or a session — `ready stuck at 0`."""
    symptom = sub.symptom
    if symptom is None:
        return "causal chain"
    name = symptom.signal.rsplit(".", 1)[-1]
    verb = "stuck at" if symptom.kind == "held" else "="
    return f"{name} {verb} {symptom.value}"


def steps(sub: Subtrace) -> list[dict[str, Any]]:
    """Replay steps, root cause first — §11.5 replays the bug forwards.

    The tree is built backwards from the symptom; the story runs the other way.
    Sorting by time does both, since the cause is by definition the earlier
    event.
    """
    ordered = sorted(sub.events, key=lambda e: (e.time, e.signal))
    return [
        {**event.to_dict(), "text": event.text or describe(event), "step": i + 1}
        for i, event in enumerate(ordered)
    ]
