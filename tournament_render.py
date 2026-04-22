"""Pillow tournament graphics. Slice-1 scope: signup-roster PNG.

Rank and character icons are fetched from ewgf.gg on first use and cached
under `assets/rank_cache/` and `assets/char_cache/`. Subsequent renders
are offline unless a new rank/character appears.

The same cache + compose machinery will be reused by slice 4 (bracket
PNG) and slice 5 (final-results archive image).
"""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

import media

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "assets"
RANK_CACHE_DIR = ASSETS_DIR / "rank_cache"
CHAR_CACHE_DIR = ASSETS_DIR / "char_cache"

# Ehrgeiz palette — near-black background with the brand red as the one
# accent, matching the logo and the embed color.
BG_COLOR = (20, 18, 20)
ROW_BG_ALT = (30, 26, 28)
HEADER_BG = (28, 22, 24)
ACCENT = (200, 30, 40)
TEXT = (235, 230, 225)
TEXT_DIM = (160, 155, 150)

WIDTH = 760
HEADER_HEIGHT = 54
ROW_HEIGHT = 72
PADDING_X = 24
PADDING_Y = 18
RANK_ICON_SIZE = 56
CHAR_ICON_SIZE = 48
INTER_PAD = 16

# Fonts are located lazily at render time; PIL's default is the fallback.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)


def _load_font(size: int):
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _cache_path_for(url: str, cache_dir: Path) -> Path:
    return cache_dir / url.rsplit("/", 1)[-1]


async def _download(
    url: str, dest: Path, session: aiohttp.ClientSession,
) -> bool:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("icon fetch %s returned HTTP %d", url, resp.status)
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(await resp.read())
            return True
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("icon fetch %s failed: %s", url, e)
        return False


async def _fetch_icon(
    url: str | None, cache_dir: Path, session: aiohttp.ClientSession | None,
) -> Image.Image | None:
    if not url:
        return None
    path = _cache_path_for(url, cache_dir)
    if not path.exists():
        if session is None:
            # Caller opted out of network — treat as missing rather than
            # block rendering on a network trip.
            return None
        if not await _download(url, path, session):
            return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception as e:
        log.warning("failed to open icon %s: %s", path, e)
        return None


def _any_missing_cache(participants: list[dict]) -> bool:
    for p in participants:
        r_url = media.rank_icon_url(p.get("rank_tier"))
        c_url = media.character_icon_url(p.get("main_char"))
        if r_url and not _cache_path_for(r_url, RANK_CACHE_DIR).exists():
            return True
        if c_url and not _cache_path_for(c_url, CHAR_CACHE_DIR).exists():
            return True
    return False


def _paste_icon(
    canvas: Image.Image, icon: Image.Image | None,
    x: int, y: int, size: int,
) -> None:
    if icon is None:
        return
    resized = icon.resize((size, size), Image.LANCZOS)
    canvas.paste(resized, (x, y), resized)


async def render_roster(participants: list[dict]) -> io.BytesIO:
    """Render a PNG of the signup roster.

    Each participant dict needs keys: display_name, rank_tier, main_char.
    Missing icons leave blank slots; missing rank/char strings show
    'Unranked' / no char icon.
    """
    RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    count = max(1, len(participants))
    height = HEADER_HEIGHT + count * ROW_HEIGHT + PADDING_Y * 2
    img = Image.new("RGBA", (WIDTH, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header bar.
    draw.rectangle([(0, 0), (WIDTH, HEADER_HEIGHT)], fill=HEADER_BG)
    draw.line(
        [(0, HEADER_HEIGHT), (WIDTH, HEADER_HEIGHT)],
        fill=ACCENT, width=2,
    )
    title_font = _load_font(26)
    draw.text(
        (PADDING_X, 13),
        f"ENTRANTS  ·  {len(participants)}",
        fill=TEXT, font=title_font,
    )

    if not participants:
        note_font = _load_font(18)
        draw.text(
            (PADDING_X, HEADER_HEIGHT + PADDING_Y + 14),
            "No one yet — be the first to step up.",
            fill=TEXT_DIM, font=note_font,
        )
        return _to_png_buf(img)

    # Only open a network session if at least one icon is missing from
    # cache. Hot path (all icons cached) stays fully local.
    session: aiohttp.ClientSession | None = None
    try:
        if _any_missing_cache(participants):
            session = aiohttp.ClientSession()
        rank_icons = await asyncio.gather(*(
            _fetch_icon(
                media.rank_icon_url(p.get("rank_tier")),
                RANK_CACHE_DIR, session,
            )
            for p in participants
        ))
        char_icons = await asyncio.gather(*(
            _fetch_icon(
                media.character_icon_url(p.get("main_char")),
                CHAR_CACHE_DIR, session,
            )
            for p in participants
        ))
    finally:
        if session is not None:
            await session.close()

    name_font = _load_font(22)
    rank_font = _load_font(16)

    for i, p in enumerate(participants):
        y = HEADER_HEIGHT + PADDING_Y + i * ROW_HEIGHT
        if i % 2 == 1:
            draw.rectangle(
                [(0, y), (WIDTH, y + ROW_HEIGHT)],
                fill=ROW_BG_ALT,
            )

        x = PADDING_X
        _paste_icon(
            img, rank_icons[i],
            x, y + (ROW_HEIGHT - RANK_ICON_SIZE) // 2,
            RANK_ICON_SIZE,
        )
        x += RANK_ICON_SIZE + INTER_PAD
        _paste_icon(
            img, char_icons[i],
            x, y + (ROW_HEIGHT - CHAR_ICON_SIZE) // 2,
            CHAR_ICON_SIZE,
        )
        x += CHAR_ICON_SIZE + INTER_PAD

        name_y = y + (ROW_HEIGHT - 44) // 2
        draw.text(
            (x, name_y), p["display_name"],
            fill=TEXT, font=name_font,
        )
        draw.text(
            (x, name_y + 26), p.get("rank_tier") or "Unranked",
            fill=TEXT_DIM, font=rank_font,
        )

    return _to_png_buf(img)


def _to_png_buf(img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
