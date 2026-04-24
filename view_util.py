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
import secrets

import discord

log = logging.getLogger(__name__)


def _short_correlation_id() -> str:
    # 8 hex chars = 4 bytes of entropy. Plenty to disambiguate a user's
    # ref in the log without being long enough to be awkward to read off
    # a phone screen.
    return secrets.token_hex(4)


async def handle_app_command_error(
    interaction: discord.Interaction,
    error: Exception,
    logger: logging.Logger,
) -> None:
    """Shared slash-command error handler used by every cog's
    `cog_app_command_error`. Logs the full error with a correlation ID
    and shows the user a generic message quoting that ID — we don't
    surface exception types or messages to the public, since those can
    leak schema names, filesystem paths, or upstream library internals."""
    cmd = interaction.command.name if interaction.command else "<unknown>"
    ref = _short_correlation_id()
    logger.exception(
        "Slash command /%s raised (ref=%s): %s", cmd, ref, error,
    )
    msg = _friendly_error_message(error, ref)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def _friendly_error_message(error: Exception, ref: str) -> str:
    # We deliberately do NOT include the exception type or message for
    # unknown errors — those can leak schema, filesystem paths, or
    # library internals to a public audience. Known-benign Discord
    # outages get specific copy; everything else goes through the
    # generic path with a correlation ID the user can quote to mods.
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
    return (
        "⚠ Something went wrong on our end. Try again; if it keeps "
        f"happening, flag a mod with reference `{ref}`."
    )


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
        ref = _short_correlation_id()
        log.exception(
            "View %s item %r raised (ref=%s): %s",
            view_name, item_label, ref, error,
        )
        msg = _friendly_error_message(error, ref)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            # Interaction token is gone (over 15 min) or Discord is
            # well and truly down — the log line is all we can leave.
            pass
