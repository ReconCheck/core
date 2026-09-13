"""Programmatic API shared by the CLI, the web layer and enterprise callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .align import align
from .errors import EmptyDocumentError
from .models import Document, Finding
from .parse import load_document
from .report import build_report
from .rules import Rule, evaluate, load_rules


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
