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


def write_json(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
