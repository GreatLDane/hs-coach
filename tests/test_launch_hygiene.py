"""Unit tests for Milestone 11 legal pages (/privacy, /terms) and password reset lifecycle."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from hs_coach.auth import generate_password_reset_token
from hs_coach.cards import CardDB
from hs_coach.config import AppConfig, validate_app_config
from hs_coach.storage import GameStore
from hs_coach.web import create_app


def _hosted_cfg() -> AppConfig:
    cfg = AppConfig(
        mode="hosted",
        host="0.0.0.0",
        session_secret="launch-hygiene-test-secret-32chars!!",
        public_url="https://coach.example.com",
        allow_raw_log_upload=False,
        allow_hosted_guests=False,
    )
    validate_app_config(cfg)
    return cfg


class TestLaunchHygiene(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "launch.db"
        self.store = GameStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_privacy_and_terms_pages_and_dashboard_footer(self) -> None:
        app = create_app(CardDB(), self.store, config=_hosted_cfg())
        with TestClient(app) as client:
            dash = client.get("/")
            self.assertEqual(dash.status_code, 200)
            self.assertIn('href="/privacy"', dash.text)
            self.assertIn('href="/terms"', dash.text)
            self.assertEqual(dash.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(dash.headers.get("X-Frame-Options"), "DENY")

            priv = client.get("/privacy")
            self.assertEqual(priv.status_code, 200)
            self.assertIn("Privacy Policy", priv.text)
            self.assertIn("BattleTags", priv.text)
            self.assertIn("100% post-game", priv.text)

            terms = client.get("/terms")
            self.assertEqual(terms.status_code, 200)
            self.assertIn("Terms of Service", terms.text)
            self.assertIn("Blizzard Entertainment", terms.text)

    def test_password_reset_lifecycle_non_enumeration_and_session_revocation(self) -> None:
        app = create_app(CardDB(), self.store, config=_hosted_cfg())
        sent_emails: list[tuple[str, str]] = []

        def _fake_send(to_email: str, reset_url: str, **_kwargs: object) -> bool:
            sent_emails.append((to_email, reset_url))
            return True

        with (
            mock.patch("hs_coach.web.send_password_reset_email", side_effect=_fake_send),
            TestClient(app) as client,
            TestClient(app) as attacker,
        ):
            # 1. Unknown email returns identical generic message without sending email
            r_unknown = client.post(
                "/api/auth/request-reset",
                json={"email": "nobody@example.com"},
            )
            self.assertEqual(r_unknown.status_code, 200)
            self.assertEqual(sent_emails, [])

            # 2. Register user and verify active session
            reg = client.post(
                "/api/auth/register",
                json={"email": "player@example.com", "password": "oldpassword123"},
            )
            self.assertEqual(reg.status_code, 200)
            uid = reg.json()["user"]["user_id"]
            self.assertEqual(client.get("/api/games").status_code, 200)

            # 3. Request reset -> same generic 200 response; link captured
            r_req = client.post(
                "/api/auth/request-reset",
                json={"email": "Player@Example.com"},
            )
            self.assertEqual(r_req.status_code, 200)
            self.assertEqual(r_req.json()["message"], r_unknown.json()["message"])
            self.assertEqual(len(sent_emails), 1)
            to_addr, reset_url = sent_emails[0]
            self.assertEqual(to_addr, "player@example.com")
            self.assertTrue(reset_url.startswith("https://coach.example.com/?reset_token="))
            raw_token = parse_qs(urlparse(reset_url).query)["reset_token"][0]

            # 4. Invalid token and short password fail cleanly with 400
            self.assertEqual(
                attacker.post(
                    "/api/auth/reset-password",
                    json={"token": "wrong-token", "new_password": "newpassword123"},
                ).status_code,
                400,
            )
            self.assertEqual(
                attacker.post(
                    "/api/auth/reset-password",
                    json={"token": raw_token, "new_password": "123"},
                ).status_code,
                400,
            )

            # 5. Valid reset succeeds and revokes all prior sessions
            r_reset = attacker.post(
                "/api/auth/reset-password",
                json={"token": raw_token, "new_password": "newpassword456"},
            )
            self.assertEqual(r_reset.status_code, 200)
            self.assertEqual(client.get("/api/games").status_code, 401)
            self.assertFalse(client.get("/api/auth/me").json()["authenticated"])

            # 6. Single-use token cannot be replayed
            self.assertEqual(
                attacker.post(
                    "/api/auth/reset-password",
                    json={"token": raw_token, "new_password": "anotherpw789"},
                ).status_code,
                400,
            )

            # 7. Old password rejected, new password succeeds
            self.assertEqual(
                attacker.post(
                    "/api/auth/login",
                    json={"email": "player@example.com", "password": "oldpassword123"},
                ).status_code,
                401,
            )
            login_ok = attacker.post(
                "/api/auth/login",
                json={"email": "player@example.com", "password": "newpassword456"},
            )
            self.assertEqual(login_ok.status_code, 200)
            self.assertEqual(attacker.get("/api/games").status_code, 200)

            # 8. Expired reset token rejected cleanly
            exp_raw, exp_hash = generate_password_reset_token()
            self.store.create_password_reset(uid, exp_hash, int(time.time()) - 60)
            self.assertEqual(
                attacker.post(
                    "/api/auth/reset-password",
                    json={"token": exp_raw, "new_password": "validnewpw999"},
                ).status_code,
                400,
            )


if __name__ == "__main__":
    unittest.main()
