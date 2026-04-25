"""Shared pytest fixtures for the Ehrgeiz Godhand test suite.

Fixtures in here are auto-discovered by pytest, so tests can just request
them by name without explicit imports.

Three groups:
  - DB isolation: `tmp_db` swaps db.DB_PATH to a per-test sqlite file.
  - Discord mocks: `mock_guild`, `mock_member`, `make_role` build
    MagicMock objects that quack like discord.py's Guild/Member/Role for
    the narrow set of attributes the onboarding code touches.
  - External API stubs: `stub_external` patches wavu/ewgf/audit so nothing
    hits the network or tries to post to a real Discord channel.
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio


# --------------------------------------------------------------------------- #
# DB isolation                                                                 #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    """Give each test its own empty SQLite DB.

    We can't just swap `db.DB_PATH` once at session-scope — every db.*
    helper opens a fresh `aiosqlite.connect(DB_PATH)` per call, so the
    monkeypatch has to stick for the whole test body. tmp_path is
    function-scoped, which is exactly what we want: one test's writes
    don't leak into the next.
    """
    import db
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    await db.init_db()
    return db_path


# --------------------------------------------------------------------------- #
# Discord mocks                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class FakeRole:
    """Minimal stand-in for discord.Role — only the attributes the
    onboarding code reads. `id` is auto-assigned from an incrementing
    counter so equality-by-id works between fixtures."""
    id: int
    name: str
    position: int = 1

    def __hash__(self) -> int:
        return hash(self.id)


class _RoleFactory:
    """Hands out FakeRole instances with unique ids."""

    def __init__(self) -> None:
        self._next_id = 1000

    def __call__(self, name: str, position: int = 1) -> FakeRole:
        self._next_id += 1
        return FakeRole(id=self._next_id, name=name, position=position)


@pytest.fixture
def make_role() -> _RoleFactory:
    return _RoleFactory()


@pytest.fixture
def mock_guild(make_role):
    """A MagicMock guild with a mutable `roles` list and a configurable
    `get_member` return-map. Tests push FakeRole objects into guild.roles
    so that `discord.utils.get(guild.roles, name=...)` finds them."""
    guild = MagicMock()
    guild.id = 999_000_001
    guild.name = "Ehrgeiz (test)"
    guild.roles = []

    guild.default_role = make_role("@everyone", position=0)
    guild.roles.append(guild.default_role)

    # get_member is populated by tests to simulate "this user is / isn't
    # in the guild right now". Default: everyone is absent.
    guild._member_map: dict[int, MagicMock] = {}
    guild.get_member = lambda uid: guild._member_map.get(uid)

    # Role creation — not exercised in current tests, but present so
    # _ensure_role doesn't blow up if a test leaves a role out of
    # guild.roles by mistake.
    async def _create_role(name, reason=None, mentionable=False):
        role = make_role(name)
        guild.roles.append(role)
        return role
    guild.create_role = _create_role

    async def _edit_role_positions(positions, reason=None):
        for role, pos in positions.items():
            role.position = pos
    guild.edit_role_positions = _edit_role_positions

    guild.text_channels = []  # audit channel lookups get nothing
    guild.me = MagicMock()
    guild.me.top_role = make_role("bot-top", position=100)

    return guild


def _make_member(guild, *, member_id: int, display_name: str = "Tester",
                 roles: Iterable[FakeRole] = ()) -> MagicMock:
    member = MagicMock()
    member.id = member_id
    member.guild = guild
    member.bot = False
    member.roles = list(roles)
    member.mention = f"<@{member_id}>"
    member.__str__ = lambda self=None: display_name  # type: ignore[assignment]

    async def _add_roles(*new_roles, reason=None):
        for r in new_roles:
            if r not in member.roles:
                member.roles.append(r)
    member.add_roles = _add_roles

    async def _remove_roles(*to_remove, reason=None):
        member.roles = [r for r in member.roles if r not in to_remove]
    member.remove_roles = _remove_roles

    async def _send(*args, **kwargs):
        return MagicMock()
    member.send = _send

    guild._member_map[member_id] = member
    return member


@pytest.fixture
def mock_member(mock_guild):
    """Default member fixture — present in guild, no roles. Tests that
    need a custom setup should call _make_member directly via the
    `make_member` factory fixture below."""
    return _make_member(mock_guild, member_id=42, display_name="Tester")


@pytest.fixture
def make_member(mock_guild):
    """Factory form: `make_member(id=..., roles=[...])` in a test."""
    def _factory(*, member_id: int, display_name: str = "Tester",
                 roles: Iterable[FakeRole] = ()) -> MagicMock:
        return _make_member(
            mock_guild, member_id=member_id,
            display_name=display_name, roles=roles,
        )
    return _factory


# --------------------------------------------------------------------------- #
# External API stubs                                                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def stub_external(monkeypatch):
    """Stub wavu.lookup_player, ewgf.find_player_rank, ewgf.find_player_name,
    audit.post_event, and the pending-verification audit post (which
    writes to a real Discord channel).

    Returns a namespace where tests can reach in and rewrite return
    values / check call counts.
    """
    import audit
    import ewgf
    import wavu
    import cogs.onboarding as onboarding

    class Stubs:
        wavu_lookup = AsyncMock()
        # wavu.find_player_rank returns a (rating_mu, rank_name) tuple or
        # None in production. Default None means "no rank from wavu,
        # ewgf is the only source." Tests that set ewgf_rank will route
        # through that path cleanly.
        wavu_rank = AsyncMock(return_value=None)
        ewgf_rank = AsyncMock(return_value=None)
        ewgf_name = AsyncMock(return_value=None)
        audit_post = AsyncMock()
        start_pending = AsyncMock()

    monkeypatch.setattr(wavu, "lookup_player", Stubs.wavu_lookup)
    monkeypatch.setattr(wavu, "find_player_rank", Stubs.wavu_rank)
    monkeypatch.setattr(ewgf, "find_player_rank", Stubs.ewgf_rank)
    monkeypatch.setattr(ewgf, "lookup_display_name", Stubs.ewgf_name)
    monkeypatch.setattr(audit, "post_event", Stubs.audit_post)
    # _start_pending_verification tries to post to a #verification-log
    # channel that doesn't exist in our MagicMock guild — swap the
    # module-level reference so refresh_player_from_api's pending branch
    # is still exercised but doesn't crash.
    monkeypatch.setattr(onboarding, "_start_pending_verification", Stubs.start_pending)

    return Stubs


@pytest.fixture(autouse=True)
def _zero_resync_delay(monkeypatch):
    """The real resync loop sleeps 250ms per member to pace Discord role
    edits against the rate limiter. In tests that's wasted wall time —
    zero it out for every test so the whole suite finishes in seconds."""
    import cogs.onboarding as onboarding
    monkeypatch.setattr(onboarding, "_RESYNC_PER_MEMBER_DELAY", 0.0)
