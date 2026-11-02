"""Qdrant and OpenAI-compatible embeddings over plain HTTP, using only the standard library."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .config import Config
from .identity import Author

SCHEMA_VERSION = 1


class StoreError(Exception):
    """A backend call failed. The message never contains keys, URLs or response bodies."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could carry the API key to another host, so treat it as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# No environment proxies either: service keys should not pass through one by accident.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _request(service: str, method: str, url: str, *, body: Any = None, headers: dict | None = None,
             timeout: float) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    all_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        raise StoreError(f"{service} returned HTTP {exc.code}", status=exc.code) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        reason = "timed out" if isinstance(getattr(exc, "reason", exc), TimeoutError) else "is unreachable"
        raise StoreError(f"{service} {reason}") from None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise StoreError(f"{service} returned an invalid response") from None
    if not isinstance(parsed, dict):
        raise StoreError(f"{service} returned an invalid response")
    return parsed


class TeamStore:
    """All access to the one configured collection. No other collection is ever addressed."""

    def __init__(self, config: Config):
        self._config = config
        self._ready = False
        self._ready_lock = threading.Lock()

    @property
    def _collection_url(self) -> str:
        qdrant = self._config.qdrant
        return f"{qdrant.url}/collections/{quote(qdrant.collection, safe='')}"

    def _qdrant(self, method: str, path: str = "", body: Any = None, timeout: float | None = None) -> dict:
        qdrant = self._config.qdrant
        headers = {"api-key": qdrant.api_key} if qdrant.api_key else None
        return _request("Qdrant", method, self._collection_url + path, body=body, headers=headers,
                        timeout=timeout or qdrant.timeout_seconds)

    def _embed(self, text: str, timeout: float | None = None) -> list[float]:
        embedding = self._config.embedding
        headers = {"Authorization": f"Bearer {embedding.api_key}"} if embedding.api_key else None
        response = _request("The embedding service", "POST", f"{embedding.base_url}/embeddings",
                            body={"model": embedding.model, "input": text}, headers=headers,
                            timeout=timeout or embedding.timeout_seconds)
        try:
            vector = response["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise StoreError("The embedding service returned an invalid response") from None
        if not isinstance(vector, list) or len(vector) != embedding.dimensions:
            raise StoreError(
                f"The embedding service returned a vector that does not match "
                f"embedding.dimensions ({embedding.dimensions})")
        return vector

    def _ensure_collection(self) -> None:
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:
                return
            dimensions = self._config.embedding.dimensions
            try:
                info = self._qdrant("GET")
            except StoreError as exc:
                if exc.status != 404:
                    raise
                body = {"vectors": {"size": dimensions, "distance": "Cosine"}}
                try:
                    self._qdrant("PUT", body=body)
                except StoreError as create_error:
                    if create_error.status != 409:  # another bot created it first
                        raise
                    info = self._qdrant("GET")
                else:
                    self._ready = True
                    return
            try:
                size = info["result"]["config"]["params"]["vectors"]["size"]
            except (KeyError, TypeError):
                raise StoreError("The Qdrant collection is not a single-vector collection") from None
            if size != dimensions:
                raise StoreError(f"The Qdrant collection has {size} dimensions, but the config says {dimensions}")
            self._ready = True

    def _call_with_collection(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> dict:
        self._ensure_collection()
        try:
            return self._qdrant(method, path, body=body, timeout=timeout)
        except StoreError as exc:
            if exc.status == 404:
                self._ready = False  # the collection may have been removed while we were running
            raise

    def add(self, text: str, author: Author) -> dict:
        """Store *text* byte for byte. Only the embedding input gets the configured prefix."""
        vector = self._embed(self._config.embedding.document_prefix + text)
        point_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "text": text,
            "author_id": author.key,
            "author_name": author.name,
            "created_at": created_at,
        }
        if self._config.bot_name:
            payload["bot"] = self._config.bot_name
        self._call_with_collection("PUT", "/points?wait=true",
                                   {"points": [{"id": point_id, "vector": vector, "payload": payload}]})
        return {"id": point_id, "created_at": created_at}

    def search(self, query: str, limit: int, score_threshold: float, *, timeout: float | None = None) -> list[dict]:
        vector = self._embed(self._config.embedding.query_prefix + query, timeout=timeout)
        response = self._call_with_collection(
            "POST", "/points/query",
            {"query": vector, "limit": limit, "score_threshold": score_threshold, "with_payload": True},
            timeout=timeout,
        )
        result = response.get("result")
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list):
            raise StoreError("Qdrant returned an invalid response")
        return points

    def get(self, point_id: str) -> dict | None:
        try:
            response = self._call_with_collection("GET", f"/points/{quote(point_id, safe='')}")
        except StoreError as exc:
            if exc.status == 404:
                return None
            raise
        result = response.get("result")
        return result if isinstance(result, dict) else None

    def delete(self, point_id: str) -> None:
        self._call_with_collection("POST", "/points/delete?wait=true", {"points": [point_id]})
