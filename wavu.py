"""
wavu.wiki scraping client.

We scrape https://wank.wavu.wiki/player/{tekken_id} because Wavu Wank does not
expose a player-lookup JSON API — only /api/replays. The page renders server-side
HTML with the player's display name, per-character glicko-2 ratings, and game
counts.

If wavu changes their markup, only this file needs to change. The bot layer
only depends on `lookup_player()` returning a `PlayerProfile`.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup

from cache import TTLCache

BASE_URL = "https://wank.wavu.wiki"
USER_AGENT = "TekkenTOBot/0.1 (discord onboarding; contact: server owner)"

_CACHE = TTLCache()

# Polaris Battle IDs are 12-char base62-ish strings. Loose validation only;
# reject obvious junk but let the wavu lookup be the source of truth.
TEKKEN_ID_RE = re.compile(r"^[A-Za-z0-9]{10,16}$")


# Tekken 8 rank_id -> rank tier name.
# Source: community rank docs (fandom, esports.gg, etc.). If a lookup returns
# a clearly-wrong label, adjust here. The `p1_rank` / `p2_rank` fields in wavu's
# /api/replays response are the authoritative integer IDs we map against.
TEKKEN_RANKS: dict[int, str] = {
    0:  "Beginner",
    1:  "1st Dan",
    2:  "2nd Dan",
    3:  "Fighter",
    4:  "Strategist",
    5:  "Combatant",
    6:  "Brawler",
    7:  "Ranger",
    8:  "Cavalry",
    9:  "Warrior",
    10: "Assailant",
    11: "Dominator",
    12: "Vanquisher",
    13: "Destroyer",
    14: "Eliminator",
    15: "Garyu",
    16: "Shinryu",
    17: "Tenryu",
    18: "Mighty Ruler",
    19: "Flame Ruler",
    20: "Battle Ruler",
    21: "Fujin",
    22: "Raijin",
    23: "Kishin",
    24: "Bushin",
    25: "Tekken King",
    26: "Tekken Emperor",
    27: "Tekken God",
    28: "Tekken God Supreme",
    29: "God of Destruction",
    30: "God of Destruction I",
    31: "God of Destruction II",
    32: "God of Destruction III",
    33: "God of Destruction ∞",
}


# Ordered list of rank names (for dropdowns in the self-report fallback).
ALL_RANK_NAMES: list[str] = [TEKKEN_RANKS[k] for k in sorted(TEKKEN_RANKS)]


def rank_id_to_name(rank_id: int) -> str:
    return TEKKEN_RANKS.get(rank_id, f"Rank {rank_id}")


@dataclass
class PlayerProfile:
    tekken_id: str
    display_name: str
    main_char: str | None
    rating_mu: float | None
    rank_tier: str | None  # None until resolved via replay API or self-report


class PlayerNotFound(Exception):
    pass


class WavuError(Exception):
    pass


# Known T8 roster. Used to anchor the character-block parser: any line equal to
# one of these names starts a stats block. Keeps false positives out.
T8_CHARACTERS = {
    "Alisa", "Anna", "Armor King", "Asuka", "Azucena", "Bryan", "Claudio",
    "Clive", "Devil Jin", "Dragunov", "Eddy", "Fahkumram", "Feng", "Heihachi",
    "Hwoarang", "Jack-8", "Jin", "Josie", "Jun", "Kazuya", "King", "Kuma",
    "Lars", "Law", "Lee", "Leo", "Leroy", "Lidia", "Lili", "Nina", "Panda",
    "Paul", "Raven", "Reina", "Shaheen", "Steve", "Victor", "Xiaoyu", "Yoshimitsu",
    "Zafina",
}

# Section headers seen on wavu player pages, ranked best → worst.
_SECTION_ORDER = ["Leaderboard", "Unqualified", "Provisional"]


def _extract_display_name(soup: BeautifulSoup, tekken_id: str) -> str:
    title = soup.find("title")
    if title and title.string:
        raw = title.string.strip()
        # Real format: "Detcader_ • Wavu Wank" — take text before the first
        # separator (•, |, –, —) and strip site-name suffix.
        parts = re.split(r"\s*[•|\u2013\u2014]\s*", raw, maxsplit=1)
        if parts and parts[0]:
            name = parts[0].strip()
            # Filter out wavu's error/placeholder titles. When wavu has no
            # record of a player (e.g. they exist on Polaris but haven't
            # played ranked), the page 200s with title "Error * Wavu Wank".
            if name.lower() not in {"wavu wank", "wank", "error", "not found", "404"}:
                return name
    # Last-resort fallback. Caller may upgrade this with a better name from
    # another source (e.g. ewgf); until then the tekken_id is used so the
    # player row has something readable.
    return tekken_id


def _extract_best_character(soup: BeautifulSoup) -> tuple[str | None, float | None, int, str | None]:
    """Return (character, mu, games, section) for the player's best-qualified main."""
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]

    # Walk the lines, tracking the most recent section header, and parse blocks
    # shaped like:
    #     Lee
    #     μ 1660
    #     σ² 63
    #     3017 games
    current_section: str | None = None
    best: tuple[str, float, int, str | None] | None = None

    for i, line in enumerate(lines):
        for s in _SECTION_ORDER:
            if line.startswith(s):
                current_section = s
                break

        if line not in T8_CHARACTERS:
            continue

        block = lines[i + 1 : i + 8]  # look a few lines ahead
        mu = games = None
        for bl in block:
            if mu is None:
                mu_m = re.match(r"[μu]\s*([0-9]+(?:\.[0-9]+)?)", bl)
                if mu_m:
                    mu = float(mu_m.group(1))
                    continue
            if games is None:
                g_m = re.match(r"(\d+)\s*games?", bl)
                if g_m:
                    games = int(g_m.group(1))
                    break
        if mu is None:
            continue

        candidate = (line, mu, games or 0, current_section)
        if best is None:
            best = candidate
            continue
        # Rank candidates: prefer better section (Leaderboard > Unqualified > Provisional),
        # then higher μ, then more games.
        def score(c):
            section_rank = _SECTION_ORDER.index(c[3]) if c[3] in _SECTION_ORDER else len(_SECTION_ORDER)
            return (-section_rank, c[1], c[2])
        if score(candidate) > score(best):
            best = candidate

    if best is None:
        return None, None, 0, None
    return best


def _parse_profile(tekken_id: str, html: str) -> PlayerProfile:
    soup = BeautifulSoup(html, "html.parser")
    display_name = _extract_display_name(soup, tekken_id)
    char, mu, _games, _section = _extract_best_character(soup)
    return PlayerProfile(
        tekken_id=tekken_id,
        display_name=display_name,
        main_char=char,
        rating_mu=mu,
        rank_tier=None,  # resolved downstream via find_player_rank() or self-report
    )


async def lookup_player(
    tekken_id: str, *, timeout_s: float = 10.0, force_refresh: bool = False
) -> PlayerProfile:
    tekken_id = re.sub(r"[\s\-_]+", "", tekken_id)
    if not TEKKEN_ID_RE.match(tekken_id):
        raise PlayerNotFound(
            "That doesn't look like a Tekken ID. It should be ~12 alphanumeric "
            "characters (find it on your Tekken 8 player card)."
        )
    return await _CACHE.get_or_fetch(
        f"wavu:profile:{tekken_id}",
        lambda: _lookup_player_uncached(tekken_id, timeout_s=timeout_s),
        force_refresh=force_refresh,
    )


async def _lookup_player_uncached(tekken_id: str, *, timeout_s: float) -> PlayerProfile:
    url = f"{BASE_URL}/player/{tekken_id}"
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 404:
                    raise PlayerNotFound(
                        f"No wavu.wiki profile found for Tekken ID `{tekken_id}`. "
                        "Double-check the ID — play at least one ranked match "
                        "online and give wavu a few minutes to index you."
                    )
                if resp.status >= 500:
                    raise WavuError(f"wavu.wiki returned {resp.status}. Try again in a bit.")
                if resp.status != 200:
                    raise WavuError(f"Unexpected response from wavu.wiki: {resp.status}")
                html = await resp.text()
    except asyncio.TimeoutError as e:
        raise WavuError("wavu.wiki took too long to respond.") from e
    except aiohttp.ClientError as e:
        raise WavuError(f"Couldn't reach wavu.wiki: {e}") from e

    return _parse_profile(tekken_id, html)


async def find_player_rank(
    tekken_id: str,
    *,
    max_pages: int = 3,
    timeout_s: float = 15.0,
    force_refresh: bool = False,
) -> tuple[int, str] | None:
    """
    Search wavu's replay stream for the target player's most recent match
    and return (rank_id, rank_name). Returns None if not found within
    `max_pages` * ~700 seconds of replay history.
    """
    tekken_id = re.sub(r"[\s\-_]+", "", tekken_id)
    return await _CACHE.get_or_fetch(
        f"wavu:rank:{tekken_id}",
        lambda: _find_player_rank_uncached(tekken_id, max_pages=max_pages, timeout_s=timeout_s),
        force_refresh=force_refresh,
    )


async def _find_player_rank_uncached(
    tekken_id: str, *, max_pages: int, timeout_s: float
) -> tuple[int, str] | None:
    url = f"{BASE_URL}/api/replays"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    before: int | None = None
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for _page in range(max_pages):
            params = {"_format": "json"}
            if before is not None:
                params["before"] = str(before)
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        raise WavuError(f"replays API returned {resp.status}")
                    data = await resp.json()
            except asyncio.TimeoutError as e:
                raise WavuError("replays API timed out.") from e
            except aiohttp.ClientError as e:
                raise WavuError(f"Couldn't reach replays API: {e}") from e

            if not data:
                return None

            oldest_battle_at = None
            for rec in data:
                bat = rec.get("battle_at")
                if oldest_battle_at is None or (bat is not None and bat < oldest_battle_at):
                    oldest_battle_at = bat
                if rec.get("p1_polaris_id") == tekken_id:
                    rid = rec.get("p1_rank")
                    if isinstance(rid, int):
                        return rid, rank_id_to_name(rid)
                if rec.get("p2_polaris_id") == tekken_id:
                    rid = rec.get("p2_rank")
                    if isinstance(rid, int):
                        return rid, rank_id_to_name(rid)

            if oldest_battle_at is None:
                return None
            before = oldest_battle_at  # paginate older

    return None
