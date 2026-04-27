"""Rank metadata helpers — colours, sections, ordinals.

The bot's rank-aware visuals (role colours on the server, embed tints,
player card progression bars, recap composites) all need the same
mapping from a Tekken rank name to a colour, section name, and
position within its section. This module is the single source of
truth so a palette tweak propagates everywhere with one edit.

Sections track the in-game rank-tier ribbon:

    Beginner       → grey
    Dan tier       → bronze
    Fighter        → green
    Ranger         → teal
    Vanquisher     → blue
    Garyu          → purple
    Ruler          → amber
    Fujin          → red
    Tekken King    → gold
    God of Destruction → violet → prismatic gold for ∞
"""
from __future__ import annotations

import discord

import wavu


# (R, G, B) tuples. Each section's tiers brighten progressively to
# telegraph "higher rank = brighter" inside that band.
RANK_COLORS: dict[str, tuple[int, int, int]] = {
    "Beginner":              (140, 140, 145),

    "1st Dan":               (130, 100,  70),
    "2nd Dan":               (170, 130,  80),

    "Fighter":               ( 60, 130,  80),
    "Strategist":            ( 75, 155, 100),
    "Combatant":             ( 95, 180, 120),
    "Brawler":               (120, 210, 140),

    "Ranger":                ( 60, 140, 150),
    "Cavalry":               ( 75, 165, 180),
    "Warrior":               ( 95, 190, 205),
    "Assailant":             (120, 215, 230),

    "Dominator":             ( 60,  90, 175),
    "Vanquisher":            ( 80, 120, 200),
    "Destroyer":             (100, 145, 220),
    "Eliminator":            (130, 170, 240),

    "Garyu":                 (110,  60, 175),
    "Shinryu":               (140,  90, 205),
    "Tenryu":                (175, 125, 230),

    "Mighty Ruler":          (200, 110,  50),
    "Flame Ruler":           (225, 145,  70),
    "Battle Ruler":          (245, 180,  95),

    "Fujin":                 (175,  50,  60),
    "Raijin":                (200,  80,  90),
    "Kishin":                (220, 110, 120),
    "Bushin":                (240, 145, 155),

    "Tekken King":           (200, 165,  50),
    "Tekken Emperor":        (225, 185,  70),
    "Tekken God":            (240, 205,  95),
    "Tekken God Supreme":    (255, 225, 130),

    "God of Destruction":    (180,  80, 200),
    "God of Destruction I":  (170, 100, 220),
    "God of Destruction II": (160, 130, 240),
    "God of Destruction III":(150, 170, 250),
    "God of Destruction ∞":  (255, 215,   0),
}


# Sections by index in wavu.TEKKEN_RANKS. Tuple is
# (section_label, [member_rank_names]) and the member list is in
# ascending tier order so position-in-section maths is just .index().
SECTIONS: list[tuple[str, list[str]]] = [
    ("Beginner",            [wavu.TEKKEN_RANKS[i] for i in range(0,  1)]),
    ("Dan",                 [wavu.TEKKEN_RANKS[i] for i in range(1,  3)]),
    ("Fighter",             [wavu.TEKKEN_RANKS[i] for i in range(3,  7)]),
    ("Ranger",              [wavu.TEKKEN_RANKS[i] for i in range(7,  11)]),
    ("Vanquisher",          [wavu.TEKKEN_RANKS[i] for i in range(11, 15)]),
    ("Garyu",               [wavu.TEKKEN_RANKS[i] for i in range(15, 18)]),
    ("Ruler",               [wavu.TEKKEN_RANKS[i] for i in range(18, 21)]),
    ("Fujin",               [wavu.TEKKEN_RANKS[i] for i in range(21, 25)]),
    ("Tekken King",         [wavu.TEKKEN_RANKS[i] for i in range(25, 29)]),
    ("God of Destruction",  [wavu.TEKKEN_RANKS[i] for i in range(29, 34)]),
]


def rank_color(rank_name: str | None) -> discord.Color:
    """Return the Discord colour for a rank, or `Color.default()` if the
    rank is None or unknown. Safe to call on user-supplied or stale rank
    strings."""
    if rank_name is None:
        return discord.Color.default()
    rgb = RANK_COLORS.get(rank_name)
    if rgb is None:
        return discord.Color.default()
    return discord.Color.from_rgb(*rgb)


def rank_color_rgb(rank_name: str | None) -> tuple[int, int, int] | None:
    """Raw RGB tuple for Pillow consumers (no Discord type involved)."""
    if rank_name is None:
        return None
    return RANK_COLORS.get(rank_name)


def rank_section(rank_name: str | None) -> str | None:
    """Return the section label for a rank ('Vanquisher', 'Tekken King',
    etc.) or None if the rank doesn't match any section. Used by the
    progression UI and rank-up cards to show 'X tier'."""
    if rank_name is None:
        return None
    for label, members in SECTIONS:
        if rank_name in members:
            return label
    return None


def rank_position_in_section(rank_name: str | None) -> tuple[int, int] | None:
    """Return (1-based position, total) within the rank's section,
    e.g. ('Destroyer' is 3rd of 4 in Vanquisher) → (3, 4). None if
    unknown."""
    if rank_name is None:
        return None
    for _, members in SECTIONS:
        if rank_name in members:
            return (members.index(rank_name) + 1, len(members))
    return None


def rank_ordinal(rank_name: str | None) -> int | None:
    """Absolute index in TEKKEN_RANKS (0..33). Used for promotion/demotion
    diff-detection; None if the rank is unknown so the caller can defer
    to the existing 'show rank as-is' fallback."""
    if rank_name is None:
        return None
    for idx, name in wavu.TEKKEN_RANKS.items():
        if name == rank_name:
            return idx
    return None


def is_promotion(from_rank: str | None, to_rank: str | None) -> bool:
    """True if a rank change moves up the tier list. Both must resolve
    to known ranks; an unknown rank short-circuits to False (no promotion
    inferred from a string we can't place)."""
    a = rank_ordinal(from_rank)
    b = rank_ordinal(to_rank)
    if a is None or b is None:
        return False
    return b > a
