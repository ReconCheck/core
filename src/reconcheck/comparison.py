"""Programmatic API shared by the CLI, the web layer and enterprise callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .align import align
from .errors import EmptyDocumentError
from .models import Document, Finding
from .parse import load_document
from .report import build_report, build_threeway_report
from .rules import DEFAULT_RULE, Rule, evaluate, load_rules


def compare_documents(
    left: Document,
    right: Document,
    rules: str | Path | list[Rule] | None = None,
    match_on: list[str] | None = None,
    normalize: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    """Compare two in-memory documents and return ``(report, findings)``.

    Mirrors the CLI pipeline: parse -> align -> judge -> cite. This is the
    variant used by the web layer, where a document may come from an uploaded
    file or from a records-style enterprise API (see ``parse.document_from_records``).
    ``rules`` accepts a YAML file/directory path, ``None`` (built-in auto
    rule), or an already-loaded list of :class:`Rule`.
    """
    if not left.tables or not right.tables:
        raise EmptyDocumentError("one of the documents contains no table")
    lt, rt = left.tables[0], right.tables[0]
    pairs = align(lt, rt, match_on=match_on or [], normalize=normalize or {})
    rule_list = rules if isinstance(rules, list) else load_rules(rules)
    findings = evaluate(rule_list, lt, rt, pairs)
    report = build_report(left, right, findings, aligned_pairs=len(pairs))
    return report, findings


def compare_files(
    left: str | Path,
    right: str | Path,
    rules: str | Path | list[Rule] | None = None,
    match_on: list[str] | None = None,
    normalize: dict[str, str] | None = None,
    sheet: str | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    """Compare two files and return ``(report, findings)``.

    ``rules`` accepts a YAML file/directory path or None for the built-in
    auto rule.
    """
    left_doc = load_document(left, sheet=sheet)
    right_doc = load_document(right, sheet=sheet)
    return compare_documents(
        left_doc, right_doc, rules=rules, match_on=match_on, normalize=normalize
    )


def _rule_list(rules: str | Path | list[Rule] | None) -> list[Rule]:
    return rules if isinstance(rules, list) else load_rules(rules)


def _tolerance_for(field: str, rules: list[Rule]) -> tuple[float, float]:
    """Pick the tightest tolerance explicit rules declare for ``field``."""
    best: tuple[float, float] | None = None
    for rule in rules:
        cols = [rule.compare] if isinstance(rule.compare, str) else (rule.compare or [])
        if field not in cols:
            continue
        rel = float(rule.tolerance.get("relative") or 0)
        abs_ = float(rule.tolerance.get("absolute") or 0)
        if best is None or (rel + abs_ < best[0] + best[1]):
            best = (rel, abs_)
    return best or (
        float(DEFAULT_RULE["tolerance"].get("relative") or 0),
        float(DEFAULT_RULE["tolerance"].get("absolute") or 0),
    )


def _within_tolerance(a: Any, b: Any, rel: float, abs_: float) -> bool:
    from decimal import Decimal, InvalidOperation

    try:
        x, y = Decimal(str(a)), Decimal(str(b))
    except (InvalidOperation, ValueError):
        return False
    return abs(x - y) <= Decimal(str(abs_)) + Decimal(str(rel)) * max(abs(x), abs(y), Decimal(1))


def compare_three(
    docs: list[Document],
    rules: str | Path | list[Rule] | None = None,
    match_on: list[str] | None = None,
    normalize: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Three-way verification (purchase order / delivery note / invoice).

    Every unordered pair is compared with the normal two-way pipeline, and a
    ``three_way`` section reports, per shared key and field, whether the three
    documents agree (numbers use the active tolerance, text compares
    case-insensitively) and which sides are the outliers.
    """
    if len(docs) != 3:
        raise ValueError("three documents are required for three-way comparison")
    for doc in docs:
        if not doc.tables:
            raise EmptyDocumentError(f"'{doc.path}' contains no table")
    rule_list = _rule_list(rules)
    tables = [doc.tables[0] for doc in docs]
    names = [doc.kind or Path(doc.path).stem for doc in docs]
    match_on = _default_threeway_key(match_on, tables)
    normalize = normalize or {}

    pair_results: list[tuple[tuple[str, str], int, list[Finding]]] = []
    total_pair_findings = 0
    auto = Rule.from_dict(DEFAULT_RULE)
    has_auto = any(r.id == DEFAULT_RULE["id"] for r in rule_list)
    for i in range(3):
        for j in range(i + 1, 3):
            pairs = align(tables[i], tables[j], match_on=match_on, normalize=normalize)
            current = rule_list if has_auto else [*rule_list, auto]
            findings = evaluate(current, tables[i], tables[j], pairs)
            total_pair_findings += len(findings)
            pair_results.append(((names[i], names[j]), len(pairs), findings))

    conflicts = _three_way_conflicts(tables, names, match_on, normalize, rule_list)
    report = build_threeway_report(
        docs, names, pair_results, conflicts, aligned=sum(n for _, n, _ in pair_results)
    )
    return report


def _default_threeway_key(match_on: list[str] | None, tables: list[Any]) -> list[str]:
    """Resolve an empty ``match_on`` the way align() does: first common header."""
    if match_on:
        return list(match_on)
    if not tables or not tables[0].headers:
        return []
    common = set.intersection(*(set(t.headers) for t in tables)) if tables else set()
    for header in tables[0].headers:
        if header in common:
            return [header]
    return [tables[0].headers[0]]


def _three_way_conflicts(
    tables: list[Any],
    names: list[str],
    match_on: list[str],
    normalize: dict[str, str],
    rules: list[Rule],
) -> list[dict[str, Any]]:
    """Per shared key + field: do the three documents agree?"""
    from .align.keys import normalize_key
    from .align.units import convert

    keyed = []
    for table in tables:
        rows_by_key: dict[tuple[str, ...], Any] = {}
        for row in table.rows:
            cells = [row.by_header(k) for k in match_on]
            if any(cell is None for cell in cells):
                continue
            key = tuple(
                normalize_key(cell.text, normalize.get(col, "raw"))
                for cell, col in zip(cells, match_on, strict=True)
            )
            rows_by_key.setdefault(key, row)
        keyed.append(rows_by_key)

    common = set.intersection(*(set(m) for m in keyed)) if keyed else set()
    conflicts: list[dict[str, Any]] = []
    for key in sorted(common, key=lambda k: k if k else ("",)):
        rows = [m[key] for m in keyed]
        fields: set[str] = set()
        for row in rows:
            fields.update(cell.loc.header for cell in row.cells)
        for field in fields:
            if field in match_on:
                continue  # the key columns are the matching basis, not a field to verify
            cells = [row.by_header(field) for row in rows]
            if any(cell is None for cell in cells):
                continue
            texts = [(cell.text or "").strip() for cell in cells]
            values = [cell.value for cell in cells]
            if all(value is not None for value in values):
                # align units to the first side before comparing (5 kg vs 5000 g)
                units = [cell.unit for cell in cells]
                based = values[:]
                if any(u for u in units):
                    converted = [
                        (
                            convert(v, u, units[0])
                            if u and units[0] and u != units[0] and v is not None
                            else v
                        )
                        for v, u in zip(values, units, strict=True)
                    ]
                    if all(c is not None for c in converted):
                        based = converted
                rel, abs_ = _tolerance_for(field, rules)
                equal = [
                    _within_tolerance(based[a], based[b], rel, abs_)
                    for a in range(3)
                    for b in range(a + 1, 3)
                ]
                consistent = all(equal)
                if consistent:
                    continue
                # majority: index is a non-outlier if it agrees with >= 1 other
                agree = [
                    sum(
                        1
                        for b in range(3)
                        if b != a and _within_tolerance(based[a], based[b], rel, abs_)
                    )
                    for a in range(3)
                ]
                outliers = [a for a in range(3) if agree[a] == 0]
                conflicts.append(
                    {
                        "key": dict(zip(match_on, key, strict=False)) if match_on else {"line": key},
                        "field": field,
                        "values": {names[a]: texts[a] for a in range(3)},
                        "consistent": False,
                        "outlier_indices": outliers or [0, 1, 2],
                        "severity": "high" if outliers else "medium",
                    }
                )
            else:
                lowered = {t.lower() for t in texts}
                if len(lowered) == 1:
                    continue
                counts: dict[str, list[int]] = {}
                for a, t in enumerate(texts):
                    counts.setdefault(t.lower(), []).append(a)
                majority = max(counts.values(), key=len)
                outliers = [a for a in range(3) if a not in majority]
                conflicts.append(
                    {
                        "key": dict(zip(match_on, key, strict=False)) if match_on else {"line": key},
                        "field": field,
                        "values": {names[a]: texts[a] for a in range(3)},
                        "consistent": False,
                        "outlier_indices": outliers,
                        "severity": "medium",
                    }
                )
    return conflicts
