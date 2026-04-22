"""External image URL helpers (character icons, rank icons, branding).

We rely on ewgf.gg's static asset CDN for character and rank icons. URL
conventions are documented inline. If ewgf changes their static hosting,
this is the only file that needs updating.

The Ehrgeiz logo is pulled from the project's GitHub repo via raw.git
so anyone running the bot from a fresh clone gets the branded thumbnail
without bundling binaries into the runtime path. Forks can override
LOGO_URL to point at their own branding.
"""
from __future__ import annotations

EWGF_BASE = "https://ewgf.gg/static"

# Project-default logo. Forks: change this if you've rebranded.
LOGO_URL = (
    "https://raw.githubusercontent.com/Detcader101/ehrgeiz-godhand/"
    "main/assets/ehrgeiz.png"
)


def character_icon_url(name: str | None) -> str | None:
    """Return a small circular character portrait URL for a Tekken 8
    character name, or None if no name is given.

    Convention: lowercase, spaces -> underscores, dashes preserved.
    Examples:
      Lee        -> lee.webp
      Devil Jin  -> devil_jin.webp
      Jack-8     -> jack-8.webp
      Armor King -> armor_king.webp
    """
    if not name:
        return None
    slug = name.strip().lower().replace(" ", "_")
    return f"{EWGF_BASE}/circular_character_icons/{slug}.webp"


# Rank-name -> ewgf icon-stem overrides for ranks whose icon name isn't
# simply "spaces removed". Anything not in this dict uses the default
# `name.replace(" ", "")` rule.
_RANK_ICON_OVERRIDES = {
    "God of Destruction":     "GodOfDestruction",
    "God of Destruction I":   "GodOfDestruction1",
    "God of Destruction II":  "GodOfDestruction2",
    "God of Destruction III": "GodOfDestruction3",
    "God of Destruction ∞": "GodOfDestructionInf",
}


def rank_icon_url(name: str | None) -> str | None:
    """Return the ewgf rank-tier icon URL for a Tekken 8 rank name, or
    None if no rank is given.

    Convention: spaces stripped, then `T8.webp` suffix. Roman numerals
    and the infinity symbol get explicit overrides.
    Examples:
      Beginner               -> BeginnerT8.webp
      1st Dan                -> 1stDanT8.webp
      Tekken King            -> TekkenKingT8.webp
      God of Destruction I   -> GodOfDestruction1T8.webp
      God of Destruction ∞   -> GodOfDestructionInfT8.webp
    """
    if not name:
        return None
    stem = _RANK_ICON_OVERRIDES.get(name) or name.replace(" ", "")
    return f"{EWGF_BASE}/rank-icons/{stem}T8.webp"
