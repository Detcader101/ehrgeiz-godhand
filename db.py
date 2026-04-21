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
