"""team_memory: shared, opt-in team memory for Hermes Agent. Registration only; no I/O happens here."""

from __future__ import annotations

from . import hooks, schemas, tools


def register(ctx):
    for schema, handler in (
        (schemas.TEAM_SHARE, tools.team_share),
        (schemas.TEAM_SEARCH, tools.team_search),
        (schemas.TEAM_DELETE, tools.team_delete),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset="team_memory",
            schema=schema,
            handler=handler,
            check_fn=tools.is_available,
        )
    ctx.register_command(
        "share",
        handler=tools.share_command,
        description="Save text to the team memory, exactly as typed",
        args_hint="<text>",
    )
    ctx.register_hook("pre_tool_call", hooks.confirm_share)
    ctx.register_hook("pre_llm_call", hooks.auto_recall)
