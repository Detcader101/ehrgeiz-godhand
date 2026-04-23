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

WIDTH = 820
HEADER_HEIGHT = 54
ROW_HEIGHT = 84
PADDING_X = 24
PADDING_Y = 18
# Icons from ewgf are NOT square — rank icons are 500x250 wide plaques and
# character icons are 450x640 portraits. We give each a bounding box whose
# aspect matches the source so nothing gets squashed.
RANK_ICON_BOX = (120, 60)    # 2:1
CHAR_ICON_BOX = (52, 72)     # 3:4
INTER_PAD = 14

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
    x: int, y: int, box_w: int, box_h: int,
) -> None:
    """Paste an icon into a (box_w, box_h) slot, preserving aspect ratio
    and centering in the leftover space. Non-square sources render
    naturally instead of being stretched into a square."""
    if icon is None:
        return
    src_w, src_h = icon.size
    scale = min(box_w / src_w, box_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = icon.resize((new_w, new_h), Image.LANCZOS)
    ox = x + (box_w - new_w) // 2
    oy = y + (box_h - new_h) // 2
    canvas.paste(resized, (ox, oy), resized)


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
            x, y + (ROW_HEIGHT - RANK_ICON_BOX[1]) // 2,
            RANK_ICON_BOX[0], RANK_ICON_BOX[1],
        )
        x += RANK_ICON_BOX[0] + INTER_PAD
        _paste_icon(
            img, char_icons[i],
            x, y + (ROW_HEIGHT - CHAR_ICON_BOX[1]) // 2,
            CHAR_ICON_BOX[0], CHAR_ICON_BOX[1],
        )
        x += CHAR_ICON_BOX[0] + INTER_PAD

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


# --------------------------------------------------------------------------- #
# Round bracket render                                                         #
# --------------------------------------------------------------------------- #

BRACKET_WIDTH = 880
BRACKET_HEADER_H = 100
BRACKET_MATCH_H = 150
BRACKET_MATCH_GAP = 14
BRACKET_SIDE_PAD = 28
BRACKET_RANK_BOX = (140, 70)   # 2:1
BRACKET_CHAR_BOX = (58, 80)    # 3:4


async def _prefetch_icons_for_players(
    players: list[dict],
) -> tuple[dict[str, Image.Image | None], dict[str, Image.Image | None]]:
    """Download-if-needed + open rank and character icons for all non-None
    players in one network pass. Returned dicts are keyed by rank_tier /
    main_char so the renderer can look them up by string."""
    rank_keys = {p["rank_tier"] for p in players if p and p.get("rank_tier")}
    char_keys = {p["main_char"] for p in players if p and p.get("main_char")}

    def _need(urls: list[str | None], cache_dir: Path) -> bool:
        return any(
            u and not _cache_path_for(u, cache_dir).exists() for u in urls
        )

    rank_urls = {k: media.rank_icon_url(k) for k in rank_keys}
    char_urls = {k: media.character_icon_url(k) for k in char_keys}

    session: aiohttp.ClientSession | None = None
    try:
        if (_need(list(rank_urls.values()), RANK_CACHE_DIR)
                or _need(list(char_urls.values()), CHAR_CACHE_DIR)):
            session = aiohttp.ClientSession()
        rank_imgs = await asyncio.gather(*(
            _fetch_icon(u, RANK_CACHE_DIR, session) for u in rank_urls.values()
        ))
        char_imgs = await asyncio.gather(*(
            _fetch_icon(u, CHAR_CACHE_DIR, session) for u in char_urls.values()
        ))
    finally:
        if session is not None:
            await session.close()

    rank_lookup = dict(zip(rank_urls.keys(), rank_imgs))
    char_lookup = dict(zip(char_urls.keys(), char_imgs))
    return rank_lookup, char_lookup


async def render_bracket(
    *,
    tournament_name: str,
    round_number: int,
    matches: list[dict],
) -> io.BytesIO:
    """Render a round's pairings as a PNG.

    Each match dict: match_number (int), player_a (dict|None), player_b
    (dict|None), is_bye (bool). Player dicts have: display_name, rank_tier,
    main_char. For a bye, player_a holds the advancing player and player_b
    is None.
    """
    RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Pre-fetch every unique rank/char icon across the round in one pass.
    all_players: list[dict] = []
    for m in matches:
        if m["player_a"]:
            all_players.append(m["player_a"])
        if m["player_b"]:
            all_players.append(m["player_b"])
    rank_lookup, char_lookup = await _prefetch_icons_for_players(all_players)

    height = (
        BRACKET_HEADER_H + BRACKET_SIDE_PAD
        + len(matches) * (BRACKET_MATCH_H + BRACKET_MATCH_GAP)
        + BRACKET_SIDE_PAD
    )
    img = Image.new("RGBA", (BRACKET_WIDTH, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header with tournament name and round label.
    draw.rectangle(
        [(0, 0), (BRACKET_WIDTH, BRACKET_HEADER_H)], fill=HEADER_BG,
    )
    draw.line(
        [(0, BRACKET_HEADER_H), (BRACKET_WIDTH, BRACKET_HEADER_H)],
        fill=ACCENT, width=3,
    )
    name_font = _load_font(34)
    round_font = _load_font(22)
    draw.text(
        (BRACKET_SIDE_PAD, 18), tournament_name,
        fill=TEXT, font=name_font,
    )
    draw.text(
        (BRACKET_SIDE_PAD, 60),
        f"ROUND {round_number}  ·  PAIRINGS",
        fill=ACCENT, font=round_font,
    )

    match_label_font = _load_font(16)
    name_font_m = _load_font(24)
    rank_font_m = _load_font(16)
    vs_font = _load_font(34)
    bye_font = _load_font(20)

    for idx, match in enumerate(matches):
        y = (
            BRACKET_HEADER_H + BRACKET_SIDE_PAD
            + idx * (BRACKET_MATCH_H + BRACKET_MATCH_GAP)
        )
        if idx % 2 == 1:
            draw.rectangle(
                [(0, y), (BRACKET_WIDTH, y + BRACKET_MATCH_H)],
                fill=ROW_BG_ALT,
            )

        # Thin accent stripe on the left of each card.
        draw.rectangle(
            [(0, y), (4, y + BRACKET_MATCH_H)], fill=ACCENT,
        )

        draw.text(
            (BRACKET_SIDE_PAD, y + 8),
            f"MATCH {match['match_number']}"
            + ("  —  BYE" if match["is_bye"] else ""),
            fill=TEXT_DIM, font=match_label_font,
        )

        if match["is_bye"]:
            _render_bye(
                draw, img,
                y=y, match=match,
                rank_lookup=rank_lookup, char_lookup=char_lookup,
                name_font=name_font_m, rank_font=rank_font_m,
                bye_font=bye_font,
            )
        else:
            _render_versus(
                draw, img,
                y=y, match=match,
                rank_lookup=rank_lookup, char_lookup=char_lookup,
                name_font=name_font_m, rank_font=rank_font_m,
                vs_font=vs_font,
            )

    return _to_png_buf(img)


def _render_versus(
    draw: ImageDraw.ImageDraw, canvas: Image.Image,
    *, y: int, match: dict,
    rank_lookup: dict, char_lookup: dict,
    name_font, rank_font, vs_font,
) -> None:
    """Two-column 'A vs B' match card."""
    a = match["player_a"]
    b = match["player_b"]
    mid_x = BRACKET_WIDTH // 2

    # Player A (left side).
    content_y = y + 38
    _draw_player_row(
        draw, canvas, a, rank_lookup, char_lookup,
        x=BRACKET_SIDE_PAD, y=content_y,
        max_text_w=mid_x - BRACKET_SIDE_PAD - 60,
        name_font=name_font, rank_font=rank_font,
        align="left",
    )
    # Player B (right side, mirrored).
    _draw_player_row(
        draw, canvas, b, rank_lookup, char_lookup,
        x=BRACKET_WIDTH - BRACKET_SIDE_PAD, y=content_y,
        max_text_w=mid_x - BRACKET_SIDE_PAD - 60,
        name_font=name_font, rank_font=rank_font,
        align="right",
    )

    # VS divider in the middle.
    vs_y = y + (BRACKET_MATCH_H // 2) - 22
    _draw_vs(draw, canvas, mid_x, vs_y, vs_font)


def _draw_player_row(
    draw: ImageDraw.ImageDraw, canvas: Image.Image, player: dict,
    rank_lookup: dict, char_lookup: dict,
    *, x: int, y: int, max_text_w: int,
    name_font, rank_font, align: str,
) -> None:
    """Render a single player block: icons + name + rank tier. align='left'
    puts icons first then text; align='right' mirrors it."""
    rank_img = rank_lookup.get(player.get("rank_tier")) if player else None
    char_img = char_lookup.get(player.get("main_char")) if player else None

    rank_w, rank_h = BRACKET_RANK_BOX
    char_w, char_h = BRACKET_CHAR_BOX

    if align == "left":
        cx = x
        _paste_icon(canvas, rank_img,
                    cx, y + (char_h - rank_h) // 2,
                    rank_w, rank_h)
        cx += rank_w + 12
        _paste_icon(canvas, char_img, cx, y, char_w, char_h)
        cx += char_w + 14
        _draw_name_rank(draw, player, cx, y, name_font, rank_font, max_text_w,
                        text_align="left")
    else:
        # right-align: compute from the right edge backward.
        cx = x
        cx -= rank_w
        _paste_icon(canvas, rank_img,
                    cx, y + (char_h - rank_h) // 2,
                    rank_w, rank_h)
        cx -= 12
        cx -= char_w
        _paste_icon(canvas, char_img, cx, y, char_w, char_h)
        cx -= 14
        _draw_name_rank(draw, player, cx, y, name_font, rank_font, max_text_w,
                        text_align="right")


def _draw_name_rank(
    draw: ImageDraw.ImageDraw, player: dict,
    x: int, y: int,
    name_font, rank_font, max_w: int,
    text_align: str,
) -> None:
    name = player["display_name"]
    rank = player.get("rank_tier") or "Unranked"

    # Truncate name if it overflows the allocated text width.
    name = _ellipsize(draw, name, name_font, max_w)
    rank = _ellipsize(draw, rank, rank_font, max_w)

    if text_align == "left":
        draw.text((x, y + 20), name, fill=TEXT, font=name_font)
        draw.text((x, y + 52), rank, fill=TEXT_DIM, font=rank_font)
    else:
        name_w = _text_width(draw, name, name_font)
        rank_w = _text_width(draw, rank, rank_font)
        draw.text((x - name_w, y + 20), name, fill=TEXT, font=name_font)
        draw.text((x - rank_w, y + 52), rank, fill=TEXT_DIM, font=rank_font)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _ellipsize(
    draw: ImageDraw.ImageDraw, text: str, font, max_w: int,
) -> str:
    if _text_width(draw, text, font) <= max_w:
        return text
    ellipsis = "…"
    # Binary-chop to find the longest prefix that fits.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_width(draw, text[:mid] + ellipsis, font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


def _draw_vs(
    draw: ImageDraw.ImageDraw, canvas: Image.Image,
    cx: int, y: int, vs_font,
) -> None:
    """VS text with a red underline — the hype moment between the two
    player blocks."""
    vs = "VS"
    w = _text_width(draw, vs, vs_font)
    draw.text((cx - w // 2, y), vs, fill=ACCENT, font=vs_font)
    draw.line(
        [(cx - 22, y + 44), (cx + 22, y + 44)],
        fill=ACCENT, width=2,
    )


def _render_bye(
    draw: ImageDraw.ImageDraw, canvas: Image.Image,
    *, y: int, match: dict,
    rank_lookup: dict, char_lookup: dict,
    name_font, rank_font, bye_font,
) -> None:
    """Single-player centered bye row."""
    player = match["player_a"]
    # Centered block: char icon + rank icon + name stacked next to "BYE".
    rank_img = rank_lookup.get(player.get("rank_tier")) if player else None
    char_img = char_lookup.get(player.get("main_char")) if player else None

    rank_w, rank_h = BRACKET_RANK_BOX
    char_w, char_h = BRACKET_CHAR_BOX

    cx = BRACKET_WIDTH // 2 - (rank_w + char_w + 100) // 2
    content_y = y + 38

    _paste_icon(canvas, rank_img,
                cx, content_y + (char_h - rank_h) // 2,
                rank_w, rank_h)
    cx += rank_w + 12
    _paste_icon(canvas, char_img, cx, content_y, char_w, char_h)
    cx += char_w + 14

    name = _ellipsize(draw, player["display_name"], name_font, 320)
    draw.text((cx, content_y + 8), name, fill=TEXT, font=name_font)
    draw.text((cx, content_y + 40),
              player.get("rank_tier") or "Unranked",
              fill=TEXT_DIM, font=rank_font)
    draw.text((cx, content_y + 66),
              "— advances to round 2 —",
              fill=ACCENT, font=bye_font)
