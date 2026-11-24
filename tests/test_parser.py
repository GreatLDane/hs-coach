"""Unit tests for hs_coach.parser covering Hearthstone Power.log edge cases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hs_coach.cards import CardDB
from hs_coach.parser import parse_power_log


def _sample_db() -> CardDB:
    return CardDB.from_records(
        [
            {"id": "HERO_08", "dbfId": 1, "name": "Jaina Proudmoore", "type": "HERO", "cardClass": "MAGE", "health": 30},
            {"id": "HERO_01", "dbfId": 2, "name": "Garrosh Hellscream", "type": "HERO", "cardClass": "WARRIOR", "health": 30},
            {"id": "CS2_034", "dbfId": 3, "name": "Fireblast", "type": "HERO_POWER", "cardClass": "MAGE", "cost": 2, "text": "Deal $1 damage."},
            {"id": "CS2_102", "dbfId": 4, "name": "Armor Up!", "type": "HERO_POWER", "cardClass": "WARRIOR", "cost": 2, "text": "Gain 2 Armor."},
            {"id": "GAME_005", "dbfId": 5, "name": "The Coin", "type": "SPELL", "cost": 0, "text": "Gain 1 Mana Crystal this turn only."},
            {"id": "CS2_029", "dbfId": 6, "name": "Fireball", "type": "SPELL", "cost": 4, "cardClass": "MAGE", "spellSchool": "FIRE", "text": "Deal $6 damage."},
            {"id": "CS2_023", "dbfId": 7, "name": "Arcane Intellect", "type": "SPELL", "cost": 3, "cardClass": "MAGE", "spellSchool": "ARCANE", "text": "Draw 2 cards."},
            {"id": "CS2_120", "dbfId": 8, "name": "River Crocolisk", "type": "MINION", "cost": 2, "attack": 2, "health": 3, "races": ["BEAST"], "text": ""},
            {"id": "CS2_121", "dbfId": 9, "name": "Frostwolf Grunt", "type": "MINION", "cost": 2, "attack": 2, "health": 2, "text": "<b>Taunt</b>"},
            {"id": "CORE_EX1_287", "dbfId": 10, "name": "Counterspell", "type": "SPELL", "cost": 3, "cardClass": "MAGE", "mechanics": ["SECRET"], "set": "CORE", "collectible": True, "text": "<b>Secret:</b> When your opponent casts a spell, <b>Counter</b> it."},
            {"id": "CORE_EX1_289", "dbfId": 11, "name": "Ice Barrier", "type": "SPELL", "cost": 3, "cardClass": "MAGE", "mechanics": ["SECRET"], "set": "CORE", "collectible": True, "text": "<b>Secret:</b> When your hero is attacked, gain 8 Armor."},
            {"id": "FIR_911", "dbfId": 12, "name": "Smoldering Grove", "type": "SPELL", "cost": 2, "cardClass": "MAGE", "spellSchool": "FIRE", "text": "[x]Draw {0} card. Upgrades each turn. <i>({1} turns left!)</i>@Draw {0} cards. <i>(Discards this turn!)</i>"},
            {"id": "LOC_001", "dbfId": 13, "name": "Cosmic Springs", "type": "LOCATION", "cost": 2, "health": 2, "text": "Refresh 2 Mana Crystals."},
            {"id": "TRD_001", "dbfId": 14, "name": "Rustrot Viper", "type": "MINION", "cost": 3, "attack": 3, "health": 4, "text": "<b>Tradeable</b>"},
            {"id": "PREP_001", "dbfId": 15, "name": "Tunneling Geomancer", "type": "MINION", "cost": 7, "attack": 5, "health": 5, "text": "<b>Prepare</b>"},
            {"id": "SHAT_A", "dbfId": 16, "name": "Split Ray", "type": "SPELL", "cost": 3, "text": "<b>Shattered</b> Deal $4 damage."},
            {"id": "SHAT_B", "dbfId": 17, "name": "Split Ray", "type": "SPELL", "cost": 3, "text": "<b>Shattered</b> Deal $4 damage."},
            {"id": "WEAP_001", "dbfId": 18, "name": "Fiery War Axe", "type": "WEAPON", "cost": 3, "attack": 3, "durability": 2, "text": ""},
            {"id": "DORM_001", "dbfId": 19, "name": "Dreadseed Stag", "type": "MINION", "cost": 3, "attack": 5, "health": 4, "text": "<b>Dormant</b> for 2 turns. <b>Taunt</b>"},
            {"id": "BE_013", "dbfId": 20, "name": "C'Thun, Eye of the Empire", "type": "MINION", "cost": 10, "attack": 12, "health": 12, "text": "Old God. Uses 2 abilities."},
            {"id": "BE_013t1", "dbfId": 21, "name": "Wrath of C'Thun", "type": "SPELL", "cost": 0, "text": "Deal 6 damage split."},
            {"id": "BE_013t2", "dbfId": 22, "name": "Domain of C'Thun", "type": "SPELL", "cost": 0, "text": "+2/+2 twice."},
            {"id": "ASM_BASE", "dbfId": 23, "name": "Tank Regiment", "type": "MINION", "cost": 4, "attack": 4, "health": 4, "text": "Top A & Bot B"},
            {"id": "ASM_D1", "dbfId": 24, "name": "Seeker of Prey", "type": "MINION", "cost": 3, "attack": 3, "health": 3, "text": "<b>Rush</b> & Gain +2/+2."},
            {"id": "ASM_D2", "dbfId": 25, "name": "Bronze Keeper", "type": "MINION", "cost": 3, "attack": 2, "health": 5, "text": "<b>Taunt</b> & Draw a card."},
            {"id": "DG_001", "dbfId": 26, "name": "Waking Nightmare", "type": "SPELL", "cost": 0, "text": "+3 Attack and Lifesteal."},
            {"id": "ENCH_SP", "dbfId": 27, "name": "Kalec's Insight", "type": "ENCHANTMENT", "text": "Spell Damage +2"},
        ]
    )


def _write_temp_log(content: str) -> Path:
    tf = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8")
    tf.write(content)
    tf.close()
    return Path(tf.name)


class TestParser(unittest.TestCase):
    def test_filters_incomplete_and_non_standard_and_strips_pii(self) -> None:
        log = "\n".join(
            [
                # Game 1: Battlegrounds (completed, should skip when standard_only=True)
                "D 10:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                "D 10:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
                "D 10:00:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                "D 10:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1",
                "D 10:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2",
                "D 10:01:00.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON",
                # Game 2: Incomplete Standard game (no terminal PLAYSTATE -> skip)
                "D 10:02:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                "D 10:02:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                "D 10:02:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                "D 10:02:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1",
                # Game 3: Completed Standard Mage mirror! Verify mirror-safe ID & zero BattleTag leaks
                "D 10:05:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                "D 10:05:00.0000000 GameState.DebugPrintPower() - GameEntity EntityID=1",
                "D 10:05:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=FIRST_PLAYER value=1",
                "D 10:05:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2",
                "D 10:05:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=HERO_08",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=30",
                "D 10:05:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=11 CardID=HERO_08",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=30",
                "D 10:05:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                "D 10:05:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                "D 10:05:00.0000000 GameState.DebugPrintGame() - ScenarioID=2",
                "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=UNKNOWN HUMAN PLAYER",
                "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=SecretUser#9999",
                "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=BEGIN_MULLIGAN",
                # Opp (p1) gets 3 cards, You (p2) get 4 + Coin (id=25)
                "D 10:05:01.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=21 CardID=CS2_029",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=COST value=4",
                "D 10:05:01.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=22 CardID=CS2_120",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=COST value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=23 CardID=CS2_023",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=COST value=3",
                "D 10:05:01.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=24 CardID=CS2_121",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=COST value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=25 CardID=GAME_005",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 10:05:01.0000000 GameState.DebugPrintPower() -     tag=COST value=0",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() - id=2 Player=SecretUser#9999 TaskList=6 ChoiceType=MULLIGAN CountMin=0 CountMax=4",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() -   Source=GameEntity",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() -   Entities[0]=[entityName=Fireball id=21 zone=HAND zonePos=1 cardId=CS2_029 player=2]",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() -   Entities[1]=[entityName=River Crocolisk id=22 zone=HAND zonePos=2 cardId=CS2_120 player=2]",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() -   Entities[2]=[entityName=Arcane Intellect id=23 zone=HAND zonePos=3 cardId=CS2_023 player=2]",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() -   Entities[3]=[entityName=Frostwolf Grunt id=24 zone=HAND zonePos=4 cardId=CS2_121 player=2]",
                "D 10:05:02.0000000 GameState.DebugPrintEntityChoices() -   Entities[4]=[entityName=The Coin id=25 zone=HAND zonePos=5 cardId=GAME_005 player=2]",
                # EntitiesChosen lists KEPT cards (22 and 24 kept; 21 and 23 tossed)
                "D 10:05:05.0000000 GameState.DebugPrintEntitiesChosen() - id=2 Player=SecretUser#9999 EntitiesCount=2",
                "D 10:05:05.0000000 GameState.DebugPrintEntitiesChosen() -   Entities[0]=[entityName=River Crocolisk id=22 zone=HAND zonePos=2 cardId=CS2_120 player=2]",
                "D 10:05:05.0000000 GameState.DebugPrintEntitiesChosen() -   Entities[1]=[entityName=Frostwolf Grunt id=24 zone=HAND zonePos=4 cardId=CS2_121 player=2]",
                "D 10:05:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
                "D 10:05:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
                "D 10:05:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                "D 10:05:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                "D 10:05:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=CONCEDED",
                "D 10:05:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=LOST",
                "D 10:05:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=WON",
            ]
        )
        p = _write_temp_log(log)
        try:
            res = parse_power_log(p, _sample_db(), standard_only=True)
        finally:
            p.unlink(missing_ok=True)

        self.assertEqual(res.total_games_found, 3)
        self.assertEqual(res.skipped_non_standard, 1)
        self.assertEqual(res.skipped_incomplete, 1)
        self.assertEqual(len(res.replays), 1)

        rep = res.replays[0]
        raw_json = rep.model_dump_json()
        self.assertNotIn("SecretUser", raw_json)
        self.assertNotIn("#9999", raw_json)
        self.assertEqual(rep.metadata.your_class, "MAGE")
        self.assertEqual(rep.metadata.opponent_class, "MAGE")
        self.assertEqual(rep.metadata.result, "win")
        self.assertEqual(rep.metadata.end_reason, "concede")
        self.assertFalse(rep.metadata.went_first)
        self.assertTrue(rep.metadata.has_coin)
        self.assertEqual(rep.mulligan.kept_names, ["River Crocolisk", "Frostwolf Grunt"])
        self.assertEqual(rep.mulligan.tossed_names, ["Fireball", "Arcane Intellect"])
        self.assertEqual(len(rep.mulligan.final_opening_hand), 5)

    def test_mechanics_and_deterministic_prechecks(self) -> None:
        log = "\n".join(
            [
                "D 11:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - GameEntity EntityID=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=FIRST_PLAYER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2",
                "D 11:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                "D 11:00:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                "D 11:00:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=LocalUser#1111",
                "D 11:00:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
                # Heroes & Hero Powers
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=HERO_08",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=30",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=11 CardID=CS2_034",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO_POWER",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=12 CardID=HERO_08",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=30",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=DAMAGE value=23",
                # Opponent has a dormant Taunt (does NOT block lethal) and an active secret
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=30 CardID=DORM_001",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ATK value=5",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=4",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=TAUNT value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=DORMANT value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=31 CardID=",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=SECRET",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CLASS value=MAGE",
                # Your board: Ready Location (LOCATION_ACTION_COOLDOWN=1 should be ignored when EXHAUSTED=0),
                # River Crocolisk (2 atk), and Weapon with missing DURABILITY tag
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=40 CardID=LOC_001",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=LOCATION",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=EXHAUSTED value=0",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=LOCATION_ACTION_COOLDOWN value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=41 CardID=CS2_120",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ATK value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=42 CardID=WEAP_001",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=WEAPON",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ATK value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=DAMAGE value=1",
                # Your hand: Fireball (4m, 6 dmg) + Smoldering Grove discarding + Tradeable + Shattered halves + Burned card entity
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=50 CardID=CS2_029",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=4",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE_POSITION value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=51 CardID=FIR_911",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=TAG_SCRIPT_DATA_NUM_1 value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=TAG_SCRIPT_DATA_NUM_2 value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=USE_ALTERNATE_CARD_TEXT value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE_POSITION value=2",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=52 CardID=TRD_001",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE_POSITION value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=53 CardID=SHAT_A",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE_POSITION value=4",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=54 CardID=SHAT_B",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=3",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE_POSITION value=6",
                "D 11:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=55 CardID=CS2_121",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 11:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=DECK",
                # Turn 1 (Your turn): 6 mana (RESOURCES=8, RESOURCES_USED=2, OVERLOAD_LOCKED=2 -> 6 mana)
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES value=8",
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=OVERLOAD_LOCKED value=2",
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES_USED value=2",
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                "D 11:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                # You trade Rustrot Viper (1m -> RESOURCES_USED=3, left=5m), cast Arcane Intellect-like? No, play nothing, test Secret rule-out via attack & Discover
                "D 11:00:02.0000000 GameState.DebugPrintPower() - BLOCK_START BlockType=DECK_ACTION Entity=[entityName=Rustrot Viper id=52 zone=HAND zonePos=3 cardId=TRD_001 player=1] EffectCardId=System.Collections.Generic.List`1[System.String] EffectIndex=0 Target=0 SubOption=-1",
                "D 11:00:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=52 tag=IS_USING_TRADE_OPTION value=1",
                "D 11:00:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES_USED value=3",
                "D 11:00:02.0000000 GameState.DebugPrintPower() - BLOCK_END",
                # Discover choice
                "D 11:00:03.0000000 GameState.DebugPrintEntityChoices() - id=5 Player=LocalUser#1111 TaskList=10 ChoiceType=GENERAL CountMin=1 CountMax=1",
                "D 11:00:03.0000000 GameState.DebugPrintEntityChoices() -   Source=[entityName=Fireball id=50 zone=HAND zonePos=1 cardId=CS2_029 player=1]",
                "D 11:00:03.0000000 GameState.DebugPrintEntityChoices() -   Entities[0]=[entityName=Fireball id=60 zone=SETASIDE zonePos=0 cardId=CS2_029 player=1]",
                "D 11:00:03.0000000 GameState.DebugPrintEntityChoices() -   Entities[1]=[entityName=River Crocolisk id=61 zone=SETASIDE zonePos=0 cardId=CS2_120 player=1]",
                "D 11:00:04.0000000 GameState.DebugPrintEntitiesChosen() - id=5 Player=LocalUser#1111 EntitiesCount=1",
                "D 11:00:04.0000000 GameState.DebugPrintEntitiesChosen() -   Entities[0]=[entityName=Fireball id=60 zone=SETASIDE zonePos=0 cardId=CS2_029 player=1]",
                # Burned card & Discarded Smoldering Grove at MAIN_END
                "D 11:00:05.0000000 GameState.DebugPrintPower() - META_DATA - Meta=BURNED_CARD Data=0 InfoCount=1",
                "D 11:00:05.0000000 GameState.DebugPrintPower() -             Info[0] = [entityName=Frostwolf Grunt id=55 zone=DECK zonePos=0 cardId=CS2_121 player=1]",
                "D 11:00:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_END",
                "D 11:00:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=51 tag=ZONE value=GRAVEYARD",
                "D 11:00:06.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_NEXT",
                # Opponent turn -> you lose
                "D 11:00:07.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=2",
                "D 11:00:07.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0",
                "D 11:00:07.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1",
                "D 11:00:07.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                "D 11:00:07.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                "D 11:00:08.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=LOST",
                "D 11:00:08.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=WON",
            ]
        )
        p = _write_temp_log(log)
        try:
            res = parse_power_log(p, _sample_db(), standard_only=True)
        finally:
            p.unlink(missing_ok=True)

        self.assertEqual(len(res.replays), 1)
        rep = res.replays[0]
        t1 = rep.turns[0]
        self.assertEqual(t1.start_state.your_mana_available, 6)  # 8 - 2 overload (not 4!)
        self.assertEqual(t1.start_state.your_hero.weapon.durability, 1)  # 2 base - 1 dmg
        self.assertTrue(t1.start_state.your_board[0].location_ready)  # EXHAUSTED==0 overrides cooldown
        self.assertTrue(t1.start_state.your_hand[1].countdown.discards_this_turn)
        self.assertIn("Shattered halves of Split Ray are not adjacent in hand.", t1.start_state.special_notes)
        self.assertEqual(t1.actions[0].action_type, "TRADE")
        self.assertEqual(t1.choices[0].chosen_names, ["Fireball"])

        # Dormant Taunt doesn't block lethal: Fireball (6) + River Crocolisk (2) = 8 >= 7 eff HP (with active secret)
        self.assertTrue(t1.pre_checks.missed_lethal)
        self.assertEqual(t1.pre_checks.start_damage_math.confidence, "check_secret_or_shield")
        self.assertEqual(t1.pre_checks.mana_floated, 5)
        self.assertEqual(t1.pre_checks.unused_ready_locations, ["Cosmic Springs"])
        self.assertEqual([c.name for c in t1.pre_checks.cards_burned], ["Frostwolf Grunt"])
        self.assertEqual([c.name for c in t1.pre_checks.cards_discarded], ["Smoldering Grove"])
        self.assertEqual(rep.pre_check_summary.missed_lethal_turns, [1])
        self.assertEqual(rep.pre_check_summary.your_cards_burned, ["Frostwolf Grunt"])
        self.assertEqual(rep.pre_check_summary.your_cards_discarded, ["Smoldering Grove"])

    def test_winning_turn_has_zero_false_flags_and_secret_ruleout(self) -> None:
        log = "\n".join(
            [
                "D 12:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - GameEntity EntityID=1",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=FIRST_PLAYER value=1",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2",
                "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                "D 12:00:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                "D 12:00:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=HeroOne#1000",
                "D 12:00:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=HERO_08",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=12 CardID=HERO_08",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=30",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=DAMAGE value=25",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=31 CardID=",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=SECRET",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CLASS value=MAGE",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=41 CardID=CS2_120",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=ATK value=2",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=3",
                "D 12:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=50 CardID=CS2_029",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=SPELL",
                "D 12:00:00.0000000 GameState.DebugPrintPower() -     tag=COST value=4",
                "D 12:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
                "D 12:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
                "D 12:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES value=10",
                "D 12:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                "D 12:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                # Play Fireball (rules out Counterspell)
                "D 12:00:02.0000000 GameState.DebugPrintPower() - BLOCK_START BlockType=PLAY Entity=[entityName=Fireball id=50 zone=HAND zonePos=1 cardId=CS2_029 player=1] EffectCardId=System.Collections.Generic.List`1[System.String] EffectIndex=0 Target=[entityName=Jaina Proudmoore id=12 zone=PLAY zonePos=0 cardId=HERO_08 player=2] SubOption=-1",
                "D 12:00:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=50 tag=ZONE value=GRAVEYARD",
                "D 12:00:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES_USED value=4",
                "D 12:00:02.0000000 GameState.DebugPrintPower() - BLOCK_END",
                "D 12:00:03.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON",
                "D 12:00:03.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST",
            ]
        )
        p = _write_temp_log(log)
        try:
            res = parse_power_log(p, _sample_db(), standard_only=True)
        finally:
            p.unlink(missing_ok=True)

        rep = res.replays[0]
        t1 = rep.turns[0]
        # Counterspell ruled out in end_state secret snapshot
        opp_sec = t1.end_state.opponent_secrets_and_quests[0]
        self.assertIn("Counterspell", opp_sec.ruled_out_secrets)
        self.assertNotIn("Counterspell", opp_sec.possible_secrets)
        # Winning turn must have zero false flags
        self.assertFalse(t1.pre_checks.missed_lethal)
        self.assertEqual(t1.pre_checks.mana_floated, 0)
        self.assertEqual(t1.pre_checks.unused_attacks, [])

    def test_observed_decklist_reconstruction_excludes_generated_and_coin(self) -> None:
        log = "\n".join(
            [
                "D 13:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - GameEntity EntityID=1",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=2]",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=FIRST_PLAYER value=1",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2 GameAccountId=[hi=1 lo=3]",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=HERO_08",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=11 CardID=HERO_01",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                # Two copies of Fireball in initial deck (note ZONE before CONTROLLER in one)
                "D 13:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=20 CardID=",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=DECK",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=21 CardID=",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=DECK",
                # Opponent deck card
                "D 13:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=22 CardID=",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=DECK",
                "D 13:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                "D 13:00:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                "D 13:00:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=LocalUser#1111",
                "D 13:00:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=BEGIN_MULLIGAN",
                "D 13:00:00.0000000 GameState.DebugPrintPower() - SHOW_ENTITY - Updating Entity=20 CardID=CS2_029",
                "D 13:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 13:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
                "D 13:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
                "D 13:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                "D 13:00:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                "D 13:00:01.0000000 GameState.DebugPrintPower() - SHOW_ENTITY - Updating Entity=21 CardID=CS2_029",
                "D 13:00:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=HAND",
                "D 13:00:01.0000000 GameState.DebugPrintPower() - SHOW_ENTITY - Updating Entity=22 CardID=CS2_120",
                "D 13:00:01.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                # Generated card shuffled into deck mid-game (must NOT enter observed deck)
                "D 13:00:02.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=88 CardID=CS2_023",
                "D 13:00:02.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                "D 13:00:02.0000000 GameState.DebugPrintPower() -     tag=ZONE value=DECK",
                "D 13:00:02.0000000 GameState.DebugPrintPower() -     tag=CREATOR value=10",
                "D 13:00:03.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON",
                "D 13:00:03.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST",
            ]
        )
        p = _write_temp_log(log)
        try:
            res = parse_power_log(p, _sample_db(), standard_only=True)
        finally:
            p.unlink(missing_ok=True)

        rep = res.replays[0]
        self.assertEqual(len(rep.your_deck_observed), 1)
        self.assertEqual(rep.your_deck_observed[0].name, "Fireball")
        self.assertEqual(rep.your_deck_observed[0].count, 2)
        self.assertEqual(len(rep.opponent_deck_observed), 1)
        self.assertEqual(rep.opponent_deck_observed[0].name, "River Crocolisk")


if __name__ == "__main__":
    unittest.main()
