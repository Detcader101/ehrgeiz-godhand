"""Behaviour spec for tournament_render text + ident-banner helpers.

Two contracts under test:

  * `_fit_text_with_ellipsis` — keeps the floor font readable by
    truncating with U+2026 instead of letting text shrink to nothing
    when a long display name overflows even at min_size.
  * `render_ident_banner` — produces a valid PNG suitable for
    attaching to an ephemeral Player Hub embed.

Implementation details (named accent constants, exact pixel sizes,
gradient colours) are deliberately NOT asserted — those are free to
change without breaking callers.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

import tournament_render


@pytest.fixture
def draw():
    return ImageDraw.Draw(Image.new("RGBA", (1000, 100)))


# --------------------------------------------------------------------------- #
# _fit_text_with_ellipsis                                                      #
# --------------------------------------------------------------------------- #

def test_short_text_passes_through_untouched(draw):
    rendered, _ = tournament_render._fit_text_with_ellipsis(
        draw, "Jay", max_w=400, max_h=80,
        max_size=72, min_size=44,
    )
    assert rendered == "Jay"


def test_long_text_is_truncated_with_ellipsis_at_floor_font(draw):
    # max_w narrow enough that the helper hits min_size and still
    # overflows — the contract is "truncate with …", not "shrink forever".
    long_name = "X" * 200
    rendered, _ = tournament_render._fit_text_with_ellipsis(
        draw, long_name, max_w=80, max_h=80,
        max_size=72, min_size=44,
    )
    assert rendered.endswith("…")
    assert len(rendered) < len(long_name)


def test_returned_text_is_never_empty(draw):
    # Pathological: even a single character + ellipsis won't fit. The
    # helper must still return *something* renderable (not "").
    rendered, _ = tournament_render._fit_text_with_ellipsis(
        draw, "wide string", max_w=1, max_h=80,
        max_size=72, min_size=44,
    )
    assert rendered != ""


# --------------------------------------------------------------------------- #
# render_ident_banner                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_returns_a_valid_png_image():
    buf = await tournament_render.render_ident_banner(
        kicker="Player Hub", title="Tester",
    )
    assert isinstance(buf, io.BytesIO)
    buf.seek(0)
    img = Image.open(buf)
    img.verify()  # raises if the bytes aren't a valid image
    assert img.format == "PNG"


@pytest.mark.asyncio
async def test_image_dimensions_fit_a_discord_embed_thumbnail_strip():
    # The banner is sized as a wide skinny strip (W >= 4*H) so it sits
    # naturally above an ephemeral embed body. Asserting on shape, not
    # exact pixels — callers consume it as an attached image.
    buf = await tournament_render.render_ident_banner(
        kicker="Player Hub", title="Tester",
    )
    buf.seek(0)
    w, h = Image.open(buf).size
    assert w > 0 and h > 0
    assert w >= 4 * h


@pytest.mark.asyncio
async def test_custom_accent_does_not_break_rendering():
    # Callers tint the strip per action (gold for rank changes, red
    # for destructive). The contract is "any RGB tuple is accepted",
    # not "produces a specific pixel" — the gradient overlay makes
    # exact-colour assertions brittle.
    buf = await tournament_render.render_ident_banner(
        kicker="Unlinked", title="Tester", accent=(180, 50, 60),
    )
    buf.seek(0)
    Image.open(buf).verify()
