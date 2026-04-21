import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    discord_id   INTEGER PRIMARY KEY,
    tekken_id    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    main_char    TEXT,
    rating_mu    REAL,
    rank_tier    TEXT,
    last_synced  TEXT NOT NULL,
    linked_by    INTEGER
);

CREATE TABLE IF NOT EXISTS warnings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  INTEGER NOT NULL,
    issued_by   INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    issued_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_warnings_discord_id ON warnings(discord_id);

-- Bot-managed persistent panels. One row per (guild, kind) so a repost deletes
-- the previous panel before creating a new one.
CREATE TABLE IF NOT EXISTS panels (
    guild_id    INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);

-- Records the last time a Discord user unlinked. Used to enforce the relink
-- cooldown (spec §5.2). One row per Discord user; later unlinks replace the
-- previous row. We keep `tekken_id` so a same-ID re-link can be allowed
-- immediately (only different-ID re-links wait out the cooldown).
CREATE TABLE IF NOT EXISTS unlinks (
    discord_id   INTEGER PRIMARY KEY,
    tekken_id    TEXT,
    unlinked_at  TEXT NOT NULL
);

-- High-rank claims (Tekken King and above) sit here until an organizer
-- confirms or rejects them. Spec §5.3.
--   message_id/channel_id point to the audit-log post that hosts the
--   Confirm/Reject buttons; both are nullable in case the post fails.
--   expired_at is set by the 72h sweeper (NULL = still pending).
CREATE TABLE IF NOT EXISTS pending_verifications (
    discord_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    tekken_id    TEXT    NOT NULL,
    rank_tier    TEXT    NOT NULL,
    rank_source  TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    message_id   INTEGER,
    channel_id   INTEGER,
    expired_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_message ON pending_verifications(message_id);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_player_by_discord(discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return await cur.fetchone()


async def get_player_by_tekken_id(tekken_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM players WHERE tekken_id = ? COLLATE NOCASE", (tekken_id,)
        ) as cur:
            return await cur.fetchone()


async def upsert_player(
    discord_id: int,
    tekken_id: str,
    display_name: str,
    main_char: str | None,
    rating_mu: float | None,
    rank_tier: str | None,
    linked_by: int | None,
    now_iso: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO players (discord_id, tekken_id, display_name, main_char,
                                 rating_mu, rank_tier, last_synced, linked_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                tekken_id    = excluded.tekken_id,
                display_name = excluded.display_name,
                main_char    = excluded.main_char,
                rating_mu    = excluded.rating_mu,
                rank_tier    = excluded.rank_tier,
                last_synced  = excluded.last_synced,
                linked_by    = excluded.linked_by
            """,
            (discord_id, tekken_id, display_name, main_char, rating_mu,
             rank_tier, now_iso, linked_by),
        )
        await db.commit()


async def delete_player(discord_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM players WHERE discord_id = ?", (discord_id,))
        await db.commit()


# --------------------------------------------------------------------------- #
# Unlinks (relink cooldown)                                                    #
# --------------------------------------------------------------------------- #

async def record_unlink(discord_id: int, tekken_id: str | None, now_iso: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO unlinks (discord_id, tekken_id, unlinked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                tekken_id   = excluded.tekken_id,
                unlinked_at = excluded.unlinked_at
            """,
            (discord_id, tekken_id, now_iso),
        )
        await db.commit()


async def get_last_unlink(discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM unlinks WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return await cur.fetchone()


async def clear_unlink(discord_id: int) -> None:
    """Drop the cooldown record (used when admin force-links — admins know
    their server)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM unlinks WHERE discord_id = ?", (discord_id,))
        await db.commit()


async def purge_unlinks_before(cutoff_iso: str) -> int:
    """Delete unlinks rows whose unlinked_at is older than the cutoff.
    Used by the sweeper to drop expired cooldown records — once the cooldown
    has elapsed the row has no behavioural effect, so holding the
    discord_id↔tekken_id pairing is needless data retention.
    Returns the number of rows deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM unlinks WHERE unlinked_at < ?", (cutoff_iso,)
        )
        await db.commit()
        return cur.rowcount or 0


# --------------------------------------------------------------------------- #
# Pending verifications (spec §5.3)                                            #
# --------------------------------------------------------------------------- #

async def upsert_pending_verification(
    *,
    discord_id: int,
    guild_id: int,
    tekken_id: str,
    rank_tier: str,
    rank_source: str,
    now_iso: str,
) -> None:
    """Create or replace a pending verification request. Resets expired_at
    and message_id; the caller is expected to set_pending_message after
    posting the audit message."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pending_verifications (discord_id, guild_id, tekken_id,
                rank_tier, rank_source, created_at, message_id, channel_id, expired_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(discord_id) DO UPDATE SET
                guild_id    = excluded.guild_id,
                tekken_id   = excluded.tekken_id,
                rank_tier   = excluded.rank_tier,
                rank_source = excluded.rank_source,
                created_at  = excluded.created_at,
                message_id  = NULL,
                channel_id  = NULL,
                expired_at  = NULL
            """,
            (discord_id, guild_id, tekken_id, rank_tier, rank_source, now_iso),
        )
        await db.commit()


async def set_pending_message(discord_id: int, channel_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_verifications SET channel_id = ?, message_id = ? WHERE discord_id = ?",
            (channel_id, message_id, discord_id),
        )
        await db.commit()


async def get_pending_by_discord(discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_verifications WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return await cur.fetchone()


async def get_pending_by_message(message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_verifications WHERE message_id = ?", (message_id,)
        ) as cur:
            return await cur.fetchone()


async def delete_pending_verification(discord_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM pending_verifications WHERE discord_id = ?", (discord_id,)
        )
        await db.commit()


async def list_stale_pending(created_before_iso: str):
    """Return non-expired rows whose created_at is older than the cutoff
    (i.e. eligible for the 72h stale-marker sweep)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM pending_verifications
            WHERE expired_at IS NULL AND created_at < ?
            """,
            (created_before_iso,),
        ) as cur:
            return await cur.fetchall()


async def mark_pending_expired(discord_id: int, now_iso: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_verifications SET expired_at = ? WHERE discord_id = ?",
            (now_iso, discord_id),
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# Panels                                                                       #
# --------------------------------------------------------------------------- #

async def get_panel(guild_id: int, kind: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM panels WHERE guild_id = ? AND kind = ?",
            (guild_id, kind),
        ) as cur:
            return await cur.fetchone()


async def set_panel(guild_id: int, kind: str, channel_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO panels (guild_id, kind, channel_id, message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, kind) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id
            """,
            (guild_id, kind, channel_id, message_id),
        )
        await db.commit()


async def delete_panel(guild_id: int, kind: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM panels WHERE guild_id = ? AND kind = ?",
            (guild_id, kind),
        )
        await db.commit()
