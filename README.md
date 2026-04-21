# Tekken 8 Discord Bot

Run-on-demand bot for a friendly Tekken 8 community server. Handles onboarding, rank-role sync from wavu.wiki, Swiss tournaments with auto-provisioned voice channels, and general moderation.

## Setup (first time)

1. Install Python 3.11+ and create a venv:
   ```
   cd tekken-bot
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux/Mac/WSL
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` — from Discord Developer Portal → Bot → Reset Token
   - `GUILD_ID` — right-click your server icon → Copy Server ID (enable Developer Mode in Discord settings first)
   - `VERIFIED_ROLE_NAME`, `ORGANIZER_ROLE_NAME` — role names the bot will use/create

3. Invite the bot to your server (already done during setup).

4. Post the Player Hub panel: go to any channel (e.g. `#player-hub`) and run `/post-player-panel`. The bot drops the unified panel with Verify / Refresh / Set Rank / My Profile / Unlink buttons. Re-running the command deletes the old panel and posts a fresh one.

## Running

```
python bot.py
```

Close the terminal window to stop the bot.

## Current status

- [x] Onboarding with Tekken ID verification via wavu.wiki
- [ ] Tournament system (Swiss, bracket images, per-match VCs)
- [ ] Moderation cog
