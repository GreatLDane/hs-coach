"""SQLite match history store, multi-user accounts, and cross-game habit aggregator."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hs_coach.cards import CardDB
from hs_coach.schemas import CoachReport, DeckReview, GameReplay

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    upload_token TEXT UNIQUE NOT NULL,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    stripe_last_event_ts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS coach_reservations (
    reservation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    month_key TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_coach_res_active_game
    ON coach_reservations (user_id, game_id) WHERE status = 'reserved';

CREATE INDEX IF NOT EXISTS idx_coach_res_user_month
    ON coach_reservations (user_id, month_key, status);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_ts INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    user_id TEXT NOT NULL DEFAULT 'local',
    game_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    your_class TEXT NOT NULL,
    opponent_class TEXT NOT NULL,
    result TEXT NOT NULL,
    end_reason TEXT NOT NULL,
    total_turns INTEGER NOT NULL,
    went_first INTEGER NOT NULL,
    missed_lethal_count INTEGER NOT NULL,
    total_mana_floated INTEGER NOT NULL,
    burned_count INTEGER NOT NULL DEFAULT 0,
    discarded_count INTEGER NOT NULL DEFAULT 0,
    replay_json TEXT NOT NULL,
    coach_json TEXT,
    coached_at TEXT,
    overall_grade TEXT,
    your_archetype TEXT,
    opponent_archetype TEXT,
    model TEXT,
    cost_usd REAL,
    share_token TEXT,
    completion_trusted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, game_id)
);

CREATE TABLE IF NOT EXISTS deck_reviews (
    user_id TEXT NOT NULL,
    deck_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    model TEXT,
    cost_usd REAL,
    review_json TEXT NOT NULL,
    PRIMARY KEY (user_id, deck_key)
);
"""

# ---- Deck & Pocket-Meta Advisor helpers ----

DECK_MIN_CARDS = 3
DECK_MIN_GAMES = 5
DECK_RECENT_GAMES = 20
_LABEL_BAD_CHARS = re.compile(r"[^A-Za-z0-9 '/-]")


def sanitize_label(value: Any) -> str:
    """LLM/client-derived labels reused in prompts: [A-Za-z0-9 '/-], max 40 chars."""
    if not isinstance(value, str):
        return ""
    return _LABEL_BAD_CHARS.sub("", value).strip()[:40].strip()


def _norm_class(value: Any) -> str:
    cleaned = re.sub(r"[^A-Z_]", "", str(value or "").upper())
    return cleaned or "UNKNOWN"


def _class_title(cls: str) -> str:
    return cls.replace("_", " ").title()


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


# Pull only the deck-relevant parts of each stored replay inside SQLite (json1), so listing
# decks never loads whole replays into Python. Avoids the `#-1` path syntax (needs SQLite
# 3.42+); the production image may ship an older SQLite.
_DECK_GAME_SELECT = """
    SELECT game_id, created_at, your_class, opponent_class, result,
           your_archetype, opponent_archetype,
           json_extract(replay_json, '$.your_deck_observed') AS deck_json,
           json_extract(replay_json, '$.mulligan.offered') AS offered_json,
           json_array_length(replay_json, '$.turns') AS n_turns,
           CASE WHEN json_array_length(replay_json, '$.turns') > 0 THEN
               json_extract(
                   replay_json,
                   '$.turns[' || (json_array_length(replay_json, '$.turns') - 1) || '].end_state.your_hand'
               )
           END AS hand_json,
           coach_json IS NOT NULL AS coached,
           CASE WHEN json_valid(coach_json) THEN json_extract(coach_json, '$.habit_tags') END
               AS habits_json
    FROM games
    WHERE user_id = ? AND json_valid(replay_json)
    ORDER BY created_at ASC, game_id ASC
"""


def _decode_deck_game(row: sqlite3.Row, card_db: CardDB) -> dict[str, Any]:
    """Extract only the deck-relevant, validated facts from one stored game row."""
    deck: dict[str, int] = {}
    for dc in _json_list(row["deck_json"]):
        cid = dc.get("card_id") if isinstance(dc, dict) else None
        cnt = dc.get("count") if isinstance(dc, dict) else None
        if cid not in card_db.by_id or not isinstance(cnt, int) or cnt < 1:
            continue
        deck[cid] = max(deck.get(cid, 0), min(cnt, card_db.max_copies(cid)))
    mull: list[tuple[str, bool]] = []
    for mc in _json_list(row["offered_json"]):
        cid = mc.get("card_id") if isinstance(mc, dict) else None
        if cid in card_db.by_id:
            mull.append((cid, bool(mc.get("kept"))))
    n_turns = row["n_turns"] or 0
    final_hand = sorted(
        {
            h.get("card_id")
            for h in _json_list(row["hand_json"])
            if isinstance(h, dict) and h.get("card_id") in card_db.by_id
        }
    )
    coach: dict[str, Any] | None = None
    if row["coached"]:
        coach = {"habit_tags": _json_list(row["habits_json"])}
    return {
        "game_id": row["game_id"],
        "created_at": row["created_at"],
        "your_class": _norm_class(row["your_class"]),
        "opponent_class": _norm_class(row["opponent_class"]),
        "result": row["result"],
        "your_archetype": sanitize_label(row["your_archetype"]),
        "opponent_archetype": sanitize_label(row["opponent_archetype"]),
        "deck": deck,
        "cards": frozenset(deck),
        "mulligan": mull,
        "final_hand": final_hand,
        "has_turns": n_turns > 0,
        "coach": coach,
    }


def _row_to_user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    keys = row.keys()
    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "plan": row["plan"],
        "upload_token": row["upload_token"],
        "stripe_customer_id": row["stripe_customer_id"] if "stripe_customer_id" in keys else None,
        "stripe_subscription_id": row["stripe_subscription_id"] if "stripe_subscription_id" in keys else None,
        "stripe_last_event_ts": int(row["stripe_last_event_ts"] or 0) if "stripe_last_event_ts" in keys else 0,
        "created_at": row["created_at"],
    }


class GameStore:
    """Thread-safe SQLite repository for users, GameReplays, and CoachReports."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.executescript(_SCHEMA_SQL)
            self._migrate_users_columns()
            self._migrate_games_columns()
            self._conn.commit()

    def _migrate_users_columns(self) -> None:
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "stripe_customer_id" not in cols:
            self._conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        if "stripe_subscription_id" not in cols:
            self._conn.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
        if "stripe_last_event_ts" not in cols:
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN stripe_last_event_ts INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_games_columns(self) -> None:
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(games)").fetchall()
        }
        if "user_id" not in cols:
            self._conn.execute(
                "ALTER TABLE games ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'"
            )
        if "coached_at" not in cols:
            self._conn.execute("ALTER TABLE games ADD COLUMN coached_at TEXT")
        if "share_token" not in cols:
            self._conn.execute("ALTER TABLE games ADD COLUMN share_token TEXT")
        if "completion_trusted" not in cols:
            self._conn.execute(
                "ALTER TABLE games ADD COLUMN completion_trusted INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_games_share_token
            ON games (share_token) WHERE share_token IS NOT NULL
            """
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        """Check SQLite connectivity."""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- User management ----

    def create_user(
        self,
        email: str,
        password_hash: str,
        upload_token: str,
        *,
        plan: str = "free",
    ) -> dict[str, Any]:
        norm_email = email.strip().lower()
        user_id = uuid.uuid4().hex[:16]
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (user_id, email, password_hash, plan, upload_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, norm_email, password_hash, plan, upload_token, created_at),
            )
            self._conn.commit()
        return {
            "user_id": user_id,
            "email": norm_email,
            "password_hash": password_hash,
            "plan": plan,
            "upload_token": upload_token,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
            "stripe_last_event_ts": 0,
            "created_at": created_at,
        }

    # ---- Server-side sessions ----

    def create_session(self, session_id: str, user_id: str, expires_at: int) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (session_id, user_id, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (session_id, user_id, created_at, int(expires_at)),
            )
            self._conn.commit()

    def is_session_active(
        self, session_id: str, user_id: str, *, now_ts: int | None = None
    ) -> bool:
        cur_ts = int(time.time()) if now_ts is None else now_ts
        with self._lock:
            row = self._conn.execute(
                """
                SELECT user_id, expires_at, revoked_at
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return False
        return (
            row["user_id"] == user_id
            and row["revoked_at"] is None
            and int(row["expires_at"]) >= cur_ts
        )

    def revoke_session(self, session_id: str) -> None:
        revoked_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE session_id = ?
                """,
                (revoked_at, session_id),
            )
            self._conn.commit()

    def revoke_all_user_sessions(self, user_id: str) -> int:
        revoked_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, user_id),
            )
            self._conn.commit()
            return cur.rowcount

    def update_user_password(self, user_id: str, new_password_hash: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE user_id = ?",
                (new_password_hash, user_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown user_id: {user_id}")
            self._conn.commit()

    def create_password_reset(
        self, user_id: str, token_hash: str, expires_at: int
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO password_resets (token_hash, user_id, expires_at, used_at, created_at)
                VALUES (?, ?, ?, NULL, ?)
                """,
                (token_hash, user_id, int(expires_at), created_at),
            )
            self._conn.commit()

    def consume_password_reset(
        self,
        token_hash: str,
        new_password_hash: str,
        *,
        now_ts: int | None = None,
    ) -> str | None:
        """Atomically redeem a valid reset token, update password, and revoke active sessions."""
        cur_ts = int(time.time()) if now_ts is None else now_ts
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    """
                    SELECT user_id, expires_at, used_at
                    FROM password_resets
                    WHERE token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
                if (
                    row is None
                    or row["used_at"] is not None
                    or int(row["expires_at"]) < cur_ts
                ):
                    self._conn.rollback()
                    return None

                uid = str(row["user_id"])
                cur_res = self._conn.execute(
                    """
                    UPDATE password_resets
                    SET used_at = ?
                    WHERE token_hash = ? AND used_at IS NULL
                    """,
                    (now_iso, token_hash),
                )
                if cur_res.rowcount != 1:
                    self._conn.rollback()
                    return None

                self._conn.execute(
                    "UPDATE users SET password_hash = ? WHERE user_id = ?",
                    (new_password_hash, uid),
                )
                self._conn.execute(
                    """
                    UPDATE sessions
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (now_iso, uid),
                )
                self._conn.commit()
                return uid
            except Exception:
                self._conn.rollback()
                raise

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        norm = email.strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email = ?", (norm,)
            ).fetchone()
        return _row_to_user(row)

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return _row_to_user(row)

    def get_user_by_upload_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE upload_token = ?", (token,)
            ).fetchone()
        return _row_to_user(row)

    def rotate_upload_token(self, user_id: str, new_token: str) -> str:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET upload_token = ? WHERE user_id = ?",
                (new_token, user_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown user_id: {user_id}")
            self._conn.commit()
        return new_token

    def set_user_plan(self, user_id: str, plan: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET plan = ? WHERE user_id = ?", (plan, user_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown user_id: {user_id}")
            self._conn.commit()

    def update_user_billing(
        self,
        user_id: str,
        *,
        plan: str,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
        last_event_ts: int | None = None,
    ) -> None:
        with self._lock:
            if last_event_ts is not None:
                cur = self._conn.execute(
                    """
                    UPDATE users
                    SET plan = ?,
                        stripe_customer_id = COALESCE(?, stripe_customer_id),
                        stripe_subscription_id = ?,
                        stripe_last_event_ts = MAX(COALESCE(stripe_last_event_ts, 0), ?)
                    WHERE user_id = ?
                    """,
                    (
                        plan,
                        stripe_customer_id,
                        stripe_subscription_id,
                        int(last_event_ts),
                        user_id,
                    ),
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE users
                    SET plan = ?,
                        stripe_customer_id = COALESCE(?, stripe_customer_id),
                        stripe_subscription_id = ?
                    WHERE user_id = ?
                    """,
                    (plan, stripe_customer_id, stripe_subscription_id, user_id),
                )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown user_id: {user_id}")
            self._conn.commit()

    def is_stripe_event_processed(self, event_id: str) -> bool:
        """Return True if event_id has already been permanently processed."""
        if not event_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM stripe_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return row is not None

    def get_stripe_subscription_state(self, subscription_id: str) -> dict[str, Any] | None:
        """Return persisted status/metadata for a Stripe subscription_id, or None."""
        if not subscription_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT subscription_id, user_id, status, updated_ts, updated_at
                FROM stripe_subscriptions
                WHERE subscription_id = ?
                """,
                (subscription_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "subscription_id": row["subscription_id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "updated_ts": int(row["updated_ts"] or 0),
            "updated_at": row["updated_at"],
        }

    def apply_stripe_event_entitlement(
        self,
        *,
        event_id: str,
        event_type: str,
        created_ts: int,
        user_id: str,
        plan: str,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
        sub_record_id: str | None = None,
        sub_status: str | None = None,
    ) -> str:
        """Atomically reconcile user entitlement, subscription state, and stripe_events.

        Returns one of: 'applied', 'duplicate', 'stale', 'already_canceled'.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        ts_val = max(0, int(created_ts))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if event_id:
                    seen = self._conn.execute(
                        "SELECT 1 FROM stripe_events WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if seen is not None:
                        self._conn.rollback()
                        return "duplicate"

                user_row = self._conn.execute(
                    "SELECT stripe_last_event_ts FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if user_row is None:
                    self._conn.rollback()
                    raise KeyError(f"Unknown user_id: {user_id}")

                existing_ts = int(user_row["stripe_last_event_ts"] or 0)
                if ts_val > 0 and ts_val < existing_ts:
                    if event_id:
                        self._conn.execute(
                            """
                            INSERT OR IGNORE INTO stripe_events (event_id, event_type, created_ts, processed_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (event_id, event_type, ts_val, now_iso),
                        )
                        self._conn.commit()
                    else:
                        self._conn.rollback()
                    return "stale"

                if plan == "pro" and sub_record_id:
                    sub_row = self._conn.execute(
                        "SELECT status FROM stripe_subscriptions WHERE subscription_id = ?",
                        (sub_record_id,),
                    ).fetchone()
                    if sub_row is not None and str(sub_row["status"]).lower() == "canceled":
                        if event_id:
                            self._conn.execute(
                                """
                                INSERT OR IGNORE INTO stripe_events (event_id, event_type, created_ts, processed_at)
                                VALUES (?, ?, ?, ?)
                                """,
                                (event_id, event_type, ts_val, now_iso),
                            )
                            self._conn.commit()
                        else:
                            self._conn.rollback()
                        return "already_canceled"

                if ts_val > 0:
                    self._conn.execute(
                        """
                        UPDATE users
                        SET plan = ?,
                            stripe_customer_id = COALESCE(?, stripe_customer_id),
                            stripe_subscription_id = ?,
                            stripe_last_event_ts = MAX(COALESCE(stripe_last_event_ts, 0), ?)
                        WHERE user_id = ?
                        """,
                        (
                            plan,
                            stripe_customer_id,
                            stripe_subscription_id,
                            ts_val,
                            user_id,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE users
                        SET plan = ?,
                            stripe_customer_id = COALESCE(?, stripe_customer_id),
                            stripe_subscription_id = ?
                        WHERE user_id = ?
                        """,
                        (plan, stripe_customer_id, stripe_subscription_id, user_id),
                    )

                if sub_record_id and sub_status:
                    self._conn.execute(
                        """
                        INSERT INTO stripe_subscriptions (
                            subscription_id, user_id, status, updated_ts, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(subscription_id) DO UPDATE SET
                            user_id = excluded.user_id,
                            status = CASE
                                WHEN stripe_subscriptions.status = 'canceled' THEN 'canceled'
                                WHEN excluded.updated_ts > 0 AND excluded.updated_ts < stripe_subscriptions.updated_ts
                                    THEN stripe_subscriptions.status
                                ELSE excluded.status
                            END,
                            updated_ts = MAX(stripe_subscriptions.updated_ts, excluded.updated_ts),
                            updated_at = excluded.updated_at
                        """,
                        (sub_record_id, user_id, sub_status, ts_val, now_iso),
                    )

                if event_id:
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO stripe_events (event_id, event_type, created_ts, processed_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (event_id, event_type, ts_val, now_iso),
                    )

                self._conn.commit()
                return "applied"
            except Exception:
                self._conn.rollback()
                raise

    def record_stripe_event(
        self, event_id: str, event_type: str, created_ts: int
    ) -> bool:
        """Record Stripe event idempotently; return True if newly recorded, False if duplicate."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO stripe_events (event_id, event_type, created_ts, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, event_type, int(created_ts), now_iso),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def get_user_by_stripe_customer(self, customer_id: str) -> dict[str, Any] | None:
        if not customer_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
            ).fetchone()
        return _row_to_user(row)

    def count_coached_this_month(self, user_id: str) -> int:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._lock:
            res_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM coach_reservations
                WHERE user_id = ?
                  AND month_key = ?
                  AND status IN ('reserved', 'completed')
                """,
                (user_id, month_prefix),
            ).fetchone()
            game_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM games
                WHERE user_id = ?
                  AND coach_json IS NOT NULL
                  AND coached_at LIKE ?
                """,
                (user_id, f"{month_prefix}%"),
            ).fetchone()
        res_cnt = int(res_row["cnt"]) if res_row else 0
        game_cnt = int(game_row["cnt"]) if game_row else 0
        return max(res_cnt, game_cnt)

    def _count_deep_locked(
        self, user_id: str, month_key: str, fast_models: frozenset[str]
    ) -> int:
        """Count this month's deep (non-fast model) coaching runs. Caller must hold self._lock."""
        fast = sorted(fast_models) or [""]
        marks = ",".join("?" for _ in fast)
        res_row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM coach_reservations
            WHERE user_id = ?
              AND month_key = ?
              AND status IN ('reserved', 'completed')
              AND model NOT IN ({marks})
            """,
            (user_id, month_key, *fast),
        ).fetchone()
        game_row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM games
            WHERE user_id = ?
              AND coach_json IS NOT NULL
              AND coached_at LIKE ?
              AND model IS NOT NULL
              AND model NOT IN ({marks})
            """,
            (user_id, f"{month_key}%", *fast),
        ).fetchone()
        return max(
            int(res_row["cnt"]) if res_row else 0,
            int(game_row["cnt"]) if game_row else 0,
        )

    def count_deep_this_month(self, user_id: str, fast_models: frozenset[str]) -> int:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._lock:
            return self._count_deep_locked(user_id, month_key, fast_models)

    def reserve_coaching_slot(
        self,
        user_id: str,
        game_id: str,
        *,
        monthly_limit: int,
        model: str,
        deep_limit: int | None = None,
        fast_models: frozenset[str] = frozenset(),
    ) -> tuple[str | None, str | None]:
        """Atomically reserve one coaching slot inside BEGIN IMMEDIATE; returns (res_id, error_code).

        When deep_limit is set and `model` is not in fast_models, the run also needs a free
        monthly deep-review slot; otherwise returns "deep_quota_exceeded".
        """
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        now_iso = datetime.now(timezone.utc).isoformat()
        reservation_id = uuid.uuid4().hex[:16]
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                active = self._conn.execute(
                    """
                    SELECT reservation_id
                    FROM coach_reservations
                    WHERE user_id = ? AND game_id = ? AND status = 'reserved'
                    """,
                    (user_id, game_id),
                ).fetchone()
                if active is not None:
                    self._conn.rollback()
                    return None, "in_flight"

                res_row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM coach_reservations
                    WHERE user_id = ?
                      AND month_key = ?
                      AND status IN ('reserved', 'completed')
                    """,
                    (user_id, month_key),
                ).fetchone()
                game_row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM games
                    WHERE user_id = ?
                      AND coach_json IS NOT NULL
                      AND coached_at LIKE ?
                    """,
                    (user_id, f"{month_key}%"),
                ).fetchone()
                used = max(
                    int(res_row["cnt"]) if res_row else 0,
                    int(game_row["cnt"]) if game_row else 0,
                )
                if used >= monthly_limit:
                    self._conn.rollback()
                    return None, "quota_exceeded"

                if deep_limit is not None and model not in fast_models:
                    deep_used = self._count_deep_locked(user_id, month_key, fast_models)
                    if deep_used >= deep_limit:
                        self._conn.rollback()
                        return None, "deep_quota_exceeded"

                self._conn.execute(
                    """
                    INSERT INTO coach_reservations (
                        reservation_id, user_id, game_id, month_key, status, model, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        user_id,
                        game_id,
                        month_key,
                        model,
                        now_iso,
                        now_iso,
                    ),
                )
                self._conn.commit()
                return reservation_id, None
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None, "in_flight"
            except Exception:
                self._conn.rollback()
                raise

    def complete_coaching_reservation(self, reservation_id: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE coach_reservations
                SET status = 'completed', updated_at = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (now_iso, reservation_id),
            )
            self._conn.commit()

    def release_coaching_reservation(self, reservation_id: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE coach_reservations
                SET status = 'released', updated_at = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (now_iso, reservation_id),
            )
            self._conn.commit()

    def release_stale_reservations(self) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE coach_reservations
                SET status = 'released', updated_at = ?
                WHERE status = 'reserved'
                """,
                (now_iso,),
            )
            self._conn.commit()
            return cur.rowcount

    # ---- Games & Coaching ----

    def upsert_replay(
        self,
        replay: GameReplay,
        *,
        user_id: str = "local",
        completion_trusted: bool = False,
    ) -> bool:
        """Insert a new replay or update replay fields without clobbering existing coaching."""
        m = replay.metadata
        ps = replay.pre_check_summary
        now_iso = datetime.now(timezone.utc).isoformat()
        replay_json = replay.model_dump_json()
        trusted_flag = 1 if completion_trusted else 0

        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM games WHERE user_id = ? AND game_id = ?",
                (user_id, m.game_id),
            )
            exists = cur.fetchone() is not None
            if exists:
                self._conn.execute(
                    """
                    UPDATE games
                    SET your_class = ?,
                        opponent_class = ?,
                        result = ?,
                        end_reason = ?,
                        total_turns = ?,
                        went_first = ?,
                        missed_lethal_count = ?,
                        total_mana_floated = ?,
                        burned_count = ?,
                        discarded_count = ?,
                        replay_json = ?,
                        completion_trusted = ?
                    WHERE user_id = ? AND game_id = ?
                    """,
                    (
                        m.your_class,
                        m.opponent_class,
                        m.result,
                        m.end_reason,
                        m.total_turns,
                        int(m.went_first),
                        len(ps.missed_lethal_turns),
                        ps.total_mana_floated,
                        len(ps.your_cards_burned),
                        len(ps.your_cards_discarded),
                        replay_json,
                        trusted_flag,
                        user_id,
                        m.game_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO games (
                        user_id, game_id, created_at, your_class, opponent_class, result,
                        end_reason, total_turns, went_first, missed_lethal_count,
                        total_mana_floated, burned_count, discarded_count, replay_json,
                        completion_trusted
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        m.game_id,
                        now_iso,
                        m.your_class,
                        m.opponent_class,
                        m.result,
                        m.end_reason,
                        m.total_turns,
                        int(m.went_first),
                        len(ps.missed_lethal_turns),
                        ps.total_mana_floated,
                        len(ps.your_cards_burned),
                        len(ps.your_cards_discarded),
                        replay_json,
                        trusted_flag,
                    ),
                )
            self._conn.commit()
            return not exists

    def is_completion_trusted(
        self, game_id: str, *, user_id: str = "local"
    ) -> bool:
        """Return True if stored game came from a trusted post-game completion path."""
        with self._lock:
            row = self._conn.execute(
                "SELECT completion_trusted FROM games WHERE user_id = ? AND game_id = ?",
                (user_id, game_id),
            ).fetchone()
        return bool(row and row["completion_trusted"])

    def save_coach_report(
        self, game_id: str, report: CoachReport, *, user_id: str = "local"
    ) -> None:
        model_name = report.usage.model if report.usage else None
        cost_usd = report.usage.estimated_cost_usd if report.usage else None
        coached_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE games
                SET coach_json = ?,
                    coached_at = ?,
                    overall_grade = ?,
                    your_archetype = ?,
                    opponent_archetype = ?,
                    model = ?,
                    cost_usd = ?
                WHERE user_id = ? AND game_id = ?
                """,
                (
                    report.model_dump_json(),
                    coached_at,
                    report.overall_grade,
                    report.your_archetype,
                    report.opponent_archetype,
                    model_name,
                    cost_usd,
                    user_id,
                    game_id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown game_id {game_id!r} for user {user_id!r}")
            self._conn.commit()

    def list_games(self, *, user_id: str = "local") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT game_id, created_at, your_class, opponent_class, result,
                       end_reason, total_turns, went_first, missed_lethal_count,
                       total_mana_floated, burned_count, discarded_count,
                       overall_grade, your_archetype, opponent_archetype, model, cost_usd,
                       share_token, completion_trusted,
                       CASE WHEN coach_json IS NOT NULL THEN 1 ELSE 0 END AS coached
                FROM games
                WHERE user_id = ?
                ORDER BY rowid DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            {
                "game_id": r["game_id"],
                "created_at": r["created_at"],
                "your_class": r["your_class"],
                "opponent_class": r["opponent_class"],
                "result": r["result"],
                "end_reason": r["end_reason"],
                "total_turns": r["total_turns"],
                "went_first": bool(r["went_first"]),
                "missed_lethal_count": r["missed_lethal_count"],
                "total_mana_floated": r["total_mana_floated"],
                "burned_count": r["burned_count"],
                "discarded_count": r["discarded_count"],
                "overall_grade": r["overall_grade"],
                "your_archetype": r["your_archetype"],
                "opponent_archetype": r["opponent_archetype"],
                "model": r["model"],
                "cost_usd": r["cost_usd"],
                "share_token": r["share_token"],
                "shared": bool(r["share_token"]),
                "completion_trusted": bool(r["completion_trusted"]),
                "coached": bool(r["coached"]),
            }
            for r in rows
        ]

    def get_game(
        self, game_id: str, *, user_id: str = "local"
    ) -> tuple[GameReplay, CoachReport | None] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT replay_json, coach_json FROM games WHERE user_id = ? AND game_id = ?",
                (user_id, game_id),
            ).fetchone()
        if row is None:
            return None
        replay = GameReplay.model_validate_json(row["replay_json"])
        report = (
            CoachReport.model_validate_json(row["coach_json"])
            if row["coach_json"]
            else None
        )
        return replay, report

    def get_game_share_token(
        self, user_id: str, game_id: str
    ) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT share_token FROM games WHERE user_id = ? AND game_id = ?",
                (user_id, game_id),
            ).fetchone()
        return str(row["share_token"]) if row and row["share_token"] else None

    def set_game_share_token(
        self, user_id: str, game_id: str, share_token: str | None
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE games SET share_token = ? WHERE user_id = ? AND game_id = ?",
                (share_token, user_id, game_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_game_by_share_token(
        self, share_token: str
    ) -> tuple[GameReplay, CoachReport | None] | None:
        if not share_token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT replay_json, coach_json FROM games WHERE share_token = ?",
                (share_token,),
            ).fetchone()
        if row is None:
            return None
        replay = GameReplay.model_validate_json(row["replay_json"])
        report = (
            CoachReport.model_validate_json(row["coach_json"])
            if row["coach_json"]
            else None
        )
        return replay, report

    def get_habit_summary(self, *, user_id: str = "local") -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT your_class, your_archetype, opponent_class, result,
                       missed_lethal_count, total_mana_floated,
                       burned_count, discarded_count, coach_json, cost_usd
                FROM games
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()

        total_games = len(rows)
        wins = sum(1 for r in rows if r["result"] == "win")
        losses = sum(1 for r in rows if r["result"] == "loss")
        ties = sum(1 for r in rows if r["result"] == "tie")
        coached_count = sum(1 for r in rows if r["coach_json"])
        total_missed_lethal = sum(r["missed_lethal_count"] for r in rows)
        total_floated = sum(r["total_mana_floated"] for r in rows)
        total_burned = sum(r["burned_count"] for r in rows)
        total_discarded = sum(r["discarded_count"] for r in rows)
        total_cost = round(
            sum(r["cost_usd"] for r in rows if r["cost_usd"] is not None), 6
        )

        matchups: dict[str, dict[str, int]] = {}
        decks: dict[str, dict[str, Any]] = {}
        mistake_counts: dict[str, dict[str, Any]] = {}
        strength_counts: dict[str, dict[str, Any]] = {}

        for r in rows:
            oc = r["opponent_class"]
            m_rec = matchups.setdefault(oc, {"games": 0, "wins": 0, "losses": 0, "ties": 0})
            m_rec["games"] += 1
            if r["result"] == "win":
                m_rec["wins"] += 1
            elif r["result"] == "loss":
                m_rec["losses"] += 1
            else:
                m_rec["ties"] += 1

            deck_label = r["your_archetype"] or f"Uncoached {r['your_class']}"
            d_rec = decks.setdefault(
                deck_label,
                {
                    "deck_label": deck_label,
                    "your_class": r["your_class"],
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "floated": 0,
                },
            )
            d_rec["games"] += 1
            if r["result"] == "win":
                d_rec["wins"] += 1
            elif r["result"] == "loss":
                d_rec["losses"] += 1
            d_rec["floated"] += r["total_mana_floated"]

            if r["coach_json"]:
                cdata = json.loads(r["coach_json"])
                for ht in cdata.get("habit_tags", []):
                    tag = ht.get("tag", "").strip()
                    if not tag:
                        continue
                    bucket = (
                        strength_counts
                        if ht.get("polarity") == "strength"
                        else mistake_counts
                    )
                    entry = bucket.setdefault(tag, {"tag": tag, "count": 0, "latest_note": ""})
                    entry["count"] += 1
                    if ht.get("note"):
                        entry["latest_note"] = ht["note"]

        matchup_list = [
            {
                "opponent_class": oc,
                **stats,
                "win_rate": round(stats["wins"] / stats["games"] * 100, 1),
            }
            for oc, stats in sorted(
                matchups.items(), key=lambda kv: (-kv[1]["games"], kv[0])
            )
        ]

        deck_list = [
            {
                "deck_label": d["deck_label"],
                "your_class": d["your_class"],
                "games": d["games"],
                "wins": d["wins"],
                "losses": d["losses"],
                "win_rate": round(d["wins"] / d["games"] * 100, 1),
                "avg_mana_floated": round(d["floated"] / d["games"], 2),
            }
            for d in sorted(decks.values(), key=lambda x: (-x["games"], x["deck_label"]))
        ]

        return {
            "total_games": total_games,
            "coached_games": coached_count,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate": round((wins / total_games * 100), 1) if total_games else 0.0,
            "avg_mana_floated": round(total_floated / total_games, 2) if total_games else 0.0,
            "total_missed_lethal": total_missed_lethal,
            "total_burned": total_burned,
            "total_discarded": total_discarded,
            "total_cost_usd": total_cost,
            "decks": deck_list,
            "matchups": matchup_list,
            "top_mistakes": sorted(
                mistake_counts.values(), key=lambda x: (-x["count"], x["tag"])
            ),
            "top_strengths": sorted(
                strength_counts.values(), key=lambda x: (-x["count"], x["tag"])
            ),
        }


    # ---- Deck & Pocket-Meta Advisor ----

    def _build_deck_clusters(self, user_id: str, card_db: CardDB) -> list[dict[str, Any]]:
        """Group a user's games into decks: one deck per class (oldest game first).

        Card-overlap clustering was tried and rejected: each game only reveals 8-16 of the
        30 cards, so overlap rules split one real deck into many (see checks.md, review).
        """
        with self._lock:
            rows = self._conn.execute(_DECK_GAME_SELECT, (user_id,)).fetchall()
        by_class: dict[str, dict[str, Any]] = {}
        for row in rows:
            g = _decode_deck_game(row, card_db)
            cls = g["your_class"]
            cluster = by_class.get(cls)
            if cluster is None:
                cluster = {
                    "deck_key": f"{cls}-main",
                    "your_class": cls,
                    "games": [],
                }
                by_class[cls] = cluster
            cluster["games"].append(g)
        for cluster in by_class.values():
            recent = cluster["games"][-DECK_RECENT_GAMES:]
            cluster["unknown"] = len(self._observed_decklist(recent, card_db)) < DECK_MIN_CARDS
        return list(by_class.values())

    @staticmethod
    def _cluster_display_name(cluster: dict[str, Any]) -> str:
        counts: dict[str, int] = {}
        for g in cluster["games"][-DECK_RECENT_GAMES:]:
            if g["coach"] is not None and g["your_archetype"]:
                counts[g["your_archetype"]] = counts.get(g["your_archetype"], 0) + 1
        if counts:
            return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return _class_title(cluster["your_class"])

    @staticmethod
    def _observed_decklist(games: list[dict[str, Any]], card_db: CardDB) -> dict[str, int]:
        """Max observed count per card over the given games (recent window, so old lists fade)."""
        merged: dict[str, int] = {}
        for g in games:
            for cid, cnt in g["deck"].items():
                merged[cid] = max(merged.get(cid, 0), min(cnt, card_db.max_copies(cid)))
        return merged

    @staticmethod
    def _record(games: list[dict[str, Any]]) -> dict[str, Any]:
        wins = sum(1 for g in games if g["result"] == "win")
        losses = sum(1 for g in games if g["result"] == "loss")
        ties = sum(1 for g in games if g["result"] == "tie")
        total = len(games)
        return {
            "games": total,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
        }

    def list_deck_clusters(self, user_id: str, card_db: CardDB) -> list[dict[str, Any]]:
        """Summaries of the user's decks (newest activity first). Every query filters by user_id."""
        out: list[dict[str, Any]] = []
        for cl in self._build_deck_clusters(user_id, card_db):
            games = cl["games"]
            recent_games = games[-DECK_RECENT_GAMES:]
            decklist = self._observed_decklist(recent_games, card_db)
            n = len(games)
            eligible = (not cl["unknown"]) and n >= DECK_MIN_GAMES
            opp_counts: dict[str, int] = {}
            for g in recent_games:
                opp_counts[g["opponent_class"]] = opp_counts.get(g["opponent_class"], 0) + 1
            top_opp = sorted(opp_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            out.append(
                {
                    "deck_key": cl["deck_key"],
                    "display_name": (
                        f"{_class_title(cl['your_class'])} (too few cards seen)"
                        if cl["unknown"]
                        else self._cluster_display_name(cl)
                    ),
                    "your_class": cl["your_class"],
                    "games": n,
                    "coached_games": sum(1 for g in games if g["coach"] is not None),
                    "observed_total": min(30, sum(decklist.values())),
                    "pocket_meta": {
                        **self._record(recent_games),
                        "top_opponent_classes": [
                            {"opponent_class": c, "games": k} for c, k in top_opp
                        ],
                    },
                    "eligible": eligible,
                    "games_needed": 0 if eligible else max(0, DECK_MIN_GAMES - n),
                    "unreviewable": bool(cl["unknown"]),
                    "latest_review": self.get_deck_review(user_id, cl["deck_key"]),
                    "_last_game": games[-1]["created_at"],
                }
            )
        out.sort(key=lambda d: (d["_last_game"], d["deck_key"]), reverse=True)
        for d in out:
            d.pop("_last_game", None)
        return out

    def aggregate_deck(
        self, user_id: str, deck_key: str, card_db: CardDB, *, recent: int = DECK_RECENT_GAMES
    ) -> dict[str, Any] | None:
        """Aggregate decklist, pocket meta, per-card signals and habits for one of the user's decks."""
        cluster = next(
            (c for c in self._build_deck_clusters(user_id, card_db) if c["deck_key"] == deck_key),
            None,
        )
        if cluster is None:
            return None
        games = cluster["games"]
        window = games[-recent:] if recent > 0 else games
        decklist = self._observed_decklist(window, card_db)

        opp_classes: dict[str, int] = {}
        opp_archetypes: dict[str, int] = {}
        kept: dict[str, int] = {}
        tossed: dict[str, int] = {}
        dead: dict[str, int] = {}
        mistakes: dict[str, int] = {}
        strengths: dict[str, int] = {}
        for g in window:
            opp_classes[g["opponent_class"]] = opp_classes.get(g["opponent_class"], 0) + 1
            if g["coach"] is not None and g["opponent_archetype"]:
                a = g["opponent_archetype"]
                opp_archetypes[a] = opp_archetypes.get(a, 0) + 1
            for cid, was_kept in g["mulligan"]:
                bucket = kept if was_kept else tossed
                bucket[cid] = bucket.get(cid, 0) + 1
            if g["result"] == "loss" and g["has_turns"]:
                for cid in g["final_hand"]:
                    dead[cid] = dead.get(cid, 0) + 1
            if g["coach"] is not None:
                for ht in g["coach"].get("habit_tags") or []:
                    if not isinstance(ht, dict):
                        continue
                    tag = sanitize_label(ht.get("tag"))
                    if not tag:
                        continue
                    bucket = strengths if ht.get("polarity") == "strength" else mistakes
                    bucket[tag] = bucket.get(tag, 0) + 1

        def _top(d: dict[str, int], n: int | None = None) -> list[dict[str, Any]]:
            items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
            return [{"name": k, "count": v} for k, v in (items[:n] if n else items)]

        return {
            "deck_key": deck_key,
            "your_class": cluster["your_class"],
            "display_name": self._cluster_display_name(cluster),
            "unreviewable": bool(cluster["unknown"]),
            "games": len(games),
            "coached_games": sum(1 for g in games if g["coach"] is not None),
            "sample_size": len(window),
            "decklist": dict(sorted(decklist.items())),
            "observed_total": min(30, sum(decklist.values())),
            "pocket_meta": {
                **self._record(window),
                "opponent_classes": _top(opp_classes),
                "opponent_archetypes": _top(opp_archetypes),
            },
            "signals": {
                "mulligan_kept": dict(sorted(kept.items())),
                "mulligan_tossed": dict(sorted(tossed.items())),
                "dead_in_hand_losses": dict(sorted(dead.items())),
            },
            "habits": {
                "mistakes": _top(mistakes, 5),
                "strengths": _top(strengths, 5),
            },
        }

    def save_deck_review(self, user_id: str, deck_key: str, review: DeckReview) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        model = review.usage.model if review.usage else None
        cost = review.usage.estimated_cost_usd if review.usage else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO deck_reviews (
                    user_id, deck_key, created_at, sample_size, model, cost_usd, review_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, deck_key) DO UPDATE SET
                    created_at = excluded.created_at,
                    sample_size = excluded.sample_size,
                    model = excluded.model,
                    cost_usd = excluded.cost_usd,
                    review_json = excluded.review_json
                """,
                (
                    user_id,
                    deck_key,
                    created_at,
                    review.sample_size,
                    model,
                    cost,
                    review.model_dump_json(),
                ),
            )
            self._conn.commit()

    def get_deck_review(self, user_id: str, deck_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT created_at, sample_size, model, cost_usd, review_json
                FROM deck_reviews
                WHERE user_id = ? AND deck_key = ?
                """,
                (user_id, deck_key),
            ).fetchone()
        if row is None:
            return None
        try:
            review = json.loads(row["review_json"])
        except ValueError:
            return None
        review["created_at"] = row["created_at"]
        return review
