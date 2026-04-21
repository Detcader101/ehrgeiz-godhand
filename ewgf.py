"""ewgf.gg scraping client (secondary rank source).

ewgf.gg is a Next.js app that embeds player data in React Server Components
streamed payloads (`self.__next_f.push([...])`). Rank tier names appear inline
as e.g. `\\"currentSeasonRank\\":\\"Tekken King\\"` — we regex them out directly
rather than parsing the full payload.

We use ewgf as a fallback to wavu's `/api/replays` lookup, which only sees
matches in roughly the last ~35 minutes and misses anyone less active.

ewgf rejects requests without a real-browser User-Agent (returns 403).

If ewgf changes their site, only this file needs to change.
"""
from __future__ import annotations

import asyncio
import re

import aiohttp

from wavu import TEKKEN_RANKS

BASE_URL = "https://ewgf.gg"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Reverse lookup: rank name -> ordinal (higher = better rank).
_RANK_ORDER: dict[str, int] = {name: i for i, name in TEKKEN_RANKS.items()}

# Match escaped JSON inside RSC payload chunks.
_CURRENT_RANK_RE = re.compile(r'\\"currentSeasonRank\\":\\"([^\\"]+)\\"')
_HIGHEST_RANK_RE = re.compile(r'\\"allTimeHighestRank\\":\\"([^\\"]+)\\"')


class EwgfError(Exception):
    pass


def _highest_known(names: list[str]) -> str | None:
    best: tuple[int, str] | None = None
    for n in names:
        idx = _RANK_ORDER.get(n)
        if idx is None:
            continue
        if best is None or idx > best[0]:
            best = (idx, n)
    return best[1] if best else None


async def find_player_rank(
    tekken_id: str, *, timeout_s: float = 15.0
) -> str | None:
    """Return the player's current rank tier from ewgf.gg, or None if unknown.

    Takes the highest current-season rank across all characters; falls back to
    the highest all-time rank if no current-season data exists.
    """
    url = f"{BASE_URL}/player/{tekken_id}"
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise EwgfError(f"ewgf.gg returned {resp.status}")
                html = await resp.text()
    except asyncio.TimeoutError as e:
        raise EwgfError("ewgf.gg took too long to respond.") from e
    except aiohttp.ClientError as e:
        raise EwgfError(f"Couldn't reach ewgf.gg: {e}") from e

    current = _CURRENT_RANK_RE.findall(html)
    best = _highest_known(current)
    if best is not None:
        return best
    highest = _HIGHEST_RANK_RE.findall(html)
    return _highest_known(highest)
