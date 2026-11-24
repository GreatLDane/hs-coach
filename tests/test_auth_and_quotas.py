"""Unit tests for hs_coach.auth, multi-user isolation, and Freemium quotas."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from hs_coach.auth import (
    create_session_token,
    hash_password,
    verify_password,
    verify_session_token,
)
from hs_coach.cards import CardDB
from hs_coach.schemas import (
    BoardStateSnapshot,
    CoachReport,
    GameReplay,
    HeroState,
    MatchMetadata,
    MatchPreCheckSummary,
    MulliganReview,
    MulliganSnapshot,
    UsageStats,
)
from hs_coach.storage import GameStore
from hs_coach.web import create_app


def _dummy_replay(game_id: str) -> GameReplay:
    h1 = HeroState(
        hero_class="MAGE",
        card_id="HERO_08",
        name="Jaina Proudmoore",
        hp=30,
        max_hp=30,
        armor=0,
        effective_hp=30,
    )
    h2 = HeroState(
        hero_class="WARRIOR",
        card_id="HERO_01",
        name="Garrosh Hellscream",
        hp=30,
        max_hp=30,
        armor=0,
        effective_hp=30,
    )
    return GameReplay(
        metadata=MatchMetadata(
            game_id=game_id,
            format_type="FT_STANDARD",
            game_type="GT_RANKED",
            is_standard_constructed=True,
            your_class="MAGE",
            opponent_class="WARRIOR",
            your_hero="Jaina Proudmoore",
            opponent_hero="Garrosh Hellscream",
            went_first=True,
            has_coin=False,
            result="win",
            your_terminal_playstate="WON",
            opponent_terminal_playstate="LOST",
            end_reason="lethal",
            total_turns=5,
            your_turns_count=3,
        ),
        mulligan=MulliganSnapshot(went_first=True, has_coin=False),
        turns=[],
        cards_seen={},
        pre_check_summary=MatchPreCheckSummary(),
    )


def _dummy_report(game_id: str, model: str) -> CoachReport:
    return CoachReport(
        game_id=game_id,
        match_summary="Clean game.",
        your_archetype="Tempo Mage",
        opponent_archetype="Control Warrior",
        overall_grade="A",
        key_takeaway="Maintained board control.",
        mulligan_review=MulliganReview(overall_grade="A", summary="Good.", card_verdicts=[]),
        turning_points=[],
        best_plays=[],
        habit_tags=[],
        usage=UsageStats(
            provider="openrouter",
            model=model,
            input_tokens=500,
            output_tokens=150,
            estimated_cost_usd=0.005,
        ),
    )


class TestAuthAndQuotas(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "saas.db"
        self.store = GameStore(self.db_path)
        self.app = create_app(CardDB(), self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_password_hashing_and_session_token_verification(self) -> None:
        hashed = hash_password("hunter2pw")
        self.assertTrue(verify_password("hunter2pw", hashed))
        self.assertFalse(verify_password("wrong", hashed))

        tok = create_session_token("user123", "secret-key")
        self.assertEqual(verify_session_token(tok, "secret-key"), "user123")
        self.assertIsNone(verify_session_token(tok + "x", "secret-key"))
        self.assertIsNone(verify_session_token(tok, "other-secret"))

    def test_legacy_db_migration(self) -> None:
        legacy_path = Path(self._tmpdir.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy_path))
        conn.execute(
            """
            CREATE TABLE games (
                game_id TEXT PRIMARY KEY,
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
                overall_grade TEXT,
                your_archetype TEXT,
                opponent_archetype TEXT,
                model TEXT,
                cost_usd REAL
            )
            """
        )
        conn.commit()
        conn.close()

        migrated = GameStore(legacy_path)
        cols = {
            r["name"]
            for r in migrated._conn.execute("PRAGMA table_info(games)").fetchall()
        }
        self.assertIn("user_id", cols)
        self.assertIn("coached_at", cols)
        migrated.close()

    def test_multi_user_isolation_token_routing_and_freemium_quotas(self) -> None:
        client_a = TestClient(self.app)
        client_b = TestClient(self.app)

        # Register User A (Free plan)
        r_a = client_a.post(
            "/api/auth/register",
            json={"email": "alice@example.com", "password": "passwordA"},
        )
        self.assertEqual(r_a.status_code, 200)
        tok_a = r_a.json()["user"]["upload_token"]
        uid_a = r_a.json()["user"]["user_id"]
        self.assertEqual(r_a.json()["quota"]["monthly_limit"], 5)

        # Register User B (Free plan)
        r_b = client_b.post(
            "/api/auth/register",
            json={"email": "bob@example.com", "password": "passwordB"},
        )
        self.assertEqual(r_b.status_code, 200)

        # Upload 6 games using Alice's Bearer upload_token (no cookie needed)
        anon_client = TestClient(self.app)
        bad_up = anon_client.post(
            "/api/replays",
            content=_dummy_replay("g0").model_dump_json(),
            headers={"Authorization": "Bearer hsc_invalid", "Content-Type": "application/json"},
        )
        self.assertEqual(bad_up.status_code, 401)

        for i in range(6):
            up = anon_client.post(
                "/api/replays",
                content=_dummy_replay(f"g{i}").model_dump_json(),
                headers={"Authorization": f"Bearer {tok_a}", "Content-Type": "application/json"},
            )
            self.assertEqual(up.status_code, 200)

        # Alice sees 6 games; Bob sees 0
        self.assertEqual(len(client_a.get("/api/games").json()), 6)
        self.assertEqual(len(client_b.get("/api/games").json()), 0)
        self.assertEqual(client_b.get("/api/games/g0").status_code, 404)

        # Rotate Alice's upload token -> old token rejected, new token accepted
        rot = client_a.post("/api/auth/rotate-token")
        self.assertEqual(rot.status_code, 200)
        new_tok_a = rot.json()["upload_token"]
        self.assertNotEqual(new_tok_a, tok_a)
        old_up = anon_client.post(
            "/api/replays",
            content=_dummy_replay("g6").model_dump_json(),
            headers={"Authorization": f"Bearer {tok_a}", "Content-Type": "application/json"},
        )
        self.assertEqual(old_up.status_code, 401)

        # Free tier model gating: Pro model rejected with 403
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False):
            forbid = client_a.post(
                "/api/games/g0/coach",
                json={"model": "google/gemini-3.1-pro-preview"},
            )
            self.assertEqual(forbid.status_code, 403)

            # Coach 5 games on Free tier (default Flash model) -> all succeed
            with mock.patch(
                "hs_coach.web.coach_game",
                side_effect=lambda rep, cfg: _dummy_report(rep.metadata.game_id, cfg.model),
            ):
                for i in range(5):
                    ok = client_a.post(f"/api/games/g{i}/coach", json={})
                    self.assertEqual(ok.status_code, 200)

                # 6th coached game hits 402 Payment Required
                capped = client_a.post("/api/games/g5/coach", json={})
                self.assertEqual(capped.status_code, 402)

                # Upgrade Alice to Pro -> unlocks Pro model & 200 monthly quota
                self.store.set_user_plan(uid_a, "pro")
                pro_ok = client_a.post(
                    "/api/games/g5/coach",
                    json={"model": "google/gemini-3.1-pro-preview"},
                )
                self.assertEqual(pro_ok.status_code, 200)

        me = client_a.get("/api/auth/me").json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["quota"]["plan"], "pro")
        self.assertEqual(me["quota"]["used_this_month"], 6)

        # Logout clears session cookie
        client_a.post("/api/auth/logout")
        self.assertFalse(client_a.get("/api/auth/me").json()["authenticated"])


if __name__ == "__main__":
    unittest.main()
