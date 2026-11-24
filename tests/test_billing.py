"""Unit tests for hs_coach.billing and /api/billing/* endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from hs_coach.billing import verify_stripe_signature
from hs_coach.cards import CardDB
from hs_coach.schemas import (
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


def _sign_stripe_payload(payload: bytes, secret: str, ts: int | None = None) -> str:
    t = int(time.time()) if ts is None else ts
    signed = f"{t}.".encode("utf-8") + payload
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _dummy_replay(game_id: str) -> GameReplay:
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
        key_takeaway="Closed cleanly.",
        mulligan_review=MulliganReview(overall_grade="A", summary="Good.", card_verdicts=[]),
        turning_points=[],
        best_plays=[],
        habit_tags=[],
        usage=UsageStats(
            provider="openrouter",
            model=model,
            input_tokens=500,
            output_tokens=150,
            estimated_cost_usd=0.01,
        ),
    )


class TestBilling(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "billing.db"
        self.store = GameStore(self.db_path)
        self.app = create_app(CardDB(), self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_stripe_signature_verification(self) -> None:
        secret = "whsec_test_123"
        payload = b'{"id":"evt_1","type":"checkout.session.completed"}'
        now = 1_700_000_000
        header = _sign_stripe_payload(payload, secret, ts=now)
        self.assertTrue(verify_stripe_signature(payload, header, secret, now_ts=now))
        self.assertFalse(verify_stripe_signature(payload + b" ", header, secret, now_ts=now))
        self.assertFalse(verify_stripe_signature(payload, header, "wrong_secret", now_ts=now))
        self.assertFalse(verify_stripe_signature(payload, header, secret, now_ts=now + 600))

    def test_checkout_portal_and_webhook_lifecycle(self) -> None:
        client = TestClient(self.app)
        wh_client = TestClient(self.app)

        # Unauthenticated checkout rejected
        self.assertEqual(client.post("/api/billing/checkout").status_code, 401)

        # Register Free user and upload 1 replay
        reg = client.post(
            "/api/auth/register",
            json={"email": "protester@example.com", "password": "password123"},
        )
        self.assertEqual(reg.status_code, 200)
        uid = reg.json()["user"]["user_id"]
        tok = reg.json()["user"]["upload_token"]

        client.post(
            "/api/replays",
            content=_dummy_replay("g_bill_1").model_dump_json(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )

        # Unconfigured Stripe returns 503 cleanly
        with mock.patch.dict(
            os.environ,
            {"STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "STRIPE_PRO_PRICE_ID": ""},
            clear=False,
        ):
            self.assertEqual(client.post("/api/billing/checkout").status_code, 503)

        stripe_env = {
            "STRIPE_SECRET_KEY": "sk_test_123",
            "STRIPE_WEBHOOK_SECRET": "whsec_test_456",
            "STRIPE_PRO_PRICE_ID": "price_pro_monthly",
            "OPENROUTER_API_KEY": "sk-or-test",
        }
        with mock.patch.dict(os.environ, stripe_env, clear=False):
            # Checkout & portal with mocked Stripe API helpers
            with mock.patch(
                "hs_coach.web.create_checkout_session",
                return_value={"id": "cs_test_1", "url": "https://checkout.stripe.com/pay/cs_test_1"},
            ):
                chk = client.post("/api/billing/checkout")
                self.assertEqual(chk.status_code, 200)
                self.assertEqual(chk.json()["checkout_url"], "https://checkout.stripe.com/pay/cs_test_1")

            # Invalid webhook signature -> 400
            bad_wh = wh_client.post(
                "/api/billing/webhook",
                content=b"{}",
                headers={"Stripe-Signature": "t=1,v1=deadbeef"},
            )
            self.assertEqual(bad_wh.status_code, 400)

            # Signed checkout.session.completed upgrades user to Pro
            ev_upgrade = json.dumps(
                {
                    "id": "evt_chk_1",
                    "created": 1700000100,
                    "type": "checkout.session.completed",
                    "data": {
                        "object": {
                            "mode": "subscription",
                            "payment_status": "paid",
                            "status": "complete",
                            "client_reference_id": uid,
                            "metadata": {"user_id": uid},
                            "customer": "cus_123",
                            "subscription": "sub_123",
                            "line_items": {"data": [{"price": {"id": "price_pro_monthly"}}]},
                        }
                    },
                }
            ).encode("utf-8")
            wh1 = wh_client.post(
                "/api/billing/webhook",
                content=ev_upgrade,
                headers={"Stripe-Signature": _sign_stripe_payload(ev_upgrade, "whsec_test_456")},
            )
            self.assertEqual(wh1.status_code, 200)
            self.assertEqual(wh1.json()["plan"], "pro")

            # Pro user can now use Pro model and open Billing Portal
            with mock.patch(
                "hs_coach.web.coach_game",
                side_effect=lambda rep, cfg: _dummy_report(rep.metadata.game_id, cfg.model),
            ):
                pro_coach = client.post(
                    "/api/games/g_bill_1/coach",
                    json={"model": "google/gemini-3.1-pro-preview"},
                )
                self.assertEqual(pro_coach.status_code, 200)

            with mock.patch(
                "hs_coach.web.create_billing_portal_session",
                return_value={"url": "https://billing.stripe.com/p/session/123"},
            ):
                portal = client.post("/api/billing/portal")
                self.assertEqual(portal.status_code, 200)
                self.assertIn("billing.stripe.com", portal.json()["portal_url"])

            # Signed customer.subscription.deleted downgrades back to Free
            ev_cancel = json.dumps(
                {
                    "id": "evt_del_1",
                    "created": 1700000200,
                    "type": "customer.subscription.deleted",
                    "data": {"object": {"id": "sub_123", "customer": "cus_123"}},
                }
            ).encode("utf-8")
            wh2 = wh_client.post(
                "/api/billing/webhook",
                content=ev_cancel,
                headers={"Stripe-Signature": _sign_stripe_payload(ev_cancel, "whsec_test_456")},
            )
            self.assertEqual(wh2.status_code, 200)
            self.assertEqual(wh2.json()["plan"], "free")

            # Downgraded user is gated back to 403 on Pro model
            downgraded = client.post(
                "/api/games/g_bill_1/coach",
                json={"model": "google/gemini-3.1-pro-preview"},
            )
            self.assertEqual(downgraded.status_code, 403)




class TestStripeErrorMessages(unittest.TestCase):
    def _cfg(self):
        from hs_coach.billing import StripeConfig

        return StripeConfig(
            secret_key="sk_live_dummy",
            webhook_secret="whsec_dummy",
            pro_price_id="price_missing",
            public_url="https://coach.example.com",
        )

    def _client(self, status: int, body: dict):
        import httpx

        return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)))

    def test_checkout_error_surfaces_stripe_message(self) -> None:
        from hs_coach.billing import StripeRequestError, create_checkout_session

        body = {"error": {"type": "invalid_request_error", "message": "No such price: 'price_missing'"}}
        with self.assertRaises(StripeRequestError) as ctx:
            create_checkout_session(self._cfg(), user_id="u1", email="a@b.c", client=self._client(400, body))
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("No such price: 'price_missing'", str(ctx.exception))

    def test_error_message_masks_secrets(self) -> None:
        from hs_coach.billing import StripeRequestError, create_billing_portal_session

        body = {"error": {"message": "Invalid API Key provided: sk_live_abc123XYZ and whsec_zzz"}}
        with self.assertRaises(StripeRequestError) as ctx:
            create_billing_portal_session(self._cfg(), customer_id="cus_1", client=self._client(401, body))
        msg = str(ctx.exception)
        self.assertNotIn("sk_live_abc123XYZ", msg)
        self.assertNotIn("whsec_zzz", msg)
        self.assertIn("[redacted]", msg)

    def test_non_json_error_still_reports_status(self) -> None:
        import httpx

        from hs_coach.billing import StripeRequestError, create_checkout_session

        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500, text="oops")))
        with self.assertRaises(StripeRequestError) as ctx:
            create_checkout_session(self._cfg(), user_id="u1", email="a@b.c", client=client)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_success_response_passes_through(self) -> None:
        from hs_coach.billing import create_checkout_session

        out = create_checkout_session(
            self._cfg(), user_id="u1", email="a@b.c", client=self._client(200, {"url": "https://checkout.stripe.com/x"})
        )
        self.assertEqual(out["url"], "https://checkout.stripe.com/x")


if __name__ == "__main__":
    unittest.main()
