"""Web layer tests: REST API, async jobs, auth."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from reconcheck.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
PO = ROOT / "examples" / "po.csv"
INVOICE = ROOT / "examples" / "invoice.csv"


def _client(tmp_path: Path, **kwargs):
    app = create_app(data_dir=tmp_path / "data", **kwargs)
    return TestClient(app)


def _upload_pair(client: TestClient, left: str | Path, right: str | Path):
    with open(left, "rb") as lf, open(right, "rb") as rf:
        return client.post(
            "/api/jobs",
            files=[
                ("files", ("PO-240913-001.csv", lf, "text/csv")),
                ("files", ("INV-240913-001.csv", rf, "text/csv")),
            ],
        )


def test_health(tmp_path: Path):
    with _client(tmp_path) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["engine"] == "reconcheck"


def test_compare_sync(tmp_path: Path):
    with _client(tmp_path) as client:
        with open(PO, "rb") as lf, open(INVOICE, "rb") as rf:
            r = client.post(
                "/api/compare",
                files=[
                    ("files", ("po.csv", lf, "text/csv")),
                    ("files", ("invoice.csv", rf, "text/csv")),
                ],
            )
        assert r.status_code == 200
        report = r.json()
        assert report["summary"]["aligned_rows"] == 5
        assert report["summary"]["total"] == 2


def test_compare_requires_two_files(tmp_path: Path):
    with _client(tmp_path) as client:
        with open(PO, "rb") as f:
            r = client.post("/api/compare", files=[("files", ("po.csv", f, "text/csv"))])
        assert r.status_code == 400


def test_jobs_batch_end_to_end(tmp_path: Path):
    with _client(tmp_path) as client:
        r = _upload_pair(client, PO, INVOICE)
        assert r.status_code == 200
        job = r.json()
        assert job["unpaired"] == []
        assert job["status"] == "queued"

        report_resp = None
        for _ in range(100):
            j = client.get(f"/api/jobs/{job['id']}").json()
            if j["status"] == "done":
                pair = j["pairs"][0]
                assert pair["findings"] == 2
                assert pair["high"] == 1 and pair["medium"] == 1
                report_resp = client.get(f"/api/reports/{pair['report_id']}")
                break
            time.sleep(0.1)
        assert report_resp is not None
        assert report_resp.status_code == 200
        assert report_resp.json()["summary"]["total"] == 2


def test_jobs_unknown_job_404(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.get("/api/jobs/nope").status_code == 404


def test_jobs_requires_shared_key(tmp_path: Path):
    with _client(tmp_path) as client:
        with open(PO, "rb") as f1, open(INVOICE, "rb") as f2:
            r = client.post(
                "/api/jobs",
                files=[
                    ("files", ("random-a.csv", f1, "text/csv")),
                    ("files", ("random-b.csv", f2, "text/csv")),
                ],
            )
        assert r.status_code == 422  # different grouping keys -> no pairs


def test_api_key_protects_api(tmp_path: Path):
    with _client(tmp_path, api_key="secret123") as client:
        assert client.get("/api/health").status_code == 401
        r = client.get("/api/health", headers={"X-API-Key": "secret123"})
        assert r.status_code == 200


def test_rules_endpoint(tmp_path: Path):
    with _client(tmp_path) as client:
        r = client.get("/api/rules")
        assert r.status_code == 200
        body = r.json()
        assert body["default"] == "auto-numeric-diff (built-in)"
        assert "base.yaml" in body["files"]
