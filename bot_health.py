"""Bot health HTTP endpoint.

Closes the operational gap where Uptime Kuma watches `node_exporter`
on `shed-tekken` (which catches host-down) but doesn't notice when the
bot's Python process is alive but disconnected from Discord — a
gateway crashloop, a stuck heartbeat, or a missing `DISCORD_TOKEN`
won't tip the host's `9100` exporter.

Endpoints (all GET, no auth — this listener is bound to localhost so
only the local Kuma probe and admins SSH-in can reach it):

  /healthz   — 200 OK when ready + gateway latency under 5s,
               503 SERVICE UNAVAILABLE otherwise. Body is a JSON
               object the human can read at a glance.

  /metrics   — Plain-text counters (guild count, total members,
               cumulative players, fitcheck count). Intended for
               quick `curl` debugging more than Prometheus scraping;
               we deliberately don't ship a metrics-format library.

Disabled by default. Set BOT_HEALTH_PORT=<int> in the env to enable;
BOT_HEALTH_HOST overrides the bind address (defaults to 127.0.0.1
since exposing the bot's internals to the LAN is unnecessary risk
for a local liveness probe).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from aiohttp import web

import db

log = logging.getLogger(__name__)


def _enabled_port() -> int | None:
    raw = os.environ.get("BOT_HEALTH_PORT")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "BOT_HEALTH_PORT=%r isn't an int — health endpoint disabled", raw,
        )
        return None


def _bind_host() -> str:
    return os.environ.get("BOT_HEALTH_HOST", "127.0.0.1")


def _build_status(bot: Any) -> dict:
    """Return the structured status used by /healthz. Separated so the
    metrics endpoint can reuse the same readiness signal."""
    is_ready = bool(bot.is_ready())
    latency_seconds = bot.latency  # discord.py exposes seconds-as-float
    healthy = is_ready and latency_seconds is not None and latency_seconds < 5.0
    return {
        "healthy": healthy,
        "ready": is_ready,
        "latency_ms": (
            round(latency_seconds * 1000, 1)
            if latency_seconds is not None and latency_seconds == latency_seconds
            else None
        ),
        "guilds": len(bot.guilds) if is_ready else None,
        "user": str(bot.user) if bot.user else None,
        "git_sha": os.environ.get("BOT_GIT_SHA"),
    }


async def _healthz(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    payload = _build_status(bot)
    status = 200 if payload["healthy"] else 503
    return web.json_response(payload, status=status)


async def _metrics(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    status = _build_status(bot)

    # Total verified players + fit-check entries — small DB peek so the
    # metrics page is genuinely useful for "is anything alive in there?"
    # spot-checks. Aggregate-only; never include user identifiers in the
    # response since this endpoint has no auth.
    total_players = 0
    total_fitchecks = 0
    try:
        total_players = len(await db.list_all_players())
    except Exception:
        log.exception("metrics: list_all_players failed")
    # Single COUNT against fitcheck_entries — small + indexed by guild.
    # We deliberately don't go via per-user helpers; this endpoint is
    # for liveness, not analytics, and we want it to stay cheap.
    try:
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM fitcheck_entries"
            ) as cur:
                row = await cur.fetchone()
                if row is not None:
                    total_fitchecks = int(row[0] or 0)
    except Exception:
        log.exception("metrics: fitcheck count failed")

    lines = [
        f'tekken_bot_ready {1 if status["ready"] else 0}',
        f'tekken_bot_healthy {1 if status["healthy"] else 0}',
        f'tekken_bot_latency_ms {status["latency_ms"] or 0}',
        f'tekken_bot_guilds {status["guilds"] or 0}',
        f'tekken_bot_players_total {total_players}',
        f'tekken_bot_fitchecks_total {total_fitchecks}',
    ]
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


class BotHealthServer:
    """Lifecycle manager for the aiohttp listener. Owned by the bot's
    setup_hook so a graceful shutdown stops the listener with everything
    else."""

    def __init__(self, bot: Any, *, host: str, port: int):
        self.bot = bot
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app["bot"] = self.bot
        app.router.add_get("/healthz", _healthz)
        app.router.add_get("/metrics", _metrics)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=self.host, port=self.port)
        await site.start()
        self._runner = runner
        self._site = site
        log.info(
            "[health] listening on http://%s:%d (healthz, metrics)",
            self.host, self.port,
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        log.info("[health] stopped")


async def maybe_start_health_server(bot: Any) -> BotHealthServer | None:
    """Boots the listener if BOT_HEALTH_PORT is set, returning the
    server handle for shutdown. Returns None when disabled — caller
    should treat that as "feature off, skip teardown."""
    port = _enabled_port()
    if port is None:
        return None
    server = BotHealthServer(bot, host=_bind_host(), port=port)
    try:
        await server.start()
    except OSError as e:
        log.warning(
            "[health] couldn't bind %s:%d (%s) — endpoint disabled",
            _bind_host(), port, e,
        )
        return None
    return server
