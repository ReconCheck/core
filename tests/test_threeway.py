"""Three-way verification (PO / delivery note / invoice) — engine, CLI, API."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from reconcheck.cli import main as cli_main
from reconcheck.comparison import compare_three
from reconcheck.parse import load_document
from reconcheck.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def _write_three(tmp_path: Path, invoice_qty: str = "199.5") -> list[Path]:
    """Three CSV docs sharing business key 240913001; invoice qty configurable."""
    header = "料号,数量\n"
    paths = []
    for name, qty in (("PO-240913-001.csv", "200"), ("DN-240913-001.csv", "200"), ("INV-240913-001.csv", invoice_qty)):
        p = tmp_path / name
        p.write_text(header + f"A0012,100\nB0034,{qty}\n", encoding="utf-8")
        paths.append(p)
    return paths


def _docs(paths: list[Path]):
    return [load_document(p) for p in paths]


def test_threeway_conflict_detected(tmp_path: Path):
    paths = _write_three(tmp_path)
    report = compare_three(_docs(paths), match_on=["料号"])
    assert report["mode"] == "three-way"
    assert report["summary"]["pairs_evaluated"] == 3
    # PO vs DN agree; both vs invoice disagree -> 2 pair findings
    assert report["summary"]["pair_findings_total"] == 2
    conflicts = report["three_way"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["field"] == "数量"
    assert conflict["consistent"] is False
    assert "INV-240913-001" in conflict["values"]
    # majority logic: the invoice disagrees with both other documents
    assert conflict["outlier_indices"] == [2]
    assert conflict["severity"] == "high"


def test_threeway_all_agree_no_findings(tmp_path: Path):
    paths = _write_three(tmp_path, invoice_qty="200")
    report = compare_three(_docs(paths), match_on=["料号"])
    assert report["summary"]["pair_findings_total"] == 0
    assert report["three_way"] == []


def test_threeway_requires_three_docs(tmp_path: Path):
    paths = _write_three(tmp_path)
    try:
        compare_three(_docs(paths)[:2], match_on=["料号"])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_cli_compare3(tmp_path: Path, capsys):
    paths = _write_three(tmp_path)
    rc = cli_main(["compare3", *[str(p) for p in paths], "--match-on", "料号", "-o", str(tmp_path / "r.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote" in out
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert report["mode"] == "three-way"
    assert report["summary"]["conflict_total"] == 1


def test_threeway_matches_normalized_keys(tmp_path: Path):
    """Three-way rows must line up under the same normalisation as alignment."""
    po = tmp_path / "PO.csv"
    dn = tmp_path / "DN.csv"
    inv = tmp_path / "INV.csv"
    po.write_text("料号,数量\nA0012,100\n", encoding="utf-8")
    dn.write_text("料号,数量\nA0012,100\n", encoding="utf-8")
    inv.write_text("料号,数量\nA012,99\n", encoding="utf-8")  # unnormalised part no
    # without normalisation the keys never collide -> no conflict
    raw = compare_three(_docs([po, dn, inv]), match_on=["料号"])
    assert raw["three_way"] == []
    # with part_no normalisation A012 == A0012 -> the invoice is the outlier
    norm = compare_three(
        _docs([po, dn, inv]), match_on=["料号"], normalize={"料号": "part_no"}
    )
    assert len(norm["three_way"]) == 1
    assert norm["three_way"][0]["outlier_indices"] == [2]


def test_threeway_aligns_units_before_comparing(tmp_path: Path):
    """5 kg vs 5 kg vs 5000 g are the same quantity in the consensus pass."""
    po = tmp_path / "PO.csv"
    dn = tmp_path / "DN.csv"
    inv = tmp_path / "INV.csv"
    po.write_text("料号,数量\nA01,5 kg\n", encoding="utf-8")
    dn.write_text("料号,数量\nA01,5 kg\n", encoding="utf-8")
    inv.write_text("料号,数量\nA01,5000 g\n", encoding="utf-8")
    report = compare_three(_docs([po, dn, inv]), match_on=["料号"])
    assert report["three_way"] == []


def test_api_compare3(tmp_path: Path):
    paths = _write_three(tmp_path)
    app = create_app(data_dir=tmp_path / "data")
    with TestClient(app) as client:
        with open(paths[0], "rb") as f0, open(paths[1], "rb") as f1, open(paths[2], "rb") as f2:
            r = client.post(
                "/api/compare3",
                files=[
                    ("files", (paths[0].name, f0, "text/csv")),
                    ("files", (paths[1].name, f1, "text/csv")),
                    ("files", (paths[2].name, f2, "text/csv")),
                ],
            )
        assert r.status_code == 200
        report = r.json()
        assert report["mode"] == "three-way"
        assert report["summary"]["conflict_total"] == 1
        assert report["three_way"][0]["field"] == "数量"


def test_api_compare3_doc_ids(tmp_path: Path):
    paths = _write_three(tmp_path)
    app = create_app(data_dir=tmp_path / "data")
    with TestClient(app) as client:
        ids = []
        for p in paths:
            with open(p, "rb") as f:
                r = client.post("/api/documents", files=[("files", (p.name, f, "text/csv"))])
            ids.append(r.json()["documents"][0]["id"])
        r = client.post("/api/compare3", data={"doc_ids": ",".join(ids)})
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["conflict_total"] == 1


def test_api_compare3_wrong_count(tmp_path: Path):
    paths = _write_three(tmp_path)
    app = create_app(data_dir=tmp_path / "data")
    with TestClient(app) as client:
        with open(paths[0], "rb") as f:
            r = client.post("/api/compare3", files=[("files", (paths[0].name, f, "text/csv"))])
        assert r.status_code == 400
        assert "three documents" in r.text


def test_threeway_respects_exception_aggregation(tmp_path: Path):
    """Three-way conflicts must judge with the same exception semantics as the
    pairwise pipeline (a rounding exemption wins even when text differs)."""
    from reconcheck.rules import Rule

    header = "k,v\n"
    for name, v in (("A.csv", "100.015"), ("B.csv", "100.016"), ("C.csv", "100.015")):
        (tmp_path / name).write_text(header + f"1,{v}\n", encoding="utf-8")
    docs = [
        load_document(p)
        for p in (tmp_path / "A.csv", tmp_path / "B.csv", tmp_path / "C.csv")
    ]
    strict = [Rule(id="strict", compare="v", tolerance={"absolute": 0, "relative": 0})]
    assert len(compare_three(docs, match_on=["k"], rules=strict)["three_way"]) == 1
    rounded = [
        Rule(
            id="rounded",
            compare="v",
            tolerance={"absolute": 0, "relative": 0},
            exceptions=[{"when": {"rounding": {"decimals": 2}}}],
        )
    ]
    assert compare_three(docs, match_on=["k"], rules=rounded)["three_way"] == []


def test_threeway_unit_anchor_not_stuck_to_first_doc(tmp_path: Path):
    """The unit anchor is the first *set* unit: 5 vs 5000 g must not be judged
    as a conflict merely because doc[0] carries no unit."""
    header = "k,v\n"
    for name, v in (("A.csv", "5"), ("B.csv", "5 kg"), ("C.csv", "5000 g")):
        (tmp_path / name).write_text(header + f"1,{v}\n", encoding="utf-8")
    docs = [
        load_document(p)
        for p in (tmp_path / "A.csv", tmp_path / "B.csv", tmp_path / "C.csv")
    ]
    report = compare_three(docs, match_on=["k"])
    assert report["three_way"] == []

def test_threeway_duplicate_keys_warn(tmp_path: Path):
    paths = []
    for name in ("PO-240913-001.csv", "DN-240913-001.csv", "INV-240913-001.csv"):
        p = tmp_path / name
        p.write_text("料号,数量\nX1,1\nX1,2\n", encoding="utf-8")
        paths.append(p)
    report = compare_three(_docs(paths), match_on=["料号"])
    assert any("duplicate" in w for w in report["warnings"])
    assert report["summary"]["aligned_rows"] > 0  # comparison still ran on the first rows
