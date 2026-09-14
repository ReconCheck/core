from decimal import Decimal
from pathlib import Path

from conftest import make_table
from reconcheck.align import align
from reconcheck.models import Severity, Table
from reconcheck.rules import DEFAULT_RULE, Rule, evaluate, load_rules


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


def test_auto_rule_never_double_reports_explicit_columns():
    """A configured rule is the single judge for its column; auto fills gaps."""
    left = make_table(["id", "数量"], [{"id": "1", "数量": "200"}])
    right = make_table(["id", "数量"], [{"id": "1", "数量": "199"}])
    explicit = Rule(
        id="qty-rule",
        compare="数量",
        severity="high",
        tolerance={"absolute": 0, "relative": 0},
    )
    rules = [explicit, Rule.from_dict(DEFAULT_RULE)]
    pairs = align(left, right, match_on=["id"])
    findings = evaluate(rules, left, right, pairs)
    assert len(findings) == 1
    assert findings[0].rule_id == "qty-rule"
    assert [f.field for f in findings] == ["数量"]


def test_auto_rule_reports_columns_explicit_rules_leave_alone():
    left = make_table(["id", "amt"], [{"id": "1", "amt": "100.00"}])
    right = make_table(["id", "amt"], [{"id": "1", "amt": "99.40"}])
    explicit = Rule(id="other", compare="missing-column")
    rules = [explicit, Rule.from_dict(DEFAULT_RULE)]
    pairs = align(left, right, match_on=["id"])
    findings = evaluate(rules, left, right, pairs)
    assert [f.field for f in findings] == ["amt"]
    assert findings[0].rule_id == DEFAULT_RULE["id"]


# ------------------------------------------------------------------ config robustness


def test_rule_from_dict_string_match_on_is_wrapped():
    """YAML `match_on: 料号` (scalar) must not be split into single characters."""
    rule = Rule.from_dict({"id": "r", "match_on": "料号", "compare": "v"})
    assert rule.match_on == ["料号"]


def test_rule_from_dict_null_tolerance_falls_back_to_defaults():
    rule = Rule.from_dict({"id": "r", "tolerance": {"relative": None, "absolute": None}})
    assert rule.tolerance["relative"] == Decimal("0.001")
    assert rule.tolerance["absolute"] == Decimal("0.01")


def test_rule_from_dict_invalid_severity_rejected():
    try:
        Rule.from_dict({"id": "r", "severity": "urgent"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_load_rules_rejects_invalid_yaml(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("not: [valid\n", encoding="utf-8")
    try:
        load_rules(tmp_path)
        raise AssertionError("expected ValueError")
    except ValueError as err:
        assert "bad.yaml" in str(err)


def test_load_rules_rejects_rule_without_id(tmp_path: Path):
    (tmp_path / "r.yaml").write_text("severity: high\ncompare: v\n", encoding="utf-8")
    try:
        load_rules(tmp_path)
        raise AssertionError("expected ValueError")
    except ValueError as err:
        assert "r.yaml" in str(err)


def test_exception_aggregation_rounding_survives_text_exception():
    """A failing text exception must not short-circuit a later rounding exemption."""
    left = make_table(["id", "v"], [{"id": "1", "v": "10.015"}])
    right = make_table(["id", "v"], [{"id": "1", "v": "10.016"}])
    rule = Rule(
        id="r",
        compare="v",
        tolerance={"absolute": 0, "relative": 0},
        exceptions=[
            {"when": {"text_equal_ignore_case": True}},
            {"when": {"rounding": {"decimals": 2}}},
        ],
    )
    pairs = align(left, right, match_on=["id"])
    assert evaluate([rule], left, right, pairs) == []


def test_text_exception_flags_differing_text_even_within_tolerance():
    """A claiming text exception reports text differences the numbers hide."""
    left = make_table(["id", "v"], [{"id": "1", "v": "10.01"}])
    right = make_table(["id", "v"], [{"id": "1", "v": "10.03"}])
    rule = Rule(
        id="r",
        compare="v",
        tolerance={"absolute": "0.1", "relative": 0},
        exceptions=[{"when": {"text_equal_ignore_case": True}}],
    )
    pairs = align(left, right, match_on=["id"])
    assert len(evaluate([rule], left, right, pairs)) == 1

def test_rules_gb18030_file_loads(tmp_path):
    from reconcheck.rules import load_rules

    p = tmp_path / "rule.yaml"
    p.write_bytes("- id: r1\n  severity: medium\n  compare: 金额\n".encode("gb18030"))
    rules = load_rules(p)
    assert rules[0].compare == "金额"
