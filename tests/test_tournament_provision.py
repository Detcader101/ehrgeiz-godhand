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
from cogs.tournament import _tournament_post_channel


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
