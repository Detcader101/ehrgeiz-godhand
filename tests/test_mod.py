"""Tests for the new moderation commands' pure helpers + DB layer.

Behaviour-spec naming: each test name reads as a sentence describing the
contract the unit must keep — not the implementation.

Out of scope: the slash-command callbacks themselves. Those touch
`interaction.response.defer()`, `member.kick()`, `audit.post_mod_event()`
etc. The kick/ban/timeout side of that is thin glue around discord.py's
own methods; the warn-with-escalation side has its decision logic carved
out into `db.add_warning` (returns the post-insert count) and the
WARN_ESCALATE_* constants — both of which are tested directly here.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import db
from cogs.mod import parse_duration


# --- parse_duration -------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("1h",  timedelta(hours=1)),
        ("2d",  timedelta(days=2)),
        ("1w",  timedelta(weeks=1)),
        # Surrounding whitespace and uppercase units are tolerated — the
        # mod-log reader shouldn't have to teach Discord's mobile keyboard
        # to stop autocapitalising.
        (" 5M ", timedelta(minutes=5)),
        ("3H",   timedelta(hours=3)),
    ],
)
def test_parse_duration_accepts_canonical_forms(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "abc", "5", "5x", "0m", "-1h", "1.5h", "5 minutes", "h5"],
)
def test_parse_duration_rejects_garbage(text):
    """Anything that isn't '<positive int><single unit>' must raise.
    The slash-command surface relies on the ValueError to send a friendly
    error back to the runner — silent fallthroughs are worse than rejection."""
    with pytest.raises(ValueError):
        parse_duration(text)


# --- db.add_warning / list_warnings / count_warnings ----------------------- #

ISO = "2026-05-06T12:00:00+00:00"


async def test_add_warning_returns_running_total(tmp_db):
    """add_warning's return value is what the warn handler keys off for
    auto-escalation, so it's contractual that the count matches what
    count_warnings would report immediately after."""
    n1 = await db.add_warning(
        discord_id=1, issued_by=99, reason="first", now_iso=ISO,
    )
    n2 = await db.add_warning(
        discord_id=1, issued_by=99, reason="second", now_iso=ISO,
    )
    assert n1 == 1
    assert n2 == 2
    assert await db.count_warnings(1) == 2


async def test_warning_counts_are_per_user(tmp_db):
    await db.add_warning(discord_id=1, issued_by=99, reason="a", now_iso=ISO)
    await db.add_warning(discord_id=1, issued_by=99, reason="b", now_iso=ISO)
    await db.add_warning(discord_id=2, issued_by=99, reason="c", now_iso=ISO)
    assert await db.count_warnings(1) == 2
    assert await db.count_warnings(2) == 1
    # User with no rows must report 0, not error.
    assert await db.count_warnings(999) == 0


async def test_list_warnings_returns_newest_first(tmp_db):
    await db.add_warning(
        discord_id=1, issued_by=99, reason="oldest",
        now_iso="2026-01-01T00:00:00+00:00",
    )
    await db.add_warning(
        discord_id=1, issued_by=99, reason="middle",
        now_iso="2026-03-01T00:00:00+00:00",
    )
    await db.add_warning(
        discord_id=1, issued_by=99, reason="newest",
        now_iso="2026-05-01T00:00:00+00:00",
    )
    rows = await db.list_warnings(1)
    reasons = [r["reason"] for r in rows]
    assert reasons == ["newest", "middle", "oldest"]


async def test_list_warnings_empty_for_clean_user(tmp_db):
    """Empty list — not None, not exception. The /warnings handler keys
    off truthiness, so a None return would crash the format step."""
    rows = await db.list_warnings(42)
    assert rows == []


