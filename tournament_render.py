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
LOGO_PATH = ASSETS_DIR / "ehrgeiz.png"

# Ehrgeiz palette — near-black background with the brand red as the one
# accent, matching the logo and the embed color.
BG_COLOR = (20, 18, 20)
ROW_BG_ALT = (30, 26, 28)
HEADER_BG = (28, 22, 24)
ACCENT = (200, 30, 40)
TEXT = (235, 230, 225)
TEXT_DIM = (160, 155, 150)

# Roster layout constants are local to render_roster — the signup render
# is a fixed card grid. Bracket-specific constants live alongside the
# bracket renderer further down.

# Typography. We run two families:
#   - Display (Bebas Neue, condensed all-caps sans) for the broadcast-style
#     hero text: tournament title, round label, MATCH labels, score cell.
#     Bundled under assets/fonts/ (OFL-licensed, see OFL.txt).
#   - Body (system bold sans) for mixed-case player names + metadata.
# This matches TWT / EVO overlay conventions — condensed caps carry the
# event branding, a straightforward bold sans keeps names readable.
_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_DISPLAY_FONT_PATH = _FONT_DIR / "BebasNeue-Regular.ttf"
_BODY_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)


def _load_font(size: int):
    """Body font — bold sans for mixed-case text."""
    for candidate in _BODY_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _load_display_font(size: int):
    """Display font — Bebas Neue (bundled). Falls back to body bold if
    the TTF is missing for any reason."""
    try:
        return ImageFont.truetype(str(_DISPLAY_FONT_PATH), size)
    except (OSError, IOError):
        return _load_font(size)


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


def _paint_header_gradient(
    canvas: Image.Image, y_top: int, y_bot: int,
) -> None:
    """Subtle vertical gradient from HEADER_BG (lighter) at the top to
    BG_COLOR (darker) at the bottom — broadcast-overlay depth trick."""
    width = canvas.width
    span = max(1, y_bot - y_top)
    top_r, top_g, top_b = HEADER_BG
    bot_r, bot_g, bot_b = BG_COLOR
    for i in range(span):
        t = i / span
        r = int(top_r + (bot_r - top_r) * t)
        g = int(top_g + (bot_g - top_g) * t)
        b = int(top_b + (bot_b - top_b) * t)
        canvas.paste((r, g, b, 255), (0, y_top + i, width, y_top + i + 1))


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


def _fill_cell_with_icon(
    canvas: Image.Image, icon: Image.Image | None,
    rect: tuple[int, int, int, int], pad: int,
) -> None:
    """Aspect-preserving fit into a rectangular grid cell (letterboxed).
    Used for rank plaques where the whole plaque image matters."""
    if icon is None:
        return
    x0, y0, x1, y1 = rect
    box_w = max(1, (x1 - x0) - 2 * pad)
    box_h = max(1, (y1 - y0) - 2 * pad)
    _paste_icon(canvas, icon, x0 + pad, y0 + pad, box_w, box_h)


def _fill_cell_with_icon_cover(
    canvas: Image.Image, icon: Image.Image | None,
    rect: tuple[int, int, int, int], pad: int,
    vertical_anchor: float = 0.0,
) -> None:
    """Scale-and-crop an icon to completely fill a grid cell (CSS
    'object-fit: cover'). Horizontal overflow crops centered; vertical
    overflow crops by `vertical_anchor` (0 = top, 1 = bottom). For
    character portraits ~0.18 gives a 'bust shot' framing — shoulders
    and face dominate, top of head sliver trimmed."""
    if icon is None:
        return
    x0, y0, x1, y1 = rect
    cell_w = max(1, (x1 - x0) - 2 * pad)
    cell_h = max(1, (y1 - y0) - 2 * pad)
    src_w, src_h = icon.size
    scale = max(cell_w / src_w, cell_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = icon.resize((new_w, new_h), Image.LANCZOS)
    if new_w > cell_w:
        offset_x = (new_w - cell_w) // 2
        resized = resized.crop((offset_x, 0, offset_x + cell_w, new_h))
    if resized.size[1] > cell_h:
        overflow = resized.size[1] - cell_h
        offset_y = int(overflow * max(0.0, min(1.0, vertical_anchor)))
        resized = resized.crop(
            (0, offset_y, resized.size[0], offset_y + cell_h),
        )
    canvas.paste(resized, (x0 + pad, y0 + pad), resized)


def _fit_text_to_box(
    draw: ImageDraw.ImageDraw, text: str,
    *, max_w: int, max_h: int,
    max_size: int, min_size: int, step: int = 2,
    font_loader=None,
):
    """Pick the largest font size at which `text` fits inside a
    (max_w, max_h) box. Defaults to the display face; pass
    `_load_font` for a mixed-case body bold instead."""
    loader = font_loader or _load_display_font
    for size in range(max_size, min_size - 1, -step):
        font = loader(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= max_w and th <= max_h:
            return font
    return loader(min_size)


async def render_roster(participants: list[dict]) -> io.BytesIO:
    """Render the signup roster as a grid of player cards.

    Each card is a strict 2x2 rectangular layout:
      ┌──────────┬────────────┐
      │          │ RANK       │
      │ PORTRAIT ├────────────┤
      │          │ NAME       │
      └──────────┴────────────┘
    The portrait is a top-anchored cover crop (bust-shot framing); the
    character name is a red chip on the portrait's bottom edge. Name
    text scales to fill the name cell — short handles dominate, long
    ones shrink, but the card silhouette never changes.

    Each participant dict needs keys: display_name, rank_tier, main_char.
    Missing icons leave blank slots; missing rank/char strings show
    'Unranked' / no char chip.
    """
    RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Pre-fetch all icons in one go (network session opened only if
    # cache misses — steady-state is fully offline).
    session: aiohttp.ClientSession | None = None
    try:
        if participants and _any_missing_cache(participants):
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

    # Pillow compose runs off-loop — a full roster can take hundreds of
    # ms and would otherwise block the gateway heartbeat during busy
    # signups (every Join/Leave click re-renders).
    return await asyncio.to_thread(
        _compose_roster_png, participants, rank_icons, char_icons,
    )


def _compose_roster_png(
    participants: list[dict],
    rank_icons: list[Image.Image | None],
    char_icons: list[Image.Image | None],
) -> io.BytesIO:
    W = 880
    COLS = 2
    CARD_W = 404
    CARD_H = 180
    HEADER_H = 120
    GAP = 16
    PORTRAIT_W = 140
    RANK_H = 110
    NAME_H = CARD_H - RANK_H
    CELL_PAD = 10
    PORTRAIT_PAD = 2
    CHIP_H = 40

    # Cell background tints — the portrait cell stays slightly darker
    # than the rank/name cells so the grid tiles read without hard
    # borders.
    PORTRAIT_CELL_BG = (24, 20, 22)
    RIGHT_CELL_BG = ROW_BG_ALT

    rows = max(1, (len(participants) + COLS - 1) // COLS)
    H = HEADER_H + GAP + rows * (CARD_H + GAP) + GAP
    img = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header — title + entrant count in Bebas over a subtle gradient.
    _paint_header_gradient(img, 0, HEADER_H)
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)
    draw.text((24, 14), "EHRGEIZ GODHAND",
              fill=TEXT, font=_load_display_font(52))
    draw.text((24, 74), f"ENTRANTS  ·  {len(participants)}",
              fill=ACCENT, font=_load_display_font(26))

    if not participants:
        note_font = _load_font(18)
        draw.text(
            (24, HEADER_H + GAP + 24),
            "No one yet — be the first to step up.",
            fill=TEXT_DIM, font=note_font,
        )
        return _to_png_buf(img)

    for i, p in enumerate(participants):
        col = i % COLS
        row = i // COLS
        cx = 24 + col * (CARD_W + 24)
        cy = HEADER_H + GAP + row * (CARD_H + GAP)

        portrait_rect = (cx, cy, cx + PORTRAIT_W, cy + CARD_H)
        rank_rect     = (cx + PORTRAIT_W, cy,
                         cx + CARD_W, cy + RANK_H)
        name_rect     = (cx + PORTRAIT_W, cy + RANK_H,
                         cx + CARD_W, cy + CARD_H)

        draw.rectangle(portrait_rect, fill=PORTRAIT_CELL_BG)
        draw.rectangle(rank_rect,     fill=RIGHT_CELL_BG)
        draw.rectangle(name_rect,     fill=RIGHT_CELL_BG)
        draw.rectangle([(cx, cy), (cx + 4, cy + CARD_H)], fill=ACCENT)
        # 1-px grid rules between cells.
        draw.line([(cx + PORTRAIT_W, cy),
                   (cx + PORTRAIT_W, cy + CARD_H)],
                  fill=BG_COLOR, width=1)
        draw.line([(cx + PORTRAIT_W, cy + RANK_H),
                   (cx + CARD_W, cy + RANK_H)],
                  fill=BG_COLOR, width=1)

        # --- Portrait cell — cover-crop above the chip strip ----------- #
        portrait_image_rect = (
            portrait_rect[0], portrait_rect[1],
            portrait_rect[2], portrait_rect[3] - CHIP_H,
        )
        _fill_cell_with_icon_cover(
            img, char_icons[i],
            portrait_image_rect, pad=PORTRAIT_PAD,
            vertical_anchor=0.18,
        )

        # Character chip — solid red strip, crisp flat edges.
        if p.get("main_char"):
            chip_top = cy + CARD_H - CHIP_H
            draw.rectangle(
                [(cx, chip_top), (cx + PORTRAIT_W, cy + CARD_H)],
                fill=ACCENT,
            )
            label = p["main_char"].upper()
            chip_font = _fit_text_to_box(
                draw, label,
                max_w=PORTRAIT_W - 12, max_h=CHIP_H - 10,
                max_size=34, min_size=14,
            )
            bbox = draw.textbbox((0, 0), label, font=chip_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text(
                (cx + (PORTRAIT_W - tw) // 2,
                 chip_top + (CHIP_H - th) // 2 - bbox[1]),
                label, fill=TEXT, font=chip_font,
            )

        # --- Rank plaque cell — aspect-preserved fill ----------------- #
        _fill_cell_with_icon(
            img, rank_icons[i], rank_rect, pad=CELL_PAD,
        )

        # --- Name cell — mixed-case body font scaled to fill ---------- #
        name_label = p["display_name"]
        name_font = _fit_text_to_box(
            draw, name_label,
            max_w=name_rect[2] - name_rect[0] - 2 * CELL_PAD,
            max_h=name_rect[3] - name_rect[1] - 2 * CELL_PAD,
            max_size=34, min_size=12,
            font_loader=_load_font,
        )
        bbox = draw.textbbox((0, 0), name_label, font=name_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = name_rect[0] + ((name_rect[2] - name_rect[0]) - tw) // 2
        ty = (name_rect[1]
              + ((name_rect[3] - name_rect[1]) - th) // 2
              - bbox[1])
        draw.text((tx, ty), name_label, fill=TEXT, font=name_font)

    return _to_png_buf(img)


def _to_png_buf(img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------- #
# Channel banner render                                                        #
# --------------------------------------------------------------------------- #

BANNER_W = 960
BANNER_H = 240


async def render_banner(
    *,
    title: str,
    subtitle: str | None = None,
    kicker: str | None = None,
    body: str | None = None,
) -> io.BytesIO:
    """Wide banner for a pinned channel panel.

    Hero band (960×240): Ehrgeiz logo left, stacked kicker/title/
    subtitle right, red accent strips. If `body` is supplied, a second
    band appears below the hero with the word-wrapped body text baked
    into the same PNG — keeps the bot's posts visually cohesive rather
    than splitting content between the image and an embed description.
    """
    body_band_h = _compute_body_band_height(body) if body else 0
    total_h = BANNER_H + body_band_h

    img = Image.new("RGBA", (BANNER_W, total_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Hero background wash — only paints the hero band, not the body.
    _paint_banner_gradient(img, y_start=0, y_end=BANNER_H)

    # Top accent strip, and a divider between hero + body (which
    # doubles as the bottom stripe if no body is present).
    draw.rectangle([(0, 0), (BANNER_W, 4)], fill=ACCENT)
    draw.rectangle([(0, BANNER_H - 4), (BANNER_W, BANNER_H)], fill=ACCENT)

    # Logo — vertically centered, a reasonable size left margin.
    logo_x = 40
    logo_w = 180
    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            src_w, src_h = logo.size
            scale = min(logo_w / src_w, (BANNER_H - 40) / src_h)
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))
            resized = logo.resize((new_w, new_h), Image.LANCZOS)
            img.alpha_composite(
                resized,
                (logo_x + (logo_w - new_w) // 2,
                 (BANNER_H - new_h) // 2),
            )
        except (OSError, IOError) as e:
            log.warning("banner logo load failed: %s", e)

    # Text block — left edge just past the logo, vertically centered
    # as a group.
    text_x = logo_x + logo_w + 30
    text_max_w = BANNER_W - text_x - 40

    kicker_h = 26 if kicker else 0
    title_h = 80
    sub_h = 32 if subtitle else 0
    gap_ks = 8 if kicker else 0
    gap_ts = 10 if subtitle else 0
    block_h = kicker_h + gap_ks + title_h + gap_ts + sub_h
    block_y = (BANNER_H - block_h) // 2

    y = block_y
    if kicker:
        kf = _fit_text_to_box(
            draw, kicker.upper(),
            max_w=text_max_w, max_h=kicker_h,
            max_size=22, min_size=14,
        )
        draw.text((text_x, y), kicker.upper(), fill=ACCENT, font=kf)
        y += kicker_h + gap_ks

    title_font = _fit_text_to_box(
        draw, title.upper(),
        max_w=text_max_w, max_h=title_h,
        max_size=96, min_size=30,
    )
    draw.text((text_x, y), title.upper(), fill=TEXT, font=title_font)
    y += title_h + gap_ts

    if subtitle:
        sf = _fit_text_to_box(
            draw, subtitle.upper(),
            max_w=text_max_w, max_h=sub_h,
            max_size=28, min_size=14,
        )
        draw.text((text_x, y), subtitle.upper(), fill=TEXT_DIM, font=sf)

    # Body band below the hero — word-wrapped text baked into the PNG.
    if body:
        _render_banner_body(img, draw, body, y_start=BANNER_H)
        draw.rectangle(
            [(0, total_h - 4), (BANNER_W, total_h)],
            fill=ACCENT,
        )

    return _to_png_buf(img)


def _paint_banner_gradient(
    canvas: Image.Image, y_start: int = 0, y_end: int | None = None,
) -> None:
    """Left-to-right wash: red-tinted shadow on the left (under the logo)
    fading into the neutral background on the right. Optionally bounded
    to a vertical slice so the body band below doesn't inherit the
    gradient."""
    width = canvas.width
    y_end = canvas.height if y_end is None else y_end
    left_tint = (55, 22, 28)
    neutral = BG_COLOR
    for i in range(width):
        t = min(1.0, i / (width * 0.5))
        r = int(left_tint[0] + (neutral[0] - left_tint[0]) * t)
        g = int(left_tint[1] + (neutral[1] - left_tint[1]) * t)
        b = int(left_tint[2] + (neutral[2] - left_tint[2]) * t)
        canvas.paste((r, g, b, 255), (i, y_start, i + 1, y_end))


# --------------------------------------------------------------------------- #
# Banner body — word-wrapped text baked into the image                         #
# --------------------------------------------------------------------------- #

BANNER_BODY_FONT_SIZE = 28
BANNER_BODY_HEADER_FONT_SIZE = 30
BANNER_BODY_LINE_GAP = 14
BANNER_BODY_PAD_X = 60
BANNER_BODY_PAD_Y = 50
BANNER_BODY_PARA_GAP = 22     # extra pixels on blank-line paragraph breaks
BANNER_BODY_HEADER_GAP = 10    # extra pixels above ## headers
BANNER_BODY_BG = (18, 16, 18)


def _strip_body_markdown(s: str) -> str:
    """Flatten embed-style markdown so the text renders cleanly as a
    flat image. `**bold**` loses its markers (the body font is already
    bold); backticks drop for the same reason."""
    s = s.replace("**", "")
    s = s.replace("`", "")
    return s


def _layout_body(body: str, max_w: int) -> list[tuple[str, str]]:
    """Tokenize the body into a list of (kind, rendered_text) rows.

    kind ∈ {"header", "body", "break"}:
      - "## X" source lines become one ("header", "X") row.
      - Blank source lines become ("break", "") — a paragraph pause.
      - Everything else gets word-wrapped as ("body", line_text).

    Wrapping uses the appropriate font for the row type so a long
    header still fits inside the body cell."""
    scratch = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(scratch)
    body_font = _load_font(BANNER_BODY_FONT_SIZE)
    header_font = _load_font(BANNER_BODY_HEADER_FONT_SIZE)

    def _wrap(paragraph: str, font) -> list[str]:
        words = paragraph.split()
        wrapped: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_w:
                current = candidate
            else:
                if current:
                    wrapped.append(current)
                current = word
        if current:
            wrapped.append(current)
        return wrapped or [paragraph]

    rows: list[tuple[str, str]] = []
    for paragraph in body.split("\n"):
        stripped = paragraph.strip()
        if not stripped:
            rows.append(("break", ""))
            continue
        if stripped.startswith("## "):
            for line in _wrap(stripped[3:], header_font):
                rows.append(("header", line.upper()))
            continue
        for line in _wrap(stripped, body_font):
            rows.append(("body", line))
    return rows


def _row_height(kind: str) -> int:
    if kind == "header":
        return (BANNER_BODY_HEADER_FONT_SIZE + BANNER_BODY_LINE_GAP
                + BANNER_BODY_HEADER_GAP)
    if kind == "break":
        return BANNER_BODY_PARA_GAP
    return BANNER_BODY_FONT_SIZE + BANNER_BODY_LINE_GAP


def _compute_body_band_height(body: str) -> int:
    rows = _layout_body(
        _strip_body_markdown(body),
        max_w=BANNER_W - 2 * BANNER_BODY_PAD_X,
    )
    total = 2 * BANNER_BODY_PAD_Y + sum(_row_height(k) for k, _ in rows)
    # Guarantee a minimum band height for single-line bodies.
    return max(total, 2 * BANNER_BODY_PAD_Y + _row_height("body"))


def _render_banner_body(
    canvas: Image.Image, draw: ImageDraw.ImageDraw,
    body: str, y_start: int,
) -> None:
    height = _compute_body_band_height(body)
    draw.rectangle(
        [(0, y_start), (BANNER_W, y_start + height)],
        fill=BANNER_BODY_BG,
    )
    body_font = _load_font(BANNER_BODY_FONT_SIZE)
    header_font = _load_font(BANNER_BODY_HEADER_FONT_SIZE)
    rows = _layout_body(
        _strip_body_markdown(body),
        max_w=BANNER_W - 2 * BANNER_BODY_PAD_X,
    )

    y = y_start + BANNER_BODY_PAD_Y
    for kind, text in rows:
        if kind == "header":
            y += BANNER_BODY_HEADER_GAP
            draw.text(
                (BANNER_BODY_PAD_X, y), text,
                fill=ACCENT, font=header_font,
            )
            y += BANNER_BODY_HEADER_FONT_SIZE + BANNER_BODY_LINE_GAP
        elif kind == "break":
            y += BANNER_BODY_PARA_GAP
        else:
            draw.text(
                (BANNER_BODY_PAD_X, y), text,
                fill=TEXT, font=body_font,
            )
            y += BANNER_BODY_FONT_SIZE + BANNER_BODY_LINE_GAP


# --------------------------------------------------------------------------- #
# Bot profile banner (shown on the bot's Discord user card)                    #
# --------------------------------------------------------------------------- #

PROFILE_BANNER_W = 680
PROFILE_BANNER_H = 240


async def render_bot_profile_banner() -> io.BytesIO:
    """Render the 680×240 Discord-profile banner that sits at the top of
    the bot's user card.

    Discord overlays the bot's avatar (already the Ehrgeiz fist) in the
    bottom-left corner of the banner and a kebab menu in the top-right,
    so we deliberately leave those zones empty and anchor the text to
    the upper-centre instead — no duplicated logo, no collisions with
    Discord UI chrome.
    """
    img = Image.new("RGBA", (PROFILE_BANNER_W, PROFILE_BANNER_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    _paint_banner_gradient(img, y_start=0, y_end=PROFILE_BANNER_H)
    draw.rectangle([(0, 0), (PROFILE_BANNER_W, 4)], fill=ACCENT)
    draw.rectangle(
        [(0, PROFILE_BANNER_H - 4), (PROFILE_BANNER_W, PROFILE_BANNER_H)],
        fill=ACCENT,
    )

    # Safe-zone layout.
    #   - Avatar circle sits bottom-left, reaching ~130px right and
    #     ~130px up from the banner's lower-left corner.
    #   - Kebab/"..." button sits top-right, ~40×40.
    # Text anchors into the LOWER-RIGHT quadrant — the only block of
    # pixels neither UI element covers. Deliberately off-centre: this
    # composition is Discord-chrome-aware, not symmetric.
    text_x = 265
    text_right_gutter = 40
    text_max_w = PROFILE_BANNER_W - text_x - text_right_gutter

    title = "EHRGEIZ GODHAND"
    tagline = "TEKKEN 8 SERVER COMPANION"

    title_h = 56
    tag_h = 20
    gap = 10
    block_h = title_h + gap + tag_h
    # Block sits below centre so the title's baseline drops past the
    # top-right kebab zone and the whole thing hovers above the bottom
    # accent strip without touching the avatar's crown.
    block_y = PROFILE_BANNER_H - block_h - 48

    title_font = _fit_text_to_box(
        draw, title,
        max_w=text_max_w, max_h=title_h,
        max_size=50, min_size=26,
    )
    tag_font = _fit_text_to_box(
        draw, tagline,
        max_w=text_max_w, max_h=tag_h,
        max_size=18, min_size=12,
    )

    draw.text((text_x, block_y), title, fill=TEXT, font=title_font)
    draw.text(
        (text_x, block_y + title_h + gap), tagline,
        fill=ACCENT, font=tag_font,
    )

    return _to_png_buf(img)


# --------------------------------------------------------------------------- #
# README artwork                                                               #
# --------------------------------------------------------------------------- #

README_HERO_W = 1200
README_HERO_H = 360


async def render_readme_hero() -> io.BytesIO:
    """Wide Ehrgeiz hero for the top of README.md. Logo anchored left,
    bold Bebas title, tagline, brand accent strips — sized generously
    for GitHub which downsizes to fit the reader's column."""
    img = Image.new("RGBA", (README_HERO_W, README_HERO_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    _paint_banner_gradient(img, y_start=0, y_end=README_HERO_H)
    draw.rectangle([(0, 0), (README_HERO_W, 5)], fill=ACCENT)
    draw.rectangle(
        [(0, README_HERO_H - 5), (README_HERO_W, README_HERO_H)],
        fill=ACCENT,
    )

    logo_x = 60
    logo_box = 260
    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            src_w, src_h = logo.size
            scale = min(logo_box / src_w, logo_box / src_h)
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))
            resized = logo.resize((new_w, new_h), Image.LANCZOS)
            img.alpha_composite(
                resized,
                (logo_x + (logo_box - new_w) // 2,
                 (README_HERO_H - new_h) // 2),
            )
        except (OSError, IOError) as e:
            log.warning("readme hero logo load failed: %s", e)

    text_x = logo_x + logo_box + 36
    text_max_w = README_HERO_W - text_x - 60

    kicker = "TEKKEN 8 DISCORD BOT"
    title = "EHRGEIZ GODHAND"
    tagline = (
        "ONBOARDING · RANK SYNC · SWISS TOURNAMENTS"
    )
    footer = "PANEL-DRIVEN · OPEN SOURCE · MIT LICENSED"

    kicker_h = 26
    title_h = 108
    tag_h = 28
    footer_h = 20
    gap_small = 10
    gap_large = 18
    block_h = (kicker_h + gap_small + title_h + gap_small
               + tag_h + gap_large + footer_h)
    block_y = (README_HERO_H - block_h) // 2

    kicker_font = _fit_text_to_box(
        draw, kicker, max_w=text_max_w, max_h=kicker_h,
        max_size=22, min_size=16,
    )
    title_font = _fit_text_to_box(
        draw, title, max_w=text_max_w, max_h=title_h,
        max_size=96, min_size=48,
    )
    tag_font = _fit_text_to_box(
        draw, tagline, max_w=text_max_w, max_h=tag_h,
        max_size=24, min_size=16,
    )
    footer_font = _fit_text_to_box(
        draw, footer, max_w=text_max_w, max_h=footer_h,
        max_size=18, min_size=12,
    )

    y = block_y
    draw.text((text_x, y), kicker, fill=ACCENT, font=kicker_font)
    y += kicker_h + gap_small
    draw.text((text_x, y), title, fill=TEXT, font=title_font)
    y += title_h + gap_small
    draw.text((text_x, y), tagline, fill=TEXT_DIM, font=tag_font)
    y += tag_h + gap_large
    draw.text((text_x, y), footer, fill=ACCENT, font=footer_font)

    return _to_png_buf(img)


# --- Rank lookup flow diagram --------------------------------------------- #

RANK_FLOW_W = 1200
RANK_FLOW_H = 340
RANK_FLOW_BOX_W = 300
RANK_FLOW_BOX_H = 180
RANK_FLOW_BOX_GAP = 50


async def render_rank_flow_diagram() -> io.BytesIO:
    """Three-box horizontal flowchart illustrating the wavu → ewgf →
    self-report rank-lookup chain."""
    img = Image.new("RGBA", (RANK_FLOW_W, RANK_FLOW_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    _paint_banner_gradient(img, y_start=0, y_end=RANK_FLOW_H)
    draw.rectangle([(0, 0), (RANK_FLOW_W, 4)], fill=ACCENT)
    draw.rectangle(
        [(0, RANK_FLOW_H - 4), (RANK_FLOW_W, RANK_FLOW_H)], fill=ACCENT,
    )

    title_font = _load_display_font(32)
    draw.text(
        (40, 28), "RANK LOOKUP CHAIN",
        fill=TEXT, font=title_font,
    )

    sub_font = _load_display_font(18)
    draw.text(
        (40, 68),
        "Try each source in order · fall through to the next if it has nothing",
        fill=ACCENT, font=sub_font,
    )

    # Three cards side-by-side, centered horizontally.
    total_w = 3 * RANK_FLOW_BOX_W + 2 * RANK_FLOW_BOX_GAP
    start_x = (RANK_FLOW_W - total_w) // 2
    card_y = 140

    steps = [
        {
            "label": "PRIMARY",
            "heading": "wavu.wiki",
            "sub": "/api/replays",
            "body": "Scrapes recent matches\nfor rank_id.",
            "note": "Authoritative\nfor last ~35 min.",
        },
        {
            "label": "SECONDARY",
            "heading": "ewgf.gg",
            "sub": "RSC payload",
            "body": "Player page scrape\nfor current season.",
            "note": "Covers inactive\nplayers wavu misses.",
        },
        {
            "label": "FALLBACK",
            "heading": "Self-report",
            "sub": "two-stage picker",
            "body": "User selects tier\nfrom a dropdown.",
            "note": "Last resort —\nuser is trusted.",
        },
    ]

    label_font = _load_display_font(18)
    head_font = _load_display_font(30)
    sub_step_font = _load_font(14)
    body_font = _load_font(15)
    note_font = _load_font(13)
    # Body (DejaVu) covers the Unicode arrow glyph — Bebas doesn't.
    arrow_font = _load_font(56)

    for i, step in enumerate(steps):
        x = start_x + i * (RANK_FLOW_BOX_W + RANK_FLOW_BOX_GAP)
        _draw_flow_card(
            draw, img, x, card_y,
            step, label_font, head_font, sub_step_font,
            body_font, note_font,
        )
        if i < len(steps) - 1:
            arrow_x = x + RANK_FLOW_BOX_W + (RANK_FLOW_BOX_GAP // 2)
            arrow_y = card_y + (RANK_FLOW_BOX_H // 2) - 28
            w = _text_width(draw, "→", arrow_font)
            draw.text(
                (arrow_x - w // 2, arrow_y), "→",
                fill=ACCENT, font=arrow_font,
            )

    return _to_png_buf(img)


def _draw_flow_card(
    draw: ImageDraw.ImageDraw, canvas: Image.Image,
    x: int, y: int, step: dict,
    label_font, head_font, sub_step_font, body_font, note_font,
) -> None:
    # Card body + left accent stripe.
    draw.rectangle(
        [(x, y), (x + RANK_FLOW_BOX_W, y + RANK_FLOW_BOX_H)],
        fill=ROW_BG_ALT,
    )
    draw.rectangle([(x, y), (x + 5, y + RANK_FLOW_BOX_H)], fill=ACCENT)

    # Label (PRIMARY / SECONDARY / FALLBACK) in red.
    draw.text((x + 18, y + 12), step["label"],
              fill=ACCENT, font=label_font)

    # Heading (wavu.wiki, etc.) — Bebas white.
    draw.text((x + 18, y + 38), step["heading"],
              fill=TEXT, font=head_font)
    draw.text((x + 18, y + 76), step["sub"],
              fill=TEXT_DIM, font=sub_step_font)

    # Body lines.
    by = y + 104
    for line in step["body"].split("\n"):
        draw.text((x + 18, by), line,
                  fill=TEXT, font=body_font)
        by += 18

    # Note (small dim footer in the card).
    ny = y + RANK_FLOW_BOX_H - 36
    for line in step["note"].split("\n"):
        draw.text((x + 18, ny), line,
                  fill=TEXT_DIM, font=note_font)
        ny += 15


# --------------------------------------------------------------------------- #
# Round bracket render                                                         #
# --------------------------------------------------------------------------- #

BRACKET_WIDTH = 880
BRACKET_HEADER_H = 120
BRACKET_MATCH_H = 168
BRACKET_MATCH_GAP = 14
BRACKET_SIDE_PAD = 28
BRACKET_RANK_BOX = (140, 70)   # 2:1
BRACKET_CHAR_BOX = (58, 80)    # 3:4
# Horizontal exclusion zone around the center VS cell — name text must
# never enter this band, so long handles shrink to fit rather than
# overlapping the VS/WIN label.
BRACKET_VS_MARGIN = 70
# A subtle red wash painted onto the winner's half of a reported match.
# Low alpha so it tints without overpowering the stripe.
WINNER_TINT = (200, 30, 40, 28)


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

    # Pillow compose runs off-loop — a full bracket can take ~500ms and
    # would otherwise stall every interaction during a round post.
    return await asyncio.to_thread(
        _compose_bracket_png,
        tournament_name, round_number, matches, rank_lookup, char_lookup,
    )


def _compose_bracket_png(
    tournament_name: str,
    round_number: int,
    matches: list[dict],
    rank_lookup: dict,
    char_lookup: dict,
) -> io.BytesIO:
    height = (
        BRACKET_HEADER_H + BRACKET_SIDE_PAD
        + len(matches) * (BRACKET_MATCH_H + BRACKET_MATCH_GAP)
        + BRACKET_SIDE_PAD
    )
    img = Image.new("RGBA", (BRACKET_WIDTH, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header with tournament name and round label — gradient background +
    # accent underline. Title is upper-cased for broadcast feel; Bebas
    # Neue (all-caps display face) carries the TWT/EVO overlay idiom.
    _paint_header_gradient(img, 0, BRACKET_HEADER_H)
    draw.line(
        [(0, BRACKET_HEADER_H), (BRACKET_WIDTH, BRACKET_HEADER_H)],
        fill=ACCENT, width=3,
    )
    title_font = _load_display_font(58)
    round_font = _load_display_font(28)
    draw.text(
        (BRACKET_SIDE_PAD, 14), tournament_name.upper(),
        fill=TEXT, font=title_font,
    )
    draw.text(
        (BRACKET_SIDE_PAD, 74),
        f"ROUND {round_number}  ·  PAIRINGS",
        fill=ACCENT, font=round_font,
    )

    match_label_font = _load_display_font(24)
    char_name_font = _load_display_font(18)
    vs_font = _load_display_font(48)
    bye_font = _load_display_font(22)

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

        # Winner-side tint (painted onto the full card half) — a quiet
        # red wash that indicates "this side won". Pre-match it's
        # absent; byes show the bye player's side tinted automatically
        # because winner_id is pre-filled.
        winner_id = match.get("winner_id")
        if winner_id is not None and not match["is_bye"]:
            a_uid = match["player_a"]["user_id"] if match["player_a"] else None
            b_uid = match["player_b"]["user_id"] if match["player_b"] else None
            if winner_id == a_uid:
                _paint_half_tint(img, y, y + BRACKET_MATCH_H, side="left")
            elif winner_id == b_uid:
                _paint_half_tint(img, y, y + BRACKET_MATCH_H, side="right")

        # Thin accent stripe on the left of each card.
        draw.rectangle(
            [(0, y), (4, y + BRACKET_MATCH_H)], fill=ACCENT,
        )

        label = f"MATCH {match['match_number']}"
        if match["is_bye"]:
            label += "  ·  BYE"
        draw.text(
            (BRACKET_SIDE_PAD, y + 6),
            label, fill=TEXT_DIM, font=match_label_font,
        )

        if match["is_bye"]:
            _render_bye(
                draw, img,
                y=y, match=match,
                rank_lookup=rank_lookup, char_lookup=char_lookup,
                char_name_font=char_name_font, bye_font=bye_font,
            )
        else:
            _render_versus(
                draw, img,
                y=y, match=match,
                rank_lookup=rank_lookup, char_lookup=char_lookup,
                char_name_font=char_name_font, vs_font=vs_font,
            )

    return _to_png_buf(img)


def _paint_half_tint(
    canvas: Image.Image, y_top: int, y_bot: int, side: str,
) -> None:
    """Lay a low-opacity red wash over one half of a match card to mark
    the winner. Uses RGBA compositing so the existing background
    (including alt-row stripes) shows through faintly."""
    mid_x = canvas.width // 2
    x0, x1 = (0, mid_x) if side == "left" else (mid_x, canvas.width)
    tint = Image.new("RGBA", (x1 - x0, y_bot - y_top), WINNER_TINT)
    canvas.alpha_composite(tint, (x0, y_top))


def _render_versus(
    draw: ImageDraw.ImageDraw, canvas: Image.Image,
    *, y: int, match: dict,
    rank_lookup: dict, char_lookup: dict,
    char_name_font, vs_font,
) -> None:
    """Two-column 'A vs B' match card. Name text auto-sizes to fit its
    zone (never bleeds into the VS cell). Loser's text dims once a
    winner is reported."""
    a = match["player_a"]
    b = match["player_b"]
    mid_x = BRACKET_WIDTH // 2
    winner_id = match.get("winner_id")
    a_lost = winner_id is not None and a and winner_id != a["user_id"]
    b_lost = winner_id is not None and b and winner_id != b["user_id"]

    content_y = y + 48
    _draw_player_row(
        draw, canvas, a, rank_lookup, char_lookup,
        x=BRACKET_SIDE_PAD, y=content_y,
        name_zone_right_limit=mid_x - BRACKET_VS_MARGIN,
        char_name_font=char_name_font,
        align="left", dim=a_lost,
    )
    _draw_player_row(
        draw, canvas, b, rank_lookup, char_lookup,
        x=BRACKET_WIDTH - BRACKET_SIDE_PAD, y=content_y,
        name_zone_right_limit=mid_x + BRACKET_VS_MARGIN,
        char_name_font=char_name_font,
        align="right", dim=b_lost,
    )

    vs_y = y + (BRACKET_MATCH_H // 2) - 28
    _draw_vs(draw, canvas, mid_x, vs_y, vs_font,
             resolved=winner_id is not None)


def _draw_player_row(
    draw: ImageDraw.ImageDraw, canvas: Image.Image, player: dict,
    rank_lookup: dict, char_lookup: dict,
    *, x: int, y: int, name_zone_right_limit: int,
    char_name_font,
    align: str, dim: bool = False,
) -> None:
    """Render icons + name + rank tier. The name zone is bounded
    explicitly by `name_zone_right_limit` (or left-limit on the right
    side of the card) so the VS cell is guaranteed clear — names scale
    down to fit rather than overlapping. Character name chip sits
    beneath the portrait."""
    rank_img = rank_lookup.get(player.get("rank_tier")) if player else None
    char_img = char_lookup.get(player.get("main_char")) if player else None
    char_name = (player or {}).get("main_char")

    rank_w, rank_h = BRACKET_RANK_BOX
    char_w, char_h = BRACKET_CHAR_BOX

    if align == "left":
        cx = x
        _paste_icon(canvas, rank_img,
                    cx, y + (char_h - rank_h) // 2,
                    rank_w, rank_h)
        cx += rank_w + 12
        _paste_icon(canvas, char_img, cx, y, char_w, char_h)
        _draw_char_name(draw, char_name, cx, y + char_h + 2, char_w,
                        char_name_font, align="left", dim=dim)
        cx += char_w + 14
        name_zone_w = max(40, name_zone_right_limit - cx)
        _draw_name_rank(draw, player, cx, y, name_zone_w,
                        text_align="left", dim=dim)
    else:
        cx = x
        cx -= rank_w
        _paste_icon(canvas, rank_img,
                    cx, y + (char_h - rank_h) // 2,
                    rank_w, rank_h)
        cx -= 12
        cx -= char_w
        _paste_icon(canvas, char_img, cx, y, char_w, char_h)
        _draw_char_name(draw, char_name, cx, y + char_h + 2, char_w,
                        char_name_font, align="center", dim=dim)
        cx -= 14
        name_zone_w = max(40, cx - name_zone_right_limit)
        _draw_name_rank(draw, player, cx, y, name_zone_w,
                        text_align="right", dim=dim)


def _draw_char_name(
    draw: ImageDraw.ImageDraw, char_name: str | None,
    x: int, y: int, box_w: int, font, *, align: str, dim: bool,
) -> None:
    """Small uppercase label beneath the character portrait. 'Kazuya',
    'Devil Jin', etc. Centred within the portrait's width."""
    if not char_name:
        return
    label = char_name.upper()
    w = _text_width(draw, label, font)
    if align == "left" or align == "center":
        # Centered within the portrait box.
        tx = x + (box_w - w) // 2
    else:
        tx = x + (box_w - w) // 2
    draw.text((tx, y), label,
              fill=(TEXT_DIM if dim else ACCENT), font=font)


def _draw_name_rank(
    draw: ImageDraw.ImageDraw, player: dict,
    x: int, y: int, zone_w: int,
    text_align: str, dim: bool = False,
) -> None:
    """Draw the name + rank-tier lines fitted to a fixed-width zone.

    Name scales 24pt→14pt to stay inside `zone_w`, rank tier scales
    16pt→12pt. This locks the horizontal silhouette of each match card:
    icons sit in their slots, VS sits in its cell, names shrink to
    whatever size is needed to avoid collisions."""
    name = player["display_name"]
    rank = player.get("rank_tier") or "Unranked"

    name_font = _fit_text_to_box(
        draw, name, max_w=zone_w, max_h=30,
        max_size=24, min_size=14, font_loader=_load_font,
    )
    rank_font = _fit_text_to_box(
        draw, rank, max_w=zone_w, max_h=22,
        max_size=16, min_size=12, font_loader=_load_font,
    )

    name_color = TEXT_DIM if dim else TEXT
    rank_color = TEXT_DIM

    if text_align == "left":
        draw.text((x, y + 16), name, fill=name_color, font=name_font)
        draw.text((x, y + 48), rank, fill=rank_color, font=rank_font)
    else:
        name_w = _text_width(draw, name, name_font)
        rank_w = _text_width(draw, rank, rank_font)
        draw.text((x - name_w, y + 16), name, fill=name_color, font=name_font)
        draw.text((x - rank_w, y + 48), rank, fill=rank_color, font=rank_font)


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
    cx: int, y: int, vs_font, *, resolved: bool,
) -> None:
    """Big VS in Bebas with a short red underline. Once the match is
    resolved we swap to 'WIN' so the card reads differently at a glance
    (paired with the winner-side tint)."""
    label = "WIN" if resolved else "VS"
    w = _text_width(draw, label, vs_font)
    draw.text((cx - w // 2, y), label, fill=ACCENT, font=vs_font)
    draw.line(
        [(cx - 26, y + 56), (cx + 26, y + 56)],
        fill=ACCENT, width=3,
    )


def _render_bye(
    draw: ImageDraw.ImageDraw, canvas: Image.Image,
    *, y: int, match: dict,
    rank_lookup: dict, char_lookup: dict,
    char_name_font, bye_font,
) -> None:
    """Single-player centered bye row."""
    player = match["player_a"]
    rank_img = rank_lookup.get(player.get("rank_tier")) if player else None
    char_img = char_lookup.get(player.get("main_char")) if player else None

    rank_w, rank_h = BRACKET_RANK_BOX
    char_w, char_h = BRACKET_CHAR_BOX

    # Reserve a fixed name zone so long handles don't push icons off-card.
    bye_name_zone_w = 320
    cx = BRACKET_WIDTH // 2 - (rank_w + char_w + bye_name_zone_w + 24) // 2
    content_y = y + 44

    _paste_icon(canvas, rank_img,
                cx, content_y + (char_h - rank_h) // 2,
                rank_w, rank_h)
    cx += rank_w + 12
    _paste_icon(canvas, char_img, cx, content_y, char_w, char_h)
    _draw_char_name(draw, (player or {}).get("main_char"),
                    cx, content_y + char_h + 2, char_w, char_name_font,
                    align="center", dim=False)
    cx += char_w + 14

    name_font = _fit_text_to_box(
        draw, player["display_name"],
        max_w=bye_name_zone_w, max_h=30,
        max_size=24, min_size=14, font_loader=_load_font,
    )
    rank_text = player.get("rank_tier") or "Unranked"
    rank_font = _fit_text_to_box(
        draw, rank_text,
        max_w=bye_name_zone_w, max_h=22,
        max_size=16, min_size=12, font_loader=_load_font,
    )
    draw.text((cx, content_y + 4), player["display_name"],
              fill=TEXT, font=name_font)
    draw.text((cx, content_y + 36), rank_text,
              fill=TEXT_DIM, font=rank_font)
    draw.text((cx, content_y + 62),
              "— ADVANCES TO ROUND 2 —",
              fill=ACCENT, font=bye_font)
