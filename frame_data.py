"""Tekken 8 move frame data — starter set for the /whats-that-move quiz.

Hardcoded list of widely-known iconic moves and their frame-on-block
values, sourced from public community frame data (wavu.wiki,
combot, character launch sheets) at the time of writing. Frame data
shifts with every Bandai balance patch — treat this as a "spirit of
the matchup" snapshot rather than gospel, and update entries (or add
new ones) as patches drop.

The module is intentionally a flat list of dataclasses with no
external lookups. If a future iteration wants per-character pages,
swap the list for a `dict[str, list[Move]]`. For now the quiz picks
random entries from the unified list.

Adding a move:
    Move("Character", "input notation", "Common name", frames_on_block)

The character name must match wavu.T8_CHARACTERS so the bot can pull
the portrait icon. Keep notation in the community style (df+2, WS+2,
qcf+1+2, etc.) so notation purists can read it without translating.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Move:
    character: str
    notation: str
    name: str
    frames_on_block: int  # negative = punishable / minus on block


# Starter set: 20 moves spanning the cast. Bias toward moves whose
# "is this safe?" answer matters in matchups so the quiz is genuinely
# educational, not just trivia.
T8_KEY_MOVES: list[Move] = [
    Move("Kazuya",     "f,n,d,df+2",     "Electric Wind God Fist", -9),
    Move("Kazuya",     "df+2",           "Hellsweep df+2",         -13),
    Move("Heihachi",   "f,n,d,df+2",     "Electric (Heihachi)",    -8),
    Move("Jin",        "df+1,2",         "df+1,2 string",          -5),
    Move("Jin",        "f+4",            "Spinning Sidekick (f+4)",-9),
    Move("King",       "f,F+2+4",        "Giant Swing",            -8),
    Move("King",       "df+2,1",         "Knee Lift to Punch",     -9),
    Move("Paul",       "qcf+2",          "Demolition Man",         -13),
    Move("Paul",       "df+2",           "Sledgehammer (df+2)",    -10),
    Move("Bryan",      "df+2",           "Mach Breaker",           -13),
    Move("Bryan",      "f,F+3",          "Mach Kick",              -10),
    Move("Lili",       "ws+1+2",         "Rising Pirouette",       -3),
    Move("Lili",       "df+3+4",         "Slide",                  -23),
    Move("Lars",       "df+1,2",         "df+1,2",                  -7),
    Move("Lars",       "f,F+1+2",        "Lightning Screw",        -9),
    Move("Reina",      "f,n,d,df+2",     "Electric (Reina)",        -9),
    Move("Reina",      "df+1+2",         "df+1+2",                  -8),
    Move("Yoshimitsu", "f,n,d,df+1+2",   "Soul Stealer",            -23),
    Move("Asuka",      "ws+1",           "ws+1 (Asuka)",            -8),
    Move("Dragunov",   "df+2",           "df+2 launcher",           -13),
]


def all_moves() -> list[Move]:
    return list(T8_KEY_MOVES)
