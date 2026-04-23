# Ehrgeiz Godhand — Tekken 8 Discord bot

Panel-driven Tekken 8 server companion for a UK/EU community. MIT-licensed, open source at **https://github.com/Detcader101/ehrgeiz-godhand**.

**Stack:** Python 3.12 + discord.py 2.x + aiosqlite + Pillow.
**Venv:** `.venv/` (Windows-flavoured; use `.venv/Scripts/python.exe`).
**Secrets:** `.env` (gitignored) — `DISCORD_TOKEN`, `GUILD_ID`, optional role overrides.

## Launch

From WSL, open a separate pwsh window with the bot running:

```
cmd.exe /c start pwsh -NoExit -Command "cd C:\Users\jayja\tekken-bot; .\.venv\Scripts\Activate.ps1; python bot.py"
```

`-NoExit` keeps logs visible. Ctrl+C in that window stops the bot.

## Admin command sequence after `/reset-server`

1. `/reset-server` — wipes + rebuilds the whole layout. Creates 39 roles (5 bot roles + 34 Tekken rank tiers), pins every banner, gates categories behind Verified.
2. `/upload-rank-emojis` — uploads 34 rank icons as custom guild emoji (`beginner`, `tekken_emperor`, `god_of_destruction_iii`, …).
3. `/set-bot-profile-banner` — applies the Ehrgeiz banner to the bot's Discord profile.
4. Drag the bot's own role **above** rank/admin/mod roles in Server Settings → Roles (Discord won't let bots raise roles above their own, so this is manual).
5. Verify yourself in `#🎴-player-hub` to unlock the gated categories.

## Features shipped

- **Onboarding** — Polaris Battle ID verification, rank chain: wavu.wiki `/api/replays` → ewgf.gg scrape → self-report fallback. 7-day relink cooldown on ID change. Tiered pending verification for Tekken King+ claims.
- **Tournaments** — `/tournament-create`, signup panel with Join/Leave, Dutch Swiss round-1 pairing, Report-a-Win flow with loser confirm, dispute routing to organizer, auto-advance between rounds, final standings with Buchholz tiebreakers, `/tournament-set-result` admin override.
- **Matchmaking** — LFG panels per region, EU primary for UK/EU audience.
- **Moderation** — `/shutup` (mods bypass rate limit; *The Silencerz* marker role gets one per hour).
- **Server setup** — `/setup-server`, `/reset-server`, `/purge-server` with preview + confirm. Pre-creates every rank role up front.
- **Branded banners** on every user-facing channel. Body text baked into the PNG, not the embed description.
- **Bot profile banner** sized for Discord's safe zones (avatar bottom-left, kebab top-right excluded).

## Testing a tournament end-to-end (solo)

```
/tournament-create name:Test match_format:FT2
  → click ⚔️ JOIN
/tournament-dev-fill name:Test count:7
/tournament-start name:Test
  → round 1 posts, click ⚔️ Report a Win, pick a match, confirm
  → bot auto-advances rounds
  → FINAL STANDINGS posts after last round
/tournament-dev-cleanup
```

## Code map

- `bot.py` — entrypoint, loads cogs.
- `db.py` — aiosqlite helpers + schema. All one-shot migrations for newer columns live in `init_db`.
- `wavu.py`, `ewgf.py` — rank lookup clients.
- `media.py` — external icon CDN URLs (character + rank icons from ewgf).
- `rank_emoji.py` — `markdown_for(guild_id, rank_name)` helper for inline custom emoji.
- `channel_util.py` — `find_text_channel` robust to emoji-prefixed names.
- `view_util.py` — `ErrorHandledView` base; catches button exceptions and surfaces them as ephemeral messages instead of silent fails.
- `tournament_render.py` — every Pillow render: roster card-grid, bracket, banners (with body-text band + `## HEADER` syntax), profile banner, README art.
- `audit.py` — `post_event` / `post_mod_event` → `#🛡️-mod-log` / `#🔍-verification-log`.
- `cogs/`
  - `onboarding.py` — PlayerHubView + verify/refresh/unlink flows. 5-button persistent panel.
  - `setup.py` — SERVER_PLAN, ROLE_PLAN, BANNER_PLAN + all admin commands + purge/reset machinery.
  - `tournament.py` — Swiss state machine, all views, match-report flow + round auto-advance.
  - `matchmaking.py` — LFG panels.
  - `mod.py` — `/shutup`.

## Design conventions

- **Emoji channel prefixes**; lookups go through `channel_util.find_text_channel` which matches both `🏆-tournaments` and bare `tournaments`, so SERVER_PLAN can rebrand without shattering downstream code.
- **Persistent views** look up their context via `interaction.message.id` against a DB column (e.g. `tournament_matches.report_message_id`, `tournaments.signup_message_id`, `panels`). One registered view class handles every live message of its kind.
- **Banner bodies** are baked into the PNG via `render_banner(body=…)`. `## HEADER` syntax renders in accent red; blank lines create paragraph breaks.
- **All bot-created roles `hoist=False`** — only the bot's own auto-created role should appear separately in the sidebar.
- **Rank-tier emoji** mapping for sorting lives in `_RANK_ORDINAL_BY_NAME` (derived from `wavu.TEKKEN_RANKS`).
- **Error handling** — every slash command has `cog_app_command_error`; every persistent View inherits `ErrorHandledView`.

## Current state

All committed + pushed to `main`. Tournament slice 2 is complete (full Swiss end-to-end). Next priorities in order:

1. **Slice 3** — auto-provisioned per-tournament category + invite-only per-match voice channels.
2. **Forum channels** for `#🎯-combos`, `#🆚-matchup-help`, `#🎬-clips-and-highlights`.
3. **Rank-emoji integration** across more embeds (`/profile`, tournament signup roster text, state announcements).
4. **Moderation expansion** — `/kick`, `/ban`, `/timeout`, `/warn`, `/warnings`, `/purge` per SPEC.md §9.
5. **Hosting prep** for Jay's Proxmox homelab — systemd unit / docker / health endpoint.

## Non-obvious gotchas

- **Discord bot user edits are heavily rate-limited** (`/set-bot-profile-banner` — roughly twice an hour).
- **Channel-name emojis**: regional flag sequences like `🇪🇺` don't render reliably in channel names — use single-codepoint globes (`🌍`) instead. Regular message content renders them fine.
- **Bebas Neue is caps-only** — lowercase letters map to uppercase glyphs. Player names use the body font (DejaVu Sans Bold) so mixed case is preserved.
- **`PRAGMA foreign_keys` isn't enabled** in the aiosqlite connections, so `ON DELETE CASCADE` doesn't fire. Deletion helpers wipe child tables explicitly.
- **Windows CP1252** in the console can choke on emoji prints during smoke tests — use `py_compile` + import checks rather than print-heavy probes.
