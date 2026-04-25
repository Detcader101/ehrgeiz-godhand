"""Tier 3 — the auto-resync flow.

These exercise the exact code paths Jay Jay's friend couldn't
self-trigger: role restore on rejoin, periodic rank refresh, and the
post-/reset-server bulk re-apply. Discord is mocked via conftest
fixtures; the DB is real (tmp file); wavu/ewgf/audit are stubbed.

The overall pattern in each test:
  1. Seed the `players` DB with one or more rows.
  2. Attach the corresponding Member mock to the mock_guild so
     `guild.get_member(discord_id)` returns it.
  3. Configure stub_external return values for this scenario.
  4. Call the function under test.
  5. Assert DB state + Member.roles + stub call counts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
import wavu
from cogs.onboarding import (
    VERIFIED_ROLE_NAME,
    refresh_player_from_api,
    restore_roles_from_db_cache,
    resync_all_players,
)


# --- fixtures specific to resync tests ------------------------------------ #

@pytest.fixture
def verified_role(mock_guild, make_role):
    """Add the Verified role to the guild so _ensure_role finds it
    instead of creating a new one."""
    role = make_role(VERIFIED_ROLE_NAME)
    mock_guild.roles.append(role)
    return role


@pytest.fixture
def rank_roles(mock_guild, make_role):
    """Pre-populate every rank role the tests might need. Returns a
    name → FakeRole dict."""
    rank_role_map = {}
    for name in wavu.ALL_RANK_NAMES:
        r = make_role(name)
        mock_guild.roles.append(r)
        rank_role_map[name] = r
    return rank_role_map


async def _seed_player(
    *, discord_id: int, tekken_id: str | None = None,
    rank_tier: str | None = "Tenryu",
    last_synced_delta: timedelta = timedelta(days=1),
) -> None:
    """Insert a players row with last_synced = now - delta.

    tekken_id defaults to a unique value derived from discord_id because
    the schema has UNIQUE on tekken_id — two players can't share one.
    """
    if tekken_id is None:
        tekken_id = f"TEK{discord_id}"
    synced_iso = (datetime.now(timezone.utc) - last_synced_delta).isoformat()
    await db.upsert_player(
        discord_id=discord_id, tekken_id=tekken_id,
        display_name=f"User{discord_id}",
        main_char="Jin", rating_mu=1400.0, rank_tier=rank_tier,
        linked_by=None, now_iso=synced_iso,
    )


def _role_names(member) -> set[str]:
    return {r.name for r in member.roles}


# --- restore_roles_from_db_cache ----------------------------------------- #

async def test_restore_roles_grants_verified_and_cached_rank(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles,
):
    """Baseline — a previously-linked user rejoining the server gets
    Verified + their stored rank role back, no API hit."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tenryu")
    row = await db.get_player_by_discord(mock_member.id)
    await restore_roles_from_db_cache(mock_member, row)
    assert VERIFIED_ROLE_NAME in _role_names(mock_member)
    assert "Tenryu" in _role_names(mock_member)


async def test_restore_roles_strips_stale_rank_when_cached_is_none(
    tmp_db, mock_guild, make_member, verified_role, rank_roles,
):
    """If a player's stored rank is NULL (e.g. mid-pending), make sure
    we don't re-grant the old rank role they still happen to be
    wearing — strip it, keep Verified only."""
    member = make_member(
        member_id=42,
        roles=[rank_roles["Tekken King"]],  # user still has the old role
    )
    await _seed_player(discord_id=42, rank_tier=None)  # but DB says no rank
    row = await db.get_player_by_discord(42)
    await restore_roles_from_db_cache(member, row)
    names = _role_names(member)
    assert VERIFIED_ROLE_NAME in names
    assert "Tekken King" not in names


async def test_restore_roles_is_idempotent(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles,
):
    """Running restore twice shouldn't duplicate the role — add_roles is
    a no-op for already-granted roles. Regression guard: earlier
    iterations of _apply_rank_and_verified appended without checking."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tenryu")
    row = await db.get_player_by_discord(mock_member.id)
    await restore_roles_from_db_cache(mock_member, row)
    await restore_roles_from_db_cache(mock_member, row)
    verified_count = sum(1 for r in mock_member.roles if r.name == VERIFIED_ROLE_NAME)
    assert verified_count == 1


async def test_restore_roles_skips_rank_when_pending_exists(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles,
):
    """Blocker from 2026-04-24 code review — regression guard.

    When `_PendingSweeper._mark_one_stale` fires (72h organizer inaction),
    it writes the *claimed* rank back into `players.rank_tier` for
    profile-display purposes without granting the role. Before the fix,
    a later rejoin would read that rank and silently grant the role
    through `_apply_rank_and_verified`. That would hand out a
    Tekken Emperor role without any organizer ever confirming it.

    Correct behaviour: if a pending row exists for this member, restore
    MUST drop to Verified-only regardless of what's in players.rank_tier.
    """
    from datetime import datetime, timezone
    await _seed_player(discord_id=mock_member.id, rank_tier="Tekken Emperor")
    # Simulate the stale-pending state: rank_tier was written back to
    # the players row by the sweeper, but the pending_verifications
    # row is still live (unresolved).
    await db.upsert_pending_verification(
        discord_id=mock_member.id, guild_id=mock_guild.id,
        tekken_id="TEK42", rank_tier="Tekken Emperor",
        rank_source="test", now_iso=datetime.now(timezone.utc).isoformat(),
    )
    row = await db.get_player_by_discord(mock_member.id)

    await restore_roles_from_db_cache(mock_member, row)

    names = _role_names(mock_member)
    assert VERIFIED_ROLE_NAME in names, \
        "Verified should still be granted — pending users stay in the server"
    assert "Tekken Emperor" not in names, (
        "Rank role MUST be withheld while pending verification is unresolved"
    )


# --- refresh_player_from_api ---------------------------------------------- #

def _profile(rank_tier=None, tekken_id="TEK1"):
    return wavu.PlayerProfile(
        tekken_id=tekken_id, display_name=f"Player-{tekken_id}",
        main_char="Jin", rating_mu=1500.0, rank_tier=rank_tier,
    )


async def test_refresh_api_status_ok_when_rank_unchanged(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles, stub_external,
):
    """Stored rank == fresh rank → no rank_tier change, no audit, no pending.
    Result is 'ok' (roles still re-applied as a side-effect)."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tenryu")
    row = await db.get_player_by_discord(mock_member.id)

    stub_external.wavu_lookup.return_value = _profile(rank_tier="Tenryu")
    stub_external.ewgf_rank.return_value = "Tenryu"

    result = await refresh_player_from_api(
        mock_guild, mock_member, row, audit_source="test",
    )
    assert result["status"] == "ok"
    # The rank-changed branch emits an audit post — must NOT fire here.
    stub_external.audit_post.assert_not_called()
    stub_external.start_pending.assert_not_called()


async def test_refresh_api_status_rank_changed_below_threshold(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles, stub_external,
):
    """Low-tier rank bump — promote the role without going to pending."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tenryu")
    row = await db.get_player_by_discord(mock_member.id)

    stub_external.wavu_lookup.return_value = _profile()
    stub_external.ewgf_rank.return_value = "Bushin"  # up from Tenryu, still below King

    result = await refresh_player_from_api(
        mock_guild, mock_member, row, audit_source="test",
    )
    assert result["status"] == "rank-changed"
    assert result["from"] == "Tenryu"
    assert result["to"] == "Bushin"
    stub_external.audit_post.assert_called_once()
    stub_external.start_pending.assert_not_called()
    assert "Bushin" in _role_names(mock_member)
    assert "Tenryu" not in _role_names(mock_member)


async def test_refresh_api_status_pending_when_new_high_rank(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles, stub_external,
):
    """Tekken King claim from a lower stored rank → Pending Verification,
    rank role withheld, pending row created via _start_pending_verification
    stub."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Bushin")
    row = await db.get_player_by_discord(mock_member.id)

    stub_external.wavu_lookup.return_value = _profile()
    stub_external.ewgf_rank.return_value = "Tekken King"

    result = await refresh_player_from_api(
        mock_guild, mock_member, row, audit_source="test",
    )
    assert result["status"] == "pending"
    stub_external.start_pending.assert_called_once()

    # Players row rank_tier MUST be None for pending claims — otherwise
    # a later sweep would treat the claim as resolved and grant the role.
    updated = await db.get_player_by_discord(mock_member.id)
    assert updated["rank_tier"] is None


async def test_refresh_api_same_high_rank_doesnt_retrigger_pending(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles, stub_external,
):
    """If stored rank IS already Tekken Emperor, a refresh that returns
    the same rank should NOT send the user back to pending again
    (spec §5.3: only NEW claims trigger pending)."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tekken Emperor")
    row = await db.get_player_by_discord(mock_member.id)

    stub_external.wavu_lookup.return_value = _profile()
    stub_external.ewgf_rank.return_value = "Tekken Emperor"

    result = await refresh_player_from_api(
        mock_guild, mock_member, row, audit_source="test",
    )
    assert result["status"] == "ok"
    stub_external.start_pending.assert_not_called()


async def test_refresh_api_wavu_error_returns_error_status(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles, stub_external,
):
    """Upstream wavu outage should produce a clean error dict, not
    propagate an exception that kills the sweep."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tenryu")
    row = await db.get_player_by_discord(mock_member.id)

    stub_external.wavu_lookup.side_effect = wavu.WavuError("timeout")

    result = await refresh_player_from_api(
        mock_guild, mock_member, row, audit_source="test",
    )
    assert result["status"] == "error"
    assert "wavu" in result["reason"]


async def test_refresh_api_keeps_stored_rank_if_autodetect_fails(
    tmp_db, mock_guild, mock_member, verified_role, rank_roles, stub_external,
):
    """If wavu succeeds (profile lookup OK) but both rank sources return
    None, fall back to the stored rank rather than dropping to 'no rank'.
    Same conservative behaviour as _flow_refresh's stored_is_valid path."""
    await _seed_player(discord_id=mock_member.id, rank_tier="Tenryu")
    row = await db.get_player_by_discord(mock_member.id)

    stub_external.wavu_lookup.return_value = _profile()
    stub_external.ewgf_rank.return_value = None  # both auto-detects failed

    result = await refresh_player_from_api(
        mock_guild, mock_member, row, audit_source="test",
    )
    assert result["status"] == "ok"
    assert "Tenryu" in _role_names(mock_member)


# --- resync_all_players --------------------------------------------------- #

async def test_resync_cached_mode_restores_roles_and_never_calls_api(
    tmp_db, mock_guild, make_member, verified_role, rank_roles, stub_external,
):
    """api_refresh=False is what /reset-server and on_member_join use.
    Must not hit wavu/ewgf under any circumstances — this test would
    catch an accidental regression where someone swaps the default."""
    for i in range(3):
        member = make_member(member_id=100 + i)
        await _seed_player(discord_id=member.id, rank_tier="Tenryu")

    results = await resync_all_players(
        mock_guild, api_refresh=False, audit_source="test",
    )
    assert results["total"] == 3
    assert results["restored"] == 3
    stub_external.wavu_lookup.assert_not_called()
    stub_external.ewgf_rank.assert_not_called()


async def test_resync_counts_members_not_in_guild(
    tmp_db, mock_guild, make_member, verified_role, rank_roles, stub_external,
):
    """Players left in the DB whose Discord account is no longer a
    member of the guild should be counted but not processed."""
    present = make_member(member_id=100)
    await _seed_player(discord_id=present.id, rank_tier="Tenryu")
    # Seed a player who isn't attached to mock_guild (no make_member call).
    await _seed_player(discord_id=999, rank_tier="Tenryu")

    results = await resync_all_players(
        mock_guild, api_refresh=False, audit_source="test",
    )
    assert results["total"] == 2
    assert results["skipped_not_in_guild"] == 1
    assert results["restored"] == 1


async def test_resync_skip_recent_respects_last_synced_window(
    tmp_db, mock_guild, make_member, verified_role, rank_roles, stub_external,
):
    """The rank sweeper uses skip_if_synced_within to avoid hammering
    wavu for players who were synced recently."""
    fresh = make_member(member_id=100)
    stale = make_member(member_id=101)
    await _seed_player(discord_id=fresh.id, last_synced_delta=timedelta(minutes=10),
                       rank_tier="Tenryu")
    await _seed_player(discord_id=stale.id, last_synced_delta=timedelta(days=1),
                       rank_tier="Tenryu")

    stub_external.wavu_lookup.return_value = _profile(rank_tier="Tenryu")
    stub_external.ewgf_rank.return_value = "Tenryu"

    results = await resync_all_players(
        mock_guild, api_refresh=True, force=False,
        skip_if_synced_within=timedelta(hours=6),
        audit_source="test",
    )
    assert results["total"] == 2
    # Fresh one hits the cached-restore path (still gets Verified back),
    # stale one hits the API path.
    assert results["skipped_recent"] == 1
    # wavu.lookup_player should have been called exactly once — for the stale user.
    assert stub_external.wavu_lookup.call_count == 1


async def test_resync_force_ignores_skip_window(
    tmp_db, mock_guild, make_member, verified_role, rank_roles, stub_external,
):
    """/admin-resync-all uses force=True to override the skip window."""
    fresh = make_member(member_id=100)
    await _seed_player(discord_id=fresh.id, last_synced_delta=timedelta(minutes=1),
                       rank_tier="Tenryu")

    stub_external.wavu_lookup.return_value = _profile(rank_tier="Tenryu")
    stub_external.ewgf_rank.return_value = "Tenryu"

    results = await resync_all_players(
        mock_guild, api_refresh=True, force=True,
        skip_if_synced_within=timedelta(hours=6),
        audit_source="test",
    )
    assert results["skipped_recent"] == 0
    assert stub_external.wavu_lookup.call_count == 1


async def test_resync_api_mode_records_rank_change(
    tmp_db, mock_guild, make_member, verified_role, rank_roles, stub_external,
):
    """End-to-end happy path of the sweeper — stale player, API reports
    a new (below-threshold) rank, DB updated, roles swapped, counter
    incremented, audit fired."""
    member = make_member(member_id=100)
    await _seed_player(discord_id=member.id, rank_tier="Tenryu",
                       last_synced_delta=timedelta(days=1))

    stub_external.wavu_lookup.return_value = _profile()
    stub_external.ewgf_rank.return_value = "Bushin"

    results = await resync_all_players(
        mock_guild, api_refresh=True, force=True, audit_source="test",
    )
    assert results["rank_changed"] == 1
    assert "Bushin" in _role_names(member)
    stub_external.audit_post.assert_called_once()


async def test_resync_empty_db_is_a_noop(
    tmp_db, mock_guild, verified_role, rank_roles, stub_external,
):
    """First-boot / freshly-reset server with zero linked players — should
    run cleanly and report zero of everything, no crash."""
    results = await resync_all_players(
        mock_guild, api_refresh=False, audit_source="test",
    )
    assert results == {
        "total": 0, "restored": 0, "rank_changed": 0, "pending": 0,
        "skipped_not_in_guild": 0, "skipped_recent": 0, "errors": 0,
    }
