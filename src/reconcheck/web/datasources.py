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

import ipaddress
import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0)
MAX_BYTES = 50 * 1024 * 1024  # 50 MB per fetched file (streaming cap)
PROBE_BYTES = 64 * 1024  # connectivity probe reads only this much
SAFE_DS_ID = re.compile(r"^[a-z0-9]{6,16}$")


def guard_url(url: str, allow_private: bool | None = None) -> str | None:
    """Return a reason string when ``url`` is not a safe fetch target (SSRF guard).

    Only http(s) is allowed, and the target must resolve to public addresses —
    loopback, private, link-local (169.254.0.0/16 incl. cloud metadata),
    multicast, reserved and unspecified addresses are all blocked.

    ``RECONCHECK_ALLOW_PRIVATE_FETCH=1`` disables the address check for
    operator-run environments that genuinely reach intranet endpoints (the
    loopback test server also needs it); the scheme check always applies.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "malformed URL"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme {parsed.scheme!r} (http/https only)"
    if allow_private is None:
        allow_private = os.environ.get("RECONCHECK_ALLOW_PRIVATE_FETCH") == "1"
    if allow_private:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "URL has no host"
    if host == "localhost" or host.endswith(".localhost"):
        return "loopback target is blocked"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"cannot resolve {host}"
    for info in infos:
        ip = (info[4][0] or "").split("%")[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return f"target resolves to a non-public address ({ip})"
    return None


class _CappedResponse:
    """Minimal httpx.Response stand-in whose body is capped while streaming."""

    def __init__(
        self,
        request: httpx.Request,
        status_code: int,
        headers: httpx.Headers,
        content: bytes,
    ) -> None:
        self.request = request
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code} for {self.request.url}",
                request=self.request,
                response=self,
            )

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


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
        raw_id = payload.get("id") or existing_id or uuid.uuid4().hex[:10]
        if not SAFE_DS_ID.fullmatch(str(raw_id)):
            raise ValueError(f"data source id {str(raw_id)[:20]!r} is not allowed")
        return cls(
            id=str(raw_id),
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
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.auth == "header" and self.token and self.header_name:
            headers[self.header_name] = self.token
        return headers

    def url_for(self, record_id: str | None = None, listing: bool = False) -> str:
        url = (self.list_url if listing and self.list_url else self.url).strip()
        if record_id and "{id}" in url:
            # quote so a record id cannot smuggle path/query characters
            url = url.replace("{id}", quote(record_id, safe=""))
        return url

    def _request(
        self,
        record_id: str | None = None,
        listing: bool = False,
        max_bytes: int | None = None,
        raise_on_cap: bool = True,
    ) -> _CappedResponse:
        """GET/POST the endpoint, streaming the body with a hard size cap."""
        if max_bytes is None:
            max_bytes = MAX_BYTES  # read at call time so tests can patch the constant
        url = self.url_for(record_id, listing=listing)
        blocked = guard_url(url)
        if blocked:
            raise httpx.RequestError(f"blocked target: {blocked}", request=httpx.Request("GET", url))
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            with client.stream(self.method, url, headers=self.headers_for()) as resp:
                # a redirect may have landed somewhere the guard rejected
                redirected = guard_url(str(resp.url))
                if redirected:
                    raise httpx.RequestError(
                        f"redirected to blocked target: {redirected}", request=resp.request
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        if raise_on_cap:
                            raise httpx.RequestError(
                                f"response too large (> {max_bytes} bytes): {url}",
                                request=resp.request,
                            )
                        break  # probe/peek: keep what we read
                    chunks.append(chunk)
                return _CappedResponse(
                    resp.request, resp.status_code, resp.headers, b"".join(chunks)
                )

    def fetch_bytes(self, record_id: str | None = None) -> bytes:
        """File-type fetch: return the raw body."""
        r = self._request(record_id)
        r.raise_for_status()
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
        """Connectivity test used by the frontend 'test' button (bounded read)."""
        try:
            r = self._request(max_bytes=min(MAX_BYTES, PROBE_BYTES), raise_on_cap=True)
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
        # belt and braces: the id is validated upstream, but the filename is
        # the final authority — never let an id escape the json file namespace
        stem = re.sub(r"[^a-z0-9]", "", source.id)
        if not stem:
            stem = uuid.uuid4().hex[:10]
        seen.add(stem)
        (folder / f"{stem}.json").write_text(
            json.dumps(source.to_dict(mask=False), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    for path in folder.glob("*.json"):
        if path.stem not in seen:
            path.unlink(missing_ok=True)
