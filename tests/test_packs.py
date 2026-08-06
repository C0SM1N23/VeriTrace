"""The protocol-pack DSL — §8.14.

Two things are being defended here. The expression language must never invent a
value (an unknown input yields an unknown result, and a rule with no verdict
stays silent), and the pack loader must tell a typo apart from a design that
simply lacks an optional field. Both are the difference between a protocol
checker people trust and one they learn to ignore.
"""

from __future__ import annotations

import pytest

from veritrace.protocol import expr, pack
from veritrace.protocol.expr import ExprError, MappingEnv

# --- the expression language -----------------------------------------------


def ev(src: str, **names):
    return expr.evaluate(expr.parse(src), MappingEnv(names))


@pytest.mark.parametrize(
    "src,want",
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("0x40 >> 2", 16),
        ("4'hF", 15),
        ("3'b010", 2),
        ("8'd200", 200),
        ("1_000 + 1", 1001),
        ("10 % 4", 2),
        ("7 / 2", 3),
        ("1 < 2", 1),
        ("2 <= 2", 1),
        ("1 ? 10 : 20", 10),
        ("0 ? 10 : 20", 20),
        ("!0", 1),
        ("-5 + 5", 0),
        ("max(1, 5, 2)", 5),
        ("min(3, 1)", 1),
        ("abs(-4)", 4),
    ],
)
def test_arithmetic_matches_systemverilog_intuition(src, want):
    assert ev(src) == want


def test_dotted_and_indexed_names():
    assert ev("AW.awlen + 1", **{"AW.awlen": 15}) == 16
    assert ev("bank[2]", bank=[0, 0, 7]) == 7


def test_bit_select_and_slice_on_an_integer():
    """§8.20's command decode reads SDRAM address fields this way:
    `args = { col = "a[9:0]", ap = "a[10]" }`. `[n]` on an int is a bit-select,
    not a list subscript — the two share syntax the way SystemVerilog itself
    overloads `[]` for both, disambiguated by the runtime type of the base."""
    a = (1 << 10) | 0x1A2  # bit 10 set, bits [9:0] = 0x1A2, bit 9 clear (0x1A2 < 0x200)
    assert ev("a[10]", a=a) == 1
    assert ev("a[9]", a=a) == 0
    assert ev("a[9:0]", a=a) == 0x1A2
    assert ev("a[15:12]", a=0xABCD) == 0xA
    assert ev("a[3:0]", a=0xABCD) == 0xD


def test_bit_slice_is_unknown_not_a_crash_when_it_cannot_be_decided():
    assert ev("a[9:0]", a=None) is None  # the bus was X this cycle
    assert ev("a[0:9]", a=5) is None  # an inverted range is nonsense, not 0
    assert ev("a[-1]", a=5) is None  # a negative bit position is nonsense too


def test_string_comparison():
    assert ev('resp == "OKAY"', resp="OKAY") == 1
    assert ev('resp == "OKAY"', resp="SLVERR") == 0


# --- three-valued semantics ------------------------------------------------


def test_unknown_propagates_instead_of_becoming_zero():
    assert ev("x + 1", x=None) is None
    assert ev("x < 3", x=None) is None


def test_unknown_compares_equal_to_nothing_not_even_itself():
    """Two sampled X's are not a match. Reporting one would be a fabricated
    answer, which is exactly what P1 forbids."""
    assert ev("x == y", x=None, y=None) is None
    assert ev("x != y", x=None, y=None) is None


def test_a_controlling_value_wins_over_an_unknown():
    """`0 && x` is 0 whatever x is — the whole point of a controlling value."""
    assert ev("a && x", a=0, x=None) == 0
    assert ev("a || x", a=1, x=None) == 1
    assert ev("a && x", a=1, x=None) is None


def test_division_by_zero_is_unknown_not_an_exception():
    assert ev("a / b", a=4, b=0) is None
    assert ev("a % b", a=4, b=0) is None


# --- errors ----------------------------------------------------------------


def test_an_unknown_name_is_an_error_but_an_unknown_value_is_not():
    """A name no channel declares is a typo in the pack; a declared field the
    design lacks is normal. Only the first may raise."""
    with pytest.raises(ExprError, match="unknown name"):
        ev("nope + 1")
    assert ev("declared + 1", declared=None) is None


@pytest.mark.parametrize("bad", ["a +", "a ]", "((a)", "a ? b", "@", "a $$ b"])
def test_malformed_expressions_are_rejected_with_the_text(bad):
    with pytest.raises(ExprError) as e:
        expr.parse(bad)
    assert bad.strip()[:3] in str(e.value) or "expected" in str(e.value) or "ends early" in str(e.value)


def test_tilde_is_refused_rather_than_answered_wrongly():
    """`~x` needs a width and a pack expression carries none, so guessing one
    would silently produce a negative number."""
    with pytest.raises(ExprError, match="width"):
        expr.parse("~a")


def test_expressions_are_never_evaluated_as_python():
    """The grammar can only produce a number. There is no path to `import`."""
    with pytest.raises(ExprError):
        expr.parse("__import__('os').system('echo hi')")


# --- temporal --------------------------------------------------------------


def test_temporal_implication_is_recognised_and_not_evaluable_at_an_instant():
    node = expr.parse("awvalid && !awready |=> awvalid")
    assert expr.is_temporal(node)
    assert expr.identifiers(node) == {"awvalid", "awready"}
    with pytest.raises(ExprError, match="two cycles"):
        expr.evaluate(node, MappingEnv({"awvalid": 1, "awready": 0}))


def test_a_plain_expression_is_not_temporal():
    assert not expr.is_temporal(expr.parse("count(W) == AW.awlen + 1"))


# --- the pack schema -------------------------------------------------------

MINIMAL = """
name = "T"
version = "1"
[detect]
required_suffixes = ["valid", "ready"]
[[channel]]
name = "X"
valid = "valid"
ready = "ready"
payload = ["data"]
[[transaction]]
name = "XFER"
start = "X"
"""


def test_a_minimal_pack_loads():
    p = pack.loads(MINIMAL)
    assert p.name == "T"
    assert [c.name for c in p.channels] == ["X"]
    assert p.transactions[0].name == "XFER"
    assert not p.warnings


def test_unknown_keys_warn_but_do_not_stop_the_pack():
    """A pack written against a later version still has to load — but must not
    look like it was understood.

    The example is a whole table from a version that does not exist yet, which
    is the case this is really about: `[[stall_reason]]` used to stand here and
    stopped being a good example the moment §8.17 implemented it.
    """
    p = pack.loads(MINIMAL + '\n[[coverpoint]]\nname = "x"\nbins = 4\n')
    assert p.transactions  # still usable
    assert any("coverpoint" in w for w in p.warnings)


def test_a_stall_reason_may_not_take_a_bucket_the_engine_fills_in():
    """§8.17's shares add to 100% because the engine owns `reset`, `transfer`
    and `other`. A pack redefining one would make two rungs write the same
    bucket and the total stop meaning anything."""
    with pytest.raises(pack.PackError, match="transfer"):
        pack.loads(MINIMAL + '\n[[stall_reason]]\nname = "transfer"\nwhen = "1"\n')


@pytest.mark.parametrize(
    "body,message",
    [
        ('name = "T"\n[detect]\nrequired_suffixes=["a"]\n', "at least one"),
        (MINIMAL.replace('start = "X"', 'start = "NOPE"'), "no channel named"),
        (MINIMAL + '[[transaction]]\nname="B"\nstart="X"\nend={channel="NOPE"}\n', "no channel"),
        # No `required_suffixes` at all: such a pack would match every scope.
        (MINIMAL.replace('required_suffixes = ["valid", "ready"]', ""), "required_suffixes"),
        (MINIMAL.replace('name = "XFER"\nstart = "X"', 'name = "XFER"'), "needs `name` and `start`"),
        (MINIMAL + '\n[[rule]]\nid="R"\ncheck="1"\nseverity="loud"\n', "unknown severity"),
    ],
)
def test_structural_errors_are_fatal_and_say_what_is_wrong(body, message):
    with pytest.raises(pack.PackError, match=message):
        pack.loads(body)


def test_a_malformed_expression_is_fatal_at_load_not_at_first_use():
    """A rule that silently never fires is worse than one that refuses to load."""
    with pytest.raises(pack.PackError, match="rule R1"):
        pack.loads(MINIMAL + '\n[[rule]]\nid="R1"\ncheck="a +"\n')


def test_key_accepts_both_spellings():
    a = pack.loads(MINIMAL.replace('start = "X"', 'start = "X"\nkey = "addr = X.data"'))
    b = pack.loads(MINIMAL.replace('start = "X"', 'start = "X"\nkey = { addr = "X.data" }'))
    assert a.transactions[0].key == b.transactions[0].key == {"addr": "X.data"}


def test_duplicate_rule_ids_are_rejected():
    dup = MINIMAL + '\n[[rule]]\nid="R"\ncheck="1"\n\n[[rule]]\nid="R"\ncheck="1"\n'
    with pytest.raises(pack.PackError, match="share an id"):
        pack.loads(dup)


# --- the shipped packs -----------------------------------------------------


def test_every_shipped_pack_loads_cleanly():
    """A broken shipped pack would be invisible: detection would simply never
    match that protocol."""
    packs = {p.slug: p for p in pack.discover()}
    assert {"handshake", "axi4lite", "axi4", "sdram"} <= set(packs)
    for p in packs.values():
        assert not p.warnings, (p.slug, p.warnings)
        # §8.20's memory pack has no channels or transactions at all — it
        # decodes a command bus instead — so the two families are checked
        # against their own shape rather than a single assumption.
        if p.is_memory:
            assert p.commands and not p.channels and not p.transactions
        else:
            assert p.channels and p.transactions and not p.commands


def test_specificity_orders_the_shipped_packs_as_expected():
    """AXI4 demands more than AXI4-Lite, which demands more than a bare
    handshake — so the generic pack can never win against a specific one."""
    p = {x.slug: x.detect.specificity for x in pack.discover()}
    assert p["axi4"] > p["handshake"]
    assert p["axi4lite"] > p["handshake"]


def test_axi4_matches_the_spec_example():
    """§8.14's worked example, as shipped."""
    p = next(x for x in pack.discover() if x.slug == "axi4")
    assert [c.name for c in p.channels] == ["AW", "W", "B", "AR", "R"]
    write = next(t for t in p.transactions if t.name == "WRITE")
    assert write.start == "AW"
    assert write.body.channel == "W"
    assert write.body.count == "AW.awlen + 1"
    assert write.body.terminator == "wlast"
    assert write.end.channel == "B"
    assert write.end.match_on == "bid == AW.awid"
    assert write.id == "awid"
    # In cycles: §8.13 prints "latency: 22 cycles", and §8.17 plots it.
    assert p.metrics["latency"] == "end.cycle - start.cycle"
    ids = {r.id for r in p.rules}
    assert {"AXI_AWSTABLE", "AXI_NODROP", "AXI_WLAST", "AXI_MAXOUTSTANDING"} <= ids
    assert next(r for r in p.rules if r.id == "AXI_MAXOUTSTANDING").params == {"max": 16}


def test_temporal_and_transactional_rules_are_told_apart_by_shape():
    p = next(x for x in pack.discover() if x.slug == "axi4")
    by_id = {r.id: r for r in p.rules}
    assert by_id["AXI_AWSTABLE"].is_temporal
    assert not by_id["AXI_WLAST"].is_temporal


def test_resolve_names_a_missing_pack_helpfully():
    with pytest.raises(pack.PackError, match="available:"):
        pack.resolve(["no_such_protocol"])


def test_a_broken_pack_in_a_directory_is_reported_not_swallowed(tmp_path):
    """One bad file must not hide the other eleven — and must not vanish."""
    (tmp_path / "packs").mkdir()
    (tmp_path / "packs" / "broken.vtp.toml").write_text("name = ", encoding="utf-8")
    errors: list[str] = []
    found = pack.discover(tmp_path, errors)
    assert {"axi4", "handshake"} <= {p.slug for p in found}
    assert any("broken" in e for e in errors)


def test_a_project_pack_shadows_the_built_in_one(tmp_path):
    (tmp_path / "packs").mkdir()
    (tmp_path / "packs" / "axi4.vtp.toml").write_text(
        MINIMAL.replace('name = "T"', 'name = "House AXI"'), encoding="utf-8"
    )
    found = {p.slug: p for p in pack.discover(tmp_path)}
    assert found["axi4"].name == "House AXI"
