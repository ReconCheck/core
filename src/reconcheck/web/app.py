"""ReconCheck REST API (FastAPI) and job runner.

Endpoints
---------
* ``GET   /api/health``                 — liveness + engine version
* ``POST  /api/compare``                — synchronous comparison (2 files and/or doc ids)
* ``POST  /api/jobs``                   — async batch comparison (files + doc ids)
* ``GET   /api/jobs/{job_id}``          — job status / progress / pair summary
* ``GET   /api/reports/{report_id}``    — stored comparison report (JSON)
* ``GET   /api/rules``                  — available rule sets
* ``GET   /api/documents`` …            — document library (register / list / preview / delete)
* ``GET   /api/datasources`` …          — enterprise data sources (CRUD / probe / list / fetch)

Authentication (optional): set ``RECONCHECK_API_KEY``. When set, every ``/api/*``
request must carry ``X-API-Key: <key>`` (the static frontend works without it).
Data source tokens are stored in the local data directory and never echoed.

Run: ``reconcheck-api`` (binds ``0.0.0.0:8765`` by default).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from httpx import HTTPError

from .. import __version__
from ..comparison import compare_documents, compare_three
from ..errors import ReconCheckError
from ..models import Document
from ..parse import load_document
from ..parse.tabular import document_from_records
from ..rules import DEFAULT_RULE, Rule, load_rules
from .datasources import DataSource, load_datasources, save_datasources
from .store import DocumentStore, safe_id

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULES_DIR = REPO_ROOT / "examples" / "rules"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PREVIEW_ROWS = 100
MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MB per uploaded file

JANITOR_INTERVAL = 3600  # seconds between TTL cleanup passes
DEFAULT_TTL_DAYS = 30


def _ttl_seconds() -> int:
    """Retention window from RECONCHECK_TTL_DAYS (default 30 days)."""
    raw = os.environ.get("RECONCHECK_TTL_DAYS", "")
    try:
        days = max(1, int(raw))
    except ValueError:
        days = DEFAULT_TTL_DAYS
    return days * 24 * 3600


class UploadTooLarge(Exception):
    """Raised by storage when an uploaded file exceeds MAX_UPLOAD_BYTES."""


def _default_rules_dir() -> Path | None:
    return DEFAULT_RULES_DIR if DEFAULT_RULES_DIR.is_dir() else None


class JobStore:
    """Disk-backed file/report storage plus an in-memory job index."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir
        self.files_dir = data_dir / "files"
        self.reports_dir = data_dir / "reports"
        for d in (self.files_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._index_path = data_dir / "jobs.json"

    # -- jobs -----------------------------------------------------------
    def load(self) -> None:
        if self._index_path.exists():
            try:
                self._jobs = json.loads(self._index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._jobs = {}

    def _persist(self) -> None:
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._jobs, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._index_path)
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def queued_jobs(self) -> list[str]:
        """Jobs that were queued but not finished (replayed after a restart)."""
        with self._lock:
            return [jid for jid, job in self._jobs.items() if job.get("status") == "queued"]

    def active_job_ids(self) -> set[str]:
        """Jobs still being worked on — their files must never be pruned."""
        with self._lock:
            return {
                jid for jid, job in self._jobs.items() if job.get("status") in ("queued", "running")
            }

    def cleanup(
        self,
        now: float | None = None,
        ttl: int | None = None,
        active: set[str] | None = None,
    ) -> int:
        """Delete job file dirs and reports older than ``ttl``; returns count."""
        now = now if now is not None else time.time()
        ttl = ttl if ttl is not None else _ttl_seconds()
        active = active if active is not None else self.active_job_ids()
        removed = 0
        for job_dir in self.files_dir.iterdir():
            if not job_dir.is_dir() or job_dir.name in active:
                continue
            try:
                if now - job_dir.stat().st_mtime > ttl:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        for report in self.reports_dir.glob("*.json"):
            try:
                if now - report.stat().st_mtime > ttl:
                    report.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def create_job(self, job: dict[str, Any]) -> None:
        with self._lock:
            self._jobs[job["id"]] = job
            self._persist()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)
            self._persist()

    # -- files ----------------------------------------------------------
    def save_upload(self, job_id: str, upload: UploadFile, index: int) -> Path:
        """Persist one uploaded file and return the on-disk path."""
        job_dir = self.files_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        name = upload.filename or f"file_{index}"
        safe = _SAFE_NAME.sub("_", name)[:120] or f"file_{index}"
        target = job_dir / f"{index:02d}_{safe}"
        total = 0
        with target.open("wb") as out:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise UploadTooLarge(f"{name}: over {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
                out.write(chunk)
        return target

    # -- reports --------------------------------------------------------
    def save_report(self, report_id: str, report: dict[str, Any]) -> None:
        (self.reports_dir / f"{report_id}.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8"
        )

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        path = self.reports_dir / f"{report_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


# ---------------------------------------------------------------------------
# filename heuristics: guess document kind, derive the grouping key
# ---------------------------------------------------------------------------

_KIND_TOKENS: dict[str, tuple[str, ...]] = {
    # purchase chain
    "po": ("po", "purchase", "order", "采购", "订单", "订购"),
    # sales chain
    "so": ("so", "sales", "sale", "销售", "销单"),
    "outbound": ("outbound", "out", "shipment", "ship", "出货", "出库", "发运"),
    "invoice": (
        "invoice",
        "inv",
        "发票",
        "siv",
        "销项",
        "iv",
        "ir",
    ),
    "delivery": ("delivery", "dn", "送货", "收货", "asn", "发货"),
}
# strip longer tokens first so overlapping ones ("siv" before "iv") behave
_ALL_TOKENS = sorted(
    (tok for tokens in _KIND_TOKENS.values() for tok in tokens),
    key=len,
    reverse=True,
)


def guess_kind(filename: str) -> str:
    base = Path(filename).stem.lower()
    for kind, tokens in _KIND_TOKENS.items():
        if any(tok in base for tok in tokens):
            return kind
    return "unknown"


def _strip_kind_token(base: str) -> str:
    lowered = base.lower()
    for tok in _ALL_TOKENS:
        lowered = lowered.replace(tok, "")
    return lowered


def base_key(filename: str) -> str:
    """Grouping key: filename minus kind tokens, extension and noise."""
    stem = Path(filename).stem
    stripped = _strip_kind_token(stem)
    key = re.sub(r"[^a-z0-9]+", "", stripped.lower())
    return key or re.sub(r"[^a-z0-9]+", "", stem.lower())


# ---------------------------------------------------------------------------
# document resolution helpers (records-type docs are JSON -> table on load)
# ---------------------------------------------------------------------------


def _doc_from_meta(docs: DocumentStore, meta: dict[str, Any]) -> Document:
    if meta["source"] == "records":
        raw = docs.content(meta["id"]) or b"[]"
        try:
            records = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as err:
            raise ReconCheckError(f"stored records of '{meta['name']}' are corrupt: {err}") from err
        return document_from_records(records, meta["name"])
    path = docs.content_path(meta["id"], meta)
    if path is None:
        raise ReconCheckError(f"content of document '{meta['name']}' is missing")
    return load_document(path)


def _doc_preview(docs: DocumentStore, meta: dict[str, Any]) -> dict[str, Any]:
    try:
        doc = _doc_from_meta(docs, meta)
    except ReconCheckError as err:
        return {"error": str(err)}
    if not doc.tables:
        return {"error": "no table found in this document"}
    table = doc.tables[0]
    return {
        "headers": table.headers[:64],
        "rows": [
            [{"text": c.text, "row": c.loc.row, "col": c.loc.col} for c in row.cells]
            for row in table.rows[:_PREVIEW_ROWS]
        ],
        "total_rows": len(table.rows),
    }


# ---------------------------------------------------------------------------
# backend worker: one thread, one queue
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, store: JobStore, documents: DocumentStore, rules_dir: Path | None) -> None:
        self.store = store
        self.documents = documents
        self.rules_dir = rules_dir
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reconcheck-worker")
        self._thread.start()
        self._janitor = threading.Thread(
            target=self._janitor_loop, daemon=True, name="reconcheck-janitor"
        )
        self._janitor.start()
        # replay jobs that were still queued when the process restarted
        for job_id in self.store.queued_jobs():
            self.submit(job_id)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._queue.put(None)
        self._thread.join(timeout=5)
        if self._janitor is not None:
            self._janitor.join(timeout=5)
        self._thread = None

    def _janitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.cleanup(time.time(), _ttl_seconds())
            except Exception:  # noqa: BLE001 - a janitor pass must never die
                pass
            self._stop.wait(JANITOR_INTERVAL)

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                self._process(job_id)
            except Exception as err:  # noqa: BLE001 - the worker must survive
                job = self.store.get_job(job_id)
                if job is not None:
                    self.store.update_job(job_id, status="failed", error=str(err))

    def _load_entry(self, entry: dict[str, Any]) -> Document:
        if entry.get("doc_id"):
            meta = self.documents.get(entry["doc_id"])
            if meta is None:
                raise ReconCheckError(f"document {entry['doc_id']} no longer exists")
            return _doc_from_meta(self.documents, meta)
        return load_document(entry["path"])

    def _rules_for(self) -> list[Rule]:
        """Configured rules plus the built-in auto rule as a baseline.

        The auto rule fills the gaps for tables the configured rules don't
        know; ``evaluate`` already de-duplicates columns an explicit rule
        covers for the same pair.
        """
        rules = load_rules(self.rules_dir)
        if not any(rule.id == DEFAULT_RULE["id"] for rule in rules):
            rules = [*rules, Rule.from_dict(DEFAULT_RULE)]
        return rules

    def _process(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return
        self.store.update_job(job_id, status="running", started_at=time.time())
        by_name = {entry["name"]: entry for entry in job["files"]}
        pairs = job["pairs"]
        rule_list = self._rules_for()
        for i, pair in enumerate(pairs):
            try:
                left_doc = self._load_entry(by_name[pair["left"]])
                right_doc = self._load_entry(by_name[pair["right"]])
                report, _findings = compare_documents(
                    left_doc,
                    right_doc,
                    rules=rule_list,
                    match_on=job["config"].get("match_on"),
                    normalize=job["config"].get("normalize"),
                )
                report_id = uuid.uuid4().hex[:12]
                self.store.save_report(report_id, report)
                s = report["summary"]
                pair.update(
                    report_id=report_id,
                    status="done",
                    findings=s["total"],
                    high=s["high"],
                    medium=s["medium"],
                    low=s["low"],
                    aligned_rows=s["aligned_rows"],
                )
            except ReconCheckError as err:
                pair.update(status="failed", error=str(err))
            except Exception as err:  # noqa: BLE001 - one bad pair must not kill the batch
                pair.update(status="failed", error=f"{type(err).__name__}: {err}")
            self.store.update_job(job_id, progress={"done": i + 1, "total": len(pairs)})
        self.store.update_job(job_id, status="done", finished_at=time.time())


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def _auth_dependency(api_key: str | None):
    def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")

    return require_key


def _require_datasource(store_id: str, sources: list[DataSource]) -> DataSource:
    for source in sources:
        if source.id == store_id:
            return source
    raise HTTPException(status_code=404, detail="data source not found")


def create_app(
    data_dir: str | Path | None = None,
    api_key: str | None = None,
) -> FastAPI:
    root = Path(data_dir or os.environ.get("RECONCHECK_DATA") or (Path.cwd() / "data"))
    root.mkdir(parents=True, exist_ok=True)
    store = JobStore(root)
    store.load()
    documents = DocumentStore(root)
    rules_dir = _default_rules_dir()
    worker = Worker(store, documents, rules_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker.start()
        yield
        worker.stop()

    app = FastAPI(
        title="ReconCheck API",
        version=__version__,
        description="Cross-document verification: parse, align, judge, cite. "
        "See README.md for examples.",
        lifespan=lifespan,
    )

    key = api_key or os.environ.get("RECONCHECK_API_KEY")
    if not key:
        print(
            "\n[reconcheck] WARNING: no RECONCHECK_API_KEY configured — the API is "
            "unauthenticated. Anyone who can reach this port can read and write data. "
            "Set RECONCHECK_API_KEY before deploying outside a trusted network.\n",
            file=sys.stderr,
            flush=True,
        )
    api = APIRouter(prefix="/api", dependencies=[Depends(_auth_dependency(key))])

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "engine": "reconcheck", "version": __version__}

    # ------------------------------------------------------------- compare
    @api.post("/compare")
    async def compare(
        files: list[UploadFile] | None = File(default=None),
        doc_ids: str = Form(""),
    ) -> JSONResponse:
        ids = [i.strip() for i in (doc_ids or "").split(",") if i.strip()]
        if (files or []) and ids:
            raise HTTPException(status_code=400, detail="pass either files or doc_ids, not both")
        if len(ids) == 2 and ids[0] == ids[1]:
            raise HTTPException(status_code=400, detail="cannot compare a document with itself")
        docs: list[Document] = []
        try:
            for doc_id in ids:
                if not safe_id(doc_id):
                    raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
                meta = documents.get(doc_id)
                if meta is None:
                    raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
                docs.append(_doc_from_meta(documents, meta))
        except ReconCheckError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        if files:
            tmp = root / "tmp" / uuid.uuid4().hex[:8]
            tmp.mkdir(parents=True, exist_ok=True)
            paths: list[Path] = []
            try:
                for i, upload in enumerate(files):
                    target = tmp / _SAFE_NAME.sub("_", upload.filename or f"file_{i}")
                    total = 0
                    with target.open("wb") as out:
                        while chunk := await upload.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413, detail=f"{upload.filename}: file too large"
                                )
                            out.write(chunk)
                    paths.append(target)
                docs = [load_document(p) for p in paths]
            except ReconCheckError as err:
                raise HTTPException(status_code=422, detail=str(err)) from err
            finally:
                for p in paths:
                    p.unlink(missing_ok=True)
                if tmp.exists():
                    tmp.rmdir()
        if len(docs) != 2:
            raise HTTPException(
                status_code=400,
                detail="exactly two documents are required (2 files or 2 doc_ids)",
            )
        try:
            report, _findings = compare_documents(docs[0], docs[1], rules=rules_dir)
            return JSONResponse(report)
        except ReconCheckError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    # ------------------------------------------------------- compare3
    @api.post("/compare3")
    async def compare3(
        files: list[UploadFile] | None = File(default=None),
        doc_ids: str = Form(""),
    ) -> JSONResponse:
        """Synchronous three-way verification (PO / delivery note / invoice)."""
        ids = [i.strip() for i in (doc_ids or "").split(",") if i.strip()]
        if (files or []) and ids:
            raise HTTPException(status_code=400, detail="pass either files or doc_ids, not both")
        if len(set(ids)) != len(ids):
            raise HTTPException(status_code=400, detail="cannot compare a document with itself")
        docs: list[Document] = []
        try:
            for doc_id in ids:
                if not safe_id(doc_id):
                    raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
                meta = documents.get(doc_id)
                if meta is None:
                    raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
                docs.append(_doc_from_meta(documents, meta))
        except ReconCheckError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        if files:
            tmp = root / "tmp" / uuid.uuid4().hex[:8]
            tmp.mkdir(parents=True, exist_ok=True)
            paths: list[Path] = []
            try:
                for i, upload in enumerate(files):
                    target = tmp / _SAFE_NAME.sub("_", upload.filename or f"file_{i}")
                    total = 0
                    with target.open("wb") as out:
                        while chunk := await upload.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413, detail=f"{upload.filename}: file too large"
                                )
                            out.write(chunk)
                    paths.append(target)
                docs = [load_document(p) for p in paths]
            except ReconCheckError as err:
                raise HTTPException(status_code=422, detail=str(err)) from err
            finally:
                for p in paths:
                    p.unlink(missing_ok=True)
                if tmp.exists():
                    tmp.rmdir()
        if len(docs) != 3:
            raise HTTPException(
                status_code=400,
                detail="exactly three documents are required (3 files or 3 doc_ids)",
            )
        try:
            report = compare_three(docs, rules=rules_dir)
            return JSONResponse(report)
        except ReconCheckError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    # --------------------------------------------------------------- jobs
    @api.post("/jobs")
    async def create_job(
        files: list[UploadFile] | None = File(default=None),
        config: str = Form("{}"),
        doc_ids: str = Form(""),
    ) -> dict[str, Any]:
        try:
            cfg = json.loads(config or "{}")
            if not isinstance(cfg, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as err:
            raise HTTPException(status_code=400, detail="config must be a JSON object") from err

        job_id = uuid.uuid4().hex[:12]
        used_names: set[str] = set()
        entries: list[dict[str, Any]] = []

        def unique(name: str) -> str:
            base, n = name, 1
            while base in used_names:
                n += 1
                base = f"{name}-{n}"
            used_names.add(base)
            return base

        try:
            for i, upload in enumerate(files or []):
                orig = upload.filename or f"file_{i}"
                name = unique(orig)
                path = store.save_upload(job_id, upload, i)
                entries.append(
                    {
                        "name": name,
                        "orig_name": orig,
                        "kind": guess_kind(name),
                        "base": base_key(name),
                        "path": str(path),
                        "doc_id": None,
                    }
                )
            for raw in (doc_ids or "").split(","):
                doc_id = raw.strip()
                if not doc_id:
                    continue
                if not safe_id(doc_id):
                    raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
                meta = documents.get(doc_id)
                if meta is None:
                    raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
                name = unique(meta["name"])
                entries.append(
                    {
                        "name": name,
                        "orig_name": meta["name"],
                        "kind": guess_kind(name),
                        "base": base_key(name),
                        "path": None,
                        "doc_id": doc_id,
                    }
                )
        except HTTPException:
            shutil.rmtree(store.files_dir / job_id, ignore_errors=True)
            raise
        except UploadTooLarge as err:
            shutil.rmtree(store.files_dir / job_id, ignore_errors=True)
            raise HTTPException(status_code=413, detail=str(err)) from err

        # drop duplicate entries: same doc_id twice, or the *same filename with
        # identical content* twice (a repeated upload of the same file). Two
        # documents with different names but identical bytes — e.g. a PO and a
        # delivery note that match perfectly — are legitimately distinct and
        # must both survive, or a clean reconciliation would vanish.
        upload_seen: dict[tuple[str, str], str] = {}
        unique_entries: list[dict[str, Any]] = []
        seen_identity: set[str] = set()
        for entry in entries:
            identity = entry["doc_id"] or entry["path"]
            if identity in seen_identity:
                continue
            if entry["path"]:
                digest = hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
                same_upload = (digest, entry.get("orig_name") or entry["name"])
                if same_upload in upload_seen:
                    continue
                upload_seen[same_upload] = identity
            seen_identity.add(identity)
            unique_entries.append(entry)
        entries = unique_entries
        if not entries:
            raise HTTPException(status_code=400, detail="no files or documents provided")
        if len(entries) < 2:
            raise HTTPException(
                status_code=422,
                detail="at least two distinct documents are required to compare",
            )

        pairs: list[dict[str, Any]] = []
        unpaired: list[str] = []
        groups: dict[str, list[str]] = {}
        for entry in entries:
            groups.setdefault(entry["base"], []).append(entry["name"])
        for key, names in groups.items():
            names = sorted(names)
            if len(names) < 2:
                unpaired.extend(names)
                continue
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    pairs.append(
                        {"key": key, "left": names[i], "right": names[j], "status": "queued"}
                    )
        if not pairs:
            raise HTTPException(
                status_code=422,
                detail="no comparable pairs found: documents must share a grouping key in their name",
            )

        job: dict[str, Any] = {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "config": cfg,
            "files": entries,
            "unpaired": unpaired,
            "pairs": pairs,
            "progress": {"done": 0, "total": len(pairs)},
        }
        store.create_job(job)
        worker.submit(job_id)
        return _job_view(job)

    @api.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_view(job)

    @api.get("/reports/{report_id}")
    def get_report(report_id: str) -> JSONResponse:
        if not safe_id(report_id):
            raise HTTPException(status_code=404, detail="report not found")
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        return JSONResponse(report)

    @api.get("/rules")
    def rules() -> dict[str, Any]:
        names: list[str] = []
        if rules_dir is not None:
            names = sorted(p.name for p in rules_dir.glob("*.yaml"))
        return {"default": "auto-numeric-diff (built-in)", "files": names}

    # -------------------------------------------------------- documents
    @api.get("/documents")
    def list_documents() -> dict[str, Any]:
        return {"documents": documents.list()}

    @api.post("/documents")
    async def register_documents(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        created: list[dict[str, Any]] = []
        for upload in files:
            name = upload.filename or "unnamed"
            data = bytearray()
            while chunk := await upload.read(1024 * 1024):
                if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"{name}: file too large")
                data.extend(chunk)
            if not data:
                raise HTTPException(status_code=422, detail=f"{name}: empty upload")
            doc_id = documents.register(name, bytes(data), source="upload")
            meta = documents.get(doc_id)
            if meta is not None:
                created.append(meta)
        return {"documents": created}

    @api.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> dict[str, Any]:
        if not safe_id(doc_id):
            raise HTTPException(status_code=404, detail="document not found")
        meta = documents.get(doc_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="document not found")
        return {**meta, "preview": _doc_preview(documents, meta)}

    @api.get("/documents/{doc_id}/content")
    def document_content(doc_id: str) -> Response:
        if not safe_id(doc_id):
            raise HTTPException(status_code=404, detail="document not found")
        meta = documents.get(doc_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="document not found")
        data = documents.content(doc_id)
        if data is None:
            raise HTTPException(status_code=404, detail="document content missing")
        safe_name = re.sub(r'[\r\n"]', "", str(meta["name"]))[:120] or "download"
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    @api.delete("/documents/{doc_id}")
    def delete_document(doc_id: str) -> dict[str, bool]:
        return {"ok": documents.delete(doc_id)}

    # ------------------------------------------------------- datasources
    @api.get("/datasources")
    def list_datasources() -> dict[str, Any]:
        sources = load_datasources(root)
        return {"datasources": [s.to_dict(mask=True) for s in sources]}

    @api.post("/datasources")
    def create_datasource(payload: dict[str, Any]) -> dict[str, Any]:
        if not str(payload.get("name", "")).strip():
            raise HTTPException(status_code=400, detail="name is required")
        if not str(payload.get("url", "")).strip():
            raise HTTPException(status_code=400, detail="url is required")
        source = DataSource.from_dict(payload)
        source.id = uuid.uuid4().hex[:10]
        save_datasources(root, load_datasources(root) + [source])
        return source.to_dict(mask=True)

    @api.put("/datasources/{ds_id}")
    def update_datasource(ds_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        sources = load_datasources(root)
        existing = _require_datasource(ds_id, sources)
        if not str(payload.get("name", "")).strip():
            raise HTTPException(status_code=400, detail="name is required")
        if not str(payload.get("url", "")).strip():
            raise HTTPException(status_code=400, detail="url is required")
        if not payload.get("token"):
            payload["token"] = existing.token  # empty/omitted keeps the stored secret
        payload["id"] = ds_id  # a body-borne id must not silently retarget
        updated = DataSource.from_dict(payload, existing_id=ds_id)
        sources = [updated if s.id == ds_id else s for s in sources]
        save_datasources(root, sources)
        return updated.to_dict(mask=True)

    @api.delete("/datasources/{ds_id}")
    def delete_datasource(ds_id: str) -> dict[str, bool]:
        sources = load_datasources(root)
        _require_datasource(ds_id, sources)
        save_datasources(root, [s for s in sources if s.id != ds_id])
        return {"ok": True}

    @api.post("/datasources/{ds_id}/probe")
    def probe_datasource(ds_id: str) -> dict[str, Any]:
        source = _require_datasource(ds_id, load_datasources(root))
        try:
            return source.probe()
        except Exception as err:  # noqa: BLE001 - surfaced to the UI
            return {"ok": False, "error": str(err)}

    @api.post("/datasources/{ds_id}/list")
    def list_datasource_items(ds_id: str) -> dict[str, Any]:
        source = _require_datasource(ds_id, load_datasources(root))
        try:
            return {"items": source.items()}
        except Exception as err:  # noqa: BLE001 - surfaced to the UI
            raise HTTPException(status_code=422, detail=str(err)) from err

    @api.post("/datasources/{ds_id}/fetch")
    async def fetch_from_datasource(
        ds_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        source = _require_datasource(ds_id, load_datasources(root))
        record_id = (payload or {}).get("record_id") or None
        try:
            if source.type == "records":
                records = source.records(record_id)
                if not records:
                    raise HTTPException(
                        status_code=422,
                        detail=("data source returned no records — check records_path / record_id"),
                    )
                name = (record_id or source.name or "records") + ".json"
                data = json.dumps(records, ensure_ascii=False).encode("utf-8")
                doc_id = documents.register(
                    name, data, source="records", datasource_id=ds_id, ext_hint=".json"
                )
            else:
                data = source.fetch_bytes(record_id)
                name = record_id or (Path(source.url.split("?")[0]).name or source.name or "file")
                doc_id = documents.register(name, data, source="datasource", datasource_id=ds_id)
        except HTTPError as err:
            raise HTTPException(status_code=502, detail=f"fetch failed: {err}") from err
        meta = documents.get(doc_id)
        if meta is None:
            raise HTTPException(status_code=500, detail="document registration failed")
        return meta

    app.include_router(api)

    static = STATIC_DIR
    if static.is_dir():
        app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "error": job.get("error"),
        "progress": job.get("progress"),
        "files": job["files"],
        "unpaired": job.get("unpaired", []),
        "pairs": [
            {
                "key": p.get("key"),
                "left": p["left"],
                "right": p["right"],
                "status": p.get("status"),
                "findings": p.get("findings"),
                "high": p.get("high"),
                "medium": p.get("medium"),
                "low": p.get("low"),
                "aligned_rows": p.get("aligned_rows"),
                "report_id": p.get("report_id"),
                "error": p.get("error"),
            }
            for p in job.get("pairs", [])
        ],
    }


def run(host: str = "0.0.0.0", port: int = 8765) -> None:
    """Console entry point: ``reconcheck-api``."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
