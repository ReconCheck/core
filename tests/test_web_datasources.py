"""Tests for user-configured data sources and the document library."""

import csv
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reconcheck.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
PO = ROOT / "examples" / "po.csv"
INVOICE = ROOT / "examples" / "invoice.csv"

PO_ROWS = list(csv.DictReader(open(PO, encoding="utf-8-sig")))
INV_ROWS = list(csv.DictReader(open(INVOICE, encoding="utf-8-sig")))
PO_BYTES = PO.read_bytes()
INV_BYTES = INVOICE.read_bytes()


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        if self.path == "/auth/file.csv":
            if self.headers.get("X-API-Key") == "SECRET123":
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.end_headers()
                self.wfile.write(PO_BYTES)
            else:
                self.send_response(401)
                self.end_headers()
            return
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, ctype, body = route()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output quiet
        pass


@pytest.fixture
def httpd() -> str:
    routes = {
        "/api/po": lambda: (
            200,
            "application/json",
            json.dumps({"data": PO_ROWS}, ensure_ascii=False).encode("utf-8"),
        ),
        "/api/inv": lambda: (
            200,
            "application/json",
            json.dumps({"data": INV_ROWS}, ensure_ascii=False).encode("utf-8"),
        ),
        "/items": lambda: (
            200,
            "application/json",
            json.dumps(
                [
                    {"id": "po", "name": "PO-240913-001.csv"},
                    {"id": "inv", "name": "INV-240913-001.csv"},
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
        ),
        "/file/po.csv": lambda: (200, "text/csv", PO_BYTES),
        "/file/invoice.csv": lambda: (200, "text/csv", INV_BYTES),
        "/big.bin": lambda: (200, "application/octet-stream", b"x" * 2000),
        "/big.json": lambda: (
            200,
            "application/json",
            json.dumps({"data": [{"k": "v" * 200}]}).encode("utf-8"),
        ),
    }
    handler = type("RoutesHandler", (_Handler,), {"routes": routes})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _client(tmp_path: Path):
    return TestClient(create_app(data_dir=tmp_path / "data"))


def _json_headers() -> dict[str, str]:
    return {"Content-Type": "application/json"}


# --------------------------------------------------------------------------- datasources CRUD


def test_datasource_crud(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        payload = {
            "name": "ERP 接口",
            "type": "records",
            "url": f"{httpd}/api/po",
            "auth": "bearer",
            "token": "secret-token",
        }
        r = client.post("/api/datasources", json=payload)
        assert r.status_code == 200
        ds = r.json()
        assert ds["name"] == "ERP 接口"
        assert ds["has_token"] is True
        assert "token" not in ds

        listed = client.get("/api/datasources").json()["datasources"]
        assert len(listed) == 1 and listed[0]["id"] == ds["id"]

        # update without touching the secret keeps it
        r = client.put(
            f"/api/datasources/{ds['id']}", json={"name": "ERP v2", "url": f"{httpd}/api/inv"}
        )
        assert r.status_code == 200
        assert r.json()["name"] == "ERP v2"

        # update with an empty token keeps the stored secret too
        r = client.put(
            f"/api/datasources/{ds['id']}",
            json={"name": "ERP v3", "url": f"{httpd}/api/inv", "token": ""},
        )
        assert r.json()["name"] == "ERP v3"

        assert client.delete(f"/api/datasources/{ds['id']}").json()["ok"] is True
        assert client.get("/api/datasources").json()["datasources"] == []


def test_datasource_validation(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/datasources", json={"name": "", "url": "x"}).status_code == 400
        assert client.post("/api/datasources", json={"name": "n", "url": ""}).status_code == 400


# --------------------------------------------------------------------------- probe / list / fetch


def test_datasource_probe_ok_and_fail(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        ok_ds = client.post(
            "/api/datasources", json={"name": "ok", "url": f"{httpd}/api/po"}
        ).json()
        assert client.post(f"/api/datasources/{ok_ds['id']}/probe").json()["ok"] is True

        bad_ds = client.post(
            "/api/datasources", json={"name": "bad", "url": "http://127.0.0.1:1/nope"}
        ).json()
        r = client.post(f"/api/datasources/{bad_ds['id']}/probe").json()
        assert r["ok"] is False and r["error"]


def test_datasource_list_items(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        ds = client.post(
            "/api/datasources",
            json={
                "name": "files",
                "type": "file",
                "url": f"{httpd}/file/po.csv",
                "list_url": f"{httpd}/items",
            },
        ).json()
        items = client.post(f"/api/datasources/{ds['id']}/list").json()["items"]
        assert len(items) == 2
        assert items[0]["name"] == "PO-240913-001.csv"


def test_datasource_auth_header_forwarded(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        good = client.post(
            "/api/datasources",
            json={
                "name": "au",
                "type": "file",
                "url": f"{httpd}/auth/file.csv",
                "auth": "header",
                "header_name": "X-API-Key",
                "token": "SECRET123",
            },
        ).json()
        r = client.post(f"/api/datasources/{good['id']}/probe").json()
        assert r["ok"] is True and r["status"] == 200

        doc = client.post(f"/api/datasources/{good['id']}/fetch", json={}).json()
        assert doc["size"] == len(PO_BYTES)

        bad = client.post(
            "/api/datasources",
            json={
                "name": "au-bad",
                "type": "file",
                "url": f"{httpd}/auth/file.csv",
                "auth": "header",
                "header_name": "X-API-Key",
                "token": "WRONG",
            },
        ).json()
        r = client.post(f"/api/datasources/{bad['id']}/probe").json()
        assert r["ok"] is False and r["status"] == 401
        fetch = client.post(f"/api/datasources/{bad['id']}/fetch", json={})
        assert fetch.status_code == 502


def test_fetch_records_missing_path_is_422(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        ds = client.post(
            "/api/datasources",
            json={
                "name": "bad-path",
                "type": "records",
                "url": f"{httpd}/api/po",
                "records_path": "data.items",
            },
        ).json()
        r = client.post(f"/api/datasources/{ds['id']}/fetch", json={})
        assert r.status_code == 422
        assert "no records" in r.json()["detail"]


def test_response_size_capped_while_streaming(tmp_path: Path, httpd: str, monkeypatch):
    """A reply larger than MAX_BYTES is cut off mid-stream, not buffered."""
    import reconcheck.web.datasources as ds_mod

    monkeypatch.setattr(ds_mod, "MAX_BYTES", 100)
    with _client(tmp_path) as client:
        ds = client.post(
            "/api/datasources",
            json={"name": "big", "type": "file", "url": f"{httpd}/big.bin"},
        ).json()

        probe = client.post(f"/api/datasources/{ds['id']}/probe").json()
        assert probe["ok"] is False and "too large" in probe["error"]

        fetch = client.post(f"/api/datasources/{ds['id']}/fetch", json={})
        assert fetch.status_code == 502

        ds_rec = client.post(
            "/api/datasources",
            json={
                "name": "big-rec",
                "type": "records",
                "url": f"{httpd}/big.json",
                "records_path": "data",
            },
        ).json()
        assert client.post(f"/api/datasources/{ds_rec['id']}/fetch", json={}).status_code == 502


def test_fetch_file_document_and_compare(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        po_ds = client.post(
            "/api/datasources",
            json={"name": "po-src", "type": "file", "url": f"{httpd}/file/po.csv"},
        ).json()
        inv_ds = client.post(
            "/api/datasources",
            json={"name": "inv-src", "type": "file", "url": f"{httpd}/file/invoice.csv"},
        ).json()
        po_doc = client.post(f"/api/datasources/{po_ds['id']}/fetch", json={}).json()
        inv_doc = client.post(f"/api/datasources/{inv_ds['id']}/fetch", json={}).json()
        assert po_doc["source"] == "datasource" and po_doc["size"] == len(PO_BYTES)

        docs = client.get("/api/documents").json()["documents"]
        assert len(docs) == 2

        r = client.post("/api/compare", data={"doc_ids": f"{po_doc['id']},{inv_doc['id']}"})
        assert r.status_code == 200
        report = r.json()
        assert report["summary"]["aligned_rows"] == 5
        assert report["summary"]["total"] == 2


def test_fetch_records_document_and_preview(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        ds = client.post(
            "/api/datasources",
            json={
                "name": "PO-240913-001",
                "type": "records",
                "url": f"{httpd}/api/po",
                "records_path": "data",
            },
        ).json()
        doc = client.post(f"/api/datasources/{ds['id']}/fetch", json={}).json()
        assert doc["source"] == "records"
        assert doc["name"] == "PO-240913-001.json"

        detail = client.get(f"/api/documents/{doc['id']}").json()
        preview = detail["preview"]
        assert preview["headers"][:3] == ["行号", "料号", "名称"]
        assert preview["total_rows"] == 5


def test_jobs_with_doc_ids(tmp_path: Path, httpd: str):
    with _client(tmp_path) as client:
        po_ds = client.post(
            "/api/datasources",
            json={
                "name": "PO-240913-001",
                "type": "records",
                "url": f"{httpd}/api/po",
                "records_path": "data",
            },
        ).json()
        inv_ds = client.post(
            "/api/datasources",
            json={
                "name": "INV-240913-001",
                "type": "records",
                "url": f"{httpd}/api/inv",
                "records_path": "data",
            },
        ).json()
        po_doc = client.post(f"/api/datasources/{po_ds['id']}/fetch", json={}).json()
        inv_doc = client.post(f"/api/datasources/{inv_ds['id']}/fetch", json={}).json()

        r = client.post("/api/jobs", data={"doc_ids": f"{po_doc['id']},{inv_doc['id']}"})
        assert r.status_code == 200
        job = r.json()
        assert job["unpaired"] == []
        assert len(job["pairs"]) == 1

        for _ in range(100):
            j = client.get(f"/api/jobs/{job['id']}").json()
            if j["status"] == "done":
                break
            time.sleep(0.1)
        assert j["pairs"][0]["status"] == "done"
        assert j["pairs"][0]["findings"] == 2


# --------------------------------------------------------------------------- document library


def test_document_upload_and_delete(tmp_path: Path):
    with _client(tmp_path) as client:
        with open(PO, "rb") as f:
            r = client.post("/api/documents", files=[("files", ("po.csv", f, "text/csv"))])
        assert r.status_code == 200
        doc = r.json()["documents"][0]
        assert doc["source"] == "upload"

        content = client.get(f"/api/documents/{doc['id']}/content")
        assert content.status_code == 200
        assert content.content == PO_BYTES

        assert client.delete(f"/api/documents/{doc['id']}").json()["ok"] is True
        assert client.get("/api/documents").json()["documents"] == []
