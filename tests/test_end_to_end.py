"""End-to-end: the CLI against the example files must produce the expected findings."""

import json
from pathlib import Path

import pytest

from reconcheck.cli import main as cli_main

ROOT = Path(__file__).resolve().parent.parent
PO = ROOT / "examples" / "po.csv"
INVOICE = ROOT / "examples" / "invoice.csv"
RULES = ROOT / "examples" / "rules"


def test_cli_compare_examples_with_rules(tmp_path: Path):
    out = tmp_path / "report.json"
    rc = cli_main(
        [
            "compare",
            str(PO),
            str(INVOICE),
            "--rules",
            str(RULES),
            "--match-on",
            "料号",
            "--normalize",
            "料号:part_no",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    findings = report["findings"]

    # Exactly two real disagreements: qty (199 vs 200) and amount (99.50 vs 100.00)
    # on the screw line. The kg/g difference on the copper strip is a legit conversion.
    assert len(findings) == 2
    by_field: dict[str, list[dict]] = {}
    for f in findings:
        by_field.setdefault(f["field"], []).append(f)
    assert sorted(by_field) == ["数量", "金额"]

    qty = by_field["数量"][0]
    assert qty["left_value"] == "200"
    assert qty["right_value"] == "199"
    assert qty["severity"] == "high"

    sides = {e["side"]: e for e in qty["evidence"]}
    assert sides["left"]["loc"]["row"] == 3
    assert sides["right"]["loc"]["row"] == 3
    assert sides["left"]["loc"]["path"] == str(PO)
    assert sides["left"]["href"].startswith("cell://")

    summary = report["summary"]
    assert summary["total"] == 2
    assert summary["high"] == 1
    assert summary["medium"] == 1
    assert summary["aligned_rows"] == 5


def test_default_rule_flags_unit_difference(tmp_path: Path):
    # Without the unit rule, 5 kg vs 5000 g is reported as a mismatch.
    out = tmp_path / "report.json"
    rc = cli_main(
        [
            "compare",
            str(PO),
            str(INVOICE),
            "--match-on",
            "料号",
            "--normalize",
            "料号:part_no",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    fields = [f["field"] for f in report["findings"]]
    assert fields.count("数量") == 2  # screw qty + copper strip qty


def test_version_flag(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc:
        cli_main(["--version"])
    assert exc.value.code == 0
    assert "reconcheck" in capsys.readouterr().out

def test_duplicate_keys_surface_as_warnings(tmp_path: Path):
    from reconcheck.comparison import compare_files

    left, right = tmp_path / "a.csv", tmp_path / "b.csv"
    left.write_text("料号,数量\nX1,1\nX1,2\n", encoding="utf-8")
    right.write_text("料号,数量\nX1,1\n", encoding="utf-8")
    report, _findings = compare_files(left, right, match_on=["料号"])
    assert any("duplicate" in w and "left" in w for w in report["warnings"])
    assert report["summary"]["aligned_rows"] == 1  # first-wins, but it is said out loud


def test_reports_carry_empty_warnings_key(tmp_path: Path):
    out = tmp_path / "report.json"
    rc = cli_main(
        [
            "compare", str(PO), str(INVOICE),
            "--match-on", "料号", "--normalize", "料号:part_no",
            "-o", str(out),
        ]
    )
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["warnings"] == []
