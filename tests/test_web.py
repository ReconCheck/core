"""Web layer tests: REST API, async jobs, auth."""

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

import reconcheck.web.app as webapp
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


def test_job_auto_rule_baseline_for_unknown_headers(tmp_path: Path):
    """Tables the example rules don't know still get the auto rule."""
    with _client(tmp_path) as client:
        left = b"id,amount\n1,100.00\n2,100.00\n"
        right = b"id,amount\n1,99.00\n2,100.00\n"
        r = client.post(
            "/api/jobs",
            files=[
                ("files", ("PO-240913-001.csv", left, "text/csv")),
                ("files", ("INV-240913-001.csv", right, "text/csv")),
            ],
        )
        assert r.status_code == 200
        job_id = r.json()["id"]
        done = None
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] == "done":
                done = j
                break
            time.sleep(0.1)
        assert done is not None
        assert done["pairs"][0]["findings"] == 1  # amount mismatch via auto rule
        assert done["pairs"][0]["medium"] == 1  # auto rule defaults to medium


def test_job_pair_failure_is_visible(tmp_path: Path):
    """A failed pair keeps its error in the job view (not 'unknown')."""
    with _client(tmp_path) as client:
        r = client.post(
            "/api/jobs",
            files=[
                ("files", ("PO-240913-001.csv", b"", "text/csv")),
                ("files", ("INV-240913-001.csv", b"id,amount\n1,100.00\n", "text/csv")),
            ],
        )
        job_id = r.json()["id"]
        done = None
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] == "done":
                done = j
                break
            time.sleep(0.1)
        assert done is not None
        assert done["pairs"][0]["status"] == "failed"
        assert done["pairs"][0]["error"]


def test_upload_over_limit_is_413(tmp_path: Path, monkeypatch):
    import reconcheck.web.app as webapp

    monkeypatch.setattr(webapp, "MAX_UPLOAD_BYTES", 32)
    with _client(tmp_path) as client:
        r = client.post(
            "/api/documents",
            files=[("files", ("big.csv", b"x" * 100, "text/csv"))],
        )
    assert r.status_code == 413


def test_document_delete_rejects_path_traversal(tmp_path: Path):
    marker = tmp_path / "keep.txt"
    marker.write_text("do not delete", encoding="utf-8")
    with _client(tmp_path) as client:
        # httpx may normalize the raw "..%2F" segments into a 405 no-match;
        # either way the request must never reach the store
        r = client.delete("/api/documents/..%2F..%2Fkeep.txt")
        assert r.status_code in (404, 405)
        # non-hex ids are refused idempotently without touching disk
        r2 = client.delete("/api/documents/nothexid")
        assert r2.status_code == 200 and r2.json().get("ok") is False
    assert marker.read_text(encoding="utf-8") == "do not delete"


def test_jobs_skip_duplicate_document_ids(tmp_path: Path):
    """The same doc_id twice in one job must not produce a self-comparison."""
    with _client(tmp_path) as client:
        with open(PO, "rb") as f:
            doc = client.post(
                "/api/documents", files=[("files", ("PO-240913-001.csv", f, "text/csv"))]
            ).json()["documents"][0]
        with open(INVOICE, "rb") as f:
            r = client.post(
                "/api/jobs",
                data={"doc_ids": f"{doc['id']},{doc['id']}"},
                files=[("files", ("INV-240913-001.csv", f, "text/csv"))],
            )
        assert r.status_code == 200
        job = r.json()
        assert len(job["pairs"]) == 1
        assert job["pairs"][0]["left"] != job["pairs"][0]["right"]


def test_jobs_identical_uploads_are_deduplicated(tmp_path: Path):
    """Uploading the very same bytes twice must not self-compare."""
    with _client(tmp_path) as client:
        data = b"id,amount\n1,100.00\n"
        r = client.post(
            "/api/jobs",
            files=[
                ("files", ("PO-240913-001.csv", data, "text/csv")),
                ("files", ("PO-240913-001.csv", data, "text/csv")),
            ],
        )
        assert r.status_code == 422  # only one distinct entry survives
        assert "two distinct documents" in r.json()["detail"]


def test_jobs_same_content_different_names_both_survive(tmp_path: Path):
    """A PO and a DN that match perfectly are two documents, not a duplicate.

    Content-hash dedupe must key on (bytes, filename): a clean
    reconciliation where both sides are byte-identical is the normal,
    desired case — merging it would show only one document.
    """
    with _client(tmp_path) as client:
        data = b"id,amount\n1,100.00\n"
        r = client.post(
            "/api/jobs",
            files=[
                ("files", ("PO-240913-001.csv", data, "text/csv")),
                ("files", ("DN-240913-001.csv", data, "text/csv")),
            ],
        )
        assert r.status_code == 200
        job = r.json()
        assert job["unpaired"] == []
        assert len(job["pairs"]) == 1
        assert job["pairs"][0]["left"] != job["pairs"][0]["right"]


def test_sales_chain_tokens_and_keys():
    """Sales-chain filenames pair under the same business-key heuristic."""
    from reconcheck.web.app import base_key, guess_kind

    assert base_key("SO-240913-001.csv") == "240913001"
    assert base_key("SIV-240913-001.csv") == "240913001"
    assert base_key("OUT-240913-001.csv") == "240913001"
    assert base_key("销售订单-240913-001.csv") == "240913001"
    assert base_key("销项发票-240913-001.csv") == "240913001"
    assert guess_kind("SO-240913-001.csv") == "so"
    assert guess_kind("SIV-240913-001.csv") == "invoice"
    assert guess_kind("OUT-240913-001.csv") == "outbound"
    # purchase chain untouched
    assert base_key("PO-240913-001.csv") == "240913001"
    assert base_key("INV-240913-001.csv") == "240913001"
    assert guess_kind("INV-240913-001.csv") == "invoice"
    assert guess_kind("PO-240913-001.csv") == "po"


def test_jobs_sales_chain_auto_pairs(tmp_path: Path):
    """SO + outbound + sales invoice group into three pairs, none unpaired."""
    with _client(tmp_path) as client:
        r = client.post(
            "/api/jobs",
            files=[
                ("files", ("SO-240913-001.csv", b"id,amount\n1,100.00\n", "text/csv")),
                ("files", ("OUT-240913-001.csv", b"id,amount\n1,99.00\n", "text/csv")),
                ("files", ("SIV-240913-001.csv", b"id,amount\n1,100.50\n", "text/csv")),
            ],
        )
        assert r.status_code == 200
        job = r.json()
        assert job["unpaired"] == []
        assert len(job["pairs"]) == 3
        kinds = {e["kind"] for e in job["files"]}
        assert kinds == {"so", "outbound", "invoice"}


def test_jobstore_cleanup_ttl(tmp_path: Path):
    """Janitor pruning: old job dirs/reports die, active jobs survive."""
    store = webapp.JobStore(tmp_path / "data")
    old = time.time() - 100 * 24 * 3600

    expired_dir = store.files_dir / "aaaaaaa11111"
    expired_dir.mkdir(parents=True)
    (expired_dir / "00_x.csv").write_bytes(b"x")
    os.utime(expired_dir, (old, old))

    expired_report = store.reports_dir / "bbbbbb222222.json"
    expired_report.write_text("{}", encoding="utf-8")
    os.utime(expired_report, (old, old))

    active_dir = store.files_dir / "ccccccc33333"
    active_dir.mkdir(parents=True)
    (active_dir / "00_y.csv").write_bytes(b"y")
    os.utime(active_dir, (old, old))

    fresh_dir = store.files_dir / "ddddddd44444"
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "00_z.csv").write_bytes(b"z")

    removed = store.cleanup(time.time(), ttl=30 * 24 * 3600, active={"ccccccc33333"})
    assert removed == 2
    assert not expired_dir.exists() and not expired_report.exists()
    assert active_dir.exists()
    assert fresh_dir.exists()


def test_compare_sync_same_name_different_content(tmp_path: Path):
    """Two uploads with the same filename must not overwrite each other: the
    first file was previously clobbered, turning the pair into a self-compare
    that silently reported everything as matching."""
    with _client(tmp_path) as client:
        r = client.post(
            "/api/compare",
            files=[
                ("files", ("dup.csv", b"id,amount\n1,100.00\n", "text/csv")),
                ("files", ("dup.csv", b"id,amount\n1,99.50\n", "text/csv")),
            ],
        )
        assert r.status_code == 200
        report = r.json()
        assert report["summary"]["aligned_rows"] == 1
        assert report["summary"]["total"] == 1  # 0.50 is outside tolerance


def test_create_job_rejects_malformed_config(tmp_path: Path):
    def _post(config):
        with _client(tmp_path) as client:
            return client.post(
                "/api/jobs",
                data={"config": json.dumps(config)},
                files=[
                    ("files", ("PO-240913-001.csv", b"id,v\n1,2\n", "text/csv")),
                    ("files", ("INV-240913-001.csv", b"id,v\n1,3\n", "text/csv")),
                ],
            )

    assert _post({"match_on": "id"}).status_code == 400  # must be a list
    assert _post({"match_on": [1, 2]}).status_code == 400  # and all strings
    assert _post({"normalize": []}).status_code == 400  # must be a dict
    assert _post({"normalize": {"id": "bogus-kind"}}).status_code == 400
    assert _post({"match_on": ["id"], "normalize": {"id": "part_no"}}).status_code == 200
