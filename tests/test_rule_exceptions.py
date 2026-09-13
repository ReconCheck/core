"""New exception kinds: date tolerance and case-insensitive text equality."""

from conftest import make_table
from reconcheck.align import align
from reconcheck.rules import Rule, evaluate


def _pairs(left, right, key="id"):
    return align(left, right, match_on=[key])


def test_dates_within_exempts_close_dates():
    left = make_table(["id", "日期"], [{"id": "1", "日期": "2024-09-13"}])
    right = make_table(["id", "日期"], [{"id": "1", "日期": "2024-09-15"}])
    rule = Rule(
        id="r",
        compare="日期",
        exceptions=[{"when": {"dates_within": {"days": 3}}}],
    )
    assert evaluate([rule], left, right, _pairs(left, right)) == []


def test_dates_within_reports_far_dates():
    left = make_table(["id", "日期"], [{"id": "1", "日期": "2024-09-13"}])
    right = make_table(["id", "日期"], [{"id": "1", "日期": "2024-09-20"}])
    rule = Rule(
        id="r",
        compare="日期",
        exceptions=[{"when": {"dates_within": {"days": 3}}}],
    )
    findings = evaluate([rule], left, right, _pairs(left, right))
    assert len(findings) == 1
    assert findings[0].field == "日期"
    assert findings[0].left_value == "2024-09-13"
    assert findings[0].right_value == "2024-09-20"


def test_dates_within_chinese_format_and_timestamp():
    left = make_table(["id", "日期"], [{"id": "1", "日期": "2024年9月13日"}])
    right = make_table(["id", "日期"], [{"id": "1", "日期": "2024-09-13 08:30:00"}])
    rule = Rule(
        id="r",
        compare="日期",
        exceptions=[{"when": {"dates_within": {"days": 1}}}],
    )
    assert evaluate([rule], left, right, _pairs(left, right)) == []


def test_dates_within_unparsable_falls_through_silently():
    left = make_table(["id", "日期"], [{"id": "1", "日期": "n/a"}])
    right = make_table(["id", "日期"], [{"id": "1", "日期": "2024-09-20"}])
    rule = Rule(
        id="r",
        compare="日期",
        exceptions=[{"when": {"dates_within": {"days": 3}}}],
    )
    # one side is not a date -> the exception says nothing -> numeric path skips
    assert evaluate([rule], left, right, _pairs(left, right)) == []


def test_text_equal_ignore_case_exempts():
    left = make_table(["id", "状态"], [{"id": "1", "状态": "OK"}])
    right = make_table(["id", "状态"], [{"id": "1", "状态": "ok"}])
    rule = Rule(
        id="r",
        compare="状态",
        exceptions=[{"when": {"text_equal_ignore_case": True}}],
    )
    assert evaluate([rule], left, right, _pairs(left, right)) == []


def test_text_equal_ignore_case_reports_real_difference():
    left = make_table(["id", "状态"], [{"id": "1", "状态": "OK"}])
    right = make_table(["id", "状态"], [{"id": "1", "状态": "OPEN"}])
    rule = Rule(
        id="r",
        compare="状态",
        exceptions=[{"when": {"text_equal_ignore_case": True}}],
    )
    findings = evaluate([rule], left, right, _pairs(left, right))
    assert len(findings) == 1
    assert findings[0].field == "状态"


def test_rounding_and_unit_still_work_after_verdict_refactor():
    left = make_table(["id", "v"], [{"id": "1", "v": "10.015"}])
    right = make_table(["id", "v"], [{"id": "1", "v": "10.016"}])
    rule = Rule(
        id="r",
        compare="v",
        exceptions=[{"when": {"rounding": {"decimals": 2}}}],
    )
    assert evaluate([rule], left, right, _pairs(left, right)) == []

    left2 = make_table(["料号", "数量"], [{"料号": "A01", "数量": "5 kg"}])
    right2 = make_table(["料号", "数量"], [{"料号": "A01", "数量": "5000 g"}])
    rule2 = Rule(
        id="qty",
        compare="数量",
        exceptions=[{"when": {"unit_conversion_between": ["kg", "g"]}}],
    )
    assert evaluate([rule2], left2, right2, align(left2, right2, match_on=["料号"])) == []


def test_multiple_exceptions_any_exempts():
    left = make_table(["id", "日期", "状态"], [{"id": "1", "日期": "2024-09-13", "状态": "ok"}])
    right = make_table(["id", "日期", "状态"], [{"id": "1", "日期": "2024-09-16", "状态": "OK"}])
    rule = Rule(
        id="r",
        exceptions=[
            {"when": {"dates_within": {"days": 5}}},
            {"when": {"text_equal_ignore_case": True}},
        ],
    )
    # both columns exempted: no findings
    assert evaluate([rule], left, right, _pairs(left, right)) == []
