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

-- Per-user /shutup cooldown tracking for The Silencerz role (members who
-- can use /shutup but only once per hour). One row per (user, guild) so a
-- user in multiple servers has independent cooldowns. Moderators bypass
-- this entirely; only consulted for non-mod silencers.
CREATE TABLE IF NOT EXISTS shutup_uses (
    discord_id    INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    last_used_at  TEXT    NOT NULL,
    PRIMARY KEY (discord_id, guild_id)
);

-- Tournaments (spec §8). One row per tournament; multiple concurrent per
-- guild are allowed, but the unique partial index below forbids duplicate
-- names while a tournament is still live.
--   match_format: 'FT2' | 'FT3' — display metadata only; bot tracks match
--     winners, not per-game scores.
--   state: SIGNUPS_OPEN → IN_PROGRESS → COMPLETED, or CANCELLED from any
--     state.
--   signup_channel_id / signup_message_id point to the Join/Leave embed;
--     nullable in case the initial post fails and we need to recover.
CREATE TABLE IF NOT EXISTS tournaments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    organizer_id      INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    match_format      TEXT    NOT NULL,
    max_players       INTEGER,
    state             TEXT    NOT NULL,
    signup_channel_id INTEGER,
    signup_message_id INTEGER,
    created_at        TEXT    NOT NULL,
    started_at        TEXT,
    ended_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_tournaments_guild_state
    ON tournaments(guild_id, state);
CREATE INDEX IF NOT EXISTS idx_tournaments_signup_message
    ON tournaments(signup_message_id);
-- Enforces "unique name per guild while live" without blocking a future
-- tournament from reusing the name after this one ends or is cancelled.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_active_name
    ON tournaments(guild_id, name COLLATE NOCASE)
    WHERE state IN ('SIGNUPS_OPEN', 'IN_PROGRESS');

-- Participants of a tournament. Snapshots display_name and rank_tier at
-- join time so the bracket/history stays stable even if the player later
-- unlinks or re-ranks. forfeited flips on if the player leaves the server
-- or unlinks mid-tournament (slice 2+ handles the consequences).
CREATE TABLE IF NOT EXISTS tournament_participants (
    tournament_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    display_name  TEXT    NOT NULL,
    rank_tier     TEXT,
    joined_at     TEXT    NOT NULL,
    forfeited     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tournament_id, user_id),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_participants_tournament
    ON tournament_participants(tournament_id);

-- Round pairings. Slice 2 adds a state machine on top of the raw
-- (winner_id, reported_at) pair that slice 1 shipped with.
--   state:  PENDING   → no result yet
--           REPORTED  → winner claimed, waiting for loser to confirm
--           DISPUTED  → loser disagreed, awaiting organizer decision
--           CONFIRMED → result locked; counts toward standings
--   reporter_id: who clicked "I won" (so Confirm/Dispute buttons can
--           verify the loser is the one clicking, not the winner).
--   Byes: player_b_id IS NULL, winner_id = player_a_id, state = CONFIRMED
--           (pre-set at pairing time — no report step needed).
CREATE TABLE IF NOT EXISTS tournament_matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id       INTEGER NOT NULL,
    round_number        INTEGER NOT NULL,
    match_number        INTEGER NOT NULL,
    player_a_id         INTEGER,
    player_b_id         INTEGER,
    winner_id           INTEGER,
    reporter_id         INTEGER,
    state               TEXT    NOT NULL DEFAULT 'PENDING',
    reported_at         TEXT,
    report_message_id   INTEGER,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_matches_tournament_round
    ON tournament_matches(tournament_id, round_number);

-- Per-guild cache of custom emoji IDs that /upload-rank-emojis has
-- created (or re-discovered) for each Tekken rank. Keyed by guild +
-- rank_name so the same bot running in multiple guilds can look up
-- the right emoji markdown in each.
CREATE TABLE IF NOT EXISTS guild_rank_emojis (
    guild_id    INTEGER NOT NULL,
    rank_name   TEXT    NOT NULL,
    emoji_id    INTEGER NOT NULL,
    emoji_name  TEXT    NOT NULL,
    uploaded_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, rank_name)
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # One-shot migrations for columns added after initial release.
        # SQLite's CREATE TABLE IF NOT EXISTS doesn't alter existing
        # tables, so new columns go here — wrapped so re-runs are
        # idempotent.
        await _safe_add_column(
            db, "tournament_matches", "state",
            "TEXT NOT NULL DEFAULT 'PENDING'",
        )
        await _safe_add_column(
            db, "tournament_matches", "reporter_id", "INTEGER",
        )
        await _safe_add_column(
            db, "tournament_matches", "report_message_id", "INTEGER",
        )
        # Back-fill state for rows from before the column existed: any
        # row with a winner was effectively CONFIRMED (it predates the
        # state machine entirely).
        await db.execute(
            "UPDATE tournament_matches SET state = 'CONFIRMED' "
            "WHERE state = 'PENDING' AND winner_id IS NOT NULL"
        )
        await db.commit()


async def _safe_add_column(
    db: aiosqlite.Connection, table: str, column: str, coltype: str,
) -> None:
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except aiosqlite.OperationalError as e:
        # aiosqlite surfaces 'duplicate column name' for the re-run
        # case; anything else is a real error and should propagate.
        if "duplicate column" not in str(e).lower():
            raise


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


async def list_all_players():
    # Oldest last_synced first — the rank sweeper processes stale rows
    # before fresh ones, so on a 12h cadence the never-touched players
    # catch up before repeatedly re-syncing active ones. Schema has
    # last_synced NOT NULL so there's no NULL case to worry about.
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM players ORDER BY last_synced ASC"
        ) as cur:
            return await cur.fetchall()


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
# Silencer /shutup cooldown                                                    #
# --------------------------------------------------------------------------- #

async def get_last_shutup_use(discord_id: int, guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT last_used_at FROM shutup_uses WHERE discord_id = ? AND guild_id = ?",
            (discord_id, guild_id),
        ) as cur:
            return await cur.fetchone()


async def record_shutup_use(discord_id: int, guild_id: int, now_iso: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO shutup_uses (discord_id, guild_id, last_used_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                last_used_at = excluded.last_used_at
            """,
            (discord_id, guild_id, now_iso),
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


# --------------------------------------------------------------------------- #
# Tournaments (spec §8)                                                        #
# --------------------------------------------------------------------------- #

async def create_tournament(
    *,
    guild_id: int,
    organizer_id: int,
    name: str,
    match_format: str,
    max_players: int | None,
    now_iso: str,
) -> int:
    """Insert a new tournament row in SIGNUPS_OPEN state and return its id.
    Caller is expected to post the signup message and then call
    set_tournament_signup_message with the resulting channel/message ids."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tournaments (guild_id, organizer_id, name, match_format,
                max_players, state, created_at)
            VALUES (?, ?, ?, ?, ?, 'SIGNUPS_OPEN', ?)
            """,
            (guild_id, organizer_id, name, match_format, max_players, now_iso),
        )
        await db.commit()
        return cur.lastrowid or 0


async def set_tournament_signup_message(
    tournament_id: int, channel_id: int, message_id: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournaments SET signup_channel_id = ?, signup_message_id = ? "
            "WHERE id = ?",
            (channel_id, message_id, tournament_id),
        )
        await db.commit()


async def get_tournament(tournament_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
        ) as cur:
            return await cur.fetchone()


async def get_tournament_by_signup_message(message_id: int):
    """Used by the signup View to resolve the tournament from a button click."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournaments WHERE signup_message_id = ?", (message_id,)
        ) as cur:
            return await cur.fetchone()


async def get_active_tournament_by_name(guild_id: int, name: str):
    """Return a SIGNUPS_OPEN or IN_PROGRESS tournament with this name, if any.
    Used by /tournament-create for a friendly duplicate-name error before we
    hit the partial unique index."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tournaments
            WHERE guild_id = ? AND name = ? COLLATE NOCASE
              AND state IN ('SIGNUPS_OPEN', 'IN_PROGRESS')
            """,
            (guild_id, name),
        ) as cur:
            return await cur.fetchone()


async def list_tournaments(guild_id: int, states: tuple[str, ...] | None = None):
    """List tournaments in a guild, optionally filtered by state(s).
    Ordered newest-first."""
    query = "SELECT * FROM tournaments WHERE guild_id = ?"
    params: list = [guild_id]
    if states:
        placeholders = ",".join("?" * len(states))
        query += f" AND state IN ({placeholders})"
        params.extend(states)
    query += " ORDER BY id DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return await cur.fetchall()


async def update_tournament_state(
    tournament_id: int, state: str, now_iso: str | None = None,
) -> None:
    """Transition a tournament's state. If transitioning to IN_PROGRESS sets
    started_at; to COMPLETED/CANCELLED sets ended_at. now_iso is required
    for transitions that timestamp, ignored otherwise."""
    async with aiosqlite.connect(DB_PATH) as db:
        if state == "IN_PROGRESS":
            await db.execute(
                "UPDATE tournaments SET state = ?, started_at = ? WHERE id = ?",
                (state, now_iso, tournament_id),
            )
        elif state in ("COMPLETED", "CANCELLED"):
            await db.execute(
                "UPDATE tournaments SET state = ?, ended_at = ? WHERE id = ?",
                (state, now_iso, tournament_id),
            )
        else:
            await db.execute(
                "UPDATE tournaments SET state = ? WHERE id = ?",
                (state, tournament_id),
            )
        await db.commit()


async def add_participant(
    *,
    tournament_id: int,
    user_id: int,
    display_name: str,
    rank_tier: str | None,
    now_iso: str,
) -> bool:
    """Insert a participant. Returns True on insert, False if the user is
    already in this tournament."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO tournament_participants
                (tournament_id, user_id, display_name, rank_tier, joined_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tournament_id, user_id, display_name, rank_tier, now_iso),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def remove_participant(tournament_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM tournament_participants "
            "WHERE tournament_id = ? AND user_id = ?",
            (tournament_id, user_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def get_participant(tournament_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournament_participants "
            "WHERE tournament_id = ? AND user_id = ?",
            (tournament_id, user_id),
        ) as cur:
            return await cur.fetchone()


async def list_participants(tournament_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournament_participants "
            "WHERE tournament_id = ? ORDER BY joined_at",
            (tournament_id,),
        ) as cur:
            return await cur.fetchall()


async def count_participants(tournament_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM tournament_participants WHERE tournament_id = ?",
            (tournament_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def create_matches(
    tournament_id: int,
    round_number: int,
    matches: list[tuple[int | None, int | None, int | None]],
    now_iso: str | None = None,
) -> None:
    """Bulk-insert pairings for a round. Each tuple is
    (player_a_id, player_b_id, winner_id) in match order — match_number is
    the list index + 1. Byes get state='CONFIRMED' inline (no report
    step needed); regular matches start PENDING."""
    if not matches:
        return
    rows: list[tuple] = []
    for i, (a, b, w) in enumerate(matches):
        is_bye = a is None or b is None
        state = "CONFIRMED" if is_bye else "PENDING"
        reported_at = now_iso if is_bye else None
        rows.append((
            tournament_id, round_number, i + 1,
            a, b, w, state, reported_at,
        ))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """
            INSERT INTO tournament_matches
                (tournament_id, round_number, match_number,
                 player_a_id, player_b_id, winner_id, state, reported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()


async def get_match(match_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournament_matches WHERE id = ?", (match_id,),
        ) as cur:
            return await cur.fetchone()


async def get_match_by_report_message(message_id: int):
    """Find a match by the public 'confirm or dispute' message_id we
    posted for it — how persistent view button callbacks route back to
    the right match after a bot restart."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournament_matches WHERE report_message_id = ?",
            (message_id,),
        ) as cur:
            return await cur.fetchone()


async def find_pending_match_for_user(tournament_id: int, user_id: int):
    """Find the PENDING match this user is a participant in (for the
    'Report a Win' match-picker). Excludes byes (already CONFIRMED),
    excludes matches already in REPORTED / DISPUTED / CONFIRMED state."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tournament_matches
            WHERE tournament_id = ? AND state = 'PENDING'
              AND (player_a_id = ? OR player_b_id = ?)
            ORDER BY round_number DESC, match_number ASC
            """,
            (tournament_id, user_id, user_id),
        ) as cur:
            return await cur.fetchone()


async def set_match_report_message(match_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournament_matches SET report_message_id = ? WHERE id = ?",
            (message_id, match_id),
        )
        await db.commit()


async def report_match_win(
    match_id: int, reporter_id: int, winner_id: int, now_iso: str,
) -> bool:
    """PENDING → REPORTED. Returns True on success, False if the match
    isn't in PENDING state (someone beat us to the claim, or admin
    already set a result)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE tournament_matches
            SET state = 'REPORTED', reporter_id = ?, winner_id = ?, reported_at = ?
            WHERE id = ? AND state = 'PENDING'
            """,
            (reporter_id, winner_id, now_iso, match_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def cancel_match_report(match_id: int, reporter_id: int) -> bool:
    """REPORTED → PENDING. Only the original reporter can undo, and only
    while the match is still REPORTED (loser hasn't touched it yet)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE tournament_matches
            SET state = 'PENDING', reporter_id = NULL, winner_id = NULL, reported_at = NULL
            WHERE id = ? AND state = 'REPORTED' AND reporter_id = ?
            """,
            (match_id, reporter_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def confirm_match_report(match_id: int) -> bool:
    """REPORTED → CONFIRMED. Caller is expected to be the loser; the
    check happens at the cog layer (looks up the non-reporter participant)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE tournament_matches
            SET state = 'CONFIRMED'
            WHERE id = ? AND state = 'REPORTED'
            """,
            (match_id,),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def dispute_match_report(match_id: int) -> bool:
    """REPORTED → DISPUTED."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE tournament_matches SET state = 'DISPUTED'
            WHERE id = ? AND state = 'REPORTED'
            """,
            (match_id,),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def resolve_disputed_match(
    match_id: int, winner_id: int, now_iso: str,
) -> bool:
    """DISPUTED → CONFIRMED with organizer's chosen winner."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE tournament_matches
            SET state = 'CONFIRMED', winner_id = ?, reported_at = ?
            WHERE id = ? AND state = 'DISPUTED'
            """,
            (winner_id, now_iso, match_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def override_match_result(
    match_id: int, winner_id: int, now_iso: str,
) -> None:
    """Any state → CONFIRMED. Used by /tournament-set-result — the
    organizer's escape hatch when something went wrong earlier."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE tournament_matches
            SET state = 'CONFIRMED', winner_id = ?, reported_at = ?
            WHERE id = ?
            """,
            (winner_id, now_iso, match_id),
        )
        await db.commit()


async def list_matches_for_tournament(tournament_id: int):
    """All matches across all rounds — used for Buchholz scoring and
    the final standings render."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tournament_matches
            WHERE tournament_id = ?
            ORDER BY round_number, match_number
            """,
            (tournament_id,),
        ) as cur:
            return await cur.fetchall()


async def list_pending_matches_for_user_in_guild(
    guild_id: int, user_id: int,
):
    """PENDING matches the user is a participant in, across every
    IN_PROGRESS tournament in the guild. Used by the Report-a-Win
    match picker — if 0 matches, the button bails; if 1, we proceed
    direct; if 2+, we show a select menu."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT m.*, t.name AS tournament_name,
                   t.match_format AS tournament_format
            FROM tournament_matches m
            JOIN tournaments t ON t.id = m.tournament_id
            WHERE t.guild_id = ? AND t.state = 'IN_PROGRESS'
              AND m.state = 'PENDING'
              AND (m.player_a_id = ? OR m.player_b_id = ?)
            ORDER BY t.id, m.round_number DESC, m.match_number
            """,
            (guild_id, user_id, user_id),
        ) as cur:
            return await cur.fetchall()


async def is_round_complete(tournament_id: int, round_number: int) -> bool:
    """A round is 'complete' once every match in it has state=CONFIRMED
    (byes pre-count as CONFIRMED). Used to decide whether to auto-advance."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM tournament_matches
            WHERE tournament_id = ? AND round_number = ?
              AND state != 'CONFIRMED'
            """,
            (tournament_id, round_number),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0) == 0


async def delete_fake_players() -> int:
    """Wipe synthetic test-bot rows from the players table (tekken_id
    LIKE 'TEST%'). Used by /tournament-dev-cleanup. Participant snapshots
    in tournament_participants are left alone — they're self-contained
    (display_name + rank_tier stored inline) and harmless."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM players WHERE tekken_id LIKE 'TEST%'"
        )
        await db.commit()
        return cur.rowcount or 0


async def purge_panels_for_guild(guild_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM panels WHERE guild_id = ?", (guild_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def purge_rank_emojis_for_guild(guild_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM guild_rank_emojis WHERE guild_id = ?", (guild_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def purge_tournaments_for_guild(guild_id: int) -> int:
    """Nuke every tournament row for the guild, plus its participants
    and matches. FK cascades aren't enabled globally, so we wipe the
    child tables explicitly. Used by /purge-server because tournament
    rows reference channel IDs that the purge is about to delete."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM tournaments WHERE guild_id = ?", (guild_id,),
        ) as cur:
            ids = [row[0] for row in await cur.fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"DELETE FROM tournament_matches WHERE tournament_id IN ({placeholders})",
            ids,
        )
        await db.execute(
            f"DELETE FROM tournament_participants WHERE tournament_id IN ({placeholders})",
            ids,
        )
        cur = await db.execute(
            "DELETE FROM tournaments WHERE guild_id = ?", (guild_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def set_rank_emoji(
    guild_id: int, rank_name: str,
    emoji_id: int, emoji_name: str, now_iso: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO guild_rank_emojis
                (guild_id, rank_name, emoji_id, emoji_name, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, rank_name) DO UPDATE SET
                emoji_id    = excluded.emoji_id,
                emoji_name  = excluded.emoji_name,
                uploaded_at = excluded.uploaded_at
            """,
            (guild_id, rank_name, emoji_id, emoji_name, now_iso),
        )
        await db.commit()


async def get_rank_emoji(guild_id: int, rank_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM guild_rank_emojis WHERE guild_id = ? AND rank_name = ?",
            (guild_id, rank_name),
        ) as cur:
            return await cur.fetchone()


async def list_rank_emojis(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM guild_rank_emojis WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()


async def list_matches_for_round(tournament_id: int, round_number: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tournament_matches
            WHERE tournament_id = ? AND round_number = ?
            ORDER BY match_number
            """,
            (tournament_id, round_number),
        ) as cur:
            return await cur.fetchall()
