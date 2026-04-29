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
# Single-player card (Player Hub /profile, fit-check, etc.)                    #
# --------------------------------------------------------------------------- #

PLAYER_CARD_W = 720
PLAYER_CARD_H = 300
# Supersampling factor — render at 2x then downsample with LANCZOS for
# crisp anti-aliased text. The OUTPUT stays at PLAYER_CARD_W × _H so
# Discord's inline preview works at native scale; the win is per-pixel
# AA from the downsample.
PLAYER_CARD_SCALE = 2


async def render_player_card(
    *,
    display_name: str,
    rank_tier: str | None,
    main_char: str | None,
    tekken_id: str | None = None,
    badge: str | None = None,
    badges: list[tuple[str, tuple[int, int, int]]] | None = None,
    is_verified: bool = False,
) -> io.BytesIO:
    """Standalone broadcast-style card for one player.

    Reuses the roster-card visual language (red accent strip, character
    portrait + chip on the left, rank plaque + stacked text on the right)
    but at a larger single-card size so it reads on its own in a profile
    embed or pinned post. `badge` is an optional small caption shown in
    place of the tekken_id (e.g. "Fit Check · KAZUYA") for reusing the
    same renderer in feature-specific contexts.
    """
    RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    rank_url = media.rank_icon_url(rank_tier)
    char_url = media.character_icon_url(main_char)

    session: aiohttp.ClientSession | None = None
    rank_icon: Image.Image | None = None
    char_icon: Image.Image | None = None
    try:
        needs_net = (
            (rank_url and not _cache_path_for(rank_url, RANK_CACHE_DIR).exists())
            or (char_url and not _cache_path_for(char_url, CHAR_CACHE_DIR).exists())
        )
        if needs_net:
            session = aiohttp.ClientSession()
        rank_icon = await _fetch_icon(rank_url, RANK_CACHE_DIR, session)
        char_icon = await _fetch_icon(char_url, CHAR_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_player_card_png,
        display_name, rank_tier, main_char, tekken_id, badge, badges or [],
        is_verified,
        rank_icon, char_icon,
    )


def _compose_player_card_png(
    display_name: str,
    rank_tier: str | None,
    main_char: str | None,
    tekken_id: str | None,
    badge: str | None,
    badges: list[tuple[str, tuple[int, int, int]]],
    is_verified: bool,
    rank_icon: Image.Image | None,
    char_icon: Image.Image | None,
) -> io.BytesIO:
    # Supersample: every dimension and font size is multiplied by SCALE
    # at render time, then we downsample to the published PLAYER_CARD_W
    # × _H at the end. The result is the same physical card size but
    # with sub-pixel-accurate AA on text edges and chip strokes.
    S = PLAYER_CARD_SCALE

    # Add a footer band when badges are present so they don't crowd the
    # name/caption above. Each badge is a chip ~36px tall; the band is
    # 52px to give breathing room.
    badge_band_h = 52 * S if badges else 0
    W = PLAYER_CARD_W * S
    H = PLAYER_CARD_H * S + badge_band_h
    PORTRAIT_W = 264 * S
    CHIP_H = 52 * S
    ACCENT_W = 8 * S
    PAD = 22 * S

    img = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Background gradient — same depth trick as the roster header.
    _paint_header_gradient(img, 0, H)

    # Left red accent strip — visual anchor borrowed from the roster card.
    draw.rectangle([(0, 0), (ACCENT_W, H)], fill=ACCENT)

    # --- Portrait region with cover-crop + character chip ------------------ #
    # Pin the portrait to the *main* card region — when a trailing badge
    # band is appended below, the portrait must NOT extend into it, or
    # the character chip and the leftmost badge would overlap.
    main_h = PLAYER_CARD_H * S
    portrait_rect = (ACCENT_W, 0, ACCENT_W + PORTRAIT_W, main_h)
    draw.rectangle(portrait_rect, fill=(24, 20, 22))
    portrait_image_rect = (
        portrait_rect[0], portrait_rect[1],
        portrait_rect[2], portrait_rect[3] - CHIP_H,
    )
    _fill_cell_with_icon_cover(
        img, char_icon, portrait_image_rect, pad=2 * S, vertical_anchor=0.18,
    )
    if main_char:
        chip_top = main_h - CHIP_H
        draw.rectangle(
            [(portrait_rect[0], chip_top), (portrait_rect[2], main_h)],
            fill=ACCENT,
        )
        label = main_char.upper()
        chip_font = _fit_text_to_box(
            draw, label,
            max_w=PORTRAIT_W - 16 * S, max_h=CHIP_H - 14 * S,
            max_size=44 * S, min_size=28 * S,
        )
        bbox = draw.textbbox((0, 0), label, font=chip_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (portrait_rect[0] + (PORTRAIT_W - tw) // 2,
             chip_top + (CHIP_H - th) // 2 - bbox[1]),
            label, fill=TEXT, font=chip_font,
        )

    # --- Right column — rank plaque + stacked text ------------------------- #
    right_x0 = ACCENT_W + PORTRAIT_W
    right_w = W - right_x0
    rank_box_h = 132 * S
    rank_rect = (right_x0, PAD, right_x0 + rank_box_h, PAD + rank_box_h)
    _fill_cell_with_icon(img, rank_icon, rank_rect, pad=4 * S)

    # Rank tier name beside the plaque (display font, accent-tinted).
    # Unranked players get a quiet em-dash in TEXT_DIM rather than a loud
    # red "UNRANKED" — the visual hierarchy still flags rank as the main
    # right-column signal without shouting at people who haven't pulled
    # one yet.
    has_rank = bool(rank_tier)
    rank_label = rank_tier.upper() if has_rank else "—"
    rank_label_color = ACCENT if has_rank else TEXT_DIM
    rank_label_x = rank_rect[2] + 16 * S
    rank_label_w = W - rank_label_x - PAD
    rank_font = _fit_text_to_box(
        draw, rank_label,
        max_w=rank_label_w, max_h=rank_box_h - 36 * S,
        max_size=54 * S, min_size=30 * S,
    )
    bbox = draw.textbbox((0, 0), rank_label, font=rank_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (rank_label_x, rank_rect[1] + (rank_box_h - th) // 2 - bbox[1]),
        rank_label, fill=rank_label_color, font=rank_font,
    )

    # Display name — body font, mixed-case preserved.
    name_y = rank_rect[3] + 18 * S
    name_box_w = right_w - 2 * PAD
    name_box_h = 70 * S
    name_font = _fit_text_to_box(
        draw, display_name,
        max_w=name_box_w, max_h=name_box_h,
        max_size=56 * S, min_size=28 * S,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), display_name, font=name_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (right_x0 + PAD, name_y - bbox[1]),
        display_name, fill=TEXT, font=name_font,
    )

    # Optional caption line — `badge` (e.g. "Fit Check · KAZUYA") wins over
    # `tekken_id`. Both render identically; the field is just routed
    # through the same slot so callers can override the default ID line.
    caption = badge if badge else (f"ID  ·  {tekken_id}" if tekken_id else None)
    caption_bottom_y = None
    if caption:
        caption_y = name_y + name_box_h + 6 * S
        caption_font = _fit_text_to_box(
            draw, caption,
            max_w=name_box_w, max_h=34 * S,
            max_size=28 * S, min_size=22 * S,
            font_loader=_load_font,
        )
        bbox = draw.textbbox((0, 0), caption, font=caption_font)
        ch = bbox[3] - bbox[1]
        draw.text(
            (right_x0 + PAD, caption_y - bbox[1]),
            caption, fill=TEXT_DIM, font=caption_font,
        )
        caption_bottom_y = caption_y + ch

    # Lowkey verified mark — universal among server members, so it sits as
    # a small dim "✓ verified" text in the right column rather than a
    # loud chip in the badge band. Right-aligned, body font, TEXT_DIM,
    # placed in the empty slot under the caption above the bottom accent.
    if is_verified:
        verified_label = "✓ VERIFIED"
        verified_font_size = 22 * S
        try:
            verified_font = _load_display_font(verified_font_size)
        except Exception:
            verified_font = _load_font(verified_font_size)
        bbox = draw.textbbox((0, 0), verified_label, font=verified_font)
        vw = bbox[2] - bbox[0]
        vh = bbox[3] - bbox[1]
        verified_x = W - PAD - vw
        # Anchor to PLAYER_CARD_H * S minus a small margin so it never
        # collides with the bottom accent or the badge band.
        verified_y = main_h - 14 * S - vh - bbox[1]
        # If a caption is present and would clip into this slot, shift up.
        if caption_bottom_y is not None and verified_y < caption_bottom_y + 6 * S:
            verified_y = caption_bottom_y + 6 * S - bbox[1]
        draw.text(
            (verified_x, verified_y),
            verified_label, fill=(110, 150, 120), font=verified_font,
        )

    # Bottom accent strip — mirrors the top, ties the card together.
    # When a badge band is present it sits below this line; otherwise the
    # line is the very last 2px of the card.
    main_bottom = PLAYER_CARD_H * S if badges else H
    draw.line([(0, main_bottom - 2 * S), (W, main_bottom - 2 * S)],
              fill=ACCENT, width=2 * S)

    if badges:
        # Badge chips on the trailing band, right-aligned so they sit
        # bottom-right of the card and never overlap the character chip
        # on the bottom-left of the portrait. Chip order is "highest
        # prestige first"; we lay out right-to-left from the right edge
        # so the priority chip lands closest to the right (most visible
        # after Discord scales the card down).
        band_top = PLAYER_CARD_H * S
        chip_h = 36 * S
        chip_y = band_top + (badge_band_h - chip_h) // 2
        chip_pad_x = 16 * S
        chip_gap = 10 * S
        right_edge = W - 14 * S
        # Don't place chips over the portrait area.
        left_limit = ACCENT_W + PORTRAIT_W + 14 * S
        chip_font_max = 28 * S
        chip_font_min = 20 * S

        sized: list[tuple[str, tuple[int, int, int], int]] = []
        for label, rgb in badges[:6]:
            label_str = label.upper()
            chip_font = _fit_text_to_box(
                draw, label_str,
                max_w=300 * S, max_h=chip_h - 10 * S,
                max_size=chip_font_max, min_size=chip_font_min,
            )
            bbox = draw.textbbox((0, 0), label_str, font=chip_font)
            tw = bbox[2] - bbox[0]
            chip_w = tw + 2 * chip_pad_x
            sized.append((label_str, rgb, chip_w))

        x = right_edge
        placements: list[tuple[int, str, tuple[int, int, int]]] = []
        for label_str, rgb, chip_w in reversed(sized):
            chip_left = x - chip_w
            if chip_left < left_limit:
                break  # no more room
            placements.append((chip_left, label_str, rgb))
            x = chip_left - chip_gap

        for chip_left, label_str, rgb in placements:
            chip_font = _fit_text_to_box(
                draw, label_str,
                max_w=300 * S, max_h=chip_h - 10 * S,
                max_size=chip_font_max, min_size=chip_font_min,
            )
            bbox = draw.textbbox((0, 0), label_str, font=chip_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            chip_w = tw + 2 * chip_pad_x
            draw.rectangle(
                [(chip_left, chip_y), (chip_left + chip_w, chip_y + chip_h)],
                fill=rgb,
            )
            draw.text(
                (chip_left + chip_pad_x,
                 chip_y + (chip_h - th) // 2 - bbox[1]),
                label_str, fill=TEXT, font=chip_font,
            )

    # Downsample to the published output size — this is what gives
    # the card crisp anti-aliased text. LANCZOS is the right filter
    # for downscale: it's slow vs BICUBIC but the per-glyph quality
    # difference is visible, and we're in an asyncio.to_thread.
    final_h = PLAYER_CARD_H + (badge_band_h // S)
    final = img.resize(
        (PLAYER_CARD_W, final_h), Image.LANCZOS,
    )
    return _to_png_buf(final)


# --------------------------------------------------------------------------- #
# Fit Check card                                                               #
# --------------------------------------------------------------------------- #

FITCHECK_HEADER_H = 100
FITCHECK_FOOTER_H = 64
FITCHECK_PAD = 14
# Cap source-image size so a 4K phone screenshot doesn't blow up the
# canvas to the point Discord re-encodes our card heavily; minimum keeps
# tiny crops from looking comically narrow inside the frame.
FITCHECK_MAX_DIM = 760
FITCHECK_MIN_DIM = 540


async def render_fitcheck_card(
    *,
    source_bytes: bytes,
    character: str,
    poster_name: str,
    rank_tier: str | None = None,
) -> io.BytesIO:
    """Compose a branded card around a user-submitted fit-check screenshot.

    Layout (top to bottom):
      [HEADER]  EHRGEIZ · FIT CHECK kicker, character title, char-icon
                medallion top-right.
      [IMAGE ]  Source screenshot, scaled to fit FITCHECK_MAX_DIM longest
                side. No letterbox: the canvas adapts to the source aspect
                ratio so a phone-portrait fit and a 4:3 cabinet capture
                both render unstretched.
      [FOOTER]  "BY {poster}" left, "VOTE BELOW" right; thin accent line
                separating from the image area.
    """
    src = await asyncio.to_thread(_open_image_bytes, source_bytes)
    sw, sh = src.size
    longest = max(sw, sh)
    if longest > FITCHECK_MAX_DIM:
        scale = FITCHECK_MAX_DIM / longest
    elif longest < FITCHECK_MIN_DIM:
        scale = FITCHECK_MIN_DIM / longest
    else:
        scale = 1.0
    new_w = max(1, int(sw * scale))
    new_h = max(1, int(sh * scale))

    char_url = media.character_icon_url(character)
    rank_url = media.rank_icon_url(rank_tier)
    session: aiohttp.ClientSession | None = None
    try:
        needs_net = (
            (char_url and not _cache_path_for(char_url, CHAR_CACHE_DIR).exists())
            or (rank_url and not _cache_path_for(rank_url, RANK_CACHE_DIR).exists())
        )
        if needs_net:
            session = aiohttp.ClientSession()
        char_icon = await _fetch_icon(char_url, CHAR_CACHE_DIR, session)
        rank_icon = await _fetch_icon(rank_url, RANK_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_fitcheck_card_png,
        src, new_w, new_h,
        character, poster_name, rank_tier,
        char_icon, rank_icon,
    )


def _open_image_bytes(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _compose_fitcheck_card_png(
    src: Image.Image,
    new_w: int,
    new_h: int,
    character: str,
    poster_name: str,
    rank_tier: str | None,
    char_icon: Image.Image | None,
    rank_icon: Image.Image | None,
) -> io.BytesIO:
    PAD = FITCHECK_PAD
    HEADER_H = FITCHECK_HEADER_H
    FOOTER_H = FITCHECK_FOOTER_H

    src_resized = src.resize((new_w, new_h), Image.LANCZOS)

    W = new_w + 2 * PAD
    H = HEADER_H + new_h + 2 * PAD + FOOTER_H

    canvas = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # --- Header ---------------------------------------------------------- #
    _paint_header_gradient(canvas, 0, HEADER_H)

    # Reserve room top-right for the character medallion.
    medal_size = HEADER_H - 24
    medal_x = W - PAD - medal_size
    medal_y = (HEADER_H - medal_size) // 2

    text_max_w = medal_x - PAD - 14  # 14px breathing room before the chip

    kicker = "EHRGEIZ  ·  FIT CHECK"
    kicker_font = _load_display_font(22)
    bbox = draw.textbbox((0, 0), kicker, font=kicker_font)
    draw.text(
        (PAD + 4, 12 - bbox[1]),
        kicker, fill=ACCENT, font=kicker_font,
    )

    # Title — character name, big and condensed.
    title = character.upper()
    title_font = _fit_text_to_box(
        draw, title,
        max_w=text_max_w, max_h=HEADER_H - 48,
        max_size=56, min_size=32,
    )
    bbox = draw.textbbox((0, 0), title, font=title_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (PAD + 4, HEADER_H - th - 14 - bbox[1]),
        title, fill=TEXT, font=title_font,
    )

    # Character medallion — solid red chip backdrop, icon centered.
    chip_pad = 6
    draw.rectangle(
        [(medal_x - chip_pad, medal_y - chip_pad),
         (medal_x + medal_size + chip_pad, medal_y + medal_size + chip_pad)],
        fill=ACCENT,
    )
    if char_icon is not None:
        _paste_icon(canvas, char_icon, medal_x, medal_y, medal_size, medal_size)
    else:
        # Initial fallback if the icon never downloaded — preserves the
        # chip silhouette so the layout doesn't visibly break.
        initial = (character[:1] or "?").upper()
        init_font = _load_display_font(int(medal_size * 0.7))
        bbox = draw.textbbox((0, 0), initial, font=init_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (medal_x + (medal_size - tw) // 2,
             medal_y + (medal_size - th) // 2 - bbox[1]),
            initial, fill=TEXT, font=init_font,
        )

    # Accent rule under the header.
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)

    # --- Image area ------------------------------------------------------ #
    image_y = HEADER_H + PAD
    canvas.paste(src_resized, (PAD, image_y))
    # Hairline frame so the image edge doesn't bleed into the canvas dark.
    draw.rectangle(
        [(PAD - 1, image_y - 1),
         (PAD + new_w, image_y + new_h)],
        outline=(60, 55, 58), width=1,
    )

    # --- Footer ---------------------------------------------------------- #
    footer_y = image_y + new_h + PAD
    draw.line([(0, footer_y), (W, footer_y)], fill=ACCENT, width=2)

    # Left: "BY <poster>" — body font preserves mixed-case names.
    left_label = f"BY  {poster_name.upper()}"
    left_font = _fit_text_to_box(
        draw, left_label,
        max_w=W // 2 - 2 * PAD, max_h=FOOTER_H - 24,
        max_size=24, min_size=20,
    )
    bbox = draw.textbbox((0, 0), left_label, font=left_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (PAD + 4, footer_y + (FOOTER_H - th) // 2 - bbox[1]),
        left_label, fill=TEXT, font=left_font,
    )

    # Right: rank flair if we have it (small plaque + tier text), else
    # a "RATE THIS FIT" call-to-action so the footer isn't empty.
    if rank_tier and rank_icon is not None:
        rk_size = FOOTER_H - 18
        rk_y = footer_y + (FOOTER_H - rk_size) // 2
        rk_x = W - PAD - rk_size
        _paste_icon(canvas, rank_icon, rk_x, rk_y, rk_size, rk_size)
        # Rank label to the left of the plaque.
        rk_label = rank_tier.upper()
        rk_font = _fit_text_to_box(
            draw, rk_label,
            max_w=W // 2 - 2 * PAD - rk_size - 12,
            max_h=FOOTER_H - 24,
            max_size=22, min_size=18,
        )
        bbox = draw.textbbox((0, 0), rk_label, font=rk_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (rk_x - 12 - tw,
             footer_y + (FOOTER_H - th) // 2 - bbox[1]),
            rk_label, fill=ACCENT, font=rk_font,
        )
    else:
        cta = "RATE THIS FIT"
        cta_font = _fit_text_to_box(
            draw, cta,
            max_w=W // 2 - 2 * PAD, max_h=FOOTER_H - 24,
            max_size=24, min_size=20,
        )
        bbox = draw.textbbox((0, 0), cta, font=cta_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (W - PAD - 4 - tw,
             footer_y + (FOOTER_H - th) // 2 - bbox[1]),
            cta, fill=ACCENT, font=cta_font,
        )

    return _to_png_buf(canvas)


# --------------------------------------------------------------------------- #
# Drip Lord celebration card                                                   #
# --------------------------------------------------------------------------- #


async def render_drip_lord_card(
    *,
    winner_name: str,
    character: str,
    rank_tier: str | None,
    fit_image_bytes: bytes | None,
    net_score: int,
) -> io.BytesIO:
    """Celebration banner posted when a weekly Drip Lord is crowned.

    Composes the winning fit (if available) inside a gold-trimmed crown
    frame with kicker / title / score callout. If the original fit image
    is missing (post deleted before the cron fires) the centre falls
    back to a brand panel with the character medallion enlarged.
    """
    char_url = media.character_icon_url(character)
    rank_url = media.rank_icon_url(rank_tier)

    if fit_image_bytes:
        src = await asyncio.to_thread(_open_image_bytes, fit_image_bytes)
        sw, sh = src.size
        longest = max(sw, sh)
        scale = min(680 / longest, 1.0) if longest > 0 else 1.0
        new_w = max(1, int(sw * scale))
        new_h = max(1, int(sh * scale))
    else:
        src = None
        new_w, new_h = 680, 380

    session: aiohttp.ClientSession | None = None
    try:
        needs_net = (
            (char_url and not _cache_path_for(char_url, CHAR_CACHE_DIR).exists())
            or (rank_url and not _cache_path_for(rank_url, RANK_CACHE_DIR).exists())
        )
        if needs_net:
            session = aiohttp.ClientSession()
        char_icon = await _fetch_icon(char_url, CHAR_CACHE_DIR, session)
        rank_icon = await _fetch_icon(rank_url, RANK_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_drip_lord_png,
        src, new_w, new_h,
        winner_name, character, rank_tier, net_score,
        char_icon, rank_icon,
    )


def _compose_drip_lord_png(
    src: Image.Image | None,
    new_w: int,
    new_h: int,
    winner_name: str,
    character: str,
    rank_tier: str | None,
    net_score: int,
    char_icon: Image.Image | None,
    rank_icon: Image.Image | None,
) -> io.BytesIO:
    GOLD = (212, 175, 55)
    GOLD_DIM = (110, 90, 30)
    PAD = 16
    HEADER_H = 130
    FOOTER_H = 90

    W = new_w + 2 * PAD
    H = HEADER_H + new_h + 2 * PAD + FOOTER_H

    canvas = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # Header — gold-tinted gradient (subtle), large title with crown
    # framing the centre.
    for i in range(HEADER_H):
        t = i / max(1, HEADER_H - 1)
        r = int(40 + (20 - 40) * t)
        g = int(34 + (18 - 34) * t)
        b = int(24 + (20 - 24) * t)
        canvas.paste((r, g, b, 255), (0, i, W, i + 1))

    kicker_font = _load_display_font(22)
    kicker = "DRIP LORD  ·  FIT OF THE WEEK"
    bbox = draw.textbbox((0, 0), kicker, font=kicker_font)
    draw.text(
        ((W - (bbox[2] - bbox[0])) // 2, 14 - bbox[1]),
        kicker, fill=GOLD, font=kicker_font,
    )

    # Winner title — display font, mixed-case-aware via body font for
    # names with lowercase letters (Bebas would uppercase them anyway,
    # but body font keeps the original spelling readable).
    title_font = _fit_text_to_box(
        draw, winner_name,
        max_w=W - 2 * PAD - 24, max_h=HEADER_H - 60,
        max_size=58, min_size=30,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), winner_name, font=title_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((W - tw) // 2, HEADER_H - th - 16 - bbox[1]),
        winner_name, fill=TEXT, font=title_font,
    )

    # Gold rule under header.
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=GOLD, width=3)
    draw.line([(0, HEADER_H + 4), (W, HEADER_H + 4)], fill=GOLD_DIM, width=1)

    # --- Centre: winning fit, or brand fallback -------------------------- #
    image_y = HEADER_H + PAD
    if src is not None:
        src_resized = src.resize((new_w, new_h), Image.LANCZOS)
        canvas.paste(src_resized, (PAD, image_y))
    else:
        # Fallback panel — solid gradient + giant character medallion so
        # the announcement still reads if the post got deleted before
        # the cron landed.
        draw.rectangle(
            [(PAD, image_y), (PAD + new_w, image_y + new_h)],
            fill=ROW_BG_ALT,
        )
        if char_icon is not None:
            ic_size = min(new_w, new_h) - 60
            _paste_icon(
                canvas, char_icon,
                PAD + (new_w - ic_size) // 2,
                image_y + (new_h - ic_size) // 2,
                ic_size, ic_size,
            )
    draw.rectangle(
        [(PAD - 1, image_y - 1),
         (PAD + new_w, image_y + new_h)],
        outline=GOLD, width=2,
    )

    # --- Footer: character chip + score callout -------------------------- #
    footer_y = image_y + new_h + PAD
    draw.line([(0, footer_y), (W, footer_y)], fill=GOLD, width=2)

    # Character pill — small icon + name.
    if char_icon is not None:
        ic_size = FOOTER_H - 28
        ic_y = footer_y + (FOOTER_H - ic_size) // 2
        _paste_icon(canvas, char_icon, PAD + 8, ic_y, ic_size, ic_size)
        char_label_x = PAD + 8 + ic_size + 12
    else:
        char_label_x = PAD + 8

    char_label = character.upper()
    char_font = _fit_text_to_box(
        draw, char_label,
        max_w=W // 2 - 2 * PAD, max_h=FOOTER_H - 30,
        max_size=30, min_size=22,
    )
    bbox = draw.textbbox((0, 0), char_label, font=char_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (char_label_x, footer_y + (FOOTER_H - th) // 2 - bbox[1]),
        char_label, fill=TEXT, font=char_font,
    )

    # Net-score callout (right side).
    score_label = f"+{net_score}  NET"
    score_font = _fit_text_to_box(
        draw, score_label,
        max_w=W // 2 - 2 * PAD, max_h=FOOTER_H - 30,
        max_size=42, min_size=26,
    )
    bbox = draw.textbbox((0, 0), score_label, font=score_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (W - PAD - 8 - tw,
         footer_y + (FOOTER_H - th) // 2 - bbox[1]),
        score_label, fill=GOLD, font=score_font,
    )

    return _to_png_buf(canvas)


# --------------------------------------------------------------------------- #
# Rank-up celebration card                                                     #
# --------------------------------------------------------------------------- #

import rank_meta as _rank_meta  # local-ish import; avoids cog/circular setup


async def render_rank_up_card(
    *,
    player_name: str,
    character: str | None,
    from_rank: str | None,
    to_rank: str,
) -> io.BytesIO:
    """Promotion card — fires when a player's rank role moves up.

    Layout:
      [LEFT]  Gold "PROMOTED" kicker, player name in big body font,
              "X tier · position-of-section" subtitle.
      [RIGHT] from_rank icon + arrow + to_rank icon, with rank labels
              underneath. New rank's section colour tints the right
              column so the visual feel scales with the ceiling moment.
    """
    rank_url_from = media.rank_icon_url(from_rank) if from_rank else None
    rank_url_to = media.rank_icon_url(to_rank)
    char_url = media.character_icon_url(character)

    session: aiohttp.ClientSession | None = None
    try:
        urls = [u for u in (rank_url_from, rank_url_to, char_url) if u]
        needs_net = any(
            not _cache_path_for(u, RANK_CACHE_DIR if "rank-icons" in u else CHAR_CACHE_DIR).exists()
            for u in urls
        )
        if needs_net:
            session = aiohttp.ClientSession()
        rank_icon_from = await _fetch_icon(rank_url_from, RANK_CACHE_DIR, session)
        rank_icon_to = await _fetch_icon(rank_url_to, RANK_CACHE_DIR, session)
        char_icon = await _fetch_icon(char_url, CHAR_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_rank_up_png,
        player_name, character, from_rank, to_rank,
        rank_icon_from, rank_icon_to, char_icon,
    )


def _compose_rank_up_png(
    player_name: str,
    character: str | None,
    from_rank: str | None,
    to_rank: str,
    rank_icon_from: Image.Image | None,
    rank_icon_to: Image.Image | None,
    char_icon: Image.Image | None,
) -> io.BytesIO:
    W = 720
    H = 240
    PAD = 18

    img = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    _paint_header_gradient(img, 0, H)

    # Section colour stripe down the right edge — telegraphs the
    # destination tier at a glance even before icons load.
    new_rgb = _rank_meta.rank_color_rgb(to_rank) or ACCENT
    stripe_w = 8
    draw.rectangle([(W - stripe_w, 0), (W, H)], fill=new_rgb)

    # Top accent rule.
    draw.line([(0, 0), (W, 6)], fill=ACCENT, width=6)

    # --- LEFT column: kicker + name + subtitle ---------------------------- #
    LEFT_W = 360
    kicker = "PROMOTED"
    kicker_font = _load_display_font(28)
    bbox = draw.textbbox((0, 0), kicker, font=kicker_font)
    draw.text((PAD + 4, 22 - bbox[1]), kicker, fill=ACCENT, font=kicker_font)

    # Player name — body font (preserves mixed case).
    name_y = 60
    name_font = _fit_text_to_box(
        draw, player_name,
        max_w=LEFT_W - PAD, max_h=70,
        max_size=48, min_size=22,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), player_name, font=name_font)
    draw.text(
        (PAD + 4, name_y - bbox[1]),
        player_name, fill=TEXT, font=name_font,
    )

    # Section subtitle — "Vanquisher tier · 3 of 4"
    section = _rank_meta.rank_section(to_rank) or ""
    pos = _rank_meta.rank_position_in_section(to_rank)
    subtitle_parts = []
    if section:
        subtitle_parts.append(f"{section.upper()} TIER")
    if pos:
        subtitle_parts.append(f"{pos[0]} OF {pos[1]}")
    subtitle = "  ·  ".join(subtitle_parts)
    if subtitle:
        sub_y = name_y + 70
        sub_font = _fit_text_to_box(
            draw, subtitle,
            max_w=LEFT_W - PAD, max_h=30,
            max_size=22, min_size=16,
        )
        bbox = draw.textbbox((0, 0), subtitle, font=sub_font)
        draw.text(
            (PAD + 4, sub_y - bbox[1]),
            subtitle, fill=TEXT_DIM, font=sub_font,
        )

    # Character chip in the bottom-left corner if we know the main.
    if character:
        chip_h = 36
        chip_y = H - chip_h - PAD
        chip_text = character.upper()
        chip_font = _fit_text_to_box(
            draw, chip_text,
            max_w=LEFT_W - PAD, max_h=chip_h - 12,
            max_size=22, min_size=16,
        )
        bbox = draw.textbbox((0, 0), chip_text, font=chip_font)
        tw = bbox[2] - bbox[0]
        chip_w = tw + 32
        if char_icon is not None:
            chip_w += chip_h + 4
        draw.rectangle(
            [(PAD, chip_y), (PAD + chip_w, chip_y + chip_h)],
            fill=ACCENT,
        )
        text_x = PAD + 16
        if char_icon is not None:
            _paste_icon(
                img, char_icon, PAD + 4, chip_y + 4,
                chip_h - 8, chip_h - 8,
            )
            text_x = PAD + chip_h + 8
        th = bbox[3] - bbox[1]
        draw.text(
            (text_x, chip_y + (chip_h - th) // 2 - bbox[1]),
            chip_text, fill=TEXT, font=chip_font,
        )

    # --- RIGHT column: from_rank → to_rank --------------------------- #
    right_x0 = LEFT_W
    right_w = W - right_x0 - stripe_w
    icon_size = 92
    gap = 28
    arrow_w = 36
    block_w = icon_size + gap + arrow_w + gap + icon_size
    block_x0 = right_x0 + (right_w - block_w) // 2
    icon_y = (H - icon_size) // 2 - 14

    # FROM icon (or muted placeholder if unranked previously).
    from_x = block_x0
    if rank_icon_from is not None:
        _paste_icon(img, rank_icon_from, from_x, icon_y, icon_size, icon_size)
    else:
        draw.ellipse(
            [(from_x, icon_y), (from_x + icon_size, icon_y + icon_size)],
            outline=TEXT_DIM, width=2,
        )

    # Arrow — gold chevron.
    arrow_x = from_x + icon_size + gap
    arrow_cy = icon_y + icon_size // 2
    arrow_pts = [
        (arrow_x, arrow_cy - 12),
        (arrow_x + arrow_w - 8, arrow_cy),
        (arrow_x, arrow_cy + 12),
    ]
    draw.polygon(arrow_pts, fill=(212, 175, 55))
    draw.line(
        [(arrow_x, arrow_cy), (arrow_x + arrow_w - 8, arrow_cy)],
        fill=(212, 175, 55), width=4,
    )

    # TO icon — bigger pop with a coloured ring underneath.
    to_x = arrow_x + arrow_w + gap
    ring_pad = 4
    draw.ellipse(
        [(to_x - ring_pad, icon_y - ring_pad),
         (to_x + icon_size + ring_pad, icon_y + icon_size + ring_pad)],
        fill=new_rgb,
    )
    if rank_icon_to is not None:
        _paste_icon(img, rank_icon_to, to_x, icon_y, icon_size, icon_size)

    # Rank labels under each icon.
    label_y = icon_y + icon_size + 12
    label_font = _load_display_font(22)
    if from_rank:
        from_label = from_rank.upper()
        bbox = draw.textbbox((0, 0), from_label, font=label_font)
        tw = bbox[2] - bbox[0]
        draw.text(
            (from_x + (icon_size - tw) // 2, label_y - bbox[1]),
            from_label, fill=TEXT_DIM, font=label_font,
        )
    to_label = to_rank.upper()
    to_font = _fit_text_to_box(
        draw, to_label,
        max_w=icon_size + 60, max_h=28,
        max_size=24, min_size=16,
    )
    bbox = draw.textbbox((0, 0), to_label, font=to_font)
    tw = bbox[2] - bbox[0]
    draw.text(
        (to_x + (icon_size - tw) // 2, label_y - bbox[1]),
        to_label, fill=TEXT, font=to_font,
    )

    # Bottom accent.
    draw.line([(0, H - 2), (W, H - 2)], fill=ACCENT, width=2)
    return _to_png_buf(img)


# --------------------------------------------------------------------------- #
# Fit-check leaderboard                                                        #
# --------------------------------------------------------------------------- #


async def render_fitcheck_leaderboard(
    *,
    entries: list[dict],
    window_label: str,
) -> io.BytesIO:
    """Top-N fit-check leaderboard rendered as a 2-column card grid,
    visually mirroring the tournament roster (`render_roster`) so the
    server's broadcast aesthetic stays consistent.

    Each entry dict needs:
        poster_name (str)
        character (str | None)
        ups (int)
        downs (int)
        rank_tier (str | None)
        position (int, 1-based)
        image_url (str | None)   # composite card URL from the original
                                 # post; we fetch it and crop the user's
                                 # submitted image out of our brand frame
                                 # to get a bust-style preview cell.
    """
    CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    char_urls = [media.character_icon_url(e.get("character")) for e in entries]
    rank_urls = [media.rank_icon_url(e.get("rank_tier")) for e in entries]

    # Composite-card fetches happen on the same session so we open at
    # most one. Each entry's bust crop is best-effort: a 404 (post was
    # deleted, CDN blip) silently falls back to the character icon
    # cover-crop, matching the roster behaviour.
    session: aiohttp.ClientSession | None = None
    try:
        needs_net = any(
            (u and not _cache_path_for(
                u, CHAR_CACHE_DIR if "character" in u else RANK_CACHE_DIR,
            ).exists())
            for u in char_urls + rank_urls if u
        )
        if needs_net or any(e.get("image_url") for e in entries):
            session = aiohttp.ClientSession()
        char_icons = await asyncio.gather(*(
            _fetch_icon(u, CHAR_CACHE_DIR, session) for u in char_urls
        ))
        rank_icons = await asyncio.gather(*(
            _fetch_icon(u, RANK_CACHE_DIR, session) for u in rank_urls
        ))
        bust_crops = await asyncio.gather(*(
            _fetch_fitcheck_bust(e.get("image_url"), session)
            for e in entries
        ))
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_fitcheck_leaderboard_png,
        entries, window_label, char_icons, rank_icons, bust_crops,
    )


async def _fetch_fitcheck_bust(
    composite_url: str | None,
    session: aiohttp.ClientSession | None,
) -> Image.Image | None:
    """Download a previously-posted fit-check composite card, strip the
    Ehrgeiz brand frame, and return a top-third bust crop ready to
    paste into a leaderboard portrait cell.

    The crop uses the FITCHECK_* layout constants the original render
    baked in — header (100px) + a 14px pad on every side + a 64px
    footer — so we slice out exactly the region the user's screenshot
    occupied. Callers that fall back here when the URL is missing or
    the fetch fails should swap in the character icon instead.
    """
    if not composite_url or session is None:
        return None
    try:
        async with session.get(
            composite_url,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                log.info(
                    "fitcheck bust fetch %s returned HTTP %d",
                    composite_url, resp.status,
                )
                return None
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.info("fitcheck bust fetch %s failed: %s", composite_url, e)
        return None
    try:
        composite = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as e:
        log.info("fitcheck bust open failed: %s", e)
        return None
    return _crop_fitcheck_source_region(composite)


def _crop_fitcheck_source_region(composite: Image.Image) -> Image.Image:
    """Recover the user's submitted image from one of our composite
    fit-check cards by stripping the brand frame. Falls back to the
    raw composite if it's smaller than the expected frame chrome —
    won't crash on hand-crafted test inputs."""
    W, H = composite.size
    PAD = FITCHECK_PAD
    top = FITCHECK_HEADER_H + PAD
    bottom = H - PAD - FITCHECK_FOOTER_H
    if bottom <= top + 4 or W <= 2 * PAD:
        return composite
    return composite.crop((PAD, top, W - PAD, bottom))


def _compose_fitcheck_leaderboard_png(
    entries: list[dict],
    window_label: str,
    char_icons: list[Image.Image | None],
    rank_icons: list[Image.Image | None],
    bust_crops: list[Image.Image | None],
) -> io.BytesIO:
    # Layout intentionally clones render_roster's geometry so the two
    # visuals read as siblings — same card silhouette, same gradient
    # header, same red accent strip.
    W = 880
    COLS = 2
    CARD_W = 404
    CARD_H = 180
    HEADER_H = 120
    GAP = 16
    PORTRAIT_W = 140
    SCORE_H = 110
    NAME_H = CARD_H - SCORE_H
    CELL_PAD = 10
    PORTRAIT_PAD = 2
    CHIP_H = 40

    PORTRAIT_CELL_BG = (24, 20, 22)
    RIGHT_CELL_BG = ROW_BG_ALT

    rows = max(1, (len(entries) + COLS - 1) // COLS)
    H = HEADER_H + GAP + rows * (CARD_H + GAP) + GAP
    img = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header band (kicker / title / window label).
    _paint_header_gradient(img, 0, HEADER_H)
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)
    draw.text((24, 14), "EHRGEIZ GODHAND",
              fill=TEXT, font=_load_display_font(48))
    draw.text((24, 70), f"FIT CHECK  ·  {window_label.upper()}",
              fill=ACCENT, font=_load_display_font(26))

    if not entries:
        note_font = _load_font(20)
        draw.text(
            (24, HEADER_H + GAP + 24),
            "No fit checks in this window — `/fitcheck-post` to start it.",
            fill=TEXT_DIM, font=note_font,
        )
        return _to_png_buf(img)

    medal_chars = {1: "🥇", 2: "🥈", 3: "🥉"}

    for i, entry in enumerate(entries):
        col = i % COLS
        row = i // COLS
        cx = 24 + col * (CARD_W + 24)
        cy = HEADER_H + GAP + row * (CARD_H + GAP)

        portrait_rect = (cx, cy, cx + PORTRAIT_W, cy + CARD_H)
        score_rect = (cx + PORTRAIT_W, cy,
                      cx + CARD_W, cy + SCORE_H)
        name_rect = (cx + PORTRAIT_W, cy + SCORE_H,
                     cx + CARD_W, cy + CARD_H)

        draw.rectangle(portrait_rect, fill=PORTRAIT_CELL_BG)
        draw.rectangle(score_rect, fill=RIGHT_CELL_BG)
        draw.rectangle(name_rect, fill=RIGHT_CELL_BG)
        # Position-coloured accent stripe — gold/silver/bronze for top-3,
        # brand red below — so the podium is legible at a glance.
        position = entry.get("position", i + 1)
        position_color = {
            1: (212, 175, 55),
            2: (192, 192, 192),
            3: (180, 130, 70),
        }.get(position, ACCENT)
        draw.rectangle([(cx, cy), (cx + 4, cy + CARD_H)], fill=position_color)
        draw.line([(cx + PORTRAIT_W, cy),
                   (cx + PORTRAIT_W, cy + CARD_H)],
                  fill=BG_COLOR, width=1)
        draw.line([(cx + PORTRAIT_W, cy + SCORE_H),
                   (cx + CARD_W, cy + SCORE_H)],
                  fill=BG_COLOR, width=1)

        # Portrait + character chip. Prefer the user's actual fit-check
        # bust (top portion of their submitted screenshot) when we
        # successfully fetched and cropped it; fall back to the
        # character icon cover-crop when the fetch failed.
        portrait_image_rect = (
            portrait_rect[0], portrait_rect[1],
            portrait_rect[2], portrait_rect[3] - CHIP_H,
        )
        bust = bust_crops[i] if i < len(bust_crops) else None
        if bust is not None:
            # Vertical anchor 0 = top-aligned, so the cover crop keeps
            # the head/shoulders region of the user's image visible.
            _fill_cell_with_icon_cover(
                img, bust,
                portrait_image_rect, pad=PORTRAIT_PAD,
                vertical_anchor=0.0,
            )
        else:
            _fill_cell_with_icon_cover(
                img, char_icons[i],
                portrait_image_rect, pad=PORTRAIT_PAD,
                vertical_anchor=0.18,
            )
        if entry.get("character"):
            chip_top = cy + CARD_H - CHIP_H
            draw.rectangle(
                [(cx, chip_top), (cx + PORTRAIT_W, cy + CARD_H)],
                fill=ACCENT,
            )
            label = entry["character"].upper()
            chip_font = _fit_text_to_box(
                draw, label,
                max_w=PORTRAIT_W - 12, max_h=CHIP_H - 10,
                max_size=34, min_size=20,
            )
            bbox = draw.textbbox((0, 0), label, font=chip_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text(
                (cx + (PORTRAIT_W - tw) // 2,
                 chip_top + (CHIP_H - th) // 2 - bbox[1]),
                label, fill=TEXT, font=chip_font,
            )

        # --- Score cell — medal + signed net + small ups/downs ----------- #
        ups = int(entry.get("ups", 0))
        downs = int(entry.get("downs", 0))
        net = ups - downs
        medal = medal_chars.get(position, f"#{position}")
        score_text = f"{medal}  {net:+d}"
        score_font = _fit_text_to_box(
            draw, score_text,
            max_w=score_rect[2] - score_rect[0] - 2 * CELL_PAD,
            max_h=score_rect[3] - score_rect[1] - 24,
            max_size=58, min_size=28,
            font_loader=_load_font,
        )
        bbox = draw.textbbox((0, 0), score_text, font=score_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (score_rect[0] + (score_rect[2] - score_rect[0] - tw) // 2,
             score_rect[1] + (score_rect[3] - score_rect[1] - th) // 2 - 6
             - bbox[1]),
            score_text, fill=position_color, font=score_font,
        )
        # Small ups/downs subtitle below the medal.
        sub_text = f"👍 {ups}  ·  👎 {downs}"
        sub_font = _fit_text_to_box(
            draw, sub_text,
            max_w=score_rect[2] - score_rect[0] - 2 * CELL_PAD,
            max_h=20,
            max_size=18, min_size=14,
        )
        bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
        tw = bbox[2] - bbox[0]
        draw.text(
            (score_rect[0] + (score_rect[2] - score_rect[0] - tw) // 2,
             score_rect[3] - 22 - bbox[1]),
            sub_text, fill=TEXT_DIM, font=sub_font,
        )

        # --- Name cell — body font, mixed-case preserved ----------------- #
        name = entry.get("poster_name", "—")
        name_font = _fit_text_to_box(
            draw, name,
            max_w=name_rect[2] - name_rect[0] - 2 * CELL_PAD,
            max_h=name_rect[3] - name_rect[1] - 2 * CELL_PAD,
            max_size=34, min_size=18,
            font_loader=_load_font,
        )
        bbox = draw.textbbox((0, 0), name, font=name_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = name_rect[0] + ((name_rect[2] - name_rect[0]) - tw) // 2
        ty = (name_rect[1]
              + ((name_rect[3] - name_rect[1]) - th) // 2
              - bbox[1])
        draw.text((tx, ty), name, fill=TEXT, font=name_font)

    return _to_png_buf(img)


# --------------------------------------------------------------------------- #
# Tournament champion card                                                     #
# --------------------------------------------------------------------------- #


async def render_tournament_champion_card(
    *,
    tournament_name: str,
    winner_name: str,
    winner_character: str | None,
    winner_rank: str | None,
    runner_up_name: str | None,
    entrants: int,
    rounds_played: int,
) -> io.BytesIO:
    """End-of-tournament champion banner — gold trim, big winner block,
    runner-up + entrants stats footer. Inverse layout of the Drip Lord
    card so the two read distinctly even side by side in a feed."""
    char_url = media.character_icon_url(winner_character)
    rank_url = media.rank_icon_url(winner_rank)

    session: aiohttp.ClientSession | None = None
    try:
        urls = [u for u in (char_url, rank_url) if u]
        needs_net = any(
            not _cache_path_for(u, RANK_CACHE_DIR if "rank-icons" in u else CHAR_CACHE_DIR).exists()
            for u in urls
        )
        if needs_net:
            session = aiohttp.ClientSession()
        char_icon = await _fetch_icon(char_url, CHAR_CACHE_DIR, session)
        rank_icon = await _fetch_icon(rank_url, RANK_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_tournament_champion_png,
        tournament_name, winner_name, winner_character, winner_rank,
        runner_up_name, entrants, rounds_played,
        char_icon, rank_icon,
    )


def _compose_tournament_champion_png(
    tournament_name: str,
    winner_name: str,
    winner_character: str | None,
    winner_rank: str | None,
    runner_up_name: str | None,
    entrants: int,
    rounds_played: int,
    char_icon: Image.Image | None,
    rank_icon: Image.Image | None,
) -> io.BytesIO:
    GOLD = (212, 175, 55)
    GOLD_DIM = (110, 90, 30)
    W = 760
    HEADER_H = 110
    PORTRAIT_BAND_H = 280
    FOOTER_H = 92
    PAD = 18
    H = HEADER_H + PORTRAIT_BAND_H + FOOTER_H

    canvas = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # Subtle gold-tinted top gradient.
    for i in range(HEADER_H):
        t = i / max(1, HEADER_H - 1)
        r = int(50 + (20 - 50) * t)
        g = int(40 + (18 - 40) * t)
        b = int(28 + (20 - 28) * t)
        canvas.paste((r, g, b, 255), (0, i, W, i + 1))

    # Kicker — "TOURNAMENT CHAMPION".
    kicker = "TOURNAMENT  ·  CHAMPION"
    kicker_font = _load_display_font(26)
    bbox = draw.textbbox((0, 0), kicker, font=kicker_font)
    draw.text(
        ((W - (bbox[2] - bbox[0])) // 2, 18 - bbox[1]),
        kicker, fill=GOLD, font=kicker_font,
    )

    # Tournament name title.
    title = tournament_name.upper()
    title_font = _fit_text_to_box(
        draw, title,
        max_w=W - 2 * PAD - 24, max_h=HEADER_H - 60,
        max_size=46, min_size=24,
    )
    bbox = draw.textbbox((0, 0), title, font=title_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((W - tw) // 2, HEADER_H - th - 14 - bbox[1]),
        title, fill=TEXT, font=title_font,
    )

    # Gold rule under header.
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=GOLD, width=3)
    draw.line([(0, HEADER_H + 4), (W, HEADER_H + 4)], fill=GOLD_DIM, width=1)

    # --- Centre band: portrait (left) + winner stack (right) -------------- #
    band_y = HEADER_H + PAD
    band_h = PORTRAIT_BAND_H - 2 * PAD
    portrait_w = 220
    portrait_rect = (PAD, band_y, PAD + portrait_w, band_y + band_h)

    # Portrait cell — solid backdrop + character cover-crop, gold ring.
    draw.rectangle(portrait_rect, fill=ROW_BG_ALT)
    if char_icon is not None:
        _fill_cell_with_icon_cover(
            canvas, char_icon, portrait_rect, pad=2, vertical_anchor=0.18,
        )
    draw.rectangle(
        [(portrait_rect[0] - 2, portrait_rect[1] - 2),
         (portrait_rect[2] + 1, portrait_rect[3] + 1)],
        outline=GOLD, width=2,
    )

    # Right column — winner name + character + rank.
    right_x0 = portrait_rect[2] + 24
    right_w = W - right_x0 - PAD

    # Trophy mark + WINNER kicker
    win_kicker = "🏆 CHAMPION"  # emoji may render as box on some Pillow setups; harmless
    win_kicker_font = _load_display_font(22)
    draw.text(
        (right_x0, band_y + 6),
        "CHAMPION", fill=GOLD, font=win_kicker_font,
    )

    # Player name (mixed case body).
    name_y = band_y + 44
    name_font = _fit_text_to_box(
        draw, winner_name,
        max_w=right_w, max_h=70,
        max_size=58, min_size=28,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), winner_name, font=name_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (right_x0, name_y - bbox[1]),
        winner_name, fill=TEXT, font=name_font,
    )

    # Character + rank line.
    sub_parts: list[str] = []
    if winner_character:
        sub_parts.append(winner_character.upper())
    if winner_rank:
        sub_parts.append(winner_rank.upper())
    if sub_parts:
        sub = "  ·  ".join(sub_parts)
        sub_y = name_y + 80
        sub_font = _fit_text_to_box(
            draw, sub,
            max_w=right_w, max_h=34,
            max_size=24, min_size=18,
        )
        bbox = draw.textbbox((0, 0), sub, font=sub_font)
        # Tint colour of the sub by the rank when known.
        sub_color = (
            _rank_meta.rank_color_rgb(winner_rank) if winner_rank else TEXT_DIM
        ) or TEXT_DIM
        draw.text(
            (right_x0, sub_y - bbox[1]),
            sub, fill=sub_color, font=sub_font,
        )

    # Rank icon medallion bottom-right of the band.
    if rank_icon is not None:
        rk_size = 80
        rk_x = W - PAD - rk_size
        rk_y = band_y + band_h - rk_size
        _paste_icon(canvas, rank_icon, rk_x, rk_y, rk_size, rk_size)

    # --- Footer: runner-up + entrants/rounds counters --------------------- #
    footer_y = HEADER_H + PORTRAIT_BAND_H
    draw.line([(0, footer_y), (W, footer_y)], fill=GOLD, width=2)

    runner_label = (
        f"RUNNER-UP  ·  {runner_up_name.upper()}" if runner_up_name
        else "RUNNER-UP  ·  —"
    )
    runner_font = _fit_text_to_box(
        draw, runner_label,
        max_w=(W // 2) - PAD, max_h=FOOTER_H - 30,
        max_size=22, min_size=16,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), runner_label, font=runner_font)
    th = bbox[3] - bbox[1]
    draw.text(
        (PAD + 4, footer_y + (FOOTER_H - th) // 2 - bbox[1]),
        runner_label, fill=TEXT, font=runner_font,
    )

    stats_label = f"{entrants} ENTRANTS  ·  {rounds_played}R SWISS"
    stats_font = _fit_text_to_box(
        draw, stats_label,
        max_w=(W // 2) - PAD, max_h=FOOTER_H - 30,
        max_size=22, min_size=16,
    )
    bbox = draw.textbbox((0, 0), stats_label, font=stats_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (W - PAD - 4 - tw,
         footer_y + (FOOTER_H - th) // 2 - bbox[1]),
        stats_label, fill=GOLD, font=stats_font,
    )

    return _to_png_buf(canvas)


# --------------------------------------------------------------------------- #
# Weekly recap composite                                                       #
# --------------------------------------------------------------------------- #


async def render_weekly_recap_card(
    *,
    week_label: str,             # "2026-04-21 → 2026-04-27"
    drip_lord_name: str | None,
    drip_lord_character: str | None,
    top_fit_poster: str | None,
    top_fit_character: str | None,
    top_fit_net: int | None,
    new_members: int,
    fitchecks_posted: int,
    tournaments_completed: int,
) -> io.BytesIO:
    """One-image weekly digest. Hits the highlights without making the
    user click through five different commands to see what happened in
    the server this week."""
    char_url_top = media.character_icon_url(top_fit_character)
    char_url_drip = media.character_icon_url(drip_lord_character)

    session: aiohttp.ClientSession | None = None
    try:
        urls = [u for u in (char_url_top, char_url_drip) if u]
        needs_net = any(
            not _cache_path_for(u, CHAR_CACHE_DIR).exists() for u in urls
        )
        if needs_net:
            session = aiohttp.ClientSession()
        char_icon_top = await _fetch_icon(char_url_top, CHAR_CACHE_DIR, session)
        char_icon_drip = await _fetch_icon(char_url_drip, CHAR_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_weekly_recap_png,
        week_label,
        drip_lord_name, drip_lord_character, char_icon_drip,
        top_fit_poster, top_fit_character, top_fit_net, char_icon_top,
        new_members, fitchecks_posted, tournaments_completed,
    )


def _draw_recap_tile(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    rect: tuple[int, int, int, int],
    kicker: str,
    kicker_color: tuple[int, int, int],
    headline: str,
    subline: str | None,
    icon: Image.Image | None = None,
) -> None:
    x0, y0, x1, y1 = rect
    PAD = 18

    # Tile background — slightly lifted from the canvas so the grid reads.
    draw.rectangle([(x0, y0), (x1, y1)], fill=ROW_BG_ALT)
    draw.rectangle([(x0, y0), (x0 + 4, y1)], fill=kicker_color)

    # Kicker line.
    kicker_font = _load_display_font(22)
    bbox = draw.textbbox((0, 0), kicker, font=kicker_font)
    draw.text(
        (x0 + PAD, y0 + 14 - bbox[1]),
        kicker, fill=kicker_color, font=kicker_font,
    )

    # Optional icon medallion in the top-right of the tile.
    icon_size = 60
    text_right_limit = x1 - PAD
    if icon is not None:
        ic_x = x1 - PAD - icon_size
        ic_y = y0 + 12
        draw.rectangle(
            [(ic_x - 4, ic_y - 4), (ic_x + icon_size + 4, ic_y + icon_size + 4)],
            fill=ACCENT,
        )
        _paste_icon(img, icon, ic_x, ic_y, icon_size, icon_size)
        text_right_limit = ic_x - 12

    # Headline — body font for mixed-case readability.
    headline_y = y0 + 56
    headline_font = _fit_text_to_box(
        draw, headline,
        max_w=text_right_limit - x0 - PAD, max_h=36,
        max_size=30, min_size=20,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), headline, font=headline_font)
    draw.text(
        (x0 + PAD, headline_y - bbox[1]),
        headline, fill=TEXT, font=headline_font,
    )

    if subline:
        sub_y = headline_y + 38
        sub_font = _fit_text_to_box(
            draw, subline,
            max_w=text_right_limit - x0 - PAD, max_h=28,
            max_size=22, min_size=16,
        )
        bbox = draw.textbbox((0, 0), subline, font=sub_font)
        draw.text(
            (x0 + PAD, sub_y - bbox[1]),
            subline, fill=TEXT_DIM, font=sub_font,
        )


def _compose_weekly_recap_png(
    week_label: str,
    drip_lord_name: str | None, drip_lord_character: str | None,
    char_icon_drip: Image.Image | None,
    top_fit_poster: str | None, top_fit_character: str | None,
    top_fit_net: int | None, char_icon_top: Image.Image | None,
    new_members: int, fitchecks_posted: int, tournaments_completed: int,
) -> io.BytesIO:
    GOLD = (212, 175, 55)
    W = 800
    HEADER_H = 130
    TILE_H = 150
    GAP = 14
    PAD = 16
    H = HEADER_H + 2 * TILE_H + GAP + 2 * PAD

    canvas = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    _paint_header_gradient(canvas, 0, HEADER_H)

    # Header — kicker / title / week label.
    kicker_font = _load_display_font(22)
    draw.text(
        (PAD + 4, 14),
        "EHRGEIZ  ·  WEEK IN REVIEW",
        fill=ACCENT, font=kicker_font,
    )
    title_font = _load_display_font(46)
    draw.text(
        (PAD + 4, 44),
        "WEEKLY RECAP",
        fill=TEXT, font=title_font,
    )
    week_font = _load_font(20)
    draw.text(
        (PAD + 4, 96),
        week_label,
        fill=TEXT_DIM, font=week_font,
    )
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)

    # 2x2 tile grid.
    tile_w = (W - 2 * PAD - GAP) // 2
    row1_y = HEADER_H + PAD
    row2_y = row1_y + TILE_H + GAP

    _draw_recap_tile(
        canvas, draw,
        rect=(PAD, row1_y, PAD + tile_w, row1_y + TILE_H),
        kicker="👑  DRIP LORD",
        kicker_color=GOLD,
        headline=drip_lord_name or "— No crown this week",
        subline=(
            f"{drip_lord_character.upper()}" if drip_lord_character
            else "Take a fit-check screenshot, post it, get votes — go for the crown."
        ),
        icon=char_icon_drip,
    )
    _draw_recap_tile(
        canvas, draw,
        rect=(PAD + tile_w + GAP, row1_y,
              W - PAD, row1_y + TILE_H),
        kicker="📸  TOP FIT",
        kicker_color=ACCENT,
        headline=top_fit_poster or "— No fits this week",
        subline=(
            f"{top_fit_character.upper()}  ·  net {top_fit_net:+d}"
            if top_fit_poster and top_fit_character is not None and top_fit_net is not None
            else "Be first — `/fitcheck-post` to start the week."
        ),
        icon=char_icon_top,
    )
    _draw_recap_tile(
        canvas, draw,
        rect=(PAD, row2_y, PAD + tile_w, row2_y + TILE_H),
        kicker="🆕  NEW MEMBERS",
        kicker_color=(95, 180, 120),
        headline=str(new_members),
        subline=(
            "Verified this week."
            if new_members else "No new joiners this week."
        ),
    )
    _draw_recap_tile(
        canvas, draw,
        rect=(PAD + tile_w + GAP, row2_y,
              W - PAD, row2_y + TILE_H),
        kicker="🏆  TOURNAMENTS",
        kicker_color=GOLD,
        headline=f"{tournaments_completed} completed",
        subline=f"{fitchecks_posted} fit checks posted",
    )

    draw.line([(0, H - 2), (W, H - 2)], fill=ACCENT, width=2)
    return _to_png_buf(canvas)


# --------------------------------------------------------------------------- #
# What's That Move — frame quiz card                                           #
# --------------------------------------------------------------------------- #


async def render_whats_that_move_card(
    *,
    character: str,
    notation: str,
    move_name: str,
    revealed_frames: int | None = None,
) -> io.BytesIO:
    """Pokemon-style "guess the answer" card for a frame-data quiz.

    `revealed_frames=None` renders the question state (obscured answer
    placeholder); a non-None value renders the post-guess state with
    the correct frames revealed in big colour-graded text (green for
    plus / mid-range, amber for slight minus, red for launch-punishable).
    Same layout in both states so the click swap reads as a card flip
    rather than a full re-render.
    """
    char_url = media.character_icon_url(character)
    session: aiohttp.ClientSession | None = None
    char_icon: Image.Image | None = None
    try:
        if char_url and not _cache_path_for(char_url, CHAR_CACHE_DIR).exists():
            session = aiohttp.ClientSession()
        char_icon = await _fetch_icon(char_url, CHAR_CACHE_DIR, session)
    finally:
        if session is not None:
            await session.close()

    return await asyncio.to_thread(
        _compose_whats_that_move_png,
        character, notation, move_name, revealed_frames, char_icon,
    )


def _compose_whats_that_move_png(
    character: str,
    notation: str,
    move_name: str,
    revealed_frames: int | None,
    char_icon: Image.Image | None,
) -> io.BytesIO:
    W = 760
    HEADER_H = 90
    BODY_H = 320
    H = HEADER_H + BODY_H
    PAD = 18

    canvas = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # --- Header ---------------------------------------------------------- #
    _paint_header_gradient(canvas, 0, HEADER_H)
    kicker = "EHRGEIZ  ·  WHAT'S THAT MOVE?"
    kicker_font = _load_display_font(28)
    bbox = draw.textbbox((0, 0), kicker, font=kicker_font)
    draw.text(
        ((W - (bbox[2] - bbox[0])) // 2, 18 - bbox[1]),
        kicker, fill=ACCENT, font=kicker_font,
    )
    # Reveal-mode subtitle so the player knows the answer is in.
    if revealed_frames is not None:
        sub = "ANSWER REVEALED"
        sub_color = ACCENT
    else:
        sub = "GUESS · FRAMES ON BLOCK"
        sub_color = TEXT_DIM
    sub_font = _load_display_font(18)
    bbox = draw.textbbox((0, 0), sub, font=sub_font)
    draw.text(
        ((W - (bbox[2] - bbox[0])) // 2, 54 - bbox[1]),
        sub, fill=sub_color, font=sub_font,
    )
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)

    # --- Body — split portrait left / text right ------------------------- #
    body_y = HEADER_H + PAD
    portrait_w = 240
    portrait_h = BODY_H - 2 * PAD
    portrait_rect = (PAD, body_y, PAD + portrait_w, body_y + portrait_h)

    draw.rectangle(portrait_rect, fill=(24, 20, 22))
    if char_icon is not None:
        _fill_cell_with_icon_cover(
            canvas, char_icon, portrait_rect, pad=4, vertical_anchor=0.18,
        )
    # Character chip on the portrait's bottom edge.
    chip_h = 44
    chip_top = body_y + portrait_h - chip_h
    draw.rectangle(
        [(portrait_rect[0], chip_top),
         (portrait_rect[2], body_y + portrait_h)],
        fill=ACCENT,
    )
    char_label = character.upper()
    chip_font = _fit_text_to_box(
        draw, char_label,
        max_w=portrait_w - 16, max_h=chip_h - 12,
        max_size=32, min_size=20,
    )
    bbox = draw.textbbox((0, 0), char_label, font=chip_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (portrait_rect[0] + (portrait_w - tw) // 2,
         chip_top + (chip_h - th) // 2 - bbox[1]),
        char_label, fill=TEXT, font=chip_font,
    )

    # Right column — notation + move name + frames placeholder/answer.
    right_x0 = portrait_rect[2] + PAD
    right_w = W - right_x0 - PAD

    # Notation, big.
    notation_font = _fit_text_to_box(
        draw, notation,
        max_w=right_w, max_h=64,
        max_size=44, min_size=22,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), notation, font=notation_font)
    draw.text(
        (right_x0, body_y + 4 - bbox[1]),
        notation, fill=TEXT, font=notation_font,
    )

    # Move name, smaller body font.
    name_y = body_y + 80
    name_font = _fit_text_to_box(
        draw, move_name,
        max_w=right_w, max_h=44,
        max_size=28, min_size=18,
        font_loader=_load_font,
    )
    bbox = draw.textbbox((0, 0), move_name, font=name_font)
    draw.text(
        (right_x0, name_y - bbox[1]),
        move_name, fill=TEXT_DIM, font=name_font,
    )

    # Question kicker.
    q_label = "FRAMES ON BLOCK"
    q_y = name_y + 70
    q_font = _load_display_font(24)
    bbox = draw.textbbox((0, 0), q_label, font=q_font)
    draw.text(
        (right_x0, q_y - bbox[1]),
        q_label, fill=ACCENT, font=q_font,
    )

    # Big answer slot — placeholder or revealed.
    slot_y = q_y + 36
    if revealed_frames is None:
        # Three obscured boxes that visually beg for an answer.
        slot_w = 72
        slot_h = 72
        slot_gap = 14
        slot_x = right_x0
        for _ in range(3):
            draw.rectangle(
                [(slot_x, slot_y),
                 (slot_x + slot_w, slot_y + slot_h)],
                fill=ROW_BG_ALT, outline=TEXT_DIM, width=2,
            )
            slot_x += slot_w + slot_gap
    else:
        # Colour the answer by safety bracket so the visual reads at a
        # glance: green for safe, amber for situational, red for launch
        # punishable. Boundaries are the rough community-canon thresholds.
        if revealed_frames >= -9:
            color = (95, 180, 120)        # safe-ish
        elif revealed_frames >= -13:
            color = (245, 180, 95)        # punishable but not launch
        else:
            color = (220, 60, 60)         # launch territory
        answer = f"{revealed_frames:+d}"
        answer_font = _load_display_font(96)
        bbox = draw.textbbox((0, 0), answer, font=answer_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (right_x0, slot_y - bbox[1]),
            answer, fill=color, font=answer_font,
        )
        # Small "frames on block" label after the number.
        unit_x = right_x0 + tw + 14
        unit_label = "ON BLOCK"
        unit_font = _load_display_font(22)
        bbox_u = draw.textbbox((0, 0), unit_label, font=unit_font)
        draw.text(
            (unit_x, slot_y + th - 32 - bbox_u[1]),
            unit_label, fill=TEXT_DIM, font=unit_font,
        )

    # Bottom accent.
    draw.line([(0, H - 2), (W, H - 2)], fill=ACCENT, width=2)
    return _to_png_buf(canvas)


# --------------------------------------------------------------------------- #
# Channel banner render                                                        #
# --------------------------------------------------------------------------- #

BANNER_W = 960
BANNER_H = 240

IDENT_BANNER_W = 720
IDENT_BANNER_H = 120

# Named accents for ident banners. Callers pick a tone via these
# constants instead of repeating raw RGB tuples at every Hub callsite.
ACCENT_GOLD = (212, 175, 55)
ACCENT_NEUTRAL = (120, 120, 130)
ACCENT_DESTRUCTIVE = (180, 50, 60)


async def render_ident_banner(
    *, kicker: str, title: str, accent: tuple[int, int, int] | None = None,
) -> io.BytesIO:
    """Skinny brand strip used on ephemeral Player Hub responses.

    Shares visual DNA with `render_banner` (logo left, accent bars, gradient
    wash) but at ~half the height — meant to sit at the top of an embed
    as a brand identifier rather than carrying body copy. `accent` lets
    callers tint the strip per action (red for destructive, gold for
    rank changes, etc.); defaults to the brand red ACCENT.
    """
    accent_color = accent or ACCENT
    img = Image.new("RGBA", (IDENT_BANNER_W, IDENT_BANNER_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    _paint_banner_gradient(img, y_start=0, y_end=IDENT_BANNER_H)

    draw.rectangle([(0, 0), (IDENT_BANNER_W, 3)], fill=accent_color)
    draw.rectangle(
        [(0, IDENT_BANNER_H - 3), (IDENT_BANNER_W, IDENT_BANNER_H)],
        fill=accent_color,
    )

    logo_x = 24
    logo_w = 92
    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            src_w, src_h = logo.size
            scale = min(logo_w / src_w, (IDENT_BANNER_H - 24) / src_h)
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))
            resized = logo.resize((new_w, new_h), Image.LANCZOS)
            img.alpha_composite(
                resized,
                (logo_x + (logo_w - new_w) // 2,
                 (IDENT_BANNER_H - new_h) // 2),
            )
        except (OSError, IOError) as e:
            log.warning("ident banner logo load failed: %s", e)

    text_x = logo_x + logo_w + 20
    text_max_w = IDENT_BANNER_W - text_x - 20

    kicker_h = 18
    title_h = 48
    gap = 6
    block_h = kicker_h + gap + title_h
    block_y = (IDENT_BANNER_H - block_h) // 2

    y = block_y
    kf = _fit_text_to_box(
        draw, kicker.upper(),
        max_w=text_max_w, max_h=kicker_h,
        max_size=16, min_size=11,
    )
    draw.text((text_x, y), kicker.upper(), fill=accent_color, font=kf)
    y += kicker_h + gap

    title_font = _fit_text_to_box(
        draw, title.upper(),
        max_w=text_max_w, max_h=title_h,
        max_size=44, min_size=18,
    )
    draw.text((text_x, y), title.upper(), fill=TEXT, font=title_font)

    return _to_png_buf(img)


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
