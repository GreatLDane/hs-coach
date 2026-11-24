"""Unit tests for hs_coach.uploader and /api/replays ingestion."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
from fastapi.testclient import TestClient

from hs_coach.cards import CardDB
from hs_coach.storage import GameStore
from hs_coach.uploader import PowerLogWatcher, find_power_logs
from hs_coach.web import create_app


def _test_db() -> CardDB:
    return CardDB.from_records(
        [
            {"id": "HERO_08", "dbfId": 1, "name": "Jaina Proudmoore", "type": "HERO", "cardClass": "MAGE", "health": 30},
            {"id": "HERO_01", "dbfId": 2, "name": "Garrosh Hellscream", "type": "HERO", "cardClass": "WARRIOR", "health": 30},
        ]
    )


def _bridge_httpx_to_testclient(tc: TestClient, captured_bodies: list[bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(request.content)
        url_path = request.url.path
        if request.url.query:
            url_path = f"{url_path}?{request.url.query.decode('utf-8')}"
        r = tc.request(
            request.method,
            url_path,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(r.status_code, content=r.content, headers=dict(r.headers))

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestUploader(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.db_path = self.root / "coach.db"
        self.store = GameStore(self.db_path)
        self.app = create_app(_test_db(), self.store)
        self.tc = TestClient(self.app)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_health_and_token_protected_replay_ingest(self) -> None:
        health = self.tc.get("/api/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["db_ok"])

        log_dir = self.root / "Logs" / "Hearthstone_2026_11_23_15_00_00"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "Power.log"
        log_file.write_text(
            "\n".join(
                [
                    # Incomplete game (must NOT be uploaded)
                    "D 10:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                    "D 10:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                    "D 10:00:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                    # Completed game with BattleTag
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
                    "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                    "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                    "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=SecretUser#9999 tag=PLAYSTATE value=WON",
                    "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(find_power_logs(self.root / "Logs"), [log_file])

        state_path = self.root / "uploader_state.json"
        captured: list[bytes] = []
        http_client = _bridge_httpx_to_testclient(self.tc, captured)

        # 1. With token required on server, wrong token fails with 401 and does NOT mark uploaded
        with mock.patch.dict(os.environ, {"HS_COACH_UPLOAD_TOKEN": "topsecret"}, clear=False):
            bad_watcher = PowerLogWatcher(
                self.root / "Logs",
                _test_db(),
                "http://127.0.0.1:8000",
                state_path,
                token="wrong",
                client=http_client,
            )
            bad_res = bad_watcher.scan_once()
            self.assertEqual(bad_res.new_games_uploaded, 0)
            self.assertEqual(len(bad_res.errors), 1)
            self.assertIn("401", bad_res.errors[0])

            # 2. Correct token uploads anonymized replay cleanly
            good_watcher = PowerLogWatcher(
                self.root / "Logs",
                _test_db(),
                "http://127.0.0.1:8000",
                state_path,
                token="topsecret",
                client=http_client,
            )
            res = good_watcher.scan_once()
            self.assertEqual(res.files_scanned, 1)
            self.assertEqual(res.new_games_uploaded, 1)
            self.assertEqual(res.errors, [])

            # Ensure BattleTag never crossed the wire or touched SQLite/state
            self.assertNotIn(b"SecretUser", captured[-1])
            self.assertNotIn(b"#9999", captured[-1])
            self.assertNotIn(b"SecretUser", self.db_path.read_bytes())
            self.assertNotIn(b"SecretUser", state_path.read_bytes())

            # 3. Unchanged file skips scan completely
            res2 = good_watcher.scan_once()
            self.assertEqual(res2.files_scanned, 0)
            self.assertEqual(res2.new_games_uploaded, 0)

            # 4. File appended with an incomplete game still skips already-uploaded game
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write("\nD 10:06:00.0000000 GameState.DebugPrintPower() - CREATE_GAME\n")
            res3 = good_watcher.scan_once()
            self.assertEqual(res3.files_scanned, 1)
            self.assertEqual(res3.new_games_uploaded, 0)
            self.assertEqual(res3.skipped_already_uploaded, 1)


if __name__ == "__main__":
    unittest.main()
