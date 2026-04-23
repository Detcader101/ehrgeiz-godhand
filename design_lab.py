"""Visual design playground for tournament graphics.

Generates several style variants of the same sample roster so we can
compare layouts side-by-side and converge on a look before committing
it into `tournament_render.py`.

Run from the project root:
    python design_lab.py

Outputs land in `design_lab_out/` (gitignored — scratch iteration files).

Variants:
    v1_current      — the currently-shipped roster render (baseline)
    v2_card_grid    — 2-column cards, char portrait dominant
    v3_leaderboard  — seeded vertical list, big seed numbers
    v4_splash_bar   — TV lower-third style, wide portrait per row

Add a new variant by writing an `async def render_variant_foo()` that
returns a PIL Image, then appending it to VARIANTS below.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image, ImageDraw

import tournament_render as tr

OUT_DIR = Path(__file__).parent / "design_lab_out"
OUT_DIR.mkdir(exist_ok=True)

# Sample data big enough to stress layout (mixed name lengths, mixed rank
# tiers from low to high so we see plenty of icon variety, mixed characters).
SAMPLE_ROSTER: list[dict] = [
    {"display_name": "Detcader_",         "rank_tier": "Tekken Emperor", "main_char": "Clive"},
    {"display_name": "NinaQueen",         "rank_tier": "Tekken King",    "main_char": "Nina"},
    {"display_name": "Shiro",             "rank_tier": "Bushin",         "main_char": "Reina"},
    {"display_name": "Randommer",         "rank_tier": "Garyu",          "main_char": "Lee"},
    {"display_name": "KazuyaMain_23",     "rank_tier": "Fujin",          "main_char": "Kazuya"},
    {"display_name": "Bakaloo",           "rank_tier": "Battle Ruler",   "main_char": "Yoshimitsu"},
    {"display_name": "Dragunov_X",        "rank_tier": "Vanquisher",     "main_char": "Dragunov"},
    {"display_name": "Hazzy1491",         "rank_tier": "Beginner",       "main_char": "Jin"},
]

# Palette shortcuts from the shared renderer so variants stay in family.
BG = tr.BG_COLOR
HEADER_BG = tr.HEADER_BG
ROW_ALT = tr.ROW_BG_ALT
ACCENT = tr.ACCENT
TEXT = tr.TEXT
TEXT_DIM = tr.TEXT_DIM


# --------------------------------------------------------------------------- #
# v1 — current baseline (call the shipped renderer)                            #
# --------------------------------------------------------------------------- #

async def render_variant_current() -> bytes:
    buf = await tr.render_roster(SAMPLE_ROSTER)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# v2 — card grid                                                               #
# Each entrant gets their own square-ish card: big character portrait on the  #
# left, rank plaque + name + rank tier on the right. Two cards per row.       #
# --------------------------------------------------------------------------- #

async def render_variant_card_grid() -> bytes:
    """Card grid, iteration 3.

    Each card is a strict 2x2-ish rectangular grid:
      ┌──────────┬────────────┐
      │          │ RANK       │
      │ PORTRAIT ├────────────┤
      │          │ NAME       │
      └──────────┴────────────┘
    Every cell is a fixed rectangle — Minecraft-map style — and each
    element (portrait, rank plaque, name text) scales to fill its cell
    while preserving aspect. The card silhouette is identical for every
    entrant; only the typography inside the NAME cell flexes to match
    the handle's length.
    """
    W = 880
    COLS = 2
    CARD_W = 404
    CARD_H = 180
    HEADER_H = 120
    GAP = 16

    # Internal grid zones (relative to card origin).
    PORTRAIT_W = 140
    RANK_H = 110         # rank cell dominates the right column
    NAME_H = CARD_H - RANK_H
    CELL_PAD = 10
    PORTRAIT_PAD = 2     # tighter pad on the portrait so the art fills its cell
    CHIP_H = 40          # character-chip strip, taller

    rank_lookup, char_lookup = await tr._prefetch_icons_for_players(SAMPLE_ROSTER)

    rows = (len(SAMPLE_ROSTER) + COLS - 1) // COLS
    H = HEADER_H + GAP + rows * (CARD_H + GAP) + GAP
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Header.
    tr._paint_header_gradient(img, 0, HEADER_H)
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)
    draw.text((24, 14), "EHRGEIZ GODHAND",
              fill=TEXT, font=tr._load_display_font(52))
    draw.text((24, 74), f"ENTRANTS  ·  {len(SAMPLE_ROSTER)}",
              fill=ACCENT, font=tr._load_display_font(26))

    # Cell backgrounds — the portrait cell stays slightly darker than
    # the rank + name cells so the grid-tile feel reads without hard
    # borders. Subtle 1-px rule lines between cells close the look.
    CELL_BG_A = (24, 20, 22)   # portrait
    CELL_BG_B = ROW_ALT         # rank + name

    for i, p in enumerate(SAMPLE_ROSTER):
        col = i % COLS
        row = i // COLS
        cx = 24 + col * (CARD_W + 24)
        cy = HEADER_H + GAP + row * (CARD_H + GAP)

        # --- Cell rectangles (axis-aligned, no rotation) --------------- #
        portrait_rect = (cx, cy, cx + PORTRAIT_W, cy + CARD_H)
        rank_rect     = (cx + PORTRAIT_W, cy,
                         cx + CARD_W, cy + RANK_H)
        name_rect     = (cx + PORTRAIT_W, cy + RANK_H,
                         cx + CARD_W, cy + CARD_H)

        draw.rectangle(portrait_rect, fill=CELL_BG_A)
        draw.rectangle(rank_rect, fill=CELL_BG_B)
        draw.rectangle(name_rect, fill=CELL_BG_B)

        # Left accent bar running the full card height.
        draw.rectangle([(cx, cy), (cx + 4, cy + CARD_H)], fill=ACCENT)
        # 1-px grid lines between cells.
        draw.line([(cx + PORTRAIT_W, cy),
                   (cx + PORTRAIT_W, cy + CARD_H)],
                  fill=BG, width=1)
        draw.line([(cx + PORTRAIT_W, cy + RANK_H),
                   (cx + CARD_W, cy + RANK_H)],
                  fill=BG, width=1)

        # --- Portrait cell -------------------------------------------- #
        # The portrait area reserved for the character image STOPS at
        # the top of the chip strip so the portrait doesn't get clipped
        # by it. Makes the image fill more of the cell without poking
        # under the label.
        char_img = char_lookup.get(p["main_char"]) if p.get("main_char") else None
        portrait_image_rect = (
            portrait_rect[0], portrait_rect[1],
            portrait_rect[2], portrait_rect[3] - CHIP_H,
        )
        # Scale-and-crop so the portrait fills the cell end-to-end
        # instead of letterboxing. Anchor ~0.18 trims a sliver off the
        # top of the head so shoulders + upper torso show — bust shot
        # framing rather than a full-body shrink.
        _fill_cell_with_icon_cover(
            img, char_img,
            portrait_image_rect, pad=PORTRAIT_PAD,
            vertical_anchor=0.18,
        )

        # Character chip — solid red strip at the portrait cell's bottom.
        # Drawing directly via ImageDraw.rectangle (instead of alpha-
        # compositing a translucent layer) guarantees a perfectly flat,
        # crisp bottom edge regardless of what's underneath.
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

        # --- Rank plaque cell ----------------------------------------- #
        rank_img = rank_lookup.get(p["rank_tier"]) if p.get("rank_tier") else None
        _fill_cell_with_icon(
            img, rank_img,
            rank_rect, pad=CELL_PAD,
        )

        # --- Name cell ------------------------------------------------ #
        # Player handles render in the BODY font (bold sans) so mixed
        # case is preserved — Bebas Neue is a caps-only display face
        # and would map "Detcader_" to "DETCADER_". Smaller max size +
        # narrower name cell keeps the rank plaque visually dominant.
        name_label = p["display_name"]
        name_font = _fit_text_to_box(
            draw, name_label,
            max_w=name_rect[2] - name_rect[0] - 2 * CELL_PAD,
            max_h=name_rect[3] - name_rect[1] - 2 * CELL_PAD,
            max_size=34, min_size=12,
            font_loader=tr._load_font,
        )
        # Center the name inside its cell.
        text_bbox = draw.textbbox((0, 0), name_label, font=name_font)
        tw = text_bbox[2] - text_bbox[0]
        # textbbox returns the glyph ink box including the top offset;
        # use the height from the ink box for correct vertical centering.
        th = text_bbox[3] - text_bbox[1]
        tx = name_rect[0] + ((name_rect[2] - name_rect[0]) - tw) // 2
        ty = (name_rect[1]
              + ((name_rect[3] - name_rect[1]) - th) // 2
              - text_bbox[1])
        draw.text((tx, ty), name_label, fill=TEXT, font=name_font)

    return tr._to_png_buf(img).getvalue()


def _fill_cell_with_icon(
    canvas: Image.Image, icon: Image.Image | None,
    rect: tuple[int, int, int, int], pad: int,
) -> None:
    """Resize an icon (aspect-preserved) to fill a grid cell with the
    given internal padding, then paste centered. Keeps the grid-tile
    discipline without cropping source art."""
    if icon is None:
        return
    x0, y0, x1, y1 = rect
    box_w = max(1, (x1 - x0) - 2 * pad)
    box_h = max(1, (y1 - y0) - 2 * pad)
    tr._paste_icon(canvas, icon, x0 + pad, y0 + pad, box_w, box_h)


def _fill_cell_with_icon_cover(
    canvas: Image.Image, icon: Image.Image | None,
    rect: tuple[int, int, int, int], pad: int,
    vertical_anchor: float = 0.0,
) -> None:
    """Scale-and-crop an icon to completely fill a grid cell (CSS
    'object-fit: cover' behaviour). Excess width is cropped evenly from
    both sides; vertical overflow is cropped according to the anchor:
        0.0 = top-anchored   (keep head, clip feet)
        0.5 = center         (framed mid-body)
        1.0 = bottom-anchored (clip head, keep feet)
    For Tekken portraits an anchor of ~0.18 trims a sliver off the top
    of the head so the face + shoulders dominate the frame — the 'bust
    shot' framing that FGC overlays typically favour.
    """
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


# --------------------------------------------------------------------------- #
# v5 — 3-column card                                                           #
# Alternate card layout: portrait LEFT, name+rank tier MIDDLE, rank plaque    #
# RIGHT as a badge. More symmetrical than v2, reads a bit like an esports     #
# player card.                                                                 #
# --------------------------------------------------------------------------- #

async def render_variant_card_3col() -> bytes:
    W = 880
    COLS = 2
    CARD_W = (W - 24 * 3) // COLS
    CARD_H = 170
    CHAR_BOX = (100, 140)
    RANK_BOX = (150, 75)
    HEADER_H = 120
    GAP = 16

    rank_lookup, char_lookup = await tr._prefetch_icons_for_players(SAMPLE_ROSTER)

    rows = (len(SAMPLE_ROSTER) + COLS - 1) // COLS
    H = HEADER_H + GAP + rows * (CARD_H + GAP) + GAP
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Header.
    tr._paint_header_gradient(img, 0, HEADER_H)
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)
    draw.text((24, 14), "EHRGEIZ GODHAND",
              fill=TEXT, font=tr._load_display_font(52))
    draw.text((24, 74), f"ENTRANTS  ·  {len(SAMPLE_ROSTER)}",
              fill=ACCENT, font=tr._load_display_font(26))

    name_font = tr._load_display_font(26)
    rank_font = tr._load_font(14)
    char_name_font = tr._load_display_font(16)

    for i, p in enumerate(SAMPLE_ROSTER):
        col = i % COLS
        row = i // COLS
        x = 24 + col * (CARD_W + 24)
        y = HEADER_H + GAP + row * (CARD_H + GAP)

        # Card body + left accent bar.
        draw.rectangle([(x, y), (x + CARD_W, y + CARD_H)], fill=ROW_ALT)
        draw.rectangle([(x, y), (x + 4, y + CARD_H)], fill=ACCENT)

        # Left — portrait + char chip.
        char_img = char_lookup.get(p["main_char"]) if p.get("main_char") else None
        char_x = x + 14
        char_y = y + (CARD_H - CHAR_BOX[1]) // 2
        tr._paste_icon(img, char_img, char_x, char_y, CHAR_BOX[0], CHAR_BOX[1])
        if p.get("main_char"):
            label = p["main_char"].upper()
            strip_y = char_y + CHAR_BOX[1] - 20
            strip = Image.new(
                "RGBA", (CHAR_BOX[0], 20), (ACCENT[0], ACCENT[1], ACCENT[2], 210),
            )
            img.alpha_composite(strip, (char_x, strip_y))
            w = tr._text_width(draw, label, char_name_font)
            draw.text(
                (char_x + (CHAR_BOX[0] - w) // 2, strip_y),
                label, fill=TEXT, font=char_name_font,
            )

        # Right — rank plaque anchored to the right edge.
        rank_img = rank_lookup.get(p["rank_tier"]) if p.get("rank_tier") else None
        rank_x = x + CARD_W - 14 - RANK_BOX[0]
        rank_y = y + (CARD_H - RANK_BOX[1]) // 2
        tr._paste_icon(img, rank_img, rank_x, rank_y,
                       RANK_BOX[0], RANK_BOX[1])

        # Middle — name (top) + rank tier (bottom), centered vertically
        # between the two icons.
        mid_x = char_x + CHAR_BOX[0] + 16
        mid_w = rank_x - mid_x - 14
        name_label = tr._ellipsize(
            draw, p["display_name"].upper(), name_font, mid_w,
        )
        rank_label = tr._ellipsize(
            draw, p.get("rank_tier") or "Unranked", rank_font, mid_w,
        )
        mid_y = y + CARD_H // 2 - 22
        draw.text((mid_x, mid_y), name_label,
                  fill=TEXT, font=name_font)
        draw.text((mid_x, mid_y + 32), rank_label,
                  fill=TEXT_DIM, font=rank_font)

    return tr._to_png_buf(img).getvalue()


# --------------------------------------------------------------------------- #
# v3 — seeded leaderboard                                                      #
# Sorted by rank ordinal descending; big seed number on the left marks the    #
# hierarchy. Tighter rows, competitive-standings feel.                         #
# --------------------------------------------------------------------------- #

async def render_variant_leaderboard() -> bytes:
    W = 820
    ROW_H = 72
    HEADER_H = 110
    PAD_X = 24
    RANK_BOX = (100, 50)
    CHAR_BOX = (48, 64)

    rank_lookup, char_lookup = await tr._prefetch_icons_for_players(SAMPLE_ROSTER)

    # Sort by rank ordinal descending (seed #1 = highest rank).
    name_to_ord = {v: k for k, v in _tekken_ranks().items()}
    ordered = sorted(
        SAMPLE_ROSTER,
        key=lambda p: -name_to_ord.get(p.get("rank_tier"), -1),
    )

    H = HEADER_H + len(ordered) * ROW_H + 24
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Header.
    tr._paint_header_gradient(img, 0, HEADER_H)
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)
    draw.text((PAD_X, 14), "LEADERBOARD",
              fill=TEXT, font=tr._load_display_font(52))
    draw.text((PAD_X, 74), "SEEDED BY RANK  ·  8 ENTRANTS",
              fill=ACCENT, font=tr._load_display_font(22))

    seed_font = tr._load_display_font(40)
    name_font = tr._load_font(22)
    rank_font = tr._load_font(14)
    char_name_font = tr._load_display_font(14)

    for i, p in enumerate(ordered):
        y = HEADER_H + i * ROW_H
        if i % 2 == 1:
            draw.rectangle([(0, y), (W, y + ROW_H)], fill=ROW_ALT)
        # Red stripe on the left of the top 3 to mark podium.
        stripe_color = ACCENT if i < 3 else (80, 60, 65)
        draw.rectangle([(0, y), (4, y + ROW_H)], fill=stripe_color)

        # Seed number — big, Bebas, red for podium else dim.
        seed_label = f"#{i + 1}"
        seed_color = ACCENT if i < 3 else TEXT_DIM
        draw.text((PAD_X, y + 14), seed_label,
                  fill=seed_color, font=seed_font)

        # Rank plaque.
        x = PAD_X + 70
        rank_img = rank_lookup.get(p["rank_tier"]) if p.get("rank_tier") else None
        tr._paste_icon(img, rank_img, x,
                       y + (ROW_H - RANK_BOX[1]) // 2,
                       RANK_BOX[0], RANK_BOX[1])

        # Character portrait.
        x += RANK_BOX[0] + 14
        char_img = char_lookup.get(p["main_char"]) if p.get("main_char") else None
        tr._paste_icon(img, char_img, x,
                       y + (ROW_H - CHAR_BOX[1]) // 2,
                       CHAR_BOX[0], CHAR_BOX[1])

        # Name + rank tier.
        x += CHAR_BOX[0] + 14
        draw.text((x, y + 18), p["display_name"],
                  fill=TEXT, font=name_font)
        draw.text((x, y + 46), p.get("rank_tier") or "Unranked",
                  fill=TEXT_DIM, font=rank_font)

        # Character name chip on the far right.
        if p.get("main_char"):
            label = p["main_char"].upper()
            w = tr._text_width(draw, label, char_name_font)
            draw.text((W - PAD_X - w, y + (ROW_H - 14) // 2),
                      label, fill=ACCENT, font=char_name_font)

    return tr._to_png_buf(img).getvalue()


# --------------------------------------------------------------------------- #
# v4 — splash-bar                                                              #
# Each row is a wide "pregame intro card": character portrait as a dominant   #
# left-edge image with a subtle gradient, name in huge Bebas on top, rank     #
# plaque anchored bottom-right. Fewer players visible per screen but every   #
# one feels like a main-event poster.                                         #
# --------------------------------------------------------------------------- #

async def render_variant_splash_bar() -> bytes:
    W = 880
    ROW_H = 128
    HEADER_H = 110
    GAP = 10
    CHAR_BOX = (120, ROW_H - 12)       # portrait fills most of row height
    RANK_BOX = (130, 65)

    rank_lookup, char_lookup = await tr._prefetch_icons_for_players(SAMPLE_ROSTER)

    H = HEADER_H + len(SAMPLE_ROSTER) * (ROW_H + GAP) + 24
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Header.
    tr._paint_header_gradient(img, 0, HEADER_H)
    draw.line([(0, HEADER_H), (W, HEADER_H)], fill=ACCENT, width=3)
    draw.text((24, 14), "THE ROSTER",
              fill=TEXT, font=tr._load_display_font(52))
    draw.text((24, 74), f"{len(SAMPLE_ROSTER)} CONTENDERS STEP INTO THE RING",
              fill=ACCENT, font=tr._load_display_font(22))

    name_font = tr._load_display_font(38)
    rank_font = tr._load_font(18)
    char_name_font = tr._load_display_font(22)

    for i, p in enumerate(SAMPLE_ROSTER):
        y = HEADER_H + 12 + i * (ROW_H + GAP)

        # Card background.
        draw.rectangle([(0, y), (W, y + ROW_H)], fill=ROW_ALT)

        # Red vertical stripe on the left edge.
        draw.rectangle([(0, y), (6, y + ROW_H)], fill=ACCENT)

        # Character portrait block (with red-to-transparent gradient overlay
        # on its right edge so it fades into the card body).
        char_img = char_lookup.get(p["main_char"]) if p.get("main_char") else None
        char_x = 18
        tr._paste_icon(img, char_img, char_x, y + 6,
                       CHAR_BOX[0], CHAR_BOX[1])
        # Subtle fade strip to the right of the portrait.
        _paint_horizontal_fade(
            img, char_x + CHAR_BOX[0], y + 6,
            width=30, height=CHAR_BOX[1],
            start_alpha=70, end_alpha=0,
        )

        # Character name — tucked below the portrait.
        if p.get("main_char"):
            label = p["main_char"].upper()
            w = tr._text_width(draw, label, char_name_font)
            draw.text((char_x + (CHAR_BOX[0] - w) // 2, y + ROW_H - 26),
                      label, fill=ACCENT, font=char_name_font)

        # Name — big Bebas, top-right of card.
        text_x = char_x + CHAR_BOX[0] + 40
        draw.text((text_x, y + 14), p["display_name"].upper(),
                  fill=TEXT, font=name_font)

        # Rank plaque + rank tier line below the name.
        rank_img = rank_lookup.get(p["rank_tier"]) if p.get("rank_tier") else None
        tr._paste_icon(img, rank_img, text_x,
                       y + 62, RANK_BOX[0], RANK_BOX[1])
        draw.text((text_x + RANK_BOX[0] + 14, y + 80),
                  p.get("rank_tier") or "Unranked",
                  fill=TEXT_DIM, font=rank_font)

    return tr._to_png_buf(img).getvalue()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _fit_display_font(
    draw: ImageDraw.ImageDraw, text: str, max_w: int,
    *, max_size: int, min_size: int, step: int = 1,
):
    """Pick the largest display-font size (between max_size and min_size)
    at which `text` fits in `max_w`. Falls back to the smallest size if
    nothing fits — callers are expected to have sensible min_size values
    so the layout doesn't collapse into unreadable text."""
    for size in range(max_size, min_size - 1, -step):
        font = tr._load_display_font(size)
        if tr._text_width(draw, text, font) <= max_w:
            return font
    return tr._load_display_font(min_size)


def _fit_text_to_box(
    draw: ImageDraw.ImageDraw, text: str,
    *, max_w: int, max_h: int,
    max_size: int, min_size: int, step: int = 2,
    font_loader=None,
):
    """Pick the largest font size at which `text` fits inside a
    (max_w, max_h) box. Both width AND height constrain the size — used
    for 'fill this cell' typography.
    `font_loader(size) -> PIL font`. Defaults to the display (Bebas)
    face; pass `tr._load_font` for a mixed-case body bold instead."""
    loader = font_loader or tr._load_display_font
    for size in range(max_size, min_size - 1, -step):
        font = loader(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= max_w and th <= max_h:
            return font
    return loader(min_size)


def _paint_horizontal_fade(
    canvas: Image.Image, x: int, y: int,
    width: int, height: int,
    start_alpha: int, end_alpha: int,
) -> None:
    """Paint a left-to-right alpha fade of the background colour over a
    rectangle. Used to soften the right edge of the splash portrait."""
    r, g, b = BG
    for i in range(width):
        t = i / max(1, width - 1)
        a = int(start_alpha + (end_alpha - start_alpha) * t)
        canvas.alpha_composite(
            Image.new("RGBA", (1, height), (r, g, b, a)),
            (x + i, y),
        )


def _tekken_ranks() -> dict[int, str]:
    # Lazy import so `python design_lab.py` from a half-broken tree still
    # crashes at a useful line.
    import wavu
    return wavu.TEKKEN_RANKS


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

VARIANTS = [
    ("v1_current",      render_variant_current),
    ("v2_card_grid",    render_variant_card_grid),
    ("v3_leaderboard",  render_variant_leaderboard),
    ("v4_splash_bar",   render_variant_splash_bar),
    ("v5_card_3col",    render_variant_card_3col),
]


async def main() -> None:
    for name, fn in VARIANTS:
        png = await fn()
        out = OUT_DIR / f"{name}.png"
        out.write_bytes(png)
        print(f"wrote {out}  ({len(png)//1024} KiB)")


if __name__ == "__main__":
    asyncio.run(main())
