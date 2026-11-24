"""Focused regression tests for HS-Coach security & production hardening."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
from fastapi.testclient import TestClient

from hs_coach.billing import StripeConfig, handle_stripe_event
from hs_coach.cards import CardDB
from hs_coach.coach import CoachConfig, coach_game
from hs_coach.config import AppConfig, load_app_config, validate_app_config
from hs_coach.schemas import (
    CoachReport,
    GameReplay,
    HeroState,
    MatchMetadata,
    MatchPreCheckSummary,
    MulliganReview,
    MulliganSnapshot,
    UsageStats,
    validate_completed_replay,
)
from hs_coach.security import RateLimitRule, RateLimiter
from hs_coach.storage import GameStore
from hs_coach.uploader import PowerLogWatcher, validate_server_url
from hs_coach.web import create_app


def _test_db() -> CardDB:
    return CardDB.from_records(
        [
            {"id": "HERO_08", "dbfId": 1, "name": "Jaina Proudmoore", "type": "HERO", "cardClass": "MAGE", "health": 30},
            {"id": "HERO_01", "dbfId": 2, "name": "Garrosh Hellscream", "type": "HERO", "cardClass": "WARRIOR", "health": 30},
        ]
    )


def _valid_replay(game_id: str = "sec_game_1") -> GameReplay:
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
            total_turns=4,
            your_turns_count=2,
        ),
        mulligan=MulliganSnapshot(went_first=True, has_coin=False),
        turns=[],
        cards_seen={},
        pre_check_summary=MatchPreCheckSummary(),
    )


def _dummy_report(game_id: str, model: str = "google/gemini-3.7-flash") -> CoachReport:
    return CoachReport(
        game_id=game_id,
        match_summary="Solid win.",
        your_archetype="Tempo Mage",
        opponent_archetype="Control Warrior",
        overall_grade="A",
        key_takeaway="Closed out cleanly.",
        mulligan_review=MulliganReview(overall_grade="A", summary="Clean keep.", card_verdicts=[]),
        turning_points=[],
        best_plays=[],
        habit_tags=[],
        usage=UsageStats(
            provider="openrouter",
            model=model,
            input_tokens=400,
            output_tokens=120,
            estimated_cost_usd=0.002,
        ),
    )


def _hosted_config(*, allow_guests: bool = False, **overrides: object) -> AppConfig:
    base = dict(
        mode="hosted",
        host="0.0.0.0",
        session_secret="super-secret-hosted-signing-key-32chars!",
        cookie_secure=False,
        trusted_proxies=(),
        public_url="https://coach.example.com",
        allow_raw_log_upload=False,
        allow_hosted_guests=allow_guests,
    )
    base.update(overrides)
    cfg = AppConfig(**base)  # type: ignore[arg-type]
    validate_app_config(cfg)
    return cfg


class TestSecurityFixes(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.store = GameStore(self.root / "sec.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_hosted_mode_rejects_unauthenticated_and_invalid_sessions(self) -> None:
        self.store.upsert_replay(_valid_replay("local_secret"), user_id="local")
        app = create_app(_test_db(), self.store, config=_hosted_config(allow_guests=False))
        with mock.patch.dict(os.environ, {"HS_COACH_UPLOAD_TOKEN": "global-secret"}, clear=False):
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/games").status_code, 401)
                self.assertEqual(client.get("/api/games/local_secret").status_code, 401)
                self.assertEqual(client.get("/api/habits").status_code, 401)
                self.assertEqual(
                    client.post("/api/replays", content=_valid_replay("g1").model_dump_json(), headers={"Content-Type": "application/json"}).status_code,
                    401,
                )
                self.assertEqual(client.post("/api/games/local_secret/coach", json={}).status_code, 401)
                self.assertEqual(client.get("/api/jobs/job123").status_code, 401)
                self.assertEqual(client.get("/games/local_secret/report").status_code, 401)

                # Hosted mode never accepts global upload token fallback into 'local'
                bearer = client.post(
                    "/api/replays",
                    content=_valid_replay("g2").model_dump_json(),
                    headers={
                        "Authorization": "Bearer global-secret",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(bearer.status_code, 401)

                # Tampered session cookie rejected cleanly
                client.cookies.set("hs_coach_session", "local:9999999999:fakejti:badbeef")
                self.assertEqual(client.get("/api/games").status_code, 401)
                self.assertFalse(client.get("/api/auth/me").json()["authenticated"])

        # Even local mode must reject forged/tampered session cookies (no fallback to 'local')
        local_app = create_app(_test_db(), self.store)
        with TestClient(local_app) as lc:
            lc.cookies.set("hs_coach_session", "local:9999999999:fakejti:badbeef")
            self.assertEqual(lc.get("/api/games").status_code, 401)

    def test_hosted_guest_isolation_and_no_local_fallback(self) -> None:
        self.store.upsert_replay(_valid_replay("local_only"), user_id="local")
        app = create_app(_test_db(), self.store, config=_hosted_config(allow_guests=True))
        with TestClient(app) as client_a, TestClient(app) as client_b:
            up_a = client_a.post(
                "/api/replays",
                content=_valid_replay("guest_a_game").model_dump_json(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(up_a.status_code, 200)
            self.assertIn("hs_coach_guest", client_a.cookies)

            games_a = client_a.get("/api/games").json()
            self.assertEqual([g["game_id"] for g in games_a], ["guest_a_game"])

            games_b_res = client_b.get("/api/games")
            self.assertEqual(games_b_res.status_code, 200)
            self.assertEqual(games_b_res.json(), [])
            self.assertNotEqual(
                client_a.cookies.get("hs_coach_guest"),
                client_b.cookies.get("hs_coach_guest"),
            )

            self.assertEqual(client_b.get("/api/games/guest_a_game").status_code, 404)
            self.assertEqual(client_b.get("/api/games/local_only").status_code, 404)
            self.assertEqual(client_b.get("/games/guest_a_game/report").status_code, 404)

    def test_logout_revokes_server_side_session(self) -> None:
        app = create_app(_test_db(), self.store, config=_hosted_config())
        with TestClient(app) as client, TestClient(app) as replay_client:
            reg = client.post(
                "/api/auth/register",
                json={"email": "logout@example.com", "password": "password123"},
            )
            self.assertEqual(reg.status_code, 200)
            tok = client.cookies.get("hs_coach_session")
            self.assertTrue(tok)
            self.assertEqual(client.get("/api/games").status_code, 200)

            out = client.post("/api/auth/logout")
            self.assertEqual(out.status_code, 200)

            # Replaying pre-logout session cookie must fail with 401
            replay_client.cookies.set("hs_coach_session", tok)  # type: ignore[arg-type]
            self.assertEqual(replay_client.get("/api/games").status_code, 401)
            self.assertFalse(replay_client.get("/api/auth/me").json()["authenticated"])

    def test_server_controlled_provider_and_model_policy(self) -> None:
        app = create_app(_test_db(), self.store, config=_hosted_config())
        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            TestClient(app) as client,
        ):
            reg = client.post(
                "/api/auth/register",
                json={"email": "policy@example.com", "password": "password123"},
            )
            uid = reg.json()["user"]["user_id"]
            client.post(
                "/api/replays",
                content=_valid_replay("g_pol").model_dump_json(),
                headers={"Content-Type": "application/json"},
            )

            # Client cannot override provider on sync coach or auto_coach
            self.assertEqual(
                client.post(
                    "/api/games/g_pol/coach",
                    json={"provider": "openai", "model": "google/gemini-3.7-flash"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/replays?auto_coach=true&provider=openai",
                    content=_valid_replay("g_pol2").model_dump_json(),
                    headers={"Content-Type": "application/json"},
                ).status_code,
                403,
            )

            # Even Pro tier rejects unapproved arbitrary model IDs
            self.store.set_user_plan(uid, "pro")
            self.assertEqual(
                client.post(
                    "/api/games/g_pol/coach",
                    json={"model": "evil/custom-unapproved-model"},
                ).status_code,
                403,
            )

            with mock.patch(
                "hs_coach.web.coach_game",
                side_effect=lambda rep, cfg: _dummy_report(rep.metadata.game_id, cfg.model),
            ):
                ok = client.post(
                    "/api/games/g_pol/coach",
                    json={"model": "google/gemini-3.1-pro-preview"},
                )
                self.assertEqual(ok.status_code, 200)

    def test_atomic_quota_reservation_sync_async_autocoach_and_release(self) -> None:
        app = create_app(_test_db(), self.store, config=_hosted_config())
        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            TestClient(app) as client,
        ):
            reg = client.post(
                "/api/auth/register",
                json={"email": "quota@example.com", "password": "password123"},
            )
            uid = reg.json()["user"]["user_id"]

            for gid in ("gq1", "gq2", "gq3", "gq4", "gq5", "gq6"):
                client.post(
                    "/api/replays",
                    content=_valid_replay(gid).model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )

            # Consume 4/5 Free credits
            with mock.patch(
                "hs_coach.web.coach_game",
                side_effect=lambda rep, cfg: _dummy_report(rep.metadata.game_id, cfg.model),
            ):
                for gid in ("gq1", "gq2", "gq3", "gq4"):
                    self.assertEqual(
                        client.post(f"/api/games/{gid}/coach", json={}).status_code,
                        200,
                    )

            # Failed async job must release the 5th reserved slot
            with mock.patch("hs_coach.web.coach_game", side_effect=RuntimeError("boom")):
                jf = client.post("/api/games/gq5/coach?async_mode=true", json={})
                self.assertEqual(jf.status_code, 202)
                jid = jf.json()["job_id"]
                deadline = time.monotonic() + 3.0
                st = client.get(f"/api/jobs/{jid}").json()
                while st["status"] in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.02)
                    st = client.get(f"/api/jobs/{jid}").json()
                self.assertEqual(st["status"], "failed")
            self.assertEqual(self.store.count_coached_this_month(uid), 4)

            # Concurrent sync requests for gq5 and gq6 when only 1 slot remains -> 1 succeeds, 1 hits 402
            gate = threading.Barrier(2)
            hold = threading.Event()
            results: list[int] = []
            res_lock = threading.Lock()

            def _slow_coach(rep: GameReplay, cfg: CoachConfig) -> CoachReport:
                hold.wait(timeout=2.0)
                return _dummy_report(rep.metadata.game_id, cfg.model)

            def _worker(target_gid: str) -> None:
                gate.wait(timeout=3.0)
                r = client.post(f"/api/games/{target_gid}/coach", json={})
                with res_lock:
                    results.append(r.status_code)

            with mock.patch("hs_coach.web.coach_game", side_effect=_slow_coach):
                t1 = threading.Thread(target=_worker, args=("gq5",))
                t2 = threading.Thread(target=_worker, args=("gq6",))
                t1.start()
                t2.start()
                time.sleep(0.15)
                hold.set()
                t1.join(timeout=3.0)
                t2.join(timeout=3.0)

            self.assertEqual(sorted(results), [200, 402])
            self.assertEqual(self.store.count_coached_this_month(uid), 5)

            # Auto-coach at quota limit saves replay cleanly with coach_error and does not call LLM
            with mock.patch("hs_coach.web.coach_game") as m_coach:
                auto_res = client.post(
                    "/api/replays?auto_coach=true",
                    content=_valid_replay("gq_over").model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(auto_res.status_code, 200)
                self.assertFalse(auto_res.json()["coached"])
                self.assertIn("quota", auto_res.json().get("coach_error", "").lower())
                m_coach.assert_not_called()

    def test_in_flight_sync_duplicate_blocked_with_409(self) -> None:
        app = create_app(_test_db(), self.store, config=_hosted_config())
        started = threading.Event()
        release = threading.Event()
        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            TestClient(app) as client,
        ):
            client.post(
                "/api/auth/register",
                json={"email": "inflight@example.com", "password": "password123"},
            )
            client.post(
                "/api/replays",
                content=_valid_replay("g_race").model_dump_json(),
                headers={"Content-Type": "application/json"},
            )

            def _blocking(rep: GameReplay, cfg: CoachConfig) -> CoachReport:
                started.set()
                release.wait(timeout=3.0)
                return _dummy_report(rep.metadata.game_id, cfg.model)

            first_code: list[int] = []

            def _first() -> None:
                r = client.post("/api/games/g_race/coach", json={})
                first_code.append(r.status_code)

            try:
                with mock.patch("hs_coach.web.coach_game", side_effect=_blocking):
                    th = threading.Thread(target=_first)
                    th.start()
                    self.assertTrue(started.wait(timeout=3.0))
                    dup = client.post("/api/games/g_race/coach", json={})
                    self.assertEqual(dup.status_code, 409)
                    release.set()
                    th.join(timeout=3.0)
            finally:
                release.set()
            self.assertEqual(first_code, [200])

    def test_hosted_mode_rejects_raw_power_log_upload(self) -> None:
        app = create_app(_test_db(), self.store, config=_hosted_config())
        with (
            mock.patch("hs_coach.web.parse_power_log") as m_parse,
            TestClient(app) as client,
        ):
            client.post(
                "/api/auth/register",
                json={"email": "noraw@example.com", "password": "password123"},
            )
            res = client.post(
                "/api/upload",
                files={"file": ("Power.log", b"D 10:00:00.0 CREATE_GAME", "text/plain")},
            )
            self.assertEqual(res.status_code, 403)
            m_parse.assert_not_called()

    def test_replay_validation_terminal_playstate_and_battletags(self) -> None:
        app = create_app(_test_db(), self.store, config=_hosted_config())
        dummy_cfg = CoachConfig(
            provider="openrouter",
            model="google/gemini-3.7-flash",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
        )
        with TestClient(app) as client:
            client.post(
                "/api/auth/register",
                json={"email": "val@example.com", "password": "password123"},
            )

            no_tps = _valid_replay("bad_no_tps")
            no_tps.metadata.your_terminal_playstate = None
            self.assertEqual(
                client.post(
                    "/api/replays",
                    content=no_tps.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                ).status_code,
                422,
            )
            with self.assertRaises(ValueError):
                coach_game(no_tps, dummy_cfg)

            mismatch = _valid_replay("bad_mismatch")
            mismatch.metadata.result = "loss"
            self.assertEqual(
                client.post(
                    "/api/replays",
                    content=mismatch.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                ).status_code,
                422,
            )
            with self.assertRaises(ValueError):
                coach_game(mismatch, dummy_cfg)

            btag = _valid_replay("bad_btag")
            btag.metadata.opponent_hero = "Griefer#4321"
            self.assertEqual(
                client.post(
                    "/api/replays",
                    content=btag.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                ).status_code,
                422,
            )
            with self.assertRaises(ValueError):
                coach_game(btag, dummy_cfg)

            zero_turns = _valid_replay("bad_t0")
            zero_turns.metadata.total_turns = 0
            with self.assertRaises(ValueError):
                validate_completed_replay(zero_turns)

    def test_ingress_body_size_limits_and_bounded_job_queue(self) -> None:
        cfg = _hosted_config(
            max_json_bytes=256,
            max_replay_bytes=1024,
            max_upload_bytes=1024,
            max_workers=1,
            max_queued_jobs=1,
        )
        app = create_app(_test_db(), self.store, config=cfg)
        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            TestClient(app) as client,
        ):
            # Declared Content-Length > 256 on JSON auth endpoint -> 413
            big_json = client.post(
                "/api/auth/login",
                content=json.dumps({"email": "a@b.com", "password": "x" * 400}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(big_json.status_code, 413)

            # Streaming chunked body exceeding 256 bytes without Content-Length -> 413
            def _chunked() -> bytes | object:
                yield b'{"email":"a@b.com","password":"'
                yield b"y" * 400
                yield b'"}'

            chunked_res = client.post(
                "/api/auth/login",
                content=_chunked(),  # type: ignore[arg-type]
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(chunked_res.status_code, 413)

            # Bounded job queue overflow returns 503 with Retry-After and releases reservation
            reg = client.post(
                "/api/auth/register",
                json={"email": "qbound@example.com", "password": "password123"},
            )
            uid = reg.json()["user"]["user_id"]
            client.post(
                "/api/replays",
                content=_valid_replay("qb1").model_dump_json(),
                headers={"Content-Type": "application/json"},
            )
            client.post(
                "/api/replays",
                content=_valid_replay("qb2").model_dump_json(),
                headers={"Content-Type": "application/json"},
            )

            started = threading.Event()
            release = threading.Event()

            def _slow(rep: GameReplay, coach_c: CoachConfig) -> CoachReport:
                started.set()
                release.wait(timeout=3.0)
                return _dummy_report(rep.metadata.game_id, coach_c.model)

            try:
                with mock.patch("hs_coach.web.coach_game", side_effect=_slow):
                    q1 = client.post("/api/games/qb1/coach?async_mode=true", json={})
                    self.assertEqual(q1.status_code, 202)
                    self.assertTrue(started.wait(timeout=3.0))

                    q2 = client.post("/api/games/qb2/coach?async_mode=true", json={})
                    self.assertEqual(q2.status_code, 503)
                    self.assertEqual(q2.headers.get("Retry-After"), "10")
                    # Only qb1's reservation should remain active
                    self.assertEqual(self.store.count_coached_this_month(uid), 1)
            finally:
                release.set()

    def test_startup_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            load_app_config(mode="local", host="0.0.0.0")

        with mock.patch.dict(
            os.environ,
            {
                "HS_COACH_MODE": "hosted",
                "HS_COACH_SESSION_SECRET": "too-short",
                "HS_COACH_PUBLIC_URL": "https://coach.example.com",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                load_app_config(host="0.0.0.0")

        with mock.patch.dict(
            os.environ,
            {
                "HS_COACH_MODE": "hosted",
                "HS_COACH_SESSION_SECRET": "x" * 40,
                "HS_COACH_PUBLIC_URL": "http://coach.example.com",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                load_app_config(host="0.0.0.0")

        with mock.patch.dict(
            os.environ,
            {
                "HS_COACH_MODE": "hosted",
                "HS_COACH_SESSION_SECRET": "x" * 40,
                "HS_COACH_PUBLIC_URL": "https://coach.example.com",
                "STRIPE_SECRET_KEY": "sk_test_only",
                "STRIPE_WEBHOOK_SECRET": "",
                "STRIPE_PRO_PRICE_ID": "",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                load_app_config(host="0.0.0.0")

        with mock.patch.dict(
            os.environ,
            {
                "HS_COACH_MODE": "hosted",
                "HS_COACH_SESSION_SECRET": "x" * 40,
                "HS_COACH_PUBLIC_URL": "https://coach.example.com",
                "STRIPE_SECRET_KEY": "",
                "STRIPE_WEBHOOK_SECRET": "",
                "STRIPE_PRO_PRICE_ID": "",
            },
            clear=False,
        ):
            ok_cfg = load_app_config(host="0.0.0.0")
            self.assertEqual(ok_cfg.mode, "hosted")
            self.assertFalse(ok_cfg.allow_raw_log_upload)

    def test_uploader_https_validation_for_non_loopback(self) -> None:
        self.assertEqual(validate_server_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(validate_server_url("http://localhost:8000"), "http://localhost:8000")
        self.assertEqual(validate_server_url("http://[::1]:8000"), "http://[::1]:8000")
        self.assertEqual(validate_server_url("https://coach.example.com/"), "https://coach.example.com")

        with self.assertRaises(ValueError):
            validate_server_url("http://coach.example.com")
        with self.assertRaises(ValueError):
            PowerLogWatcher(
                self.root,
                _test_db(),
                "http://192.168.1.50:8000",
                self.root / "state.json",
            )

    def test_forwarded_headers_only_from_trusted_proxies(self) -> None:
        # Untrusted peer: ignore X-Forwarded-Proto & X-Forwarded-For
        limiter_untrusted = RateLimiter({"auth": RateLimitRule(2, 60.0)})
        app_untrusted = create_app(
            _test_db(),
            self.store,
            config=_hosted_config(trusted_proxies=()),
            rate_limiter=limiter_untrusted,
        )
        with TestClient(app_untrusted) as client:
            for ip in ("1.1.1.1", "2.2.2.2"):
                r = client.post(
                    "/api/auth/login",
                    json={"email": "x@y.com", "password": "password123"},
                    headers={"X-Forwarded-For": ip, "X-Forwarded-Proto": "https"},
                )
                self.assertEqual(r.status_code, 401)
                self.assertIsNone(r.headers.get("Strict-Transport-Security"))
            # 3rd request from spoofed IP still hits same peer limit -> 429
            blocked = client.post(
                "/api/auth/login",
                json={"email": "x@y.com", "password": "password123"},
                headers={"X-Forwarded-For": "3.3.3.3", "X-Forwarded-Proto": "https"},
            )
            self.assertEqual(blocked.status_code, 429)

        # Trusted proxy peer: honors X-Forwarded-For per client IP & X-Forwarded-Proto
        limiter_trusted = RateLimiter({"auth": RateLimitRule(2, 60.0)})
        app_trusted = create_app(
            _test_db(),
            self.store,
            config=_hosted_config(trusted_proxies=("testclient",)),
            rate_limiter=limiter_trusted,
        )
        with TestClient(app_trusted) as client:
            for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
                r = client.post(
                    "/api/auth/login",
                    json={"email": "x@y.com", "password": "password123"},
                    headers={"X-Forwarded-For": ip, "X-Forwarded-Proto": "https"},
                )
                self.assertEqual(r.status_code, 401)
                self.assertEqual(
                    r.headers.get("Strict-Transport-Security"),
                    "max-age=31536000; includeSubDomains",
                )

    def test_stripe_eligibility_idempotency_and_out_of_order(self) -> None:
        user = self.store.create_user(
            email="stripe@example.com",
            password_hash="pbkdf2_sha256$200000$00$00",
            upload_token="hsc_tok_stripe",
            plan="free",
        )
        uid = user["user_id"]
        cfg = StripeConfig(
            secret_key="sk_test",
            webhook_secret="whsec_test",
            pro_price_id="price_pro",
            public_url="https://coach.example.com",
        )

        # 1. Unpaid checkout rejected
        res_unpaid = handle_stripe_event(
            {
                "id": "evt_unpaid",
                "created": 100,
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "mode": "subscription",
                        "payment_status": "unpaid",
                        "status": "complete",
                        "client_reference_id": uid,
                        "customer": "cus_s",
                        "subscription": "sub_pro",
                        "line_items": {"data": [{"price": {"id": "price_pro"}}]},
                    }
                },
            },
            self.store,
            config=cfg,
        )
        self.assertFalse(res_unpaid["handled"])
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "free")

        # 2. Wrong price checkout rejected
        res_wrong_price = handle_stripe_event(
            {
                "id": "evt_wrong_price",
                "created": 110,
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "mode": "subscription",
                        "payment_status": "paid",
                        "status": "complete",
                        "client_reference_id": uid,
                        "customer": "cus_s",
                        "subscription": "sub_other",
                        "line_items": {"data": [{"price": {"id": "price_cheap"}}]},
                    }
                },
            },
            self.store,
            config=cfg,
        )
        self.assertFalse(res_wrong_price["handled"])
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "free")

        # 3. Valid Pro upgrade
        upgrade_ev = {
            "id": "evt_pro_ok",
            "created": 200,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "subscription",
                    "payment_status": "paid",
                    "status": "complete",
                    "client_reference_id": uid,
                    "customer": "cus_s",
                    "subscription": "sub_pro",
                    "line_items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            },
        }
        res_ok = handle_stripe_event(upgrade_ev, self.store, config=cfg)
        self.assertTrue(res_ok["handled"])
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "pro")

        # 4. Duplicate event ID is skipped idempotently
        res_dup = handle_stripe_event(upgrade_ev, self.store, config=cfg)
        self.assertFalse(res_dup["handled"])
        self.assertTrue(res_dup.get("duplicate"))

        # 5. Stale out-of-order deletion (created < 200) does not downgrade Pro
        res_stale = handle_stripe_event(
            {
                "id": "evt_stale_del",
                "created": 100,
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": "sub_pro", "customer": "cus_s"}},
            },
            self.store,
            config=cfg,
        )
        self.assertFalse(res_stale["handled"])
        self.assertEqual(res_stale["reason"], "stale_event")
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "pro")

        # 6. Unrelated subscription delete & update ignored cleanly
        res_unrel_del = handle_stripe_event(
            {
                "id": "evt_unrel_del",
                "created": 300,
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": "sub_other", "customer": "cus_s"}},
            },
            self.store,
            config=cfg,
        )
        self.assertFalse(res_unrel_del["handled"])
        self.assertEqual(res_unrel_del["reason"], "unrelated_subscription")
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "pro")

        res_unrel_upd = handle_stripe_event(
            {
                "id": "evt_unrel_upd",
                "created": 310,
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_other",
                        "customer": "cus_s",
                        "status": "canceled",
                        "items": {"data": [{"price": {"id": "price_pro"}}]},
                    }
                },
            },
            self.store,
            config=cfg,
        )
        self.assertFalse(res_unrel_upd["handled"])
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "pro")

        # 7. Real subscription deletion downgrades cleanly
        res_real_del = handle_stripe_event(
            {
                "id": "evt_real_del",
                "created": 400,
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": "sub_pro", "customer": "cus_s"}},
            },
            self.store,
            config=cfg,
        )
        self.assertTrue(res_real_del["handled"])
        self.assertEqual(self.store.get_user_by_id(uid)["plan"], "free")


if __name__ == "__main__":
    unittest.main()
