from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import audit
import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
)
log = logging.getLogger("tekken-bot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True

INITIAL_COGS = [
    "cogs.onboarding",
    "cogs.setup",
    "cogs.mod",
    "cogs.tournament",
    "cogs.matchmaking",
    "cogs.fitcheck",
    "cogs.admin",
]


class TekkenBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=INTENTS)
        # on_ready can fire on every gateway reconnect; gate the deploy
        # announcement so we only post once per process.
        self._deploy_announced = False

    async def setup_hook(self) -> None:
        await db.init_db()
        for cog in INITIAL_COGS:
            await self.load_extension(cog)
        # Guild-scoped sync: commands appear instantly in our server only.
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d slash commands to guild %s", len(synced), GUILD_ID)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        # on_ready fires on every gateway reconnect; one-shot everything
        # below so the banner doesn't re-log and the deploy embed doesn't
        # re-post on a flaky network.
        if self._deploy_announced:
            return
        self._deploy_announced = True

        try:
            from cogs.onboarding import (
                PENDING_SWEEP_INTERVAL,
                RANK_SWEEP_INTERVAL,
                RANK_SWEEP_SKIP_IF_SYNCED_WITHIN,
                VERIFIED_ROLE_NAME,
            )
        except ImportError:
            pass
        else:
            log.info(
                "[startup] intents members=%s msg_content=%s guilds=%d",
                INTENTS.members, INTENTS.message_content, len(self.guilds),
            )
            for g in self.guilds:
                log.info("[startup] guild id=%s name=%r members=%d",
                         g.id, g.name, g.member_count)
            log.info(
                "[startup] config verified_role=%r pending_sweep=%ss "
                "rank_sweep=%ss rank_skip_if_synced_within=%ss",
                VERIFIED_ROLE_NAME,
                int(PENDING_SWEEP_INTERVAL.total_seconds()),
                int(RANK_SWEEP_INTERVAL.total_seconds()),
                int(RANK_SWEEP_SKIP_IF_SYNCED_WITHIN.total_seconds()),
            )

        sha = os.getenv("BOT_GIT_SHA")
        subject = os.getenv("BOT_GIT_SUBJECT")
        if not sha:
            return
        guild = self.get_guild(GUILD_ID)
        fields: list[tuple[str, str, bool]] = [("Commit", f"`{sha}`", True)]
        if subject:
            fields.append(("Subject", subject, False))
        await audit.post_mod_event(
            guild,
            title="Deploy",
            color=discord.Color.green(),
            fields=fields,
        )


async def main() -> None:
    bot = TekkenBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
