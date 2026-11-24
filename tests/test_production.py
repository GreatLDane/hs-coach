"""Unit tests for Milestone 9 production security headers, TLS cookies, rate limits, health check, and async coaching jobs."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
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
from hs_coach.config import load_app_config
from hs_coach.security import RateLimitRule, RateLimiter
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
        "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=TestPlayer#1234",
        "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES value=4",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
        "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
        "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=TestPlayer#1234 tag=PLAYSTATE value=WON",
        "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST",
    ]
    return "\n".join(lines).encode("utf-8")


def _fake_report(game_id: str) -> CoachReport:
    return CoachReport(
        game_id=game_id,
        match_summary="Clean Mage victory.",
        your_archetype="Tempo Mage",
        opponent_archetype="Control Warrior",
        overall_grade="A-",
        key_takeaway="Closed the game cleanly.",
        mulligan_review=MulliganReview(overall_grade="A", summary="Good keep.", card_verdicts=[]),
        turning_points=[
            TurningPoint(
                rank=1,
                turn_number=1,
                severity="Inaccuracy",
                title="Minor mana float",
                turn_context="Turn 1",
                what_was_played="Passed",
                better_line="Develop early",
                why_it_mattered="Minor tempo",
                habit_tag="mana-efficiency",
            )
        ],
        best_plays=[
            BestPlay(
                turn_number=1,
                title="Found lethal",
                what_was_played="Pushed damage",
                why_it_was_good="Ended game",
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
            input_tokens=500,
            output_tokens=150,
            estimated_cost_usd=0.001,
        ),
    )


class TestProductionReadiness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "prod_test.db"
        self.store = GameStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_security_headers_and_health_endpoint(self) -> None:
        app = create_app(_test_db(), self.store)
        with TestClient(app) as client:
            res = client.get("/api/health")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["status"], "ok")
            self.assertTrue(body["db_ok"])
            self.assertEqual(body["cards_loaded"], 3)

            self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(
                res.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin"
            )
            self.assertIn("geolocation=()", res.headers.get("Permissions-Policy", ""))

            with mock.patch.object(self.store, "ping", return_value=False):
                degraded = client.get("/api/health")
            self.assertEqual(degraded.status_code, 503)
            self.assertEqual(degraded.json(), {"status": "degraded", "db_ok": False})

    def test_secure_cookie_flag_behind_https_proxy(self) -> None:
        with mock.patch.dict(
            os.environ, {"HS_COACH_TRUSTED_PROXIES": "testclient"}, clear=False
        ):
            cfg = load_app_config(mode="local", host="127.0.0.1")
        app = create_app(_test_db(), self.store, config=cfg)
        with TestClient(app) as client:
            res = client.post(
                "/api/auth/register",
                json={"email": "tls@example.com", "password": "password123"},
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "203.0.113.10",
                },
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(
                res.headers.get("Strict-Transport-Security"),
                "max-age=31536000; includeSubDomains",
            )
            set_cookie = res.headers.get("set-cookie", "").lower()
            self.assertIn("hs_coach_session=", set_cookie)
            self.assertIn("httponly", set_cookie)
            self.assertIn("secure", set_cookie)

    def test_rate_limiting_returns_429_with_retry_after(self) -> None:
        limiter = RateLimiter(
            {
                "auth": RateLimitRule(2, 60.0),
                "upload": RateLimitRule(30, 60.0),
                "coach": RateLimitRule(20, 60.0),
            }
        )
        app = create_app(_test_db(), self.store, rate_limiter=limiter)
        with TestClient(app) as client:
            for _ in range(2):
                r = client.post(
                    "/api/auth/login",
                    json={"email": "nobody@example.com", "password": "password123"},
                )
                self.assertEqual(r.status_code, 401)

            blocked = client.post(
                "/api/auth/login",
                json={"email": "nobody@example.com", "password": "password123"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertGreaterEqual(int(blocked.headers.get("Retry-After", "0")), 1)

    def test_async_coaching_job_lifecycle_dedup_and_isolation(self) -> None:
        app = create_app(_test_db(), self.store, max_workers=2)
        started_evt = threading.Event()
        release_evt = threading.Event()
        call_count = 0

        def _slow_coach(replay, _cfg) -> CoachReport:
            nonlocal call_count
            call_count += 1
            started_evt.set()
            release_evt.wait(timeout=5.0)
            return _fake_report(replay.metadata.game_id)

        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            TestClient(app) as client_a,
            TestClient(app) as client_b,
        ):
            reg_a = client_a.post(
                "/api/auth/register",
                json={"email": "usera@example.com", "password": "password123"},
            )
            self.assertEqual(reg_a.status_code, 200)

            reg_b = client_b.post(
                "/api/auth/register",
                json={"email": "userb@example.com", "password": "password123"},
            )
            self.assertEqual(reg_b.status_code, 200)

            up = client_a.post(
                "/api/upload",
                files={"file": ("Power.log", _sample_power_log(), "text/plain")},
            )
            self.assertEqual(up.status_code, 200)
            gid = up.json()["game_ids"][0]

            try:
                with mock.patch("hs_coach.web.coach_game", side_effect=_slow_coach):
                    q1 = client_a.post(
                        f"/api/games/{gid}/coach?async_mode=true",
                        json={"model": "google/gemini-3.7-flash"},
                    )
                    self.assertEqual(q1.status_code, 202)
                    q1_data = q1.json()
                    job_id = q1_data["job_id"]
                    self.assertEqual(q1_data["poll_url"], f"/api/jobs/{job_id}")
                    self.assertIn(q1_data["status"], ("queued", "running"))

                    self.assertTrue(started_evt.wait(timeout=3.0))

                    # Duplicate async request for the same in-flight job returns same job_id
                    q2 = client_a.post(
                        f"/api/games/{gid}/coach?async_mode=true",
                        json={"model": "google/gemini-3.7-flash"},
                    )
                    self.assertEqual(q2.status_code, 202)
                    self.assertEqual(q2.json()["job_id"], job_id)
                    self.assertEqual(call_count, 1)

                    # Another user cannot view User A's job
                    other_poll = client_b.get(f"/api/jobs/{job_id}")
                    self.assertEqual(other_poll.status_code, 404)

                    running_poll = client_a.get(f"/api/jobs/{job_id}")
                    self.assertEqual(running_poll.status_code, 200)
                    self.assertEqual(running_poll.json()["status"], "running")

                    release_evt.set()

                    deadline = time.monotonic() + 3.0
                    job_state = running_poll.json()
                    while job_state["status"] in ("queued", "running") and time.monotonic() < deadline:
                        time.sleep(0.02)
                        job_state = client_a.get(f"/api/jobs/{job_id}").json()

                    self.assertEqual(job_state["status"], "ready")
                    self.assertIsNotNone(job_state["finished_at"])
                    self.assertIsNone(job_state["error"])
                    self.assertEqual(job_state["result"]["overall_grade"], "A-")
            finally:
                release_evt.set()

            saved = client_a.get(f"/api/games/{gid}")
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["report"]["overall_grade"], "A-")

            # Failed background job transitions to "failed" with error populated
            with mock.patch(
                "hs_coach.web.coach_game",
                side_effect=RuntimeError("Upstream timeout"),
            ):
                qf = client_a.post(
                    f"/api/games/{gid}/coach?async_mode=true",
                    json={"model": "google/gemini-3.7-flash"},
                )
                self.assertEqual(qf.status_code, 202)
                fail_job_id = qf.json()["job_id"]
                self.assertNotEqual(fail_job_id, job_id)

                deadline = time.monotonic() + 3.0
                fail_state = client_a.get(f"/api/jobs/{fail_job_id}").json()
                while fail_state["status"] in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.02)
                    fail_state = client_a.get(f"/api/jobs/{fail_job_id}").json()

                self.assertEqual(fail_state["status"], "failed")
                self.assertIn("Upstream timeout", fail_state["error"] or "")


if __name__ == "__main__":
    unittest.main()
