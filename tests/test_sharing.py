"""Unit tests for Milestone 12 shareable unlisted reports and OpenGraph metadata."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from hs_coach.cards import CardDB
from hs_coach.coach import coach_game
from hs_coach.config import AppConfig, validate_app_config
from hs_coach.schemas import (
    BestPlay,
    CoachReport,
    GameReplay,
    HabitTag,
    MatchMetadata,
    MatchPreCheckSummary,
    MulliganReview,
    MulliganSnapshot,
    TurningPoint,
    UsageStats,
)
from hs_coach.storage import GameStore
from hs_coach.web import create_app


def _hosted_cfg() -> AppConfig:
    cfg = AppConfig(
        mode="hosted",
        host="0.0.0.0",
        session_secret="sharing-test-hosted-secret-32chars!!",
        public_url="https://coach.example.com",
        allow_raw_log_upload=False,
        allow_hosted_guests=False,
    )
    validate_app_config(cfg)
    return cfg


def _sample_replay(game_id: str = "share_g1") -> GameReplay:
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
            total_turns=6,
            your_turns_count=3,
        ),
        mulligan=MulliganSnapshot(went_first=True, has_coin=False),
        turns=[],
        cards_seen={},
        pre_check_summary=MatchPreCheckSummary(
            missed_lethal_turns=[], total_mana_floated=2
        ),
    )


def _sample_report(game_id: str = "share_g1") -> CoachReport:
    return CoachReport(
        game_id=game_id,
        match_summary="Tempo Mage closed cleanly on turn 6.",
        your_archetype="Tempo Mage",
        opponent_archetype="Control Warrior",
        overall_grade="A-",
        key_takeaway='Kept board pressure & avoided "greedy" lines <script>',
        mulligan_review=MulliganReview(overall_grade="A", summary="Clean keep.", card_verdicts=[]),
        turning_points=[
            TurningPoint(
                rank=1,
                turn_number=4,
                severity="Inaccuracy",
                title="Floated 2 mana",
                turn_context="Turn 4 board control",
                what_was_played="Hero Power pass",
                better_line="Develop 2-drop",
                why_it_mattered="Minor tempo drop",
                habit_tag="mana-efficiency",
            )
        ],
        best_plays=[
            BestPlay(
                turn_number=6,
                title="Exact burn lethal",
                what_was_played="Fireball + Hero Power face",
                why_it_was_good="Ended game before Warrior stabilized",
            )
        ],
        habit_tags=[
            HabitTag(
                tag="mana-efficiency",
                polarity="mistake",
                turns=[4],
                note="Floated 2 mana on turn 4",
            )
        ],
        usage=UsageStats(
            provider="openrouter",
            model="google/gemini-3.7-flash",
            input_tokens=500,
            output_tokens=160,
            estimated_cost_usd=0.002,
        ),
    )


class TestSharing(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "share.db"
        self.store = GameStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_share_link_lifecycle_isolation_and_opengraph_escaping(self) -> None:
        app = create_app(CardDB(), self.store, config=_hosted_cfg())
        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            TestClient(app) as owner,
            TestClient(app) as other_user,
            TestClient(app) as public_viewer,
        ):
            r1 = owner.post(
                "/api/auth/register",
                json={"email": "owner@example.com", "password": "password123"},
            )
            self.assertEqual(r1.status_code, 200)
            owner_uid = r1.json()["user"]["user_id"]
            self.assertEqual(
                other_user.post(
                    "/api/auth/register",
                    json={"email": "other@example.com", "password": "password123"},
                ).status_code,
                200,
            )

            self.store.upsert_replay(
                _sample_replay("share_g1"),
                user_id=owner_uid,
                completion_trusted=True,
            )

            with mock.patch("hs_coach.web.coach_game", return_value=_sample_report("share_g1")):
                cr = owner.post("/api/games/share_g1/coach", json={})
                self.assertEqual(cr.status_code, 200)

            # Unauthenticated or another user cannot enable sharing on owner's game
            self.assertEqual(
                public_viewer.post("/api/games/share_g1/share", json={"enabled": True}).status_code,
                401,
            )
            self.assertEqual(
                other_user.post("/api/games/share_g1/share", json={"enabled": True}).status_code,
                404,
            )

            # Owner enables share link (reuses same token on repeat call)
            s1 = owner.post("/api/games/share_g1/share", json={"enabled": True})
            self.assertEqual(s1.status_code, 200)
            d1 = s1.json()
            self.assertTrue(d1["shared"])
            tok = d1["share_token"]
            self.assertEqual(d1["share_url"], f"https://coach.example.com/shared/{tok}")

            s2 = owner.post("/api/games/share_g1/share", json={"enabled": True})
            self.assertEqual(s2.status_code, 200)
            self.assertEqual(s2.json()["share_token"], tok)

            # Dashboard list reflects shared status
            games_list = owner.get("/api/games").json()
            self.assertTrue(games_list[0]["shared"])
            self.assertEqual(games_list[0]["share_token"], tok)

            # Public unauthenticated viewer can read /shared/{tok} with escaped OpenGraph metadata
            pub = public_viewer.get(f"/shared/{tok}")
            self.assertEqual(pub.status_code, 200)
            self.assertIn('<meta property="og:type" content="article" />', pub.text)
            self.assertIn("MAGE vs WARRIOR (WIN · Grade A-) — HS-Coach Report", pub.text)
            self.assertIn('<meta name="twitter:card" content="summary" />', pub.text)
            self.assertIn("Try HS-Coach Free", pub.text)
            self.assertIn("Exact burn lethal", pub.text)
            self.assertNotIn("<script>", pub.text)
            self.assertIn("&lt;script&gt;", pub.text)

            # Another user cannot disable sharing; owner disabling returns 404 for public viewer
            self.assertEqual(
                other_user.post("/api/games/share_g1/share", json={"enabled": False}).status_code,
                404,
            )
            revoked = owner.post("/api/games/share_g1/share", json={"enabled": False})
            self.assertEqual(revoked.status_code, 200)
            self.assertFalse(revoked.json()["shared"])
            self.assertEqual(public_viewer.get(f"/shared/{tok}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
