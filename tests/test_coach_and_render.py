"""Unit tests for hs_coach.coach and hs_coach.render."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from hs_coach.coach import (
    CoachConfig,
    build_coaching_prompt,
    coach_game,
    dry_run_summary,
    estimate_cost_usd,
    resolve_coach_config,
)
from hs_coach.render import render_html_report
from hs_coach.schemas import (
    BoardEntitySnapshot,
    BoardStateSnapshot,
    CardReference,
    DeckCard,
    GameReplay,
    HeroState,
    MatchMetadata,
    MatchPreCheckSummary,
    MulliganCard,
    MulliganSnapshot,
    TurnAction,
    TurnPreChecks,
    TurnSnapshot,
    WeaponState,
)


def _dummy_replay() -> GameReplay:
    hero_you = HeroState(
        hero_class="MAGE",
        card_id="HERO_08",
        name="Jaina Proudmoore",
        hp=30,
        max_hp=30,
        armor=0,
        effective_hp=30,
    )
    hero_opp = HeroState(
        hero_class="WARRIOR",
        card_id="HERO_01",
        name="Garrosh Hellscream",
        hp=30,
        max_hp=30,
        armor=0,
        effective_hp=30,
    )
    state = BoardStateSnapshot(
        your_mana_available=4,
        your_mana_max=4,
        opponent_mana_available=3,
        opponent_mana_max=3,
        your_hero=hero_you,
        opponent_hero=hero_opp,
    )
    return GameReplay(
        metadata=MatchMetadata(
            game_id="abc123def4567890",
            format_type="FT_STANDARD",
            game_type="GT_RANKED",
            is_standard_constructed=True,
            your_class="MAGE",
            opponent_class="WARRIOR",
            your_hero="Jaina Proudmoore",
            opponent_hero="Garrosh Hellscream",
            went_first=True,
            has_coin=False,
            result="loss",
            your_terminal_playstate="LOST",
            opponent_terminal_playstate="WON",
            end_reason="lethal",
            total_turns=8,
            your_turns_count=4,
        ),
        mulligan=MulliganSnapshot(
            went_first=True,
            has_coin=False,
            offered=[
                MulliganCard(
                    entity_id=10,
                    card_id="CS2_029",
                    name="Fireball",
                    cost=4,
                    text="Deal 6 damage.",
                    kept=False,
                )
            ],
            tossed_names=["Fireball"],
        ),
        turns=[
            TurnSnapshot(
                turn_number=1,
                player_turn_number=1,
                active_player="you",
                start_state=state,
                end_state=state,
                pre_checks=TurnPreChecks(mana_floated=1),
            )
        ],
        cards_seen={
            "CS2_029": CardReference(
                card_id="CS2_029",
                name="Fireball",
                card_type="SPELL",
                base_cost=4,
                spell_school="Fire",
                text="Deal 6 damage.",
            )
        },
        pre_check_summary=MatchPreCheckSummary(total_mana_floated=1),
    )


def _valid_coach_payload() -> dict:
    return {
        "match_summary": "Mage failed to race Warrior early and lost on turn 8.",
        "your_archetype": "Fire Spell Mage",
        "opponent_archetype": "Control Warrior",
        "overall_grade": "B-",
        "key_takeaway": "Protect early tempo instead of floating mana on turn 1.",
        "mulligan_review": {
            "overall_grade": "A",
            "summary": "Tossing Fireball for early drops was disciplined.",
            "card_verdicts": [
                {
                    "card_name": "Fireball",
                    "card_id": "CS2_029",
                    "action_taken": "tossed",
                    "verdict": "good",
                    "recommended_action": "toss",
                    "reasoning": "Too slow in opening hand.",
                }
            ],
        },
        "turning_points": [
            {
                "rank": 1,
                "turn_number": 1,
                "severity": "Inaccuracy",
                "title": "Floated mana early",
                "turn_context": "Turn 1 open board",
                "what_was_played": "Passed turn",
                "better_line": "Develop a 1-drop if available.",
                "why_it_mattered": "Conceded early board initiative.",
                "habit_tag": "mana-efficiency",
            }
        ],
        "best_plays": [
            {
                "turn_number": 1,
                "title": "Disciplined mulligan",
                "what_was_played": "Tossed 4-cost Fireball",
                "why_it_was_good": "Increased odds of curving out.",
            }
        ],
        "habit_tags": [
            {
                "tag": "mana-efficiency",
                "polarity": "mistake",
                "turns": [1],
                "note": "Floated 1 mana.",
            }
        ],
    }


class TestCoachAndRender(unittest.TestCase):
    def test_prompt_and_dry_run(self) -> None:
        rep = _dummy_replay()
        sys_p, user_p = build_coaching_prompt(rep)
        self.assertIn("CoachReportContent", sys_p)
        self.assertIn("Fireball", user_p)
        self.assertIn("Deal 6 damage.", user_p)

        dr = dry_run_summary(rep, model="claude-sonnet-4-5")
        self.assertGreater(dr["estimated_input_tokens"], 100)
        self.assertIn("claude-sonnet-4-5", dr["estimated_costs_usd"])
        self.assertIsNotNone(estimate_cost_usd("gpt-4.1", 1000, 500))

    def test_coach_game_anthropic_repair_retry(self) -> None:
        rep = _dummy_replay()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                # First response has broken JSON to verify repair retry
                body = {
                    "content": [{"type": "text", "text": "```json\n{\"match_summary\": \"bad\"}\n```"}],
                    "usage": {"input_tokens": 500, "output_tokens": 20},
                }
            else:
                body = {
                    "content": [{"type": "text", "text": "```json\n" + json.dumps(_valid_coach_payload()) + "\n```"}],
                    "usage": {"input_tokens": 600, "output_tokens": 300, "cache_read_input_tokens": 100},
                }
            return httpx.Response(200, json=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = CoachConfig(
            provider="anthropic",
            model="claude-sonnet-4-5",
            api_key="test-key",
            base_url="https://api.anthropic.com",
        )
        report = coach_game(rep, cfg, client=client)
        self.assertEqual(calls, 2)
        self.assertEqual(report.overall_grade, "B-")
        self.assertEqual(report.game_id, rep.metadata.game_id)
        self.assertEqual(report.usage.input_tokens, 1100)
        self.assertEqual(report.usage.output_tokens, 320)
        self.assertGreater(report.usage.estimated_cost_usd, 0)

        html = render_html_report(rep, report)
        self.assertIn("Fire Spell Mage vs Control Warrior", html)
        self.assertIn("Floated mana early", html)
        self.assertIn("claude-sonnet-4-5", html)

    def test_render_replay_only_and_escaping(self) -> None:
        rep = _dummy_replay()
        rep.cards_seen["XSS"] = CardReference(
            card_id="XSS",
            name="<script>alert(1)</script>",
            text="Test <b>safe</b>",
        )
        html = render_html_report(rep, None)
        self.assertIn("Replay-Only Preview", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_openrouter_resolution_pricing_and_call(self) -> None:
        env = {"OPENROUTER_API_KEY": "sk-or-test"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = resolve_coach_config()
        self.assertEqual(cfg.provider, "openrouter")
        self.assertEqual(cfg.model, "google/gemini-3.1-pro-preview")
        self.assertEqual(cfg.base_url, "https://openrouter.ai/api/v1")

        self.assertIsNotNone(estimate_cost_usd("anthropic/claude-sonnet-4.5", 10000, 1000))
        self.assertIsNotNone(estimate_cost_usd("google/gemini-2.5-flash", 10000, 1000))

        rep = _dummy_replay()
        captured_payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(_valid_coach_payload())}}],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 250, "cost": 0.04123},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        or_cfg = CoachConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.5",
            api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
        )
        report = coach_game(rep, or_cfg, client=client)
        self.assertNotIn("response_format", captured_payloads[0])
        self.assertEqual(report.usage.estimated_cost_usd, 0.04123)

    def test_prompt_surfaces_legality_and_end_state_markers(self) -> None:
        rep = _dummy_replay()
        t = rep.turns[0]
        t.start_state.your_hero.weapon = WeaponState(
            entity_id=99,
            card_id="W1",
            name="Stone Gauntlet",
            attack=2,
            durability=1,
            effective_text="Gain 5 Armor after attacking.",
        )
        t.start_state.your_board = [
            BoardEntitySnapshot(
                position=1,
                entity_id=101,
                card_id="M1",
                name="Fresh Golem",
                card_type="MINION",
                attack=5,
                health=5,
                can_attack=False,
            )
        ]
        t.start_state.opponent_board = [
            BoardEntitySnapshot(
                position=1,
                entity_id=201,
                card_id="M2",
                name="Seismopod",
                card_type="MINION",
                attack=6,
                health=6,
                keywords=["Taunt", "Elusive"],
            )
        ]
        t.end_state = t.start_state.model_copy(deep=True)
        t.end_state.opponent_board = []

        sys_p, user_p = build_coaching_prompt(rep)
        self.assertIn("[CANNOT ATTACK THIS TURN]", sys_p)
        self.assertIn("[CANNOT ATTACK THIS TURN]", user_p)
        self.assertIn("[TAUNT ACTIVE: attacks MUST target Taunt minion(s): Seismopod", user_p)
        self.assertIn("Equipped Weapon: Stone Gauntlet 2/1 (Gain 5 Armor after attacking.)", user_p)
        self.assertIn("Opp Board: (empty — all enemy minions cleared)", user_p)

    def test_observed_decklists_in_prompt_and_html(self) -> None:
        rep = _dummy_replay()
        rep.your_deck_observed = [
            DeckCard(card_id="CS2_029", name="Fireball", cost=4, count=2, card_type="SPELL")
        ]
        rep.opponent_deck_observed = [
            DeckCard(card_id="CS2_120", name="River Crocolisk", cost=2, count=1, card_type="MINION")
        ]
        _, user_p = build_coaching_prompt(rep)
        self.assertIn("Observed Decklists", user_p)
        self.assertIn("(4) Fireball x2", user_p)
        self.assertIn("(2) River Crocolisk", user_p)

        html = render_html_report(rep, None)
        self.assertIn("Observed Decklists", html)
        self.assertIn("(4) Fireball x2", html)

    def test_polish_prompts_glossary_non_mana_actions_and_habit_tags(self) -> None:
        rep = _dummy_replay()
        rep.turns[0].actions = [
            TurnAction(
                seq=1,
                actor="you",
                action_type="PLAY",
                source_card_id="CS2_029",
                source_name="Fireball",
                mana_cost=4,
                summary="Plays Fireball",
            ),
            TurnAction(
                seq=2,
                actor="you",
                action_type="ATTACK",
                source_card_id="CS2_120",
                source_name="River Crocolisk",
                mana_cost=2,  # Legacy replay non-mana cost must still not display
                summary="Attacks with River Crocolisk",
            ),
            TurnAction(
                seq=3,
                actor="you",
                action_type="ACTIVATE",
                source_card_id="LOC_001",
                source_name="Cosmic Springs",
                mana_cost=2,
                summary="Activates Cosmic Springs",
            ),
        ]
        rep.cards_seen["HERO_08"] = CardReference(
            card_id="HERO_08", name="Jaina Proudmoore", card_type="HERO", text=""
        )
        rep.cards_seen["DET_1"] = CardReference(
            card_id="DET_1",
            name="Detected!",
            card_type="SPELL",
            base_cost=0,
            text="This was played on the friendly side.",
        )
        rep.cards_seen["ENCH_1"] = CardReference(
            card_id="ENCH_1",
            name="Extra Steel",
            card_type="ENCHANTMENT",
            text="+2 Armor",
        )
        rep.cards_seen["CS2_029"].text = "Battlecry:& Deal d6 damage."

        sys_p, user_p = build_coaching_prompt(rep)
        # New prompt guardrails
        self.assertIn("[Stealth]", sys_p)
        self.assertIn("[Elusive]", sys_p)
        self.assertIn("Evaluate high-impact `Choices`", sys_p)
        self.assertIn("game-ending", sys_p)
        # Prompt omits non-mana cost tags & separates/filters internal glossary entries
        self.assertIn("1. Plays Fireball (4m)", user_p)
        self.assertIn("2. Attacks with River Crocolisk\n", user_p)
        self.assertNotIn("River Crocolisk (2m)", user_p)
        self.assertNotIn("Cosmic Springs (2m)", user_p)
        self.assertNotIn("Detected!", user_p)
        self.assertNotIn("Extra Steel", user_p)
        self.assertNotIn("[HERO_08]", user_p)
        self.assertIn("Battlecry: Deal 6 damage.", user_p)

        report_payload = _valid_coach_payload()
        report_payload["turning_points"][0]["habit_tag"] = "threat_assessment"
        report_payload["habit_tags"][0]["tag"] = "resource_management"
        from hs_coach.schemas import CoachReport

        report = CoachReport(**report_payload, game_id=rep.metadata.game_id)
        html = render_html_report(rep, report)
        self.assertIn("Threat Assessment", html)
        self.assertIn("Resource Management", html)
        self.assertNotIn("threat_assessment", html)
        self.assertNotIn("resource_management", html)
        self.assertIn("Card Glossary (1 playable cards seen in match)", html)
        self.assertIn("Enchantments &amp; Effects (1)", html)
        self.assertNotIn("Detected!", html)
        self.assertNotIn("<code>HERO_08</code>", html)
        self.assertIn("Plays Fireball <span style=\"color: var(--muted);\">(4m)</span>", html)
        self.assertNotIn("Attacks with River Crocolisk <span", html)
        self.assertNotIn("Activates Cosmic Springs <span", html)


if __name__ == "__main__":
    unittest.main()
