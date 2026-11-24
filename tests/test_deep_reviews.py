"""Pro plan: 200 coached games/month with Flash by default, plus a capped number of deep reviews."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from hs_coach.auth import FAST_MODEL, FAST_MODELS, PLAN_CONFIGS, is_deep_model
from hs_coach.cards import CardDB
from hs_coach.storage import GameStore
from hs_coach.web import create_app
from test_auth_and_quotas import _dummy_replay, _dummy_report

DEEP = "google/gemini-3.1-pro-preview"
SONNET = "anthropic/claude-sonnet-4.5"


class TestDeepReviewConfig(unittest.TestCase):
    def test_pro_defaults_to_fast_with_30_deep_reviews(self) -> None:
        pro = PLAN_CONFIGS["pro"]
        self.assertEqual(pro.monthly_coach_limit, 200)
        self.assertEqual(pro.default_model, FAST_MODEL)
        self.assertEqual(pro.monthly_deep_limit, 30)
        self.assertEqual(PLAN_CONFIGS["free"].monthly_deep_limit, 0)
        self.assertFalse(is_deep_model(FAST_MODEL))
        self.assertTrue(is_deep_model(DEEP))
        self.assertTrue(is_deep_model(SONNET))
        self.assertFalse(is_deep_model(None))


class TestDeepReviewStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = GameStore(Path(self._tmpdir.name) / "deep.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def _reserve(self, game_id: str, model: str, *, deep_limit: int = 3, monthly_limit: int = 200):
        return self.store.reserve_coaching_slot(
            "u1",
            game_id,
            monthly_limit=monthly_limit,
            model=model,
            deep_limit=deep_limit,
            fast_models=FAST_MODELS,
        )

    def test_deep_cap_refuses_extra_deep_but_allows_fast(self) -> None:
        for i in range(3):
            res_id, err = self._reserve(f"d{i}", DEEP if i % 2 == 0 else SONNET)
            self.assertIsNone(err)
            self.store.complete_coaching_reservation(res_id)
        self.assertEqual(self.store.count_deep_this_month("u1", FAST_MODELS), 3)

        res_id, err = self._reserve("d3", DEEP)
        self.assertIsNone(res_id)
        self.assertEqual(err, "deep_quota_exceeded")

        res_id, err = self._reserve("f0", FAST_MODEL)
        self.assertIsNone(err)
        self.assertIsNotNone(res_id)
        self.assertEqual(self.store.count_deep_this_month("u1", FAST_MODELS), 3)

    def test_released_deep_reservations_do_not_count(self) -> None:
        res_id, err = self._reserve("d0", DEEP, deep_limit=1)
        self.assertIsNone(err)
        self.store.release_coaching_reservation(res_id)  # e.g. LLM call failed
        self.assertEqual(self.store.count_deep_this_month("u1", FAST_MODELS), 0)
        res_id, err = self._reserve("d1", DEEP, deep_limit=1)
        self.assertIsNone(err)

    def test_total_monthly_limit_still_applies_to_fast(self) -> None:
        for i in range(2):
            res_id, err = self._reserve(f"f{i}", FAST_MODEL, monthly_limit=2)
            self.assertIsNone(err)
            self.store.complete_coaching_reservation(res_id)
        _, err = self._reserve("f2", FAST_MODEL, monthly_limit=2)
        self.assertEqual(err, "quota_exceeded")

    def test_no_deep_limit_means_unmetered(self) -> None:
        for i in range(5):
            res_id, err = self.store.reserve_coaching_slot(
                "u1", f"d{i}", monthly_limit=200, model=DEEP
            )
            self.assertIsNone(err)
            self.store.complete_coaching_reservation(res_id)


class TestDeepReviewApi(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = GameStore(Path(self._tmpdir.name) / "deepapi.db")
        self.app = create_app(CardDB(), self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_pro_default_is_fast_and_deep_cap_returns_402(self) -> None:
        client = TestClient(self.app)
        reg = client.post("/api/auth/register", json={"email": "pro@example.com", "password": "password1"})
        self.assertEqual(reg.status_code, 200)
        uid = reg.json()["user"]["user_id"]
        tok = reg.json()["user"]["upload_token"]
        self.assertEqual(reg.json()["quota"]["deep_limit"], 0)  # Free has no deep reviews
        self.store.set_user_plan(uid, "pro")

        for gid in ("g0", "g1", "g2"):
            up = client.post(
                "/api/replays",
                content=_dummy_replay(gid).model_dump_json(),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            )
            self.assertEqual(up.status_code, 200)

        used_models: list[str] = []

        def fake_coach(rep, cfg):
            used_models.append(cfg.model)
            return _dummy_report(rep.metadata.game_id, cfg.model)

        with (
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=False),
            mock.patch("hs_coach.web.coach_game", side_effect=fake_coach),
        ):
            # Plan default for Pro is the fast model and does not use a deep review
            self.assertEqual(client.post("/api/games/g0/coach", json={}).status_code, 200)
            self.assertEqual(used_models[-1], FAST_MODEL)

            # A deep review is allowed and counted
            self.assertEqual(client.post("/api/games/g1/coach", json={"model": DEEP}).status_code, 200)
            quota = client.get("/api/auth/me").json()["quota"]
            self.assertEqual(quota["deep_used"], 1)
            self.assertEqual(quota["deep_limit"], 30)
            self.assertEqual(quota["deep_remaining"], 29)
            self.assertEqual(quota["used_this_month"], 2)

            # Use up the remaining deep reviews directly in the store
            for i in range(29):
                res_id, err = self.store.reserve_coaching_slot(
                    uid, f"x{i}", monthly_limit=200, model=SONNET,
                    deep_limit=30, fast_models=FAST_MODELS,
                )
                self.assertIsNone(err)
                self.store.complete_coaching_reservation(res_id)

            capped = client.post("/api/games/g2/coach", json={"model": SONNET})
            self.assertEqual(capped.status_code, 402)
            self.assertIn("deep reviews", capped.json()["detail"])
            self.assertIn("Fast", capped.json()["detail"])

            # Fast coaching still works after the deep allowance is gone
            self.assertEqual(client.post("/api/games/g2/coach", json={}).status_code, 200)
            self.assertEqual(used_models[-1], FAST_MODEL)

    def test_copy_mentions_deep_reviews(self) -> None:
        client = TestClient(self.app)
        dash = client.get("/").text
        terms = client.get("/terms").text
        self.assertIn("30 deep reviews", dash)
        self.assertIn("30 deep reviews", terms)
        self.assertIn("deep_used", dash)  # badge JS shows the deep counter
        self.assertNotIn("GPT-4.1", dash)


if __name__ == "__main__":
    unittest.main()
