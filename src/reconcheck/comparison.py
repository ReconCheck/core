"""Programmatic API shared by the CLI, the web layer and enterprise callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .align import align
from .errors import EmptyDocumentError
from .models import Finding
from .parse import load_document
from .report import build_report
from .rules import evaluate, load_rules


def compare_files(
    left: str | Path,
    right: str | Path,
    rules: str | Path | None = None,
    match_on: list[str] | None = None,
    normalize: dict[str, str] | None = None,
    sheet: str | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    """Compare two files and return ``(report, findings)``.

    Mirrors the CLI pipeline: parse -> align -> judge -> cite.
    ``rules`` accepts a YAML file/directory path or None for the built-in
    auto rule.
    """
    left_doc = load_document(left, sheet=sheet)
    right_doc = load_document(right, sheet=sheet)
    if not left_doc.tables or not right_doc.tables:
        raise EmptyDocumentError("one of the documents contains no table")
    lt, rt = left_doc.tables[0], right_doc.tables[0]
    pairs = align(lt, rt, match_on=match_on or [], normalize=normalize or {})
    findings = evaluate(load_rules(rules), lt, rt, pairs)
    report = build_report(left_doc, right_doc, findings, aligned_pairs=len(pairs))
    return report, findings
