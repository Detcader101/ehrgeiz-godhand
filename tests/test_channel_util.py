"""Behaviour spec for channel_util.find_text_channel.

Contract: callers ask for a *base* name like "verification-log" and get
the channel whether SERVER_PLAN has rebranded it with an emoji prefix
or not. This is the load-bearing piece of every audit-log lookup, the
banner provisioner, the panel installer, and the tournament cog.
"""
from __future__ import annotations

from types import SimpleNamespace

import channel_util


def _channel(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _guild(*names: str) -> SimpleNamespace:
    return SimpleNamespace(text_channels=[_channel(n) for n in names])


# --------------------------------------------------------------------------- #
# find_text_channel                                                            #
# --------------------------------------------------------------------------- #

def test_finds_channel_by_bare_name():
    found = channel_util.find_text_channel(
        _guild("verification-log", "general"), "verification-log",
    )
    assert found is not None
    assert found.name == "verification-log"


def test_finds_channel_when_emoji_prefix_present():
    # Regression — SERVER_PLAN brands names with emoji prefixes, and
    # callers must not need to know which form is live in any guild.
    found = channel_util.find_text_channel(
        _guild("🔍-verification-log", "general"), "verification-log",
    )
    assert found is not None
    assert found.name == "🔍-verification-log"


def test_returns_none_when_no_channel_matches():
    assert channel_util.find_text_channel(
        _guild("general", "off-topic"), "verification-log",
    ) is None


def test_does_not_match_a_channel_that_merely_contains_the_base_name():
    # "verification-log-extra" is a different channel, not a prefixed
    # variant. The matcher must require exact equality or the
    # "<prefix>-{base}" suffix shape.
    assert channel_util.find_text_channel(
        _guild("verification-log-extra"), "verification-log",
    ) is None


# --------------------------------------------------------------------------- #
# base_name_of                                                                 #
# --------------------------------------------------------------------------- #

def test_strips_emoji_prefix_to_recover_base_name():
    assert channel_util.base_name_of(_channel("🏆-tournaments")) == "tournaments"


def test_keeps_ascii_dashed_name_unchanged():
    # `off-topic` has no emoji prefix — its dashes are part of the name.
    assert channel_util.base_name_of(_channel("off-topic")) == "off-topic"


def test_returns_unprefixed_name_unchanged():
    assert channel_util.base_name_of(_channel("general")) == "general"
