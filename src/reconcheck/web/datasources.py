"""User-configured enterprise data sources (custom web APIs).

A data source is a small, inspectable wrapper around an HTTP endpoint:

* ``type=file``    — the endpoint returns the document as a byte stream
  (CSV/XLSX/PDF — anything the pipeline can parse).
* ``type=records`` — the endpoint returns JSON; a JSON path selects an array
  of flat records that is converted into a table
  (``parse.document_from_records``).

Authentication: ``none``, ``bearer`` (``Authorization: Bearer <token>``) or
``header`` (a custom header whose name is stored in ``header_name``). Tokens
are stored in the local data directory and never echoed by the API.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0)
MAX_BYTES = 50 * 1024 * 1024  # 50 MB per fetched file


@dataclass
class DataSource:
    id: str
    name: str
    type: str = "file"  # file | records
    url: str = ""
    method: str = "GET"
    auth: str = "none"  # none | bearer | header
    token: str = ""
    header_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    records_path: str = ""  # dot path into the JSON reply, e.g. "data.items"
    id_field: str = ""
    name_field: str = ""
    list_url: str = ""  # optional: endpoint returning {id, name} entries

    # -- serialisation --------------------------------------------------
    def to_dict(self, mask: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "method": self.method,
            "auth": self.auth,
            "header_name": self.header_name,
            "headers": self.headers,
            "records_path": self.records_path,
            "id_field": self.id_field,
            "name_field": self.name_field,
            "list_url": self.list_url,
        }
        if mask:
            out["has_token"] = bool(self.token)
        else:
            out["token"] = self.token
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any], existing_id: str | None = None) -> DataSource:
        raw_token = payload.get("token")
        return cls(
            id=payload.get("id") or existing_id or uuid.uuid4().hex[:10],
            name=str(payload.get("name", "")).strip(),
            type=str(payload.get("type", "file")),
            url=str(payload.get("url", "")).strip(),
            method=str(payload.get("method", "GET")).upper(),
            auth=str(payload.get("auth", "none")),
            token=str(raw_token) if raw_token else "",
            header_name=str(payload.get("header_name", "")).strip(),
            headers=payload.get("headers") or {},
            records_path=str(payload.get("records_path", "")).strip(),
            id_field=str(payload.get("id_field", "")).strip(),
            name_field=str(payload.get("name_field", "")).strip(),
            list_url=str(payload.get("list_url", "")).strip(),
        )

    # -- HTTP ------------------------------------------------------------
    def headers_for(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.auth == "bearer" and self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        elif self.auth == "header" and self.token and self.header_name:
            headers.setdefault(self.header_name, self.token)
        return headers

    def url_for(self, record_id: str | None = None, listing: bool = False) -> str:
        url = (self.list_url if listing and self.list_url else self.url).strip()
        if record_id and "{id}" in url:
            url = url.replace("{id}", record_id)
        return url

    def _request(self, record_id: str | None = None, listing: bool = False) -> httpx.Response:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            return client.request(
                self.method,
                self.url_for(record_id, listing=listing),
                headers=self.headers_for(),
            )

    def fetch_bytes(self, record_id: str | None = None) -> bytes:
        """File-type fetch: return the raw body."""
        r = self._request(record_id)
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            raise httpx.RequestError(
                f"response too large ({len(r.content)} bytes)", request=r.request
            )
        return r.content

    def fetch_json(self, record_id: str | None = None, listing: bool = False) -> Any:
        r = self._request(record_id, listing=listing)
        r.raise_for_status()
        return r.json()

    def records(self, record_id: str | None = None) -> list[dict[str, Any]]:
        """Extract the array of records (type=records) from the reply."""
        data = self.fetch_json(record_id)
        if self.records_path:
            data = deep_get(data, self.records_path)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            data = []
        if record_id:
            id_field = self.id_field or "id"
            for rec in data:
                if isinstance(rec, dict) and str(rec.get(id_field)) == str(record_id):
                    return [rec]
            return []
        return [d for d in data if isinstance(d, dict)]

    def items(self) -> list[dict[str, Any]]:
        """Human-pickable entries ``{id, name}`` for the picker UI."""
        if self.type == "file" and self.list_url:
            data = self.fetch_json(listing=True)
            if self.records_path:
                data = deep_get(data, self.records_path)
        else:
            data = self.fetch_json()
            if self.records_path:
                data = deep_get(data, self.records_path)
        entries = data if isinstance(data, list) else []
        id_field, name_field = (self.id_field or "id"), (self.name_field or "name")
        out: list[dict[str, Any]] = []
        for e in entries:
            if isinstance(e, dict) and e.get(id_field) is not None:
                out.append({"id": str(e[id_field]), "name": str(e.get(name_field) or e[id_field])})
        return out

    def probe(self) -> dict[str, Any]:
        """Connectivity test used by the frontend 'test' button."""
        try:
            r = self._request()
            ok = 200 <= r.status_code < 300
            return {
                "ok": ok,
                "status": r.status_code,
                "bytes": len(r.content),
                "head": r.text[:200],
            }
        except httpx.HTTPError as err:
            return {"ok": False, "error": f"{type(err).__name__}: {err}"}
        except ValueError as err:
            return {"ok": False, "error": str(err)}


def deep_get(obj: Any, path: str) -> Any:
    """Resolve a dotted path (``a.b.c``) inside nested JSON."""
    for part in path.split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


# ---------------------------------------------------------------------------
# persistence: one JSON file per data source under <root>/datasources/
# ---------------------------------------------------------------------------


def load_datasources(root: Path) -> list[DataSource]:
    folder = root / "datasources"
    out: list[DataSource] = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("id"):
                out.append(DataSource.from_dict(payload))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def save_datasources(root: Path, sources: list[DataSource]) -> None:
    folder = root / "datasources"
    folder.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for source in sources:
        seen.add(source.id)
        (folder / f"{source.id}.json").write_text(
            json.dumps(source.to_dict(mask=False), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    for path in folder.glob("*.json"):
        if path.stem not in seen:
            path.unlink(missing_ok=True)
