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
        if self._deploy_announced:
            return
        self._deploy_announced = True
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
