"""Tier 1 — pure-logic helpers in cogs/onboarding.py.

No Discord, no DB, no network. These are the cheapest tests to run so
they double as a smoke test that the module still imports cleanly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cogs.onboarding import (
    PENDING_THRESHOLD_RANK,
    RELINK_COOLDOWN,
    _bot_managed_rank_names,
    _cooldown_remaining,
    _format_duration,
    _normalize_id,
    _rank_ordinal,
    _requires_pending,
)


# --- _normalize_id --------------------------------------------------------- #

@pytest.mark.parametrize("raw, expected", [
    ("3mN929qaBEEG", "3mn929qabeeg"),
    ("3mN-929_qa BEEG", "3mn929qabeeg"),
    ("  3mN929qaBEEG  ", "3mn929qabeeg"),
    ("abc--DEF__ghi", "abcdefghi"),
    ("", ""),
    (None, ""),
])
def test_normalize_id_strips_separators_and_lowercases(raw, expected):
    assert _normalize_id(raw) == expected


def test_normalize_id_makes_equivalent_ids_match():
    # Two IDs a user might enter with different punctuation should
    # normalise to the same key.
    assert _normalize_id("abc-123_XYZ") == _normalize_id(" ABC 123 xyz ")


# --- _requires_pending ----------------------------------------------------- #

def test_requires_pending_low_ranks_dont_trigger():
    assert _requires_pending("Beginner") is False
    assert _requires_pending("Battle Ruler") is False  # just below Fujin (current threshold)


def test_requires_pending_threshold_rank_triggers():
    # Sanity: the threshold rank itself DOES trigger, by spec §5.3.
    assert _requires_pending(PENDING_THRESHOLD_RANK) is True


def test_requires_pending_above_threshold_triggers():
    # Emperor sits above King — also pending.
    assert _requires_pending("Tekken Emperor") is True
    assert _requires_pending("Tekken God") is True


def test_requires_pending_none_or_unknown_is_false():
    # Unknown rank names (old data, typos) shouldn't accidentally send
    # people to pending verification — fail safe.
    assert _requires_pending(None) is False
    assert _requires_pending("Not A Real Rank") is False


# --- _rank_ordinal --------------------------------------------------------- #

def test_rank_ordinal_known_rank_returns_int():
    assert isinstance(_rank_ordinal("Beginner"), int)


def test_rank_ordinal_ordering_is_monotonic_up_the_ladder():
    # Low rank < high rank in ordinal space. This is what the pending
    # threshold check relies on.
    low = _rank_ordinal("Beginner")
    high = _rank_ordinal("Tekken Emperor")
    assert low is not None and high is not None
    assert low < high


def test_rank_ordinal_unknown_returns_none():
    assert _rank_ordinal("Totally Fake Rank") is None
    assert _rank_ordinal(None) is None


# --- _cooldown_remaining --------------------------------------------------- #

def test_cooldown_remaining_fresh_unlink_returns_positive_delta():
    just_now = datetime.now(timezone.utc).isoformat()
    remaining = _cooldown_remaining(just_now)
    assert remaining is not None
    # Should be within a hair's breadth of the full cooldown.
    assert remaining > RELINK_COOLDOWN - timedelta(seconds=5)


def test_cooldown_remaining_expired_unlink_returns_none():
    old = (datetime.now(timezone.utc) - RELINK_COOLDOWN - timedelta(hours=1)).isoformat()
    assert _cooldown_remaining(old) is None


def test_cooldown_remaining_malformed_iso_returns_none():
    # Defensive: DB corruption or an old schema shouldn't raise.
    assert _cooldown_remaining("not-a-date") is None


# --- _format_duration ------------------------------------------------------ #

@pytest.mark.parametrize("td, expected", [
    (timedelta(days=2, hours=3), "2d 3h"),
    (timedelta(days=1), "1d 0h"),
    (timedelta(hours=5, minutes=20), "5h 20m"),
    (timedelta(hours=1), "1h 0m"),
    (timedelta(minutes=30), "30m"),
    (timedelta(seconds=10), "1m"),  # sub-minute floor: "1m"
])
def test_format_duration_human_readable(td, expected):
    assert _format_duration(td) == expected


# --- _bot_managed_rank_names ----------------------------------------------- #

def test_bot_managed_rank_names_includes_every_valid_rank():
    """A role-strip pass must recognise every rank the bot knows about —
    otherwise `_apply_rank_and_verified` would leak stale rank roles when
    a user deranks."""
    import wavu
    managed = _bot_managed_rank_names()
    for r in wavu.ALL_RANK_NAMES:
        assert r in managed, f"missing {r!r} from managed set"


def test_bot_managed_rank_names_keeps_legacy_roles():
    # Legacy roles must stay in the set so re-sync can strip them from
    # users who verified during the old schema. If this breaks, old
    # users could end up with phantom roles forever.
    managed = _bot_managed_rank_names()
    assert "Unranked" in managed
    assert "True God of Destruction" in managed
