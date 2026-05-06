"""Tests for slice 3 channel provisioning (issue #1).

Covers:
  - db.set_tournament_resources persists both ids and reads back via
    get_tournament — i.e. the migration columns are actually live.
  - _tournament_post_channel routes to announcements when set, falls
    back to signup_channel_id otherwise. This is the single decision
    point that keeps round-bracket / FINAL-STANDINGS / champion-card
    posts landing in the right place; if it ever silently flips back
    to signup_channel_id for a provisioned tournament, the in-progress
    posts go to #tournaments and ping every server member who ever
    looked at signups.

Out of scope: live channel creation against a real Discord guild
(_provision_tournament_channels). That's an integration test by nature
— the rollback-on-failure path is what we'd want to assert and that
needs a richer mock than what's wired up in conftest.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

import db
from cogs.tournament import _match_voice_overwrites, _tournament_post_channel


ISO = "2026-05-06T12:00:00+00:00"


# --- DB layer -------------------------------------------------------------- #

async def _make_tournament(*, name="Test") -> int:
    return await db.create_tournament(
        guild_id=999,
        organizer_id=1,
        name=name,
        match_format="FT2",
        max_players=None,
        now_iso=ISO,
    )


async def test_resources_default_null_until_set(tmp_db):
    """Fresh tournament must have both new columns NULL — the post
    helper's fallback branch depends on this default."""
    tid = await _make_tournament()
    row = await db.get_tournament(tid)
    assert row["category_id"] is None
    assert row["announcements_channel_id"] is None


async def test_set_tournament_resources_round_trips(tmp_db):
    tid = await _make_tournament()
    await db.set_tournament_resources(tid, 4242, 5353)
    row = await db.get_tournament(tid)
    assert row["category_id"] == 4242
    assert row["announcements_channel_id"] == 5353


async def test_set_match_voice_channel_round_trips(tmp_db):
    """Stamp-then-clear flow — provisioning sets the id; round advance
    clears it back to None. Both halves must round-trip via get_match."""
    tid = await _make_tournament()
    await db.create_matches(tid, 1, [(101, 102, None)])
    matches = await db.list_matches_for_round(tid, 1)
    mid = matches[0]["id"]
    assert matches[0]["voice_channel_id"] is None  # default

    await db.set_match_voice_channel(mid, 7777)
    row = await db.get_match(mid)
    assert row["voice_channel_id"] == 7777

    await db.set_match_voice_channel(mid, None)
    row = await db.get_match(mid)
    assert row["voice_channel_id"] is None


async def test_set_tournament_resources_can_clear_back_to_null(tmp_db):
    """Cleanup path: when the organizer accepts the delete prompt at
    end-of-tournament, the channels are removed and the ids should be
    nulled out so a stale row doesn't reference deleted snowflakes."""
    tid = await _make_tournament()
    await db.set_tournament_resources(tid, 1, 2)
    await db.set_tournament_resources(tid, None, None)
    row = await db.get_tournament(tid)
    assert row["category_id"] is None
    assert row["announcements_channel_id"] is None


# --- _tournament_post_channel routing ------------------------------------- #

def _row(*, signup: int | None, ann: int | None) -> dict:
    """sqlite3.Row supports `row[key]`; a plain dict satisfies the same
    access pattern for the helper under test."""
    return {
        "signup_channel_id": signup,
        "announcements_channel_id": ann,
    }


def _client_with(channels: dict[int, object]):
    client = MagicMock()
    client.get_channel.side_effect = lambda cid: channels.get(cid)
    return client


def _text_channel(cid: int) -> MagicMock:
    """MagicMock that passes isinstance(..., discord.TextChannel)."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    return ch


def test_post_channel_prefers_announcements_when_set():
    ann = _text_channel(2222)
    sign = _text_channel(1111)
    client = _client_with({1111: sign, 2222: ann})
    assert _tournament_post_channel(
        client, _row(signup=1111, ann=2222),
    ) is ann


def test_post_channel_falls_back_to_signup_when_no_announcements():
    """Pre-slice-3 tournaments (and ones where provisioning failed) have
    a NULL announcements_channel_id. They must keep posting to
    signup_channel_id so historic in-flight events don't break."""
    sign = _text_channel(1111)
    client = _client_with({1111: sign})
    assert _tournament_post_channel(
        client, _row(signup=1111, ann=None),
    ) is sign


def test_post_channel_falls_back_when_announcements_lookup_fails():
    """Channel was deleted out from under us (manual cleanup, server
    purge). get_channel returns None — fall back to signup so an
    in-flight bracket post still lands somewhere visible."""
    sign = _text_channel(1111)
    client = _client_with({1111: sign})  # 2222 not registered → None
    assert _tournament_post_channel(
        client, _row(signup=1111, ann=2222),
    ) is sign


def test_post_channel_returns_none_when_nothing_resolvable():
    """No channels found at all — caller short-circuits silently rather
    than crash on a None.send()."""
    client = _client_with({})
    assert _tournament_post_channel(
        client, _row(signup=None, ann=None),
    ) is None


# --- _match_voice_overwrites ---------------------------------------------- #
#
# Per spec: @everyone locked out, paired players + Organizer role + bot
# get view+connect. The room is *invite-only* — if this helper ever
# stops emitting `view_channel=False` for @everyone, the whole point of
# the per-match VC collapses (every server member can drop in).

def test_match_voice_overwrites_locks_out_everyone(mock_guild, make_member):
    a = make_member(member_id=1, display_name="A")
    b = make_member(member_id=2, display_name="B")
    overwrites = _match_voice_overwrites(
        mock_guild, a, b, organizer_role=None,
    )
    assert overwrites[mock_guild.default_role].view_channel is False
    assert overwrites[mock_guild.default_role].connect is False


def test_match_voice_overwrites_grants_paired_players(mock_guild, make_member):
    a = make_member(member_id=1, display_name="A")
    b = make_member(member_id=2, display_name="B")
    overwrites = _match_voice_overwrites(
        mock_guild, a, b, organizer_role=None,
    )
    for member in (a, b):
        assert member in overwrites
        assert overwrites[member].view_channel is True
        assert overwrites[member].connect is True


def test_match_voice_overwrites_grants_organizer_role(mock_guild, make_member, make_role):
    """Organizer role gets the spectator/bridge access — that's the
    escape hatch when a dispute or no-show needs staff in the room."""
    a = make_member(member_id=1, display_name="A")
    b = make_member(member_id=2, display_name="B")
    organizer = make_role("Organizer")
    overwrites = _match_voice_overwrites(mock_guild, a, b, organizer)
    assert organizer in overwrites
    assert overwrites[organizer].view_channel is True
    assert overwrites[organizer].connect is True


def test_match_voice_overwrites_skips_missing_member(mock_guild, make_member):
    """A player who left the server mid-tournament resolves to None via
    guild.get_member. Their overwrite is silently skipped — the room
    still spins up for the remaining player + Organizer rather than
    failing the whole round."""
    b = make_member(member_id=2, display_name="B")
    overwrites = _match_voice_overwrites(
        mock_guild, member_a=None, member_b=b, organizer_role=None,
    )
    assert b in overwrites
    # No KeyError, no crash — and crucially @everyone is still locked.
    assert overwrites[mock_guild.default_role].view_channel is False


def test_match_voice_overwrites_always_grants_bot(mock_guild, make_member):
    """The bot needs view + manage_channels so it can delete the room
    on round advance. If this drops, cleanup silently fails forever and
    abandoned VCs accumulate."""
    a = make_member(member_id=1, display_name="A")
    b = make_member(member_id=2, display_name="B")
    overwrites = _match_voice_overwrites(
        mock_guild, a, b, organizer_role=None,
    )
    assert mock_guild.me in overwrites
    assert overwrites[mock_guild.me].view_channel is True
    assert overwrites[mock_guild.me].manage_channels is True


def test_post_channel_ignores_non_text_channel():
    """If the announcements id resolves to something weird (voice
    channel, category) the helper must not return it — falls through
    to signup. Defensive check against a manual reshuffle of channel
    types."""
    not_text = MagicMock(spec=discord.VoiceChannel)
    sign = _text_channel(1111)
    client = _client_with({2222: not_text, 1111: sign})
    assert _tournament_post_channel(
        client, _row(signup=1111, ann=2222),
    ) is sign
