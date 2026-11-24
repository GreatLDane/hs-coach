"""Unit tests for hs_coach.cards."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hs_coach.cards import CardDB, clean_card_text, partition_glossary
from hs_coach.schemas import CardReference


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

    def test_markup_stripping_and_glossary_partitioning(self) -> None:
        raw_amp = "<b>Battlecry and Deathrattle:</b>& Draw a card. Gain d2 Armor. Deal {0} damage."
        self.assertEqual(
            clean_card_text(raw_amp),
            "Battlecry and Deathrattle: Draw a card. Gain 2 Armor. Deal X damage.",
        )
        self.assertEqual(
            clean_card_text("Deal {0} damage.", replace_placeholders=False),
            "Deal {0} damage.",
        )

        cards_seen = {
            "HERO_01": CardReference(card_id="HERO_01", name="Garrosh Hellscream", card_type="HERO", text=""),
            "DET_1": CardReference(
                card_id="DET_1",
                name="Detected!",
                card_type="SPELL",
                base_cost=0,
                text="This was played on the friendly side.",
            ),
            "DG_INT": CardReference(
                card_id="DG_INT",
                name="Dark Gift",
                card_type="SPELL",
                text="Executes nightmare bonus",
            ),
            "ONY_1": CardReference(card_id="ONY_1", name="Onyxia's Wing", card_type="MINION", base_cost=1, text="Rush"),
            "ONY_1t": CardReference(card_id="ONY_1t", name="Onyxia's Wing", card_type="MINION", base_cost=1, text="Rush"),
            "ENCH_1": CardReference(card_id="ENCH_1", name="Black Scales", card_type="ENCHANTMENT", text="+2 Attack"),
            "SPELL_1": CardReference(card_id="SPELL_1", name="Shield Block", card_type="SPELL", base_cost=3, text="Gain d5 Armor."),
        }
        playable, enchantments = partition_glossary(cards_seen)
        self.assertEqual([c.name for c in playable], ["Onyxia's Wing", "Shield Block"])
        self.assertEqual([c.card_id for c in playable], ["ONY_1", "SPELL_1"])
        self.assertEqual([e.name for e in enchantments], ["Black Scales"])


if __name__ == "__main__":
    unittest.main()
