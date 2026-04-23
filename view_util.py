"""Shared discord.ui.View base with graceful error handling.

Discord persistent views don't route exceptions through the cog-level
app_command_error handler — the default on_error just logs to the
`discord.ui.view` logger, and the user sees the interaction silently
fail (or Discord's generic 'This interaction failed' toast). That's
miserable when the cause is a transient 5xx from Discord's own upstream.

ErrorHandledView catches any exception raised inside a button/select
callback, logs it with context, and sends an ephemeral message back to
the user with a friendlier description of the failure mode. Every
view in the bot inherits from this instead of discord.ui.View directly.
"""
from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


def _friendly_error_message(error: Exception) -> str:
    if isinstance(error, discord.DiscordServerError):
        return (
            "⚠ Discord had a server hiccup on their end (5xx). "
            "Nothing's wrong with your click — try again in a moment."
        )
    if isinstance(error, discord.Forbidden):
        return (
            "⚠ Permission error. The bot probably needs its role "
            "moved higher, or a permission granted. Flag an admin."
        )
    if isinstance(error, discord.NotFound):
        return "⚠ Discord couldn't find that — it may have been deleted."
    return f"⚠ `{type(error).__name__}`: {error}"


class ErrorHandledView(discord.ui.View):
    """View whose button/select callbacks can never silently fail.
    Replaces the default on_error with a logger + ephemeral-user-message
    pair so transient Discord outages are visible to the clicker."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        view_name = type(self).__name__
        item_label = getattr(item, "label", None) or type(item).__name__
        log.exception(
            "View %s item %r raised: %s", view_name, item_label, error,
        )
        msg = _friendly_error_message(error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            # Interaction token is gone (over 15 min) or Discord is
            # well and truly down — the log line is all we can leave.
            pass
