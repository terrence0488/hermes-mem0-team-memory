"""Load and validate the plugin's user-side JSON config file.

The file holds service keys, so it must be owned by the user running Hermes and must not be
readable by anyone else. Nothing here touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "team_memory.json"
CONFIG_PATH_ENV = "TEAM_MEMORY_CONFIG"
# Entries are short notes, each embedded as a single vector.
MAX_SHAREABLE_CHARS = 3000

_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    """The config file is missing, unsafe or invalid. The message never contains secrets."""


@dataclass(frozen=True)
class QdrantConfig:
    url: str
    api_key: str
    collection: str
    timeout_seconds: float


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimensions: int
    api_key: str
    document_prefix: str
    query_prefix: str
    timeout_seconds: float


@dataclass(frozen=True)
class AutoRecallConfig:
    enabled: bool
    limit: int
    score_threshold: float
    min_chars: int
    timeout_seconds: float


@dataclass(frozen=True)
class Config:
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    allowed_platforms: frozenset[str]
    admins: frozenset[str]
    confirm_tool_share: bool
    max_chars: int
    bot_name: str
    search_limit: int
    search_score_threshold: float
    auto_recall: AutoRecallConfig


def config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / CONFIG_FILENAME


def _section(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be an object")
    return value


def _required_str(section: dict, name: str, label: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{label}' is required")
    if "REPLACE_ME" in value:
        raise ConfigError(f"'{label}' still holds a placeholder from the example config")
    return value.strip()


def _optional_str(section: dict, name: str, label: str) -> str:
    value = section.get(name, "")
    if not isinstance(value, str):
        raise ConfigError(f"'{label}' must be a string")
    if "REPLACE_ME" in value:
        raise ConfigError(f"'{label}' still holds a placeholder from the example config")
    return value


def _number(section: dict, name: str, label: str, default: Any, *, minimum: float, maximum: float) -> Any:
    value = section.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{label}' must be a number")
    if not minimum <= value <= maximum:
        raise ConfigError(f"'{label}' must be between {minimum} and {maximum}")
    return value


def _integer(section: dict, name: str, label: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _number(section, name, label, default, minimum=minimum, maximum=maximum)
    if not isinstance(value, int):
        raise ConfigError(f"'{label}' must be a whole number")
    return value


def _boolean(section: dict, name: str, label: str, default: bool) -> bool:
    value = section.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{label}' must be true or false")
    return value


def _string_list(section: dict, name: str, label: str) -> list[str]:
    value = section.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"'{label}' must be a list of non-empty strings")
    if any("REPLACE_ME" in item for item in value):
        raise ConfigError(f"'{label}' still holds a placeholder from the example config")
    return [item.strip() for item in value]


def _service_url(section: dict, name: str, label: str, has_key: bool) -> str:
    url = _required_str(section, name, label).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"'{label}' must be an http or https URL")
    if parts.username or parts.password:
        raise ConfigError(f"'{label}' must not contain credentials")
    if has_key and parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        logger.warning("team_memory: '%s' sends an API key over plain http to a non-loopback host", label)
    return url


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("the config must be a JSON object")

    qdrant = _section(data, "qdrant")
    qdrant_key = _optional_str(qdrant, "api_key", "qdrant.api_key")
    collection = qdrant.get("collection", "team_memory")
    if not isinstance(collection, str) or not _COLLECTION_RE.match(collection):
        raise ConfigError("'qdrant.collection' may only use letters, digits, '_' and '-'")

    embedding = _section(data, "embedding")
    embedding_key = _optional_str(embedding, "api_key", "embedding.api_key")

    access = _section(data, "access")
    platforms = frozenset(p.lower() for p in _string_list(access, "allowed_platforms", "access.allowed_platforms"))
    if not platforms:
        raise ConfigError("'access.allowed_platforms' must name at least one platform")
    admins = frozenset(_string_list(access, "admins", "access.admins"))
    for admin in admins:
        if ":" not in admin:
            raise ConfigError("'access.admins' entries must look like 'platform:user_id'")

    sharing = _section(data, "sharing")
    search = _section(data, "search")
    recall = _section(data, "auto_recall")

    return Config(
        qdrant=QdrantConfig(
            url=_service_url(qdrant, "url", "qdrant.url", bool(qdrant_key)),
            api_key=qdrant_key,
            collection=collection,
            timeout_seconds=_number(qdrant, "timeout_seconds", "qdrant.timeout_seconds", 5, minimum=0.1, maximum=60),
        ),
        embedding=EmbeddingConfig(
            base_url=_service_url(embedding, "base_url", "embedding.base_url", bool(embedding_key)),
            model=_required_str(embedding, "model", "embedding.model"),
            dimensions=_integer(embedding, "dimensions", "embedding.dimensions", 0, minimum=1, maximum=65536),
            api_key=embedding_key,
            document_prefix=_optional_str(embedding, "document_prefix", "embedding.document_prefix"),
            query_prefix=_optional_str(embedding, "query_prefix", "embedding.query_prefix"),
            timeout_seconds=_number(
                embedding, "timeout_seconds", "embedding.timeout_seconds", 10, minimum=0.1, maximum=120),
        ),
        allowed_platforms=platforms,
        admins=admins,
        confirm_tool_share=_boolean(sharing, "confirm_tool_share", "sharing.confirm_tool_share", True),
        max_chars=_integer(sharing, "max_chars", "sharing.max_chars", 2000, minimum=1, maximum=MAX_SHAREABLE_CHARS),
        bot_name=_optional_str(sharing, "bot_name", "sharing.bot_name").strip(),
        search_limit=_integer(search, "limit", "search.limit", 5, minimum=1, maximum=50),
        search_score_threshold=_number(search, "score_threshold", "search.score_threshold", 0.0, minimum=-1, maximum=1),
        auto_recall=AutoRecallConfig(
            enabled=_boolean(recall, "enabled", "auto_recall.enabled", False),
            limit=_integer(recall, "limit", "auto_recall.limit", 3, minimum=1, maximum=20),
            score_threshold=_number(recall, "score_threshold", "auto_recall.score_threshold", 0.6, minimum=-1, maximum=1),
            min_chars=_integer(recall, "min_chars", "auto_recall.min_chars", 12, minimum=1, maximum=10000),
            timeout_seconds=_number(recall, "timeout_seconds", "auto_recall.timeout_seconds", 3, minimum=0.1, maximum=25),
        ),
    )


def read_config(path: Path) -> Config:
    """Read *path*, refusing a file that other users could read or that someone else owns."""
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from None
    if os.name == "posix":
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigError(f"config file {path} must not be accessible by group or others (use chmod 600)")
        if info.st_uid != os.getuid():
            raise ConfigError(f"config file {path} must be owned by the user running Hermes")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from None
    except ValueError as exc:
        # JSONDecodeError's message carries a position, never the document text.
        line = getattr(exc, "lineno", "?")
        raise ConfigError(f"config file {path} is not valid JSON (line {line})") from None
    return parse_config(data)


_lock = threading.Lock()
_cached: tuple[tuple, Config] | None = None


def load_config() -> Config:
    """Return the current config, re-reading the file only when it changes."""
    global _cached
    path = config_path()
    try:
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid)
    except OSError:
        key = None
    with _lock:
        if key is not None and _cached is not None and _cached[0] == key:
            return _cached[1]
    config = read_config(path)
    with _lock:
        _cached = (key, config) if key is not None else None
    return config


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None
