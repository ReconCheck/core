"""Document library: registered files/records reusable across comparisons."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


class DocumentStore:
    """Disk-backed library of "documents" (files or fetched records).

    Layout: ``<root>/documents/<doc_id>/`` with ``original<ext>`` (raw bytes)
    plus ``meta.json`` (name, source, size, ...).
    """

    def __init__(self, root: Path) -> None:
        self.docs_dir = root / "documents"
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def register(
        self,
        name: str,
        data: bytes,
        *,
        source: str = "upload",
        datasource_id: str | None = None,
        ext_hint: str | None = None,
    ) -> str:
        """Store raw bytes and return the new document id."""
        doc_id = uuid.uuid4().hex[:12]
        folder = self.docs_dir / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        ext = (ext_hint or Path(name).suffix or ".bin").lower()
        (folder / f"original{ext}").write_bytes(data)
        meta = {
            "id": doc_id,
            "name": name,
            "ext": ext,
            "source": source,
            "datasource_id": datasource_id,
            "size": len(data),
            "created_at": time.time(),
        }
        (folder / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        return doc_id

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for folder in self.docs_dir.iterdir():
            meta = self._read_meta(folder)
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m["created_at"], reverse=True)
        return out

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self._read_meta(self.docs_dir / doc_id)

    def content(self, doc_id: str) -> bytes | None:
        path = self.content_path(doc_id)
        if path is None:
            return None
        return path.read_bytes()

    def content_path(self, doc_id: str, meta: dict[str, Any] | None = None) -> Path | None:
        """On-disk path of the raw content (None when the meta is missing)."""
        meta = meta or self.get(doc_id)
        if meta is None:
            return None
        path = self.docs_dir / doc_id / f"original{meta['ext']}"
        if not path.exists():
            return None
        return path

    def delete(self, doc_id: str) -> bool:
        folder = self.docs_dir / doc_id
        if not folder.is_dir():
            return False
        for child in folder.iterdir():
            child.unlink(missing_ok=True)
        folder.rmdir()
        return True

    def _read_meta(self, folder: Path) -> dict[str, Any] | None:
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta if isinstance(meta, dict) else None
        except (json.JSONDecodeError, OSError):
            return None
