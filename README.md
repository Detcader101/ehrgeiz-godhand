# Ehrgeiz Godhand

<p align="center">
  <img src="assets/ehrgeiz.png" alt="Ehrgeiz Godhand — black and white fist logo with lightning slashes" width="280">
</p>

**Panel-driven Discord bot for Tekken 8 communities — role assignment by rank/ELO, bracket forming, and general moderation.**

> Hello, this bot was created by Claude as an open source project with my prompt and direction. I want this bot to be useful for as many people as possible, because Tekken needs Tournaments. Any improvements I'd love, and you can clone this bot and use it however. This is for the community, for every group that wants it. Ehrgeiz Godhand is a light reference, and the original server this bot was created for does not matter; have fun ;)
>
> — Detcader_

---

Players verify with their Polaris Battle ID, their in-game rank becomes a Discord role, and (coming soon) organizers run rank-seeded Swiss tournaments with auto-provisioned per-match voice channels.

Built for small friendly servers — designed to be run on-demand on a laptop rather than deployed 24/7 on a VPS.

## Features

**Onboarding & rank sync**
- Unified Player Hub panel with buttons: Verify, My Profile, Refresh Rank, Set Rank Manually, Unlink Me.
- Verification via Polaris Battle ID — bot looks up the player on [wank.wavu.wiki](https://wank.wavu.wiki) and confirms the name/main character with the user before granting roles.
- Auto-detects current Tekken rank from the player's most recent ranked match via wavu's replay API. Falls back to a two-stage self-report dropdown when no recent match is found.
- One Tekken ID per Discord account (enforced at the DB level); admins can override via `/admin-link`.
- Persistent panels with stable custom IDs — buttons keep working after bot restarts.
- Ephemeral responses with auto-delete so channels stay clean.

**Planned**
- Swiss tournaments seeded by rank tier
- Pillow-rendered bracket images
- Auto-provisioned per-match invite-only voice channels
- Organizer-confirmed cleanup + archive to a tournament-history channel
- Moderation cog (kick / ban / timeout / warn-with-history / purge)

## Setup

### 1. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), create a new application.
2. Open the **Bot** tab, enable all three **Privileged Gateway Intents** (Presence, Server Members, Message Content).
3. Click **Reset Token** and copy it.
4. Under **OAuth2 → URL Generator**, check `bot` + `applications.commands`, and give it `Administrator` permission (simplest for a friendly server — tighten later). Open the generated URL to invite the bot to your server.
5. In Discord, drag the bot's role above any role you want it to manage (Server Settings → Roles).

### 2. Install

Requires Python 3.10+.

```bash
git clone https://github.com/gnutgnut/ehrgeiz-godhand.git
cd ehrgeiz-godhand
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac/WSL
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Copy `.env.example` to `.env` and fill in:

| Variable | What it is |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Developer Portal |
| `GUILD_ID` | Your server's ID (Developer Mode → right-click server → Copy Server ID) |
| `VERIFIED_ROLE_NAME` | Name of the role verified users get (default `Verified`) |
| `ORGANIZER_ROLE_NAME` | Name of the tournament-organizer role (default `Organizer`) |
| `MOD_LOG_CHANNEL_ID` | *(Optional, for future mod cog)* |

### 4. Run

```bash
python bot.py
```

In Discord, pick a channel (e.g. `#player-hub`) and run `/post-player-panel`. The unified panel appears with all the buttons.

## How the rank lookup works

Wavu.wiki doesn't expose an official player-lookup API. The bot does two things:

1. **Scrapes `https://wank.wavu.wiki/player/{TekkenID}`** for the player's display name and main character (via the HTML page).
2. **Queries `https://wank.wavu.wiki/api/replays`** — the only documented wavu endpoint — paginating through the most recent matches to find one containing the player's Polaris ID. The `p1_rank` / `p2_rank` fields in each replay record are the authoritative in-game rank integer, which the bot maps to a tier name via the `TEKKEN_RANKS` table in `wavu.py`.

If neither step finds the player (e.g. they haven't played a ranked match in the last ~35 minutes of replay stream), the bot falls back to a two-stage dropdown so the user can self-report.

**If wavu changes their markup or API**, only `wavu.py` needs updating — the rest of the bot treats it as an abstract data source.

## Architecture

```
bot.py              # Entry point. Loads cogs, syncs slash commands to guild.
db.py               # aiosqlite helpers. Tables: players, warnings, panels.
wavu.py             # wavu.wiki client. PlayerProfile, rank lookup, rank map.
cogs/
  onboarding.py     # PlayerHubView + all rank/verify logic.
```

**Schema (SQLite):**
- `players(discord_id PK, tekken_id UNIQUE, display_name, main_char, rating_mu, rank_tier, last_synced, linked_by)`
- `warnings(id, discord_id, issued_by, reason, issued_at)`
- `panels(guild_id, kind, channel_id, message_id)` — one row per guild+panel kind, so reposts delete the old message.

## Credits

- **[Wavu Wank](https://wank.wavu.wiki/)** — the data source for all player ranks and stats. Please don't abuse it (the bot uses a custom User-Agent and single-flight requests).
- Built with **[discord.py](https://github.com/Rapptz/discord.py)** (v2.x).

## License

MIT — see [LICENSE](LICENSE).

## Contributing

PRs welcome. A few things worth knowing:

- The rank-ID → tier-name table in `wavu.py` (`TEKKEN_RANKS`) is community-sourced; if you find a wrong mapping, please open a PR with a citation.
- Scraping logic lives entirely in `wavu.py`. If wavu changes their markup, that's the only file that should need edits.
- The bot currently has no automated tests — add some if you're changing non-trivial logic.
