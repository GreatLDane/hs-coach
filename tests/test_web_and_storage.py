"""Unit tests for hs_coach.storage and hs_coach.web."""

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
from hs_coach.schemas import (
    BestPlay,
    CoachReport,
    HabitTag,
    MulliganReview,
    TurningPoint,
    UsageStats,
)
from hs_coach.storage import GameStore
from hs_coach.web import create_app


def _test_db() -> CardDB:
    return CardDB.from_records(
        [
            {"id": "HERO_08", "dbfId": 1, "name": "Jaina Proudmoore", "type": "HERO", "cardClass": "MAGE", "health": 30},
            {"id": "HERO_01", "dbfId": 2, "name": "Garrosh Hellscream", "type": "HERO", "cardClass": "WARRIOR", "health": 30},
            {"id": "CS2_029", "dbfId": 6, "name": "Fireball", "type": "SPELL", "cost": 4, "cardClass": "MAGE", "text": "Deal $6 damage."},
        ]
    )


def _sample_power_log() -> bytes:
    lines = [
        # Incomplete game (should skip)
        "D 10:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
        "D 10:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        "D 10:00:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
        # Completed Battlegrounds (should skip)
        "D 10:01:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
        "D 10:01:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
        "D 10:01:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
        "D 10:01:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1",
        "D 10:01:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON",
        # Completed Standard game with BattleTags that must be stripped
        "D 10:05:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
        "D 10:05:00.0000000 GameState.DebugPrintPower() - GameEntity EntityID=1",
        "D 10:05:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=2]",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=FIRST_PLAYER value=1",
        "D 10:05:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2 GameAccountId=[hi=1 lo=3]",
        "D 10:05:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=HERO_08",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
        "D 10:05:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=11 CardID=HERO_01",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
        "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
        "D 10:05:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        "D 10:05:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
        "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=SecretUser#9999",
        "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES value=4",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
        "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=SecretUser#9999 tag=PLAYSTATE value=WON",
        "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST",
    ]
    return "\n".join(lines).encode("utf-8")


class TestWebAndStorage(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test_coach.db"
        self.store = GameStore(self.db_path)
        self.app = create_app(_test_db(), self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_upload_dedup_coach_habits_and_report(self) -> None:
        dash = self.client.get("/")
        self.assertEqual(dash.status_code, 200)
        self.assertIn("HS-Coach", dash.text)

        # 1. Upload Power.log containing 3 games (1 incomplete, 1 BG, 1 Standard)
        resp = self.client.post(
            "/api/upload",
            files={"file": ("Power.log", _sample_power_log(), "text/plain")},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total_games_found"], 3)
        self.assertEqual(data["new_games"], 1)
        self.assertEqual(data["duplicate_games"], 0)
        self.assertEqual(data["skipped_incomplete"], 1)
        self.assertEqual(data["skipped_non_standard"], 1)
        gid = data["game_ids"][0]

        # Confirm BattleTags never enter SQLite
        raw_db_bytes = self.db_path.read_bytes()
        self.assertNotIn(b"SecretUser", raw_db_bytes)
        self.assertNotIn(b"#9999", raw_db_bytes)

        # 2. Duplicate re-upload inserts 0 new games
        resp2 = self.client.post(
            "/api/upload",
            files={"file": ("Power.log", _sample_power_log(), "text/plain")},
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["new_games"], 0)
        self.assertEqual(resp2.json()["duplicate_games"], 1)

        # 3. Mock coach_game + resolve_coach_config and run POST /api/games/{id}/coach
        fake_report = CoachReport(
            game_id=gid,
            match_summary="Clean Mage win.",
            your_archetype="Tempo Mage",
            opponent_archetype="Control Warrior",
            overall_grade="A-",
            key_takeaway="Used resources efficiently.",
            mulligan_review=MulliganReview(overall_grade="A", summary="Solid keep.", card_verdicts=[]),
            turning_points=[
                TurningPoint(
                    rank=1,
                    turn_number=1,
                    severity="Inaccuracy",
                    title="Floated mana",
                    turn_context="Turn 1 open board",
                    what_was_played="Passed turn",
                    better_line="Play 1-drop if drawn",
                    why_it_mattered="Minor tempo loss",
                    habit_tag="mana-efficiency",
                )
            ],
            best_plays=[
                BestPlay(
                    turn_number=1,
                    title="Closed game cleanly",
                    what_was_played="Won on turn 1",
                    why_it_was_good="Secured lethal",
                )
            ],
            habit_tags=[
                HabitTag(
                    tag="mana-efficiency",
                    polarity="mistake",
                    turns=[1],
                    note="Floated 4 mana",
                )
            ],
            usage=UsageStats(
                provider="openrouter",
                model="google/gemini-3.7-flash",
                input_tokens=1000,
                output_tokens=200,
                estimated_cost_usd=0.0025,
            ),
        )

        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            mock.patch("hs_coach.web.coach_game", return_value=fake_report),
        ):
            cres = self.client.post(
                f"/api/games/{gid}/coach",
                json={"model": "google/gemini-3.7-flash"},
            )
        self.assertEqual(cres.status_code, 200)
        self.assertEqual(cres.json()["overall_grade"], "A-")

        # 4. Re-uploading the same replay must NOT wipe saved coaching
        resp3 = self.client.post(
            "/api/upload",
            files={"file": ("Power.log", _sample_power_log(), "text/plain")},
        )
        self.assertEqual(resp3.status_code, 200)

        games = self.client.get("/api/games").json()
        self.assertEqual(len(games), 1)
        self.assertTrue(games[0]["coached"])
        self.assertEqual(games[0]["overall_grade"], "A-")

        habits = self.client.get("/api/habits").json()
        self.assertEqual(habits["total_games"], 1)
        self.assertEqual(habits["coached_games"], 1)
        self.assertEqual(habits["top_mistakes"][0]["tag"], "mana-efficiency")

        rep_html = self.client.get(f"/games/{gid}/report")
        self.assertEqual(rep_html.status_code, 200)
        self.assertIn("Back to HS-Coach Dashboard", rep_html.text)
        self.assertIn("Tempo Mage vs Control Warrior", rep_html.text)
        self.assertIn("Floated mana", rep_html.text)


if __name__ == "__main__":
    unittest.main()
