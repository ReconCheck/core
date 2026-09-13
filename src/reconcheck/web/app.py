"""ReconCheck REST API (FastAPI) and job runner.

Endpoints
---------
* ``GET   /api/health``                 — liveness + engine version
* ``POST  /api/compare``                — synchronous comparison of two files
* ``POST  /api/jobs``                   — async batch comparison of many files
* ``GET   /api/jobs/{job_id}``          — job status / progress / pair summary
* ``GET   /api/reports/{report_id}``    — stored comparison report (JSON)
* ``GET   /api/rules``                  — available rule sets

Authentication (optional): set ``RECONCHECK_API_KEY``. When set, every ``/api/*``
request must carry ``X-API-Key: <key>`` (the static frontend works without it).

Run: ``reconcheck-api`` (binds ``0.0.0.0:8765`` by default).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..comparison import compare_files
from ..errors import ReconCheckError

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULES_DIR = REPO_ROOT / "examples" / "rules"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


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
        try:
            self._index_path.write_text(
                json.dumps(self._jobs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

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
        with target.open("wb") as out:
            while chunk := upload.file.read(1024 * 1024):
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
    "po": ("po", "purchase", "order", "采购", "订单", "订购"),
    "invoice": ("inv", "invoice", "发票", "iv", "ir"),
    "delivery": ("dn", "delivery", "送货", "收货", "asn", "发货"),
}


def guess_kind(filename: str) -> str:
    base = Path(filename).stem.lower()
    for kind, tokens in _KIND_TOKENS.items():
        if any(tok in base for tok in tokens):
            return kind
    return "unknown"


def _strip_kind_token(base: str) -> str:
    lowered = base.lower()
    for tokens in _KIND_TOKENS.values():
        for tok in tokens:
            lowered = lowered.replace(tok, "")
    return lowered


def base_key(filename: str) -> str:
    """Grouping key: filename minus kind tokens, extension and noise."""
    stem = Path(filename).stem
    stripped = _strip_kind_token(stem)
    key = re.sub(r"[^a-z0-9]+", "", stripped.lower())
    return key or re.sub(r"[^a-z0-9]+", "", stem.lower())


# ---------------------------------------------------------------------------
# backend worker: one thread, one queue
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, store: JobStore, rules_dir: Path | None) -> None:
        self.store = store
        self.rules_dir = rules_dir
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reconcheck-worker")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)
        self._thread = None

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

    def _process(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return
        self.store.update_job(job_id, status="running", started_at=time.time())
        pairs = job["pairs"]
        for i, pair in enumerate(pairs):
            try:
                report, _findings = compare_files(
                    pair["left_path"],
                    pair["right_path"],
                    rules=self.rules_dir,
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
            self.store.update_job(
                job_id, progress={"done": i + 1, "total": len(pairs)}
            )
        self.store.update_job(job_id, status="done", finished_at=time.time())


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def _auth_dependency(api_key: str | None):
    def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")

    return require_key


def create_app(
    data_dir: str | Path | None = None,
    api_key: str | None = None,
) -> FastAPI:
    root = Path(data_dir or os.environ.get("RECONCHECK_DATA") or (Path.cwd() / "data"))
    root.mkdir(parents=True, exist_ok=True)
    store = JobStore(root)
    store.load()
    rules_dir = _default_rules_dir()
    worker = Worker(store, rules_dir)

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
    api = APIRouter(prefix="/api", dependencies=[Depends(_auth_dependency(key))])

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "engine": "reconcheck", "version": __version__}

    @api.post("/compare")
    async def compare(files: list[UploadFile] = File(...)) -> JSONResponse:
        if len(files) != 2:
            raise HTTPException(status_code=400, detail="exactly two files are required")
        tmp = root / "tmp" / uuid.uuid4().hex[:8]
        tmp.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        try:
            for i, upload in enumerate(files):
                target = tmp / _SAFE_NAME.sub("_", upload.filename or f"file_{i}")
                with target.open("wb") as out:
                    while chunk := await upload.read(1024 * 1024):
                        out.write(chunk)
                paths.append(target)
            report, _findings = compare_files(
                paths[0], paths[1], rules=rules_dir
            )
            return JSONResponse(report)
        except ReconCheckError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        finally:
            for p in paths:
                p.unlink(missing_ok=True)
            tmp.rmdir() if tmp.exists() else None

    @api.post("/jobs")
    async def create_job(
        files: list[UploadFile] = File(...),
        config: str = Form("{}"),
    ) -> dict[str, Any]:
        try:
            cfg = json.loads(config or "{}")
            if not isinstance(cfg, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as err:
            raise HTTPException(status_code=400, detail="config must be a JSON object") from err

        job_id = uuid.uuid4().hex[:12]
        entries: list[dict[str, Any]] = []
        saved: dict[str, Path] = {}
        for i, upload in enumerate(files):
            name = upload.filename or f"file_{i}"
            path = store.save_upload(job_id, upload, i)
            saved[name] = path
            entries.append(
                {
                    "name": name,
                    "kind": guess_kind(name),
                    "base": base_key(name),
                    "path": str(path),
                }
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
                        {
                            "key": key,
                            "left": names[i],
                            "right": names[j],
                            "left_path": str(saved[names[i]]),
                            "right_path": str(saved[names[j]]),
                            "status": "queued",
                        }
                    )
        if not pairs:
            raise HTTPException(
                status_code=422,
                detail="no comparable pairs found: files must share a grouping key in their name",
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

    app.include_router(api)

    static = STATIC_DIR
    if static.is_dir():
        app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
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
