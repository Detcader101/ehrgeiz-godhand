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

- **Onboarding** — Polaris Battle ID verification, rank chain: wavu.wiki `/api/replays` → ewgf.gg scrape → self-report fallback. 7-day relink cooldown on ID change. Tiered pending verification for Tekken King+ claims. **My Profile** (button in `#🎴-player-hub`) now renders a Pillow player card alongside the embed.
- **Tournaments** — `/tournament-create`, signup panel with Join/Leave, Dutch Swiss round-1 pairing, Report-a-Win flow with loser confirm, dispute routing to organizer, auto-advance between rounds, final standings with Buchholz tiebreakers, `/tournament-set-result` admin override.
- **Matchmaking** — LFG panels per region, EU primary for UK/EU audience.
- **Fit Check** (Slice 3, ace-of-spades) — `#📸-fit-check` channel for character-customisation screenshots. `/fitcheck-post` composites the user image into a Pillow-rendered Ehrgeiz card (header band, character medallion, rank flair). Verified members vote 👍/👎 (Reddit-style toggle, self-votes blocked). `/fitcheck-leaderboard` ranks last week's net-score winners. Weekly background task crowns a **Drip Lord** — rotating role + gold celebration card posted in `#📣-announcements`. `/fitcheck-rotate-now` is the admin force-trigger. Posts and deletes log to `#📦-mod-log-dump`.
- **Moderation** — `/shutup` (mods bypass rate limit; *The Silencerz* marker role gets one per hour).
- **Server setup** — `/setup-server`, `/reset-server`, `/purge-server` with preview + confirm. Pre-creates every rank role up front with section-banded colours (Beginner grey · Dan bronze · Fighter green · Ranger teal · Vanquisher blue · Garyu purple · Ruler amber · Fujin red · Tekken gold · GoD violet→prismatic). Re-running `/setup-server` syncs colours idempotently. Channels include `📦-mod-log-dump` (low-priority audit) and `📈-rank-ups` (promotion celebrations). Roles include `Drip Lord` (rotating fashion crown).
- **Rank-up celebrations** — when a verified player promotes (sweeper or self-refresh), the bot posts a Pillow rank-up card to `#📈-rank-ups`: "PROMOTED" kicker, player name, from-rank → to-rank icons with a gold chevron, section colour stripe.
- **Tournament champion card** — gold-trim Pillow banner posted alongside `FINAL STANDINGS` showing the winner's character, rank, and the runner-up.
- **Achievement badges** — player cards show chips for current `Drip Lord` 👑, lifetime `Champion` 🏆, fit-check `Veteran` 📸 (10+ posts), and `Verified` ✅. Up to 6 chips per card.
- **Weekly recap** — every 7 days (Mondays in practice), `cogs/recap.py` posts a Pillow digest in `#📣-announcements`: Drip Lord, top fit, new members, tournaments completed, fit checks posted. `/recap-now` for admin force-trigger. Idempotent via `posted_messages` keyed on ISO week.
- **Bot health endpoint** — optional aiohttp listener (`bot_health.py`) on `BOT_HEALTH_PORT`. `/healthz` returns 200 when ready + gateway latency under 5s; `/metrics` exposes basic counters. Closes the "host alive but bot crashloop" blind spot for Uptime Kuma.
- **Per-rank embed colours** — profile embeds tint by the player's rank tier so the role-list colour story carries through.
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
- `tournament_render.py` — every Pillow render: roster card-grid, bracket, banners, profile banner, README art, **single-player card** (with optional badge chips), **fit-check card**, **Drip Lord celebration card**, **rank-up card** (`render_rank_up_card`), **tournament champion card** (`render_tournament_champion_card`), **weekly recap composite** (`render_weekly_recap_card`).
- `rank_meta.py` — single source of truth for rank colours, sections, ordinals, promotion detection. Imported by setup (role colours), tournament_render (card tints), onboarding (embed tints, rank-up celebrations), recap.
- `bot_health.py` — optional HTTP liveness listener bound to localhost. Activates only when `BOT_HEALTH_PORT` is set in env.
- `audit.py` — `post_event` / `post_mod_event` / `post_dump_event` → `#🛡️-mod-log` / `#🔍-verification-log` / `#📦-mod-log-dump` (low-priority feed).
- `cogs/`
  - `onboarding.py` — PlayerHubView + verify/refresh/unlink flows. 5-button persistent panel. Rank-up celebrations fire from the rank-change call sites.
  - `setup.py` — SERVER_PLAN, ROLE_PLAN, BANNER_PLAN + all admin commands + purge/reset machinery.
  - `tournament.py` — Swiss state machine, all views, match-report flow + round auto-advance. Champion card posts on completion.
  - `matchmaking.py` — LFG panels.
  - `fitcheck.py` — fit-check post/vote/delete flow, FitcheckVoteView (persistent 👍/👎), `_DripLordRotator` weekly background task, `/fitcheck-rotate-now` and `/fitcheck-set-drip-lord` admin triggers.
  - `admin.py` — cross-feature staff diagnostics. `/admin-inspect-user` dumps every relevant DB row.
  - `recap.py` — `_RecapPoster` weekly background task + `/recap-now` admin trigger.
  - `mod.py` — `/shutup`.

## Admin escape hatches

Every button-driven flow has an admin slash-command equivalent so staff can resolve user-facing issues without depending on a clickable message:

| Button flow | Admin override |
|---|---|
| Player Hub → Verify | `/admin-link member tekken_id rank?` |
| Player Hub → Unlink Me | `/admin-unlink member` |
| (cooldown blocks Verify) | `/admin-clear-cooldown member` |
| #verification-log → Confirm/Reject | `/admin-pending-resolve member action:confirm\|reject` |
| Tournament signup → Join | `/tournament-add-player name member` |
| Tournament signup → Leave | `/tournament-remove-player name member` |
| Match report → Confirm/Dispute | `/tournament-set-result name round_number match_number winner` |
| Drip Lord auto-rotation | `/fitcheck-rotate-now` (force run) or `/fitcheck-set-drip-lord member` (manual crown) |
| Fit Check post (button-less by design) | `/fitcheck-post` is the only path; `/fitcheck-delete` removes posts |

For triage: **`/admin-inspect-user member`** dumps every relevant DB row + role state in one ephemeral embed.

## Crash-safety / idempotent posting

Background tasks that send Discord messages must survive a process restart without double-posting. Two primitives in `db.py`:

- `bot_state` — per-guild key/value store. Stamp the rotation timestamp **before** side effects so a crash mid-flow can't re-fire the rotation on next loop.
- `posted_messages` — idempotency log keyed by `(kind, identity, guild_id)`. Callers consult `find_posted_message` before posting and call `record_posted_message` after. The Drip Lord rotator uses this with `identity = YYYY-MM-DD` so two rotations on the same calendar day are forbidden.

Same pattern applies to:
- Channel banner provisioning (already idempotent via `db.panels`)
- Player Hub panel (already idempotent via `db.panels`)
- Drip Lord weekly announcement (now via `posted_messages`)

When adding a new background-emitted post, route through `posted_messages` with a deterministic identity key. Log on duplicate-skip; the dedup is the load-bearing safety, not the log line.

## Design conventions

- **Emoji channel prefixes**; lookups go through `channel_util.find_text_channel` which matches both `🏆-tournaments` and bare `tournaments`, so SERVER_PLAN can rebrand without shattering downstream code.
- **Persistent views** look up their context via `interaction.message.id` against a DB column (e.g. `tournament_matches.report_message_id`, `tournaments.signup_message_id`, `panels`). One registered view class handles every live message of its kind.
- **Banner bodies** are baked into the PNG via `render_banner(body=…)`. `## HEADER` syntax renders in accent red; blank lines create paragraph breaks.
- **All bot-created roles `hoist=False`** — only the bot's own auto-created role should appear separately in the sidebar.
- **Rank-tier emoji** mapping for sorting lives in `_RANK_ORDINAL_BY_NAME` (derived from `wavu.TEKKEN_RANKS`).
- **Error handling** — every slash command has `cog_app_command_error`; every persistent View inherits `ErrorHandledView`.

## Current state

All committed + pushed to `main`. Tournament slice 2 is complete (full Swiss end-to-end). Bot is **live in prod** on shed-tekken (CT 104, see `../shednet/tekken-bot/`) — deploys auto-update from `origin/main` every ~2 min via systemd timer, and post a Deploy embed to `#🛡️-mod-log` on first `on_ready`. Mandatory-verification onboarding shipped (`#👋-welcome` is verified-only; rules + announcements stay public). Rank auto-refresh sweeper now runs in-process. Next priorities in order:

1. **Slice 3** — auto-provisioned per-tournament category + invite-only per-match voice channels.
2. **Forum channels** for `#🎯-combos`, `#🆚-matchup-help`, `#🎬-clips-and-highlights`.
3. **Rank-emoji integration** across more embeds (`/profile`, tournament signup roster text, state announcements).
4. **Moderation expansion** — `/kick`, `/ban`, `/timeout`, `/warn`, `/warnings`, `/purge` per SPEC.md §9.
5. **Health endpoint** — small HTTP listener so Kuma can probe the bot itself (currently it watches `node_exporter:9100`, which catches host-down but not a bot crashloop with the host alive). Closes the last v1.0 op gap from `SPEC.md` §7.2.

## Non-obvious gotchas

- **Discord bot user edits are heavily rate-limited** (`/set-bot-profile-banner` — roughly twice an hour).
- **Channel-name emojis**: regional flag sequences like `🇪🇺` don't render reliably in channel names — use single-codepoint globes (`🌍`) instead. Regular message content renders them fine.
- **Bebas Neue is caps-only** — lowercase letters map to uppercase glyphs. Player names use the body font (DejaVu Sans Bold) so mixed case is preserved.
- **`PRAGMA foreign_keys` isn't enabled** in the aiosqlite connections, so `ON DELETE CASCADE` doesn't fire. Deletion helpers wipe child tables explicitly.
- **Windows CP1252** in the console can choke on emoji prints during smoke tests — use `py_compile` + import checks rather than print-heavy probes.
