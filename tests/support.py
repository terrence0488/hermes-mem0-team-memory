"""Test helpers: a stand-in for the Hermes session API, plus fake Qdrant and embedding servers.

Tests run on the standard library alone and never need Hermes. ``fake session context`` below
copies the documented behaviour of ``gateway.session_context``: per-task ContextVars, with a
fallback to ``os.environ`` when a variable was never bound. ``test_hermes_integration.py`` runs
the same checks against the real module.
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "team_memory_under_test"
DIMENSIONS = 16
QDRANT_KEY = "test-qdrant-key"

logging.getLogger(PACKAGE).setLevel(logging.CRITICAL)  # refusals are asserted, not printed

_UNSET = object()
_VARS = {
    name: contextvars.ContextVar(name, default=_UNSET)
    for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID", "HERMES_SESSION_USER_NAME")
}
_engaged = False


def _get_session_env(name, default=""):
    value = _VARS[name].get() if name in _VARS else _UNSET
    return os.getenv(name, default) if value is _UNSET else value


def _session_context_engaged():
    return _engaged


def using_real_hermes() -> bool:
    return os.environ.get("TEAM_MEMORY_REAL_HERMES") == "1"


def _install_fake_hermes() -> None:
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = _get_session_env
    session_context.session_context_engaged = _session_context_engaged
    gateway.session_context = session_context
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tempfile.gettempdir()
    sys.modules.update({
        "gateway": gateway,
        "gateway.session_context": session_context,
        "hermes_constants": constants,
    })


if not using_real_hermes():
    _install_fake_hermes()


def load_plugin():
    """Import the plugin the way Hermes does: a package under a private name."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def plugin_module(name: str):
    load_plugin()
    return sys.modules[f"{PACKAGE}.{name}"]


def bind_session(platform="telegram", user_id="", user_name="") -> None:
    """Bind the sender for the current context, as the gateway does for each inbound message."""
    global _engaged
    if using_real_hermes():
        from gateway.session_context import set_session_vars

        set_session_vars(platform=platform, user_id=user_id, user_name=user_name)
        return
    _engaged = True
    _VARS["HERMES_SESSION_PLATFORM"].set(platform)
    _VARS["HERMES_SESSION_USER_ID"].set(user_id)
    _VARS["HERMES_SESSION_USER_NAME"].set(user_name)


def unbind_session() -> None:
    """Return to a process in which no session was ever bound."""
    global _engaged
    if using_real_hermes():
        from gateway.session_context import reset_session_vars

        reset_session_vars()
        return
    _engaged = False
    for var in _VARS.values():
        var.set(_UNSET)


def embed(text: str) -> list[float]:
    """A deterministic bag-of-words vector, so shared words produce a higher score."""
    vector = [0.0] * DIMENSIONS
    for word in re.findall(r"\w+", text.lower()):
        index = int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSIONS
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class Backend:
    """In-memory Qdrant and embedding endpoints on a loopback port, with a request log."""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.fail_with: int | None = None
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=None):
                data = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                backend.requests.append({
                    "method": self.command, "path": self.path, "body": body,
                    "headers": {key.lower(): value for key, value in self.headers.items()}})
                if backend.fail_with:
                    return self._reply(backend.fail_with, {"status": {"error": "secret-looking backend detail"}})
                if self.path.startswith("/v1/embeddings"):
                    return self._reply(200, {"data": [{"embedding": embed(body["input"])}]})
                if self.headers.get("api-key") != QDRANT_KEY:
                    return self._reply(401)
                return backend.qdrant(self, body)

            do_GET = do_PUT = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def qdrant(self, handler, body):
        match = re.fullmatch(r"/collections/([^/?]+)(/points[^?]*)?(\?.*)?", handler.path)
        if not match:
            return handler._reply(404)
        name, points_path = match.group(1), match.group(2)
        method = handler.command
        if points_path is None:
            if method == "PUT":
                self.collections[name] = {"size": body["vectors"]["size"], "points": {}}
                return handler._reply(200, {"result": True})
            if name not in self.collections:
                return handler._reply(404)
            size = self.collections[name]["size"]
            return handler._reply(200, {"result": {"config": {"params": {"vectors": {"size": size}}}}})
        if name not in self.collections:
            return handler._reply(404)
        points = self.collections[name]["points"]
        if points_path == "/points" and method == "PUT":
            for point in body["points"]:
                points[point["id"]] = point
            return handler._reply(200, {"result": {"status": "completed"}})
        if points_path == "/points/query":
            scored = []
            for point in points.values():
                score = sum(a * b for a, b in zip(body["query"], point["vector"]))
                if score >= body["score_threshold"]:
                    scored.append({"id": point["id"], "score": score, "payload": point["payload"]})
            scored.sort(key=lambda item: item["score"], reverse=True)
            return handler._reply(200, {"result": {"points": scored[:body["limit"]]}})
        if points_path == "/points/delete":
            for point_id in body["points"]:
                points.pop(point_id, None)
            return handler._reply(200, {"result": {"status": "completed"}})
        point_id = points_path.removeprefix("/points/")
        if point_id in points:
            point = points[point_id]
            return handler._reply(200, {"result": {"id": point_id, "payload": point["payload"]}})
        return handler._reply(404)

    def stored_points(self, collection="team_memory") -> list[dict]:
        return list(self.collections.get(collection, {}).get("points", {}).values())


def config_dict(port: int, **overrides) -> dict:
    config = {
        "qdrant": {"url": f"http://127.0.0.1:{port}", "api_key": QDRANT_KEY, "collection": "team_memory"},
        "embedding": {
            "base_url": f"http://127.0.0.1:{port}/v1", "model": "fake-embedder", "dimensions": DIMENSIONS,
            "document_prefix": "search_document: ", "query_prefix": "search_query: ",
        },
        "access": {"allowed_platforms": ["telegram"], "admins": ["telegram:admin-1"]},
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


class PluginTestCase(unittest.TestCase):
    """Each test gets a fresh backend, a config file with mode 600, and an unbound session."""

    config_overrides: dict = {}

    def setUp(self):
        self.plugin = load_plugin()
        self.tools = plugin_module("tools")
        self.hooks = plugin_module("hooks")
        self.config_module = plugin_module("config")
        self.backend = Backend()
        self.addCleanup(self.backend.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_file = Path(self.tmp.name) / "team_memory.json"
        self.write_config(**self.config_overrides)
        previous = os.environ.get("TEAM_MEMORY_CONFIG")
        os.environ["TEAM_MEMORY_CONFIG"] = str(self.config_file)
        self.addCleanup(self._restore_env, previous)
        self.config_module.reset_cache()
        self.tools.reset_store()
        unbind_session()
        self.addCleanup(unbind_session)

    @staticmethod
    def _restore_env(previous):
        if previous is None:
            os.environ.pop("TEAM_MEMORY_CONFIG", None)
        else:
            os.environ["TEAM_MEMORY_CONFIG"] = previous

    def write_config(self, **overrides):
        self.config_file.write_text(json.dumps(config_dict(self.backend.port, **overrides)))
        os.chmod(self.config_file, stat.S_IRUSR | stat.S_IWUSR)
        if hasattr(self, "config_module"):
            self.config_module.reset_cache()

    def call(self, handler, args, **kwargs) -> dict:
        result = handler(args, **kwargs)
        self.assertIsInstance(result, str, "handlers must return a JSON string")
        return json.loads(result)

    def share_as(self, user_id, text, user_name="Someone") -> dict:
        bind_session(user_id=user_id, user_name=user_name)
        result = self.call(self.tools.team_share, {"text": text})
        self.assertEqual(result.get("status"), "shared", result)
        return result
