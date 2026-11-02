"""Work out who is talking to Hermes, from data the model cannot touch.

The author always comes from the session Hermes bound for the inbound message (tools and hooks)
or from the gateway's ``source`` object (slash commands). Tool arguments are never consulted.
Anything uncertain is refused rather than attributed to a guess.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import Config

_MAX_NAME_CHARS = 64


class IdentityError(Exception):
    """No trustworthy sender for this call. The message is safe to show to the model."""


@dataclass(frozen=True)
class Author:
    platform: str
    user_id: str
    name: str

    @property
    def key(self) -> str:
        """Stable identity used for permissions. Platform ids alone can collide."""
        return f"{self.platform}:{self.user_id}"


def clean_display_name(name: str) -> str:
    """Display names are chosen by users, so drop control and format characters and cap the length."""
    kept = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
    return kept.strip()[:_MAX_NAME_CHARS]


def _build(platform: str, user_id: str, name: str, config: Config) -> Author:
    platform = platform.strip().lower()
    user_id = user_id.strip()
    if not platform or not user_id:
        raise IdentityError("team memory needs an identified sender, and this session has none")
    if platform not in config.allowed_platforms:
        raise IdentityError(f"team memory is not enabled for platform '{platform}'")
    return Author(platform=platform, user_id=user_id, name=clean_display_name(name) or user_id)


def author_from_session(config: Config) -> Author:
    """Read the sender bound to the current turn."""
    try:
        from gateway.session_context import get_session_env, session_context_engaged
    except ImportError:
        raise IdentityError("team memory needs the Hermes gateway session context") from None
    # get_session_env falls back to os.environ when a task has no binding. Before any session is
    # bound, an environment value is all it could return, and that is not a verified sender.
    if not session_context_engaged():
        raise IdentityError("team memory needs an identified sender, and this session has none")
    return _build(
        get_session_env("HERMES_SESSION_PLATFORM", ""),
        get_session_env("HERMES_SESSION_USER_ID", ""),
        get_session_env("HERMES_SESSION_USER_NAME", ""),
        config,
    )


def author_from_source(source: Any, platform: str, config: Config) -> Author:
    """Read the sender of a slash command from the gateway's SessionSource."""
    if source is None:
        raise IdentityError("/share only works from a chat platform with an identified sender")
    source_platform = getattr(getattr(source, "platform", None), "value", None) or platform or ""
    return _build(
        str(source_platform),
        str(getattr(source, "user_id", "") or ""),
        str(getattr(source, "user_name", "") or ""),
        config,
    )
