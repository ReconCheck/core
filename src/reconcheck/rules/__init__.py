"""Differential rule engine.

A rule is YAML that says: on aligned row pairs, compare these columns, and
report a finding when the difference exceeds tolerance *and* no exception
applies. The format is the subset of the spec in the private
ReconCheck/rules repo that the engine implements today (see DESIGN.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..align import AlignedPair
from ..align.units import convert
from ..models import Cell, Evidence, Finding, Severity, Table

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y年%m月%d日")

DEFAULT_RULE: dict[str, Any] = {
    "id": "auto-numeric-diff",
    "severity": "medium",
    "tolerance": {"relative": 0.001, "absolute": 0.01},
    "evidence": {"require": "both_sides"},
}


@dataclass
class Rule:
    """One differential rule."""

    id: str
    severity: Severity = Severity.MEDIUM
    match_on: list[str] = field(default_factory=list)
    compare: str | None = None
    tolerance: dict[str, Decimal] = field(
        default_factory=lambda: {"relative": Decimal("0.001"), "absolute": Decimal("0.01")}
    )
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    require_both_sides: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        value = str(data.get("severity", "medium")).lower()
        if value not in ("high", "medium", "low"):
            raise ValueError(
                f"invalid severity {value!r} in rule {data.get('id', '?')!r} "
                "(expected high | medium | low)"
            )
        sev = Severity(value)
        tolerance = data.get("tolerance") or {}
        evidence = data.get("evidence") or {}
        match_on = data.get("match_on") or []
        if not isinstance(match_on, list):
            match_on = [match_on]  # YAML `match_on: 料号` must not split into 单/字
        return cls(
            id=str(data["id"]),
            severity=sev,
            match_on=[str(x) for x in match_on],
            compare=data.get("compare"),
            tolerance={
                "relative": _tol(tolerance.get("relative"), "0.001"),
                "absolute": _tol(tolerance.get("absolute"), "0.01"),
            },
            exceptions=list(data.get("exceptions") or []),
            require_both_sides=evidence.get("require") == "both_sides",
        )


def _tol(raw: Any, default: str) -> Decimal:
    """Coerce a tolerance value, tolerating explicit nulls and junk (-> default)."""
    if raw is None:
        return Decimal(default)
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001 - malformed tolerance falls back to default
        return Decimal(default)


def _read_rule_file(path: Path) -> str:
    """Read a rule file, falling back to GB18030 when a Windows-spawned file
    is not valid UTF-8 (previously such configs hard-failed)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030")


def load_rules(source: str | Path | None = None) -> list[Rule]:
    """Load rules from a YAML file or a directory of ``*.yaml`` files.

    ``None`` yields the built-in auto rule (any shared numeric column,
    tolerance relative 0.001 / absolute 0.01).
    """
    if source is None:
        return [Rule.from_dict(DEFAULT_RULE)]
    p = Path(source)
    if p.is_dir():
        # glob() is case-insensitive on Windows: "RULES.YAML" matches both
        # patterns, so resolve once and dedupe before parsing.
        files = sorted({f.resolve() for f in p.glob("*.yaml")} | {f.resolve() for f in p.glob("*.yml")})
    elif p.is_file():
        files = [p]
    else:
        raise FileNotFoundError(f"rules source not found: {source}")
    rules: list[Rule] = []
    for f in files:
        try:
            text = _read_rule_file(f)
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as err:
            raise ValueError(f"invalid rule YAML in '{f}': {err}") from err
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if item is None:
                continue
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError(
                    f"'{f}' must contain a rule mapping (or a list of them) with an 'id'"
                )
            try:
                rules.append(Rule.from_dict(item))
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError(f"invalid rule in '{f}': {err}") from err
    return rules


def evaluate(
    rules: list[Rule],
    left_table: Table,
    right_table: Table,
    pairs: list[AlignedPair],
) -> list[Finding]:
    """Run every rule over the aligned pairs and collect findings.

    The built-in auto rule never double-reports a column that an *applying*
    explicit rule already covers for the same pair — so a configured rule set
    (e.g. ``数量`` with kg/g exemption) stays the single judge for its
    columns while the auto rule fills the gaps elsewhere.
    """
    auto_id = DEFAULT_RULE["id"]
    explicit = [rule for rule in rules if rule.id != auto_id]
    findings: list[Finding] = []
    for rule in rules:
        for pair in pairs:
            if not _rule_applies(rule, pair):
                continue
            if rule.id == auto_id and explicit:
                covered = _explicit_covered(explicit, pair)
                if covered is None:
                    continue  # an explicit rule claims every column
                findings.extend(_diff_pair(rule, pair, skip=covered))
            else:
                findings.extend(_diff_pair(rule, pair))
    return findings


def _explicit_covered(explicit: list[Rule], pair: AlignedPair) -> set[str] | None:
    """Headers an *applying* explicit rule claims; ``None`` means 'all'."""
    covered: set[str] = set()
    for rule in explicit:
        if not _rule_applies(rule, pair):
            continue
        if rule.compare:
            columns = [rule.compare] if isinstance(rule.compare, str) else list(rule.compare)
            covered.update(columns)
        else:
            return None
    return covered


def _rule_applies(rule: Rule, pair: AlignedPair) -> bool:
    return all(
        pair.left.by_header(col) is not None and pair.right.by_header(col) is not None
        for col in rule.match_on
    )


def _compare_columns(
    rule: Rule, pair: AlignedPair, skip: set[str] | None = None
) -> Iterable[tuple[str, Cell, Cell]]:
    """Yield ``(header, left_cell, right_cell)`` triples for one pair."""
    if rule.compare:
        headers = [rule.compare] if isinstance(rule.compare, str) else list(rule.compare)
        for header in headers:
            if skip and header in skip:
                continue
            left_cell = pair.left.by_header(header)
            right_cell = pair.right.by_header(header)
            if left_cell is not None and right_cell is not None:
                yield header, left_cell, right_cell
        return
    # auto mode: every shared header that has numbers on both sides
    for cell in pair.left.cells:
        header = cell.loc.header
        if skip and header in skip:
            continue
        right_cell = pair.right.by_header(header)
        if right_cell is not None and cell.value is not None and right_cell.value is not None:
            yield header, cell, right_cell


def _diff_pair(rule: Rule, pair: AlignedPair, skip: set[str] | None = None) -> list[Finding]:
    out: list[Finding] = []
    for header, left_cell, right_cell in _compare_columns(rule, pair, skip=skip):
        verdict = _exception_verdict(rule.exceptions, left_cell, right_cell)
        if verdict == "exempt":
            continue
        if verdict == "text_differs":
            # a date/text exception declared interest and the values differ:
            # report it even though the column is not numeric
            if rule.require_both_sides and (not left_cell.loc.row or not right_cell.loc.row):
                continue
            claim = f"{header}: {left_cell.text.strip()} vs {right_cell.text.strip()} on key {pair.key!r}"
            out.append(_make_finding(rule, header, left_cell, right_cell, claim))
            continue
        if left_cell.value is None or right_cell.value is None:
            continue
        if _within_tolerance(left_cell.value, right_cell.value, rule.tolerance):
            continue
        claim = (
            f"{header}: {left_cell.text.strip()} vs {right_cell.text.strip()} on key {pair.key!r}"
        )
        out.append(_make_finding(rule, header, left_cell, right_cell, claim))
    return out


def _make_finding(
    rule: Rule, header: str, left_cell: Cell, right_cell: Cell, claim: str
) -> Finding:
    return Finding(
        id=uuid.uuid4().hex[:10],
        rule_id=rule.id,
        severity=rule.severity,
        claim=claim,
        field=header,
        left_value=left_cell.text.strip(),
        right_value=right_cell.text.strip(),
        evidence=[
            Evidence(side="left", loc=left_cell.loc, excerpt=left_cell.text.strip()),
            Evidence(side="right", loc=right_cell.loc, excerpt=right_cell.text.strip()),
        ],
    )


def _within_tolerance(left: Decimal, right: Decimal, tolerance: dict[str, Any]) -> bool:
    t_abs = Decimal(str(tolerance.get("absolute", "0")))
    t_rel = Decimal(str(tolerance.get("relative", "0")))
    scale = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) <= t_abs + t_rel * scale


def _exception_verdict(exceptions: list[dict[str, Any]], left: Cell, right: Cell) -> str:
    """Classify an exception match for one cell pair.

    Returns ``"exempt"`` when an exception swallows the difference,
    ``"text_differs"`` when a date/text exception applies but the values
    genuinely differ (so a non-numeric column can still produce a finding),
    or ``""`` when no exception cares.

    Every exception is evaluated before the verdict is decided: one
    exception yielding ``exempt`` wins over another yielding
    ``text_differs``, so e.g. a rounding exemption is not skipped just
    because a case-insensitive text check ran first.
    """
    any_text_differs = False
    for exc in exceptions:
        when = exc.get("when") if isinstance(exc, dict) else None
        if not when:
            continue
        if when.get("text_equal_ignore_case"):
            lt = (left.text or "").strip()
            rt = (right.text or "").strip()
            if not lt or not rt:
                continue
            if lt.lower() == rt.lower():
                return "exempt"
            any_text_differs = True
            continue
        if "dates_within" in when:
            ld = _parse_date(left.text)
            rd = _parse_date(right.text)
            if ld is None or rd is None:
                continue
            try:
                days = max(0, int(when["dates_within"].get("days", 3)))
            except (TypeError, ValueError):
                days = 3
            if abs((ld - rd).days) <= days:
                return "exempt"
            any_text_differs = True
            continue
        if "rounding" in when:
            if left.value is None or right.value is None:
                continue
            try:
                decimals = int(when["rounding"].get("decimals", 2))
            except (TypeError, ValueError):
                decimals = 2
            quantum = Decimal(f"1e-{max(0, decimals)}")
            if left.value.quantize(quantum) == right.value.quantize(quantum):
                return "exempt"
            continue
        if "unit_conversion_between" in when:
            allowed = when["unit_conversion_between"]
            if left.value is None or right.value is None or len(allowed) != 2:
                continue
            if _units_covered(allowed, left.unit, right.unit):
                converted = convert(right.value, right.unit, left.unit)
                if converted is not None and _within_tolerance(
                    left.value,
                    converted,
                    {"relative": Decimal("0.001"), "absolute": Decimal("0.0001")},
                ):
                    return "exempt"
    return "text_differs" if any_text_differs else ""


def _parse_date(text: str | None):
    """Parse common date layouts (ISO, slash, dotted, compact, Chinese)."""
    t = (text or "").strip()
    if not t:
        return None
    for sep in (" ", "T"):
        if sep in t:
            t = t.split(sep)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _units_covered(allowed: list[Any], a: str | None, b: str | None) -> bool:
    from ..align.units import canonical

    expected = {canonical(str(x)) for x in allowed}
    return canonical(a) in expected and canonical(b) in expected
