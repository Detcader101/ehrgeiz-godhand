"""Tier 2 — DB round-trip tests.

Each test gets its own tmp SQLite file via the `tmp_db` fixture so state
doesn't leak between tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db


# --- players --------------------------------------------------------------- #

async def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def test_players_upsert_and_fetch_by_discord(tmp_db):
    await db.upsert_player(
        discord_id=1, tekken_id="ABC123", display_name="Alice",
        main_char="Jin", rating_mu=1500.0, rank_tier="Tenryu",
        linked_by=None, now_iso=await _iso_now(),
    )
    row = await db.get_player_by_discord(1)
    assert row is not None
    assert row["tekken_id"] == "ABC123"
    assert row["display_name"] == "Alice"
    assert row["main_char"] == "Jin"
    assert row["rank_tier"] == "Tenryu"


async def test_players_upsert_replaces_existing_row(tmp_db):
    # discord_id is the PK — a second upsert for the same discord_id
    # should replace, not duplicate.
    await db.upsert_player(
        discord_id=1, tekken_id="OLD", display_name="Alice",
        main_char=None, rating_mu=None, rank_tier="Beginner",
        linked_by=None, now_iso=await _iso_now(),
    )
    await db.upsert_player(
        discord_id=1, tekken_id="NEW", display_name="Alice Updated",
        main_char="Kazuya", rating_mu=1700.0, rank_tier="Tekken King",
        linked_by=999, now_iso=await _iso_now(),
    )
    row = await db.get_player_by_discord(1)
    assert row["tekken_id"] == "NEW"
    assert row["display_name"] == "Alice Updated"
    assert row["rank_tier"] == "Tekken King"
    assert row["linked_by"] == 999


async def test_players_tekken_id_lookup_is_case_insensitive(tmp_db):
    await db.upsert_player(
        discord_id=1, tekken_id="AbCdEf", display_name="Alice",
        main_char=None, rating_mu=None, rank_tier=None,
        linked_by=None, now_iso=await _iso_now(),
    )
    # Lookup should match regardless of casing — schema has COLLATE NOCASE
    # on the tekken_id column.
    row_upper = await db.get_player_by_tekken_id("ABCDEF")
    row_lower = await db.get_player_by_tekken_id("abcdef")
    assert row_upper is not None
    assert row_lower is not None
    assert row_upper["discord_id"] == 1
    assert row_lower["discord_id"] == 1


async def test_players_delete(tmp_db):
    await db.upsert_player(
        discord_id=1, tekken_id="X", display_name="A",
        main_char=None, rating_mu=None, rank_tier=None,
        linked_by=None, now_iso=await _iso_now(),
    )
    await db.delete_player(1)
    assert await db.get_player_by_discord(1) is None


async def test_list_all_players_orders_oldest_sync_first(tmp_db):
    # The rank sweeper relies on this ordering: never-synced / stale
    # rows get refreshed first, fresh rows go to the back of the queue.
    now = datetime.now(timezone.utc)
    await db.upsert_player(
        discord_id=1, tekken_id="FRESH", display_name="Fresh",
        main_char=None, rating_mu=None, rank_tier=None, linked_by=None,
        now_iso=now.isoformat(),
    )
    await db.upsert_player(
        discord_id=2, tekken_id="OLD", display_name="Old",
        main_char=None, rating_mu=None, rank_tier=None, linked_by=None,
        now_iso=(now - timedelta(days=30)).isoformat(),
    )
    await db.upsert_player(
        discord_id=3, tekken_id="MID", display_name="Mid",
        main_char=None, rating_mu=None, rank_tier=None, linked_by=None,
        now_iso=(now - timedelta(hours=2)).isoformat(),
    )
    rows = await db.list_all_players()
    ids = [r["discord_id"] for r in rows]
    assert ids == [2, 3, 1]


async def test_list_all_players_empty_returns_empty_list(tmp_db):
    assert await db.list_all_players() == []


# --- unlinks (relink cooldown) -------------------------------------------- #

async def test_unlinks_record_and_fetch(tmp_db):
    now = await _iso_now()
    await db.record_unlink(discord_id=1, tekken_id="ABC", now_iso=now)
    row = await db.get_last_unlink(1)
    assert row is not None
    assert row["tekken_id"] == "ABC"
    assert row["unlinked_at"] == now


async def test_unlinks_clear(tmp_db):
    await db.record_unlink(discord_id=1, tekken_id="ABC", now_iso=await _iso_now())
    await db.clear_unlink(1)
    assert await db.get_last_unlink(1) is None


async def test_unlinks_purge_drops_rows_older_than_cutoff(tmp_db):
    # Privacy hygiene: after the cooldown elapses, _PendingSweeper
    # purges the row so we no longer hold the discord↔tekken pairing.
    now = datetime.now(timezone.utc)
    await db.record_unlink(
        discord_id=1, tekken_id="OLD",
        now_iso=(now - timedelta(days=10)).isoformat(),
    )
    await db.record_unlink(
        discord_id=2, tekken_id="FRESH",
        now_iso=(now - timedelta(hours=1)).isoformat(),
    )
    cutoff = (now - timedelta(days=7)).isoformat()
    purged = await db.purge_unlinks_before(cutoff)
    assert purged == 1
    assert await db.get_last_unlink(1) is None
    assert await db.get_last_unlink(2) is not None


# --- pending_verifications lifecycle -------------------------------------- #

async def test_pending_verification_full_lifecycle(tmp_db):
    now = await _iso_now()
    await db.upsert_pending_verification(
        discord_id=1, guild_id=100, tekken_id="HI",
        rank_tier="Tekken Emperor", rank_source="auto", now_iso=now,
    )
    row = await db.get_pending_by_discord(1)
    assert row is not None
    assert row["rank_tier"] == "Tekken Emperor"
    assert row["expired_at"] is None
    # Before set_pending_message, message_id is NULL.
    assert row["message_id"] is None

    await db.set_pending_message(discord_id=1, channel_id=500, message_id=600)
    row = await db.get_pending_by_discord(1)
    assert row["message_id"] == 600
    assert row["channel_id"] == 500

    # message_id lookup should round-trip.
    by_msg = await db.get_pending_by_message(600)
    assert by_msg is not None
    assert by_msg["discord_id"] == 1

    later = await _iso_now()
    await db.mark_pending_expired(1, later)
    row = await db.get_pending_by_discord(1)
    assert row["expired_at"] == later

    await db.delete_pending_verification(1)
    assert await db.get_pending_by_discord(1) is None


async def test_pending_verification_upsert_resets_message_and_expiry(tmp_db):
    """If a player bumps their pending claim (e.g. re-runs refresh with a
    different rank), the upsert should drop their old message_id/
    channel_id/expired_at so the old audit message becomes orphaned and
    the new one takes over cleanly."""
    now = await _iso_now()
    await db.upsert_pending_verification(
        discord_id=1, guild_id=100, tekken_id="HI",
        rank_tier="Tekken King", rank_source="a", now_iso=now,
    )
    await db.set_pending_message(1, 500, 600)
    await db.mark_pending_expired(1, now)

    # Upsert again — should clear message_id + expired_at.
    await db.upsert_pending_verification(
        discord_id=1, guild_id=100, tekken_id="HI",
        rank_tier="Tekken Emperor", rank_source="b", now_iso=now,
    )
    row = await db.get_pending_by_discord(1)
    assert row["message_id"] is None
    assert row["channel_id"] is None
    assert row["expired_at"] is None
    assert row["rank_tier"] == "Tekken Emperor"


async def test_list_stale_pending_excludes_already_expired_rows(tmp_db):
    # Spec: the sweeper only marks rows that are *not yet* expired.
    # Rows that got marked stale in a previous tick shouldn't come back.
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=80)).isoformat()
    await db.upsert_pending_verification(
        discord_id=1, guild_id=100, tekken_id="X",
        rank_tier="Tekken King", rank_source="a", now_iso=old,
    )
    await db.upsert_pending_verification(
        discord_id=2, guild_id=100, tekken_id="Y",
        rank_tier="Tekken King", rank_source="a", now_iso=old,
    )
    await db.mark_pending_expired(2, now.isoformat())

    cutoff = (now - timedelta(hours=72)).isoformat()
    rows = await db.list_stale_pending(cutoff)
    ids = sorted(r["discord_id"] for r in rows)
    assert ids == [1]  # #2 excluded because it was already marked expired
