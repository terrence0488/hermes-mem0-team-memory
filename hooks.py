"""Lifecycle hooks: human confirmation for model-initiated shares, and optional auto-recall."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
from typing import Any

from .config import ConfigError, load_config
from .identity import IdentityError, author_from_session
from .store import StoreError
from .tools import TRUST_NOTICE, format_entry, get_store, validated_share_text

logger = logging.getLogger(__name__)

_PROMPT_HEADER = "Share this exact text with the team?\n\n"
# Hermes v0.22.5 cuts the approval reason to 300 escaped characters on Discord and 500 on Telegram
# and Slack. Past this size a person could approve words the prompt no longer shows.
MAX_PROMPT_CHARS = 300


def _prompt_length(message: str) -> int:
    """Measure the worst case: after HTML escaping, in UTF-16 units as Telegram counts them."""
    return len(html.escape(message).encode("utf-16-le")) // 2


def confirm_share(tool_name: str = "", args: Any = None, **kwargs: Any) -> dict | None:
    """Send every model-initiated ``team_share`` through Hermes's human-approval gate.

    Hermes runs this for all tools, and blocks the call if the gate is denied, times out or fails.
    Keying the rule on the text means an "always" answer can only ever cover these same words.
    Text too long for the prompt to show in full is blocked, so nobody approves unseen words.
    """
    if tool_name != "team_share":
        return None
    try:
        config = load_config()
    except ConfigError:
        return None  # the handler reports the config problem and stores nothing
    if not config.confirm_tool_share:
        return None
    try:
        text = validated_share_text(args if isinstance(args, dict) else {}, config)
    except ValueError:
        return None  # the handler rejects this input and stores nothing
    message = _PROMPT_HEADER + text
    if _prompt_length(message) > MAX_PROMPT_CHARS:
        return {
            "action": "block",
            "message": ("Not shared: this text is too long to show in full in the approval prompt. "
                        "Ask the user to send it themselves with /share <text>, or to shorten it."),
        }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {
        "action": "approve",
        "message": message,
        "rule_key": f"team_share:{digest}",
    }


def _message_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = [p.get("text", "") for p in user_message if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(part for part in parts if isinstance(part, str))
    return ""


def auto_recall(user_message: Any = None, parent_session_id: str = "", **kwargs: Any) -> dict | None:
    """Add matching team entries to the current turn. Any failure just means no extra context."""
    try:
        config = load_config()
        recall = config.auto_recall
        if not recall.enabled or parent_session_id:
            return None
        query = _message_text(user_message).strip()
        if len(query) < recall.min_chars or query.startswith("/"):
            return None
        author_from_session(config)
        points = get_store(config).search(
            query[:2000], recall.limit, recall.score_threshold, timeout=recall.timeout_seconds)
    except (ConfigError, IdentityError, StoreError) as exc:
        logger.debug("team_memory auto-recall skipped: %s", exc)
        return None
    except Exception:
        logger.exception("team_memory auto-recall failed")
        return None
    if not points:
        return None
    # JSON encoding keeps each entry on one line, and an entry cannot guess the random closing tag.
    tag = f"team_memory_data_{secrets.token_hex(8)}"
    lines = [json.dumps(format_entry(point), ensure_ascii=False) for point in points]
    block = "\n".join(lines)
    return {"context": f"Possibly relevant team memory. {TRUST_NOTICE}\n<{tag}>\n{block}\n</{tag}>"}
