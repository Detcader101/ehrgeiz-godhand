"""Tests for slice 3 part 3 — end-of-tournament organizer cleanup
prompt + #tournament-history archive (issue #1).

Scope, by behaviour:
  - DB: cleanup_message_id round-trips, can be cleared back to NULL,
    and lookup-by-message resolves the right tournament.
  - Permission gate on TournamentCleanupView: organizer-of-this-row,
    Organizer-role-holder, and Admin all pass; a verified rando fails
    with an ephemeral nudge instead of running cleanup.
  - Routing: archive helper writes to #tournament-history when present,
    silently no-ops back to None when the channel doesn't exist (so a
    server that hasn't re-run /setup-server doesn't crash the cleanup
    button).

Out of scope: the actual category.delete() side effect, since
discord.CategoryChannel doesn't readily mock through the mock_guild
fixture and this gets covered end-to-end in the manual smoke test.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import db
from cogs.tournament import (
    TournamentCleanupView,
    _archive_tournament_to_history,
)


ISO = "2026-05-07T12:00:00+00:00"


async def _make_tournament(*, name="Cleanup Test", organizer_id=10) -> int:
    return await db.create_tournament(
        guild_id=999,
        organizer_id=organizer_id,
        name=name,
        match_format="FT2",
        max_players=None,
        now_iso=ISO,
    )


# --- DB layer -------------------------------------------------------------- #

async def test_cleanup_message_defaults_null(tmp_db):
    """Fresh tournament has no cleanup prompt yet — the column must
    default to NULL or the lookup helper returns rows from prior
    tournaments by accident."""
    tid = await _make_tournament()
    row = await db.get_tournament(tid)
    assert row["cleanup_message_id"] is None


async def test_set_cleanup_message_round_trips(tmp_db):
    tid = await _make_tournament()
    await db.set_tournament_cleanup_message(tid, 7777)
    row = await db.get_tournament(tid)
    assert row["cleanup_message_id"] == 7777


async def test_set_cleanup_message_can_clear(tmp_db):
    """After organizer answers Yes/No, the id is nulled so a stale
    reference doesn't keep the persistent view bound to a deleted
    message and re-fire on the next click."""
    tid = await _make_tournament()
    await db.set_tournament_cleanup_message(tid, 7777)
    await db.set_tournament_cleanup_message(tid, None)
    row = await db.get_tournament(tid)
    assert row["cleanup_message_id"] is None


async def test_lookup_by_cleanup_message_finds_tournament(tmp_db):
    tid = await _make_tournament(name="Lookup")
    await db.set_tournament_cleanup_message(tid, 4242)
    row = await db.get_tournament_by_cleanup_message(4242)
    assert row is not None
    assert row["id"] == tid
    assert row["name"] == "Lookup"


async def test_lookup_by_cleanup_message_misses_returns_none(tmp_db):
    """The view's resolver depends on a clean None when no tournament
    matches — otherwise the permission check would crash on a None row."""
    await _make_tournament()
    assert await db.get_tournament_by_cleanup_message(9999) is None


# --- Permission gate on TournamentCleanupView ----------------------------- #
#
# The view's permission contract: organizer of the row, Organizer-role
# holder, or guild Admin. Anyone else gets an ephemeral "only organizer
# can decide" reply and the cleanup does NOT run. If this gate breaks
# any verified member could trash the channels mid-conversation.


def _make_interaction(*, member_id: int, is_admin: bool, role_names: list[str],
                       message_id: int | None = 4242):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.guild_permissions = MagicMock(administrator=is_admin)
    member.roles = [MagicMock(name=n) for n in role_names]
    for r, name in zip(member.roles, role_names):
        r.name = name

    interaction = MagicMock()
    interaction.user = member
    if message_id is not None:
        interaction.message = MagicMock()
        interaction.message.id = message_id
    else:
        interaction.message = None
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_admin_passes_check(tmp_db):
    tid = await _make_tournament(organizer_id=10)
    await db.set_tournament_cleanup_message(tid, 4242)
    view = TournamentCleanupView()
    interaction = _make_interaction(
        member_id=99, is_admin=True, role_names=["@everyone"],
    )
    assert await view.interaction_check(interaction) is True


async def test_organizer_role_holder_passes_check(tmp_db):
    tid = await _make_tournament(organizer_id=10)
    await db.set_tournament_cleanup_message(tid, 4242)
    view = TournamentCleanupView()
    interaction = _make_interaction(
        member_id=99, is_admin=False, role_names=["Organizer"],
    )
    assert await view.interaction_check(interaction) is True


async def test_row_organizer_passes_even_without_role(tmp_db):
    """The user who created the tournament can clean it up even if
    staff stripped the Organizer role from them after the fact —
    otherwise a staff role-shuffle locks the organizer out of their
    own tournament's prompt."""
    tid = await _make_tournament(organizer_id=10)
    await db.set_tournament_cleanup_message(tid, 4242)
    view = TournamentCleanupView()
    interaction = _make_interaction(
        member_id=10, is_admin=False, role_names=["Verified"],
    )
    assert await view.interaction_check(interaction) is True


async def test_random_member_blocked_with_ephemeral(tmp_db):
    tid = await _make_tournament(organizer_id=10)
    await db.set_tournament_cleanup_message(tid, 4242)
    view = TournamentCleanupView()
    interaction = _make_interaction(
        member_id=99, is_admin=False, role_names=["Verified"],
    )
    assert await view.interaction_check(interaction) is False
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


# --- Archive routing ------------------------------------------------------ #

async def test_archive_returns_none_when_history_channel_missing(
    tmp_db, monkeypatch,
):
    """Server hasn't re-run /setup-server after the slice landed —
    #tournament-history doesn't exist yet. Helper returns None so the
    button reports it back instead of crashing."""
    tid = await _make_tournament()
    row = await db.get_tournament(tid)

    guild = MagicMock(spec=discord.Guild)
    client = MagicMock()
    client.get_guild.return_value = guild

    import cogs.tournament as tcog
    monkeypatch.setattr(
        tcog.channel_util, "find_text_channel", lambda g, n: None,
    )
    result = await _archive_tournament_to_history(client, row)
    assert result is None


async def test_archive_returns_none_when_guild_missing(tmp_db):
    """Bot booted out of the guild between completion and click —
    get_guild returns None. Don't blow up trying to render bracket
    against a phantom guild."""
    tid = await _make_tournament()
    row = await db.get_tournament(tid)
    client = MagicMock()
    client.get_guild.return_value = None
    assert await _archive_tournament_to_history(client, row) is None
