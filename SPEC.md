# Ehrgeiz Godhand — Specification

> **Why this exists:** so more people play Tekken for longer, and the community lasts longer.

This document is the vision and roadmap for Ehrgeiz Godhand. It's written to be useful for three audiences, in order:

1. **Contributors** — so PRs and discussions aim in a shared direction.
2. **Server admins** — deciding whether to adopt the bot, and what's coming.
3. **Me (Detcader_)** — so I remember what I actually decided.

If you're reading this and you have opinions, open an issue. This is a living document.

---

## 1. Current state

As of the initial spec publication:

- **Onboarding & rank sync** — Polaris Battle ID verification, Player Hub panel with 5 buttons (Verify / My Profile / Refresh / Set Rank Manually / Unlink Me), two-source rank chain (wavu `/api/replays` → ewgf.gg → self-report dropdown), legacy rank-role cleanup.
- **One-command server setup** — `/setup-server` provisions the default server structure (7 categories, 16 channels, 4 roles, staff-only perms) idempotently.
- **Persistence** — SQLite via `aiosqlite`. Tables: `players`, `warnings`, `panels`.
- **Persistent panels** — survive bot restarts via stable `custom_id`s.

Everything below is *not yet built* unless marked ✅.

---

## 2. Vision

Ehrgeiz Godhand is a **fighting-game community OS, Tekken-8-first**.

It should be the thing a Tekken server admin installs on day one and never has to swap out — onboarding, rank-gated roles, tournaments, moderation, and community-building tools, all in one coherent package designed by and for FGC players.

**Tekken 8 is the game we build for in practice.** Multi-game support is welcome via contributor PRs, but not on the maintainer's personal roadmap. The architecture keeps game-specific code (rank tables, ID formats, data-source clients) behind adapters so a contributor *could* add SF6 or GG Strive without refactoring the world, but this isn't a promised milestone.

---

## 3. Design principles

These are the rules I'd like every PR, feature, and polish pass to honour.

1. **Robot first, Godhand second.** The bot is a competent tool that happens to wear a Tekken-themed jacket. Voice is neutral/technical by default; flavour is rare, tasteful, and never gets in the way of clarity. Error messages explain the problem, not taunt the user.
2. **Branded visual flair throughout.** Custom rank emojis, character icons in profile and match cards, a consistent Ehrgeiz colour palette on every embed. The difference between "a generic bot" and "*this* bot" is the craft in the UI.
3. **À-la-carte provisioning.** Admins pick which feature bundles they want. No server is forced to accept the full template.
4. **Data sources live behind adapters.** `wavu.py` and `ewgf.py` are the only files that know about their respective sites. If either source changes markup or disappears, one file changes.
5. **Zero friction for the common case; real friction only where it matters.** Most verifications, rank refreshes, and tournament joins should happen in one click. Anti-abuse measures kick in only at the tiers where impersonation has real social payoff.
6. **Fork freely, rebrand or don't.** MIT licence, no white-label restriction, no "powered by" footer requirement. If another community forks and renames to something else, good. If they keep "Ehrgeiz Godhand," also good.
7. **Self-host and hosted coexist.** The same code runs on a laptop for one server or 24/7 for many. No hosted-instance-only features; no self-host-only features.
8. **Data stays where it's hosted.** No telemetry, no analytics, no "phone home." Whoever runs the bot owns the data it holds.

---

## 4. Data & privacy

Ehrgeiz Godhand is a free, open-source project with no monetisation, no central data collection, and no advertising. What it stores is the minimum needed to function:

- **Discord user ID** — so the bot can identify you across sessions.
- **Polaris Battle ID** — so the bot can look up your rank.
- **Display name, main character, current rank** — cached from the latest wavu/ewgf lookup.
- **Warnings** — if the moderation cog is in use.
- **Panel message IDs** — so reposting a panel deletes the old one.

All data lives in the SQLite database of whichever instance is running the bot. There is no central server. Your data doesn't leave the host.

Users can self-delete their link at any time via the **Unlink Me** button in the Player Hub, which removes their row from `players` and strips the bot-assigned roles. Admins can do this on anyone's behalf with `/admin-unlink`.

For the public hosted instance specifically: a short plain-language privacy notice will live at the repo root (`PRIVACY.md`) describing what's stored, who sees it, and how to delete it.

---

## 5. Anti-abuse posture

At small scale, honor-based linking is fine. At the scale of a hosted public instance, false rank claims become a real vector (e.g. claiming a top-ranked player's Polaris ID to gain social cred). The design balances impersonation-resistance against friction for the ~95% of legitimate users.

### 5.1 Audit log
Every link, unlink, rank-change, and admin override posts to a staff-only `#verification-log` channel, auto-created by the à-la-carte setup. Servers can opt into a public version of this log instead. Most impersonation attempts die here: the regulars see activity and spot "that's not the real person."

### 5.2 Relink cooldown
Once a Discord account unlinks, they cannot re-link to a different Tekken ID for **7 days**. Stops rotating identities to probe.

### 5.3 Tiered claim friction
Based on the rank returned by the two-source chain:

- **Below Tekken King** — auto-verified, zero friction. Covers the vast majority of players; impersonating a random Kishin has near-zero social payoff.
- **Tekken King and above** — status "Pending Verification." The user gets the **Verified** role immediately but **not the rank role** until any organizer clicks **Confirm** in `#verification-log`. Pending requests time out after **72 hours** and auto-downgrade to a "self-reported" rank (which an organizer can still confirm later).

The rank threshold, cooldown duration, and public-vs-staff log channel are all configurable during à-la-carte setup.

### 5.4 Admin override
`/admin-link` bypasses everything. Admins know their server.

---

## 6. Rate-limit politeness

The bot talks to wavu.wiki and ewgf.gg on user-triggered lookups. At hosted scale with many guilds and auto-refresh, naive behaviour could hammer those sites and get the bot blocked (which kills the service for everyone).

**v1.0 architectural commitments:**

- **Short-TTL cache** per Tekken ID (5 minutes) for both wavu profile and rank-chain results. Manual `Refresh Rank` bypasses the cache.
- **Single-flight in-flight dedupe** — if two users refresh the same Tekken ID simultaneously, exactly one request leaves the bot.
- **Exponential backoff on 429/5xx** — on transient errors from wavu or ewgf, step back rather than retry-spam.
- **Custom `User-Agent`** on every outgoing request, identifying the bot and contact email, so if either source wants to flag abuse they can.

---

## 7. Hosting

The project targets two deployment models, equally supported.

### 7.1 Self-host
A server admin clones the repo, creates their own Discord application, installs Python, and runs `python bot.py`. Works on a laptop, a Raspberry Pi, or a VPS. Good for a single server with technical admins.

### 7.2 Public hosted instance
A single bot application hosted 24/7, which any Discord server can `/invite` and configure with `/setup-server` → à-la-carte. The maintainer runs this instance (initial home: a Proxmox VM on the shednet homelab).

**v1.0 hosted-instance requirements:**

- Container image (Dockerfile) with auto-restart on crash.
- Structured logging to stdout (container log aggregator captures it).
- `/admin-status` command for operators to check bot health from inside Discord.
- Multi-guild safety: no global singletons, all per-guild data keyed by `guild_id`, one `aiosqlite` connection scoped per operation (already the case).

---

## 8. Tournaments — the headline feature

The feature this bot exists for. The design brief comes straight from the project brief.

### 8.1 Format
**Swiss.** Everyone gets games regardless of how they're doing. No elimination. N rounds determined by bracket size (standard Swiss rounds ≈ log₂(players)).

### 8.2 Size & cadence
- **Scalable from ~4 to 64+ players.** Small tournaments are the common case; the architecture shouldn't fall over at 32+.
- **Both scheduled and on-demand.** Organizers can create a recurring weekly/monthly tournament *or* run `/tournament-now` when enough people are online.

### 8.3 Seeding
- **By current rank** (descending), highest seed plays lowest in round 1.
- **Organizer override** — drag-and-drop (button-based) manual reseeding before round 1 starts, for cases where the bot's rank data is stale or disputed.

### 8.4 Match flow
- **Self-report by default.** Both players click a button to confirm the winner. Disagreement escalates.
- **Organizer fallback with waiting period.** If players disagree, an organizer is pinged. The bot waits **up to 10 minutes** for an organizer response, then defaults to a tie/re-play.
- **Finals specifically** — organizer *presence* is preferred but not required. Same 10-minute waiting window before self-report takes over.

### 8.5 Voice channels
Every match auto-provisions a **private voice channel**:
- **Players + organizer role** → can join, can speak.
- **Everyone else** → can join **muted**. They can watch and listen; they cannot speak or interfere.
- Channel deletes 5 minutes after the match closes, or at the end of the tournament, whichever comes first.

### 8.6 Stakes & ranked mode
- **Casual** — default and only mode at v1.0. Tournaments are for fun, bragging rights, and bracket images.
- **Ranked tournament mode** — v2.5+ milestone. Results contribute to a server-internal ELO/leaderboard separate from in-game Tekken rank. Details TBD closer to the time.

### 8.7 Archive
Every tournament, once closed, gets archived: final bracket image, results table, and VOD/replay-code submissions if players contributed them. Searchable via `/tournament-history`.

---

## 9. Moderation

A first-class mod cog, scoped small on purpose. If a server needs heavier moderation they should reach for a dedicated tool; Ehrgeiz's job is "enough for a friendly Tekken server."

**v1.0 mod scope:**
- `/shutup <user>` ✅ — convenience combo: deletes the user's last 5 messages in the current channel and times them out for 2 minutes. The "this person is being annoying right now" one-click hammer.
- `/kick <user> [reason]`
- `/ban <user> [reason] [delete_messages_days]`
- `/timeout <user> <duration> [reason]`
- `/warn <user> <reason>` with per-user warning history
- `/warnings <user>` to view history
- `/purge <count>` to bulk-delete messages in the current channel

All mod actions log to `#mod-log` (created by à-la-carte setup if the mod bundle is selected).

Auto-mod, raid protection, reaction-roles, slowmode automation, and similar are **out of scope** for v1.0. They can come later if demand appears.

---

## 10. Roadmap

### v1.0 — "Tell another Tekken server to install this"
- ✅ Onboarding + rank chain (wavu → ewgf → self-report)
- ✅ Player Hub panel
- ✅ `/setup-server` (opinionated default — à-la-carte rework below)
- **À-la-carte setup rework.** Admin picks feature bundles: Onboarding, Tournaments, Mod, VC Matchmaker, Verification Log. Each bundle provisions its own channels/roles.
- **Tournament cog.** Full design from §8.
- **Moderation cog.** Scope from §9.
- **VC Matchmaker** — `/lookingforfights` queue, auto-pair, private VC spin-up. Daily-use infrastructure.
- **Anti-abuse tiered verification** — §5.
- **Rate-limit cache + single-flight** — §6.
- **Visual flair pass** — custom rank emojis, character icons in profile and match embeds, Ehrgeiz colour palette throughout.
- **24/7 hardening** — Dockerfile, structured logging, `/admin-status`, restart resilience.
- **Public hosted instance live** on the shednet homelab.
- **`PRIVACY.md`** — plain-language privacy notice.

### v1.5 — Community-OS foundations
- **Character-mains roles** — auto-assigned from wavu main-character data.
- **Rank leaderboard** — `/leaderboard` top-N members by current rank.
- **Season migration** — when Bandai resets ranks at season boundary, batch-refresh everyone, archive old ranks to `seasons` table.
- **Tournament history archive** — `/tournament-history`, searchable, with bracket images.
- **Test suite** — at minimum, smoke tests for the rank chain, the setup-server provisioner, and the tournament state machine. A prerequisite for heavy contributor PRs.

### v2 — Community-OS expansion
- **Replay sharing channel** — players drop a replay code, bot fetches metadata from wavu and posts a tidy embed.
- **Matchup stats** — `/stats` pulls the player's matchup spread from wavu.
- **Tier-list polls** — bot-managed character tier-list voting with visualised results.

### v2.5+ — Nice to have
- **Fight of the week** voting.
- **Ranked tournament mode.**

---

## 11. Non-goals

Explicit things this project is **not** going to do (at least not in the foreseeable maintainer-led direction). Contributor PRs that want to add these are welcome but will need a champion.

- **Internationalisation.** English-first. Tekken is global, but translating every embed is out of scope until someone fluent in another language steps up to own it.
- **Cross-server tournaments.** Tournaments are scoped to a single Discord server.
- **Native multi-game support.** The architecture won't block it, but no maintainer-led work on adding SF6 / GG / etc. adapters.
- **Formal versioning/changelog.** Git tags and release notes are enough for now. Semver comes when breaking changes start hurting operators.

---

## 12. Contributing

The project is licensed MIT and actively welcomes contributors. Forks are encouraged — with or without rebranding.

A `CONTRIBUTING.md` will follow shortly, covering:
- Dev setup (local Discord test server, `.env.example`, running the bot)
- Code style (type hints, ruff, black — keep it boring)
- How to propose changes to this spec (open an issue tagged `spec`)
- What makes a good PR (one focused change, tests if the area is covered, README updates if the change is user-visible)

Until then: open an issue, describe what you want to do, we'll talk it through.

If you run Ehrgeiz Godhand in your server and have feedback, open an issue with the `field-report` label. Real-world usage signal is the single most valuable input to this project.

---

## 13. Credits

- **[Wavu Wank](https://wank.wavu.wiki/)** — primary player data source.
- **[ewgf.gg](https://ewgf.gg/)** — secondary rank source.
- **[discord.py](https://github.com/Rapptz/discord.py)** — bot framework.
- Anyone who files a bug, tests a pre-release, or tells a server admin friend about this project.

— Detcader_
