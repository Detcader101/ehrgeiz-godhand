"""Permission-model tests for the mandatory-verification onboarding gate.

These are pure-function tests over `_category_overwrites` and
`_channel_verified_only_overwrites` — no Discord API calls, just
asserting the dict shape returned by the overwrite helpers given a
CategorySpec/ChannelSpec and a mock guild + role set.

Why this matters: Verify is a *focus* gate, not an information gate.
Rules and announcements stay readable to @everyone so newcomers can
decide whether to link a Polaris ID; #welcome alone is verified-only
because its banner reads as a post-verify landing. If either helper
silently stops emitting `view_channel=False` for @everyone on a
verified-only channel, the post-verify welcome leaks to unverified
arrivals and the funnel breaks.
"""
from __future__ import annotations

import pytest

import discord

from cogs.setup import (
    SERVER_PLAN,
    CategorySpec,
    ChannelSpec,
    _category_overwrites,
    _channel_verified_only_overwrites,
)


# --- fixtures -------------------------------------------------------------- #

@pytest.fixture
def roles(make_role):
    """Minimal role set the overwrite helpers expect."""
    return {
        "verified": make_role("Verified"),
        "admin": make_role("Admin"),
        "mod": make_role("Moderator"),
    }


# --- _category_overwrites -------------------------------------------------- #

def test_public_category_emits_no_overwrites(mock_guild, roles):
    """A category with neither staff_only nor verified_only should
    inherit guild defaults (empty overwrite dict) — that's how Info
    stays visible to @everyone so player-hub is reachable."""
    spec = CategorySpec("Info", channels=[], staff_only=False, verified_only=False)
    overwrites = _category_overwrites(
        mock_guild, spec, roles["admin"], roles["mod"], roles["verified"],
    )
    assert overwrites == {}


def test_verified_only_category_hides_from_everyone(mock_guild, roles):
    spec = CategorySpec("Matchmaking", channels=[], verified_only=True)
    overwrites = _category_overwrites(
        mock_guild, spec, roles["admin"], roles["mod"], roles["verified"],
    )
    # @everyone must be locked out — the whole point of the gate.
    assert mock_guild.default_role in overwrites
    assert overwrites[mock_guild.default_role].view_channel is False
    # Verified, Admin, Mod, bot must all be able to see.
    for key in (roles["verified"], roles["admin"], roles["mod"], mock_guild.me):
        assert key in overwrites
        assert overwrites[key].view_channel is True


def test_staff_only_category_excludes_verified_role(mock_guild, roles):
    """Staff category: @everyone + Verified users all locked out,
    only Admin/Mod/bot can see."""
    spec = CategorySpec("Staff", channels=[], staff_only=True)
    overwrites = _category_overwrites(
        mock_guild, spec, roles["admin"], roles["mod"], roles["verified"],
    )
    assert overwrites[mock_guild.default_role].view_channel is False
    # Verified is NOT granted view — staff_only is stricter than verified_only.
    assert roles["verified"] not in overwrites
    assert overwrites[roles["admin"]].view_channel is True
    assert overwrites[roles["mod"]].view_channel is True


# --- _channel_verified_only_overwrites ------------------------------------ #

def test_channel_verified_gate_hides_from_everyone(mock_guild, roles):
    """The per-channel gate: @everyone locked, Verified + staff + bot
    can see. Used for welcome/rules/announcements inside the public Info
    category so only #🎴-player-hub stays visible pre-verify."""
    overwrites = _channel_verified_only_overwrites(
        mock_guild, roles["admin"], roles["mod"], roles["verified"],
    )
    assert overwrites[mock_guild.default_role].view_channel is False
    for key in (roles["verified"], roles["admin"], roles["mod"], mock_guild.me):
        assert overwrites[key].view_channel is True


def test_channel_verified_gate_handles_missing_roles(mock_guild):
    """If Admin/Mod/Verified roles aren't created yet (fresh guild mid-
    setup), the helper must still emit the @everyone lockout + bot
    access — no crash, no skipped gate."""
    overwrites = _channel_verified_only_overwrites(
        mock_guild, admin_role=None, mod_role=None, verified_role=None,
    )
    # The @everyone lockout and bot access are non-negotiable — those
    # are what make the channel actually hidden. Verified/Admin/Mod are
    # optional grantees.
    assert overwrites[mock_guild.default_role].view_channel is False
    assert overwrites[mock_guild.me].view_channel is True


# --- SERVER_PLAN layout invariants ---------------------------------------- #
# These lock in the mandatory-verification posture at the data layer:
# if a future SERVER_PLAN edit accidentally unlocks one of the gated
# channels (or gates the player-hub), tests catch it before ship.

def _find_info_category():
    for cat in SERVER_PLAN:
        if cat.name == "📋 Info":
            return cat
    pytest.fail("Info category missing from SERVER_PLAN")


def test_info_category_itself_is_public():
    """Info category must remain visible to @everyone so #🎴-player-hub
    is reachable without any role. Gating is per-channel inside it."""
    info = _find_info_category()
    assert info.verified_only is False
    assert info.staff_only is False


def test_welcome_is_the_only_gated_info_channel():
    """Verify is a *focus* gate, not an information gate: rules and
    announcements stay readable by @everyone so newcomers can decide
    whether to link a Polaris ID. #welcome is the one exception — its
    banner copy is post-verify ('you made it past the gate') and would
    confuse unverified arrivals. If this test fails, somebody's about
    to ship a regression that hides the rules from newcomers (or
    publishes the post-verify welcome to them)."""
    info = _find_info_category()
    gated = [c for c in info.channels if c.verified_only]
    gated_names = [c.name for c in gated]
    assert gated_names == ["👋-welcome"], (
        f"Only #👋-welcome should be verified_only in Info; got {gated_names}"
    )
    # And rules/announcements/player-hub must stay public.
    ungated_names = {c.name for c in info.channels if not c.verified_only}
    for required in ("📜-rules", "📣-announcements", "🎴-player-hub"):
        assert required in ungated_names, (
            f"#{required} must NOT be verified_only — newcomers need it"
        )


def test_gameplay_categories_all_verified_only():
    """Matchmaking / Community / Tekken / Competitive / Voice should
    all be gated at the category level — they're the 'reward' for
    verifying. Regression guard: a careless refactor shouldn't
    accidentally unlock them."""
    expected_gated = {
        "🎮 Matchmaking", "💬 Community", "🥊 Tekken",
        "🏆 Competitive", "🔊 Voice",
    }
    for cat in SERVER_PLAN:
        if cat.name in expected_gated:
            assert cat.verified_only is True, (
                f"Category {cat.name} should be verified_only"
            )


def test_staff_category_is_staff_only():
    for cat in SERVER_PLAN:
        if cat.name == "🛠️ Staff":
            assert cat.staff_only is True
            return
    pytest.fail("Staff category missing from SERVER_PLAN")
