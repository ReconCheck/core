from conftest import make_table
from reconcheck.align import align
from reconcheck.models import Severity, Table
from reconcheck.rules import Rule, evaluate, load_rules


def _rules_dir() -> Table:
    return make_table(
        ["料号", "数量", "金额"],
        [{"料号": "A01", "数量": "5 kg", "金额": "3500.00"}],
    )


def test_rule_from_dict_matches_repo_spec():
    rule = Rule.from_dict(
        {
            "id": "po-invoice-qty-mismatch",
            "severity": "high",
            "match_on": ["line_no", "part_no"],
            "compare": "quantity",
            "tolerance": {"relative": 0, "absolute": 0},
            "evidence": {"require": "both_sides"},
        }
    )
    assert rule.id == "po-invoice-qty-mismatch"
    assert rule.severity is Severity.HIGH
    assert rule.match_on == ["line_no", "part_no"]
    assert rule.require_both_sides


def test_tolerance_absolute_and_relative():
    left = make_table(["id", "amount"], [{"id": "1", "amount": "100.00"}])
    right = make_table(["id", "amount"], [{"id": "1", "amount": "100.005"}])
    rule = Rule(id="r", tolerance={"absolute": 0.01, "relative": 0.001})
    pairs = align(left, right, match_on=["id"])
    assert evaluate([rule], left, right, pairs) == []  # 0.005 <= 0.01 + 0.001*100

    right2 = make_table(["id", "amount"], [{"id": "1", "amount": "100.20"}])
    findings = evaluate([rule], left, right2, align(left, right2, match_on=["id"]))
    assert len(findings) == 1
    assert findings[0].field == "amount"


def test_rounding_exception():
    left = make_table(["id", "v"], [{"id": "1", "v": "10.015"}])
    right = make_table(["id", "v"], [{"id": "1", "v": "10.016"}])
    rule = Rule(
        id="r",
        compare="v",
        tolerance={"absolute": 0, "relative": 0},
        exceptions=[{"when": {"rounding": {"decimals": 2}}}],
    )
    pairs = align(left, right, match_on=["id"])
    assert evaluate([rule], left, right, pairs) == []  # same at 2 dp

    strict = Rule(id="r", compare="v", tolerance={"absolute": 0, "relative": 0})
    assert len(evaluate([strict], left, right, pairs)) == 1


def test_unit_conversion_exception():
    left = make_table(["料号", "数量"], [{"料号": "A01", "数量": "5 kg"}])
    right = make_table(["料号", "数量"], [{"料号": "A01", "数量": "5000 g"}])
    rule = Rule(
        id="qty",
        compare="数量",
        exceptions=[{"when": {"unit_conversion_between": ["kg", "g"]}}],
    )
    pairs = align(left, right, match_on=["料号"])
    assert evaluate([rule], left, right, pairs) == []

    wrong = make_table(["料号", "数量"], [{"料号": "A01", "数量": "5100 g"}])
    pairs2 = align(left, wrong, match_on=["料号"])
    assert len(evaluate([rule], left, wrong, pairs2)) == 1


def test_rule_match_on_filters_unrelated_rows():
    left = make_table(
        ["k", "v"],
        [{"k": "1", "v": "10"}, {"k": "2", "v": "10"}],
    )
    right = make_table(
        ["k", "v"],
        [{"k": "1", "v": "99"}, {"k": "2", "v": "99"}],
    )
    rule = Rule(id="r", match_on=["k"], compare="v")
    pairs = align(left, right, match_on=["k"])
    findings = evaluate([rule], left, right, pairs)
    assert len(findings) == 2


def test_auto_rule_yaml_load():
    rules = load_rules("examples/rules")
    assert {r.id for r in rules} == {"po-invoice-amount-mismatch", "po-invoice-qty-unit-agnostic"}
