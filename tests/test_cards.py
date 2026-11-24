"""Unit tests for hs_coach.cards."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hs_coach.cards import CardDB, clean_card_text


class TestCards(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            {
                "id": "FIR_929",
                "dbfId": 1001,
                "name": "Living Flame",
                "type": "MINION",
                "cost": 2,
                "attack": 2,
                "health": 2,
                "cardClass": "MAGE",
                "races": ["ELEMENTAL"],
                "text": "[x]<b>Battlecry:</b> Draw a Fire spell.\n~GAMEPLAY ASPROXY",
                "set": "EMERALD_DREAM",
                "collectible": True,
            },
            {
                "id": "CORE_EX1_287",
                "dbfId": 1002,
                "name": "Counterspell",
                "type": "SPELL",
                "cost": 3,
                "cardClass": "MAGE",
                "spellSchool": "ARCANE",
                "mechanics": ["SECRET"],
                "text": "<b>Secret:</b> When your opponent casts a spell, <b>Counter</b> it.",
                "set": "CORE",
                "collectible": True,
            },
            {
                "id": "TEST_WEAP",
                "dbfId": 1003,
                "name": "Arcanite Blade",
                "type": "WEAPON",
                "cost": 4,
                "attack": 3,
                "durability": 2,
                "cardClass": "WARRIOR",
                "text": "Adjacent minions have +1 Attack.",
                "set": "CORE",
                "collectible": True,
            },
            {
                "id": "TEST_SPELL",
                "dbfId": 1004,
                "name": "Fireball",
                "type": "SPELL",
                "cost": 4,
                "cardClass": "MAGE",
                "spellSchool": "FIRE",
                "text": "Deal $6 damage.",
                "set": "CORE",
                "collectible": True,
            },
        ]
        self.db = CardDB.from_records(self.records)

    def test_clean_card_text(self) -> None:
        raw = "[x]<b>Battlecry:</b> Deal $4 damage.\u00a0#2@Alternate ~GAMEPLAY ASPROXY"
        self.assertEqual(
            clean_card_text(raw, strip_alternate=True),
            "Battlecry: Deal 4 damage. 2",
        )
        self.assertEqual(
            self.db.clean_text("FIR_929"),
            "Battlecry: Draw a Fire spell.",
        )

    def test_indexes_and_references(self) -> None:
        self.assertEqual(self.db.by_dbf_id[1001], "FIR_929")
        self.assertEqual(self.db.races["FIR_929"], ["Elemental"])
        self.assertEqual(self.db.spell_school["TEST_SPELL"], "Fire")
        self.assertEqual(self.db.base_durability("TEST_WEAP"), 2)
        self.assertTrue(self.db.position_matters("TEST_WEAP"))
        self.assertFalse(self.db.position_matters("FIR_929"))

        ref = self.db.to_reference("TEST_WEAP")
        self.assertEqual(ref.base_health, 2)
        self.assertTrue(ref.position_matters)

    def test_standard_secret_pool(self) -> None:
        self.assertIn("Counterspell", self.db.all_secret_names)
        self.assertIn("Counterspell", self.db.secret_pool_by_class["MAGE"])
        self.assertIn(
            "When your opponent casts a spell",
            self.db.secret_pool_by_class["MAGE"]["Counterspell"],
        )


if __name__ == "__main__":
    unittest.main()
