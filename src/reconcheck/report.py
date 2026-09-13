"""Report assembly: findings -> a JSON evidence chain."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import Document, Finding


def build_report(
    left: Document,
    right: Document,
    findings: list[Finding],
    aligned_pairs: int = 0,
) -> dict[str, Any]:
    """Assemble the machine-readable report contract."""
    summary: dict[str, int] = {
        "total": len(findings),
        "high": 0,
        "medium": 0,
        "low": 0,
        "aligned_rows": aligned_pairs,
    }
    for finding in findings:
        summary[str(finding.severity)] += 1
    return {
        "engine": {"name": "reconcheck", "version": __version__},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "documents": [left.to_dict(), right.to_dict()],
        "findings": [finding.to_dict() for finding in findings],
        "summary": summary,
    }


def build_threeway_report(
    docs: list[Document],
    names: list[str],
    pair_results: list[tuple[tuple[str, str], int, list[Finding]]],
    conflicts: list[dict[str, Any]],
    aligned: int = 0,
) -> dict[str, Any]:
    """Assemble a three-way verification report.

    ``pair_results`` holds ``((left_name, right_name), aligned_rows, findings)``
    for every unordered pair (three pairs for three documents).
    """
    pairs_payload: list[dict[str, Any]] = []
    total_pair_findings = 0
    for pair, aligned_rows, findings in pair_results:
        summary: dict[str, int] = {"total": len(findings), "high": 0, "medium": 0, "low": 0}
        for finding in findings:
            summary[str(finding.severity)] += 1
        total_pair_findings += len(findings)
        pairs_payload.append(
            {
                "pair": list(pair),
                "aligned_rows": aligned_rows,
                "summary": summary,
                "findings": [finding.to_dict() for finding in findings],
            }
        )
    return {
        "engine": {"name": "reconcheck", "version": __version__},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "three-way",
        "documents": [doc.to_dict() for doc in docs],
        "pairs": pairs_payload,
        "three_way": conflicts,
        "summary": {
            "pairs_evaluated": len(pairs_payload),
            "pair_findings_total": total_pair_findings,
            "conflict_total": len(conflicts),
            "conflict_high": sum(1 for c in conflicts if c["severity"] == "high"),
            "conflict_medium": sum(1 for c in conflicts if c["severity"] == "medium"),
            "aligned_rows": aligned,
        },
    }


def write_json(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
