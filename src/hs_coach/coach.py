"""Model-agnostic post-game LLM coach, grounded prompt builder, and strict validator."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from hs_coach.cards import CardDB, canonicalize_replay, clean_hs_display_text, partition_glossary
from hs_coach.schemas import (
    BoardEntitySnapshot,
    BoardStateSnapshot,
    CoachReport,
    CoachReportContent,
    DeckReview,
    DeckReviewContent,
    GameReplay,
    TurnSnapshot,
    UsageStats,
    validate_completed_replay,
)

ProviderName = Literal["anthropic", "openai", "gemini", "openrouter", "openai_compatible"]

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4.1",
    "gemini": "gemini-2.5-pro",
    "openrouter": "google/gemini-3.1-pro-preview",
    "openai_compatible": "gpt-4o-mini",
}

DEFAULT_BASE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai_compatible": "http://localhost:11434/v1",
}

# Approximate USD pricing per 1M tokens (input, output) for cost transparency.
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "o4-mini": (1.10, 4.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
}


@dataclass(frozen=True)
class CoachConfig:
    provider: ProviderName
    model: str
    api_key: str
    base_url: str
    timeout: float = 120.0
    temperature: float = 0.2


def estimate_tokens(text: str) -> int:
    """Fast character-based token estimate (~3.8 chars per token for mixed prose/JSON)."""
    if not text:
        return 0
    return math.ceil(len(text) / 3.8)


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Estimate USD cost for known models using longest prefix match."""
    norm = model.lower().strip()
    if "/" in norm:
        norm = norm.split("/", 1)[1]
    norm = norm.replace("claude-sonnet-4.5", "claude-sonnet-4-5").replace(
        "claude-opus-4.5", "claude-opus-4-5"
    ).replace("claude-haiku-4.5", "claude-haiku-4-5").replace(
        "claude-3.5-sonnet", "claude-3-5-sonnet"
    ).replace("claude-3.5-haiku", "claude-3-5-haiku")
    matched_rates: tuple[float, float] | None = None
    best_len = -1
    for prefix, rates in MODEL_PRICING_PER_1M.items():
        if norm.startswith(prefix) and len(prefix) > best_len:
            matched_rates = rates
            best_len = len(prefix)
    if matched_rates is None:
        return None
    in_rate, out_rate = matched_rates
    cost = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0
    return round(cost, 6)


def _load_dotenv_if_present(dotenv_path: Path | None = None) -> None:
    """Populate missing os.environ entries from a local .env file without overriding set vars."""
    path = dotenv_path or (Path.cwd() / ".env")
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def resolve_coach_config(
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
) -> CoachConfig:
    """Resolve LLM provider, model, API key, and base URL from explicit args or env vars."""
    _load_dotenv_if_present()
    raw_provider = (provider or os.environ.get("HS_COACH_PROVIDER", "")).strip().lower()
    env_key = os.environ.get("HS_COACH_API_KEY", "").strip()

    if not raw_provider:
        if os.environ.get("OPENROUTER_API_KEY"):
            raw_provider = "openrouter"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            raw_provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            raw_provider = "openai"
        elif os.environ.get("GEMINI_API_KEY"):
            raw_provider = "gemini"
        elif os.environ.get("HS_COACH_BASE_URL"):
            raw_provider = "openai_compatible"
        else:
            raise ValueError(
                "No LLM provider configured. Set HS_COACH_PROVIDER and an API key "
                "(HS_COACH_API_KEY, OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY)."
            )

    if raw_provider not in ("anthropic", "openai", "gemini", "openrouter", "openai_compatible"):
        raise ValueError(f"Unsupported HS_COACH_PROVIDER: {raw_provider!r}")
    prov: ProviderName = raw_provider  # type: ignore[assignment]

    key = (api_key or env_key).strip()
    if not key:
        if prov == "openrouter":
            key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        elif prov == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        elif prov == "openai":
            key = os.environ.get("OPENAI_API_KEY", "").strip()
        elif prov == "gemini":
            key = os.environ.get("GEMINI_API_KEY", "").strip()
        elif prov == "openai_compatible":
            key = os.environ.get("OPENAI_API_KEY", "").strip() or "local"

    if not key and prov != "openai_compatible":
        raise ValueError(
            f"Missing API key for provider {prov!r}. Set HS_COACH_API_KEY or the provider env var."
        )

    resolved_model = (
        model or os.environ.get("HS_COACH_MODEL", "").strip() or DEFAULT_MODELS[prov]
    )
    resolved_base = (
        base_url
        or os.environ.get("HS_COACH_BASE_URL", "").strip()
        or DEFAULT_BASE_URLS[prov]
    ).rstrip("/")

    return CoachConfig(
        provider=prov,
        model=resolved_model,
        api_key=key,
        base_url=resolved_base,
        timeout=timeout,
    )


def _fmt_board_entity(
    ent: BoardEntitySnapshot, *, is_your_board: bool = False
) -> str:
    if ent.card_type == "LOCATION":
        state = "READY" if ent.location_ready else "COOLDOWN"
        return f"[{ent.position}] Location: {ent.name} ({ent.health} uses, {state})"
    parts = [f"[{ent.position}] {ent.name} {ent.attack}/{ent.health}"]
    if ent.races:
        parts.append(f"<{'/'.join(ent.races)}>")
    if ent.keywords:
        parts.append(f"({', '.join(ent.keywords)})")
    if ent.spell_damage:
        parts.append(f"(Spell Dmg +{ent.spell_damage})")
    if ent.old_god_abilities:
        rem = [a.name for a in ent.old_god_abilities if not a.used]
        parts.append(f"[Old God unused: {', '.join(rem) or 'all used'}]")
    if ent.dark_gift:
        parts.append(f"[Dark Gift: {ent.dark_gift}]")
    if ent.assembled_note:
        parts.append(f"[{ent.assembled_note}]")
    if is_your_board and not ent.dormant:
        parts.append("[CAN ATTACK]" if ent.can_attack else "[CANNOT ATTACK THIS TURN]")
    return " ".join(parts)


def _fmt_board_state(
    state: BoardStateSnapshot,
    *,
    include_hand: bool,
    is_end_state: bool = False,
) -> list[str]:
    lines: list[str] = []
    yh = state.your_hero
    oh = state.opponent_hero
    yw_txt = (
        f" ({yh.weapon.effective_text})"
        if yh.weapon and yh.weapon.effective_text
        else ""
    )
    ow_txt = (
        f" ({oh.weapon.effective_text})"
        if oh.weapon and oh.weapon.effective_text
        else ""
    )
    yw = (
        f" | Equipped Weapon: {yh.weapon.name} {yh.weapon.attack}/{yh.weapon.durability}{yw_txt}"
        if yh.weapon
        else ""
    )
    ow = (
        f" | Equipped Weapon: {oh.weapon.name} {oh.weapon.attack}/{oh.weapon.durability}{ow_txt}"
        if oh.weapon
        else ""
    )
    yhp = (
        f" | HP: ({yh.hero_power.cost}) {yh.hero_power.name}"
        + (" [used]" if yh.hero_power.used else "")
        if yh.hero_power
        else ""
    )
    ohp = (
        f" | HP: ({oh.hero_power.cost}) {oh.hero_power.name}"
        if oh.hero_power
        else ""
    )
    lines.append(
        f"  You ({yh.hero_class}): {yh.hp} HP + {yh.armor} Armor | "
        f"Mana {state.your_mana_available}/{state.your_mana_max}"
        + (f" (Overload {state.your_overload_locked})" if state.your_overload_locked else "")
        + f" | Deck {state.your_deck_size}{yw}{yhp}"
    )
    lines.append(
        f"  Opp ({oh.hero_class}): {oh.hp} HP + {oh.armor} Armor | "
        f"Mana {state.opponent_mana_available}/{state.opponent_mana_max} | "
        f"Hand {state.opponent_hand_size} | Deck {state.opponent_deck_size}{ow}{ohp}"
    )
    if state.opponent_board:
        lines.append(
            "  Opp Board (L->R): "
            + "; ".join(_fmt_board_entity(e) for e in state.opponent_board)
        )
        taunts = [
            e.name
            for e in state.opponent_board
            if e.card_type == "MINION"
            and "Taunt" in e.keywords
            and not e.dormant
            and "Stealth" not in e.keywords
            and "Immune" not in e.keywords
        ]
        if taunts:
            lines.append(
                f"  [TAUNT ACTIVE: attacks MUST target Taunt minion(s): {', '.join(taunts)} — "
                "cannot attack hero or non-Taunt minions]"
            )
    elif is_end_state:
        lines.append("  Opp Board: (empty — all enemy minions cleared)")
    else:
        lines.append("  Opp Board: (empty)")

    if state.your_board:
        lines.append(
            "  Your Board (L->R): "
            + "; ".join(
                _fmt_board_entity(e, is_your_board=(not is_end_state))
                for e in state.your_board
            )
        )
    else:
        lines.append("  Your Board: (empty)")

    if state.opponent_secrets_and_quests:
        sq_txt = []
        for sq in state.opponent_secrets_and_quests:
            if sq.kind in ("quest", "side_quest"):
                sq_txt.append(f"{sq.name} ({sq.quest_progress}/{sq.quest_total})")
            elif sq.revealed:
                sq_txt.append(f"Revealed Secret: {sq.name}")
            else:
                sq_txt.append(
                    f"Hidden {sq.card_class.title()} Secret "
                    f"(could be: {', '.join(sq.possible_secrets) or 'unknown'})"
                )
        lines.append("  Opp Secrets/Quests: " + "; ".join(sq_txt))

    if state.your_secrets_and_quests:
        ysq = [
            f"{sq.name}"
            + (
                f" ({sq.quest_progress}/{sq.quest_total})"
                if sq.quest_total is not None
                else ""
            )
            for sq in state.your_secrets_and_quests
        ]
        lines.append("  Your Secrets/Quests: " + "; ".join(ysq))

    if include_hand:
        if state.your_hand:
            hparts = []
            for hc in state.your_hand:
                tag_bits = []
                if hc.outcast_active:
                    tag_bits.append("OUTCAST")
                if hc.spell_damage_bonus:
                    tag_bits.append(f"+{hc.spell_damage_bonus}sp")
                if hc.countdown:
                    tag_bits.append(
                        "DISCARDS_THIS_TURN"
                        if hc.countdown.discards_this_turn
                        else f"cd:{hc.countdown.turns_left_including_this}t"
                    )
                if hc.prepare and hc.prepare.available:
                    tag_bits.append("PREPARE_AVAIL")
                elif hc.prepare and hc.prepare.prepared_discount:
                    tag_bits.append(f"PREPARED(-{hc.prepare.prepared_discount})")
                if hc.shattered_half:
                    tag_bits.append("SHATTERED_HALF")
                if hc.enchantments:
                    tag_bits.append(f"ench:{','.join(hc.enchantments)}")
                stats = (
                    f" {hc.attack}/{hc.health}"
                    if hc.attack is not None and hc.health is not None
                    else ""
                )
                extra = f" [{' '.join(tag_bits)}]" if tag_bits else ""
                hparts.append(f"[{hc.position}] ({hc.cost}) {hc.name}{stats}{extra}")
            lines.append("  Your Hand (L->R): " + " | ".join(hparts))
        else:
            lines.append("  Your Hand: (empty)")

    if state.special_notes:
        lines.append("  Special Notes: " + " | ".join(state.special_notes))
    return lines


def _fmt_turn(t: TurnSnapshot) -> str:
    header = (
        f"### Turn {t.turn_number} "
        f"({'Your' if t.active_player == 'you' else 'Opponent'} Turn #{t.player_turn_number})"
    )
    out = [header, "Start State:"]
    out.extend(_fmt_board_state(t.start_state, include_hand=(t.active_player == "you")))

    if t.choices:
        out.append("Choices:")
        for ch in t.choices:
            if ch.options_visible:
                opts = ", ".join(
                    f"({o.cost}) {o.name}" if o.cost is not None else o.name
                    for o in ch.options
                )
                picked = ", ".join(ch.chosen_names) or "none"
                out.append(
                    f"  - [{ch.actor}] {ch.kind} from {ch.source_name or '?'}: "
                    f"offered [{opts}] -> picked [{picked}]"
                )
            else:
                out.append(
                    f"  - [{ch.actor}] hidden {ch.kind} from {ch.source_name or '?'}"
                )

    if t.actions:
        out.append("Actions (in order):")
        for act in t.actions:
            cost_s = (
                f" ({act.mana_cost}m)"
                if act.action_type in ("PLAY", "HERO_POWER") and act.mana_cost is not None
                else ""
            )
            out.append(f"  {act.seq}. {act.summary}{cost_s}")
    else:
        out.append("Actions: (none)")

    pc = t.pre_checks
    flag_lines: list[str] = []
    if pc.missed_lethal and pc.missed_lethal_detail:
        flag_lines.append(f"MISSED_LETHAL: {pc.missed_lethal_detail}")
    elif pc.start_damage_math:
        dm = pc.start_damage_math
        flag_lines.append(
            f"Start damage math: max {dm.max_face_damage} vs opp eff HP {dm.enemy_effective_hp}"
            + (" (Taunt blocking)" if dm.enemy_taunt_blocking else "")
        )
    if t.active_player == "you":
        flag_lines.append(f"Mana floated at EOT: {pc.mana_floated}")
    if pc.prepare_was_available:
        flag_lines.append("Prepare card was available to absorb leftover mana")
    if pc.unused_attacks:
        flag_lines.append(f"Unused attacks at EOT: {', '.join(pc.unused_attacks)}")
    if pc.unused_ready_locations:
        flag_lines.append(
            f"Unused READY locations at EOT: {', '.join(pc.unused_ready_locations)}"
        )
    if pc.unused_hero_power:
        flag_lines.append("Hero Power usable with floated mana but unused")
    if pc.cards_burned:
        flag_lines.append(
            "Burned: " + ", ".join(f"{c.name} ({c.owner})" for c in pc.cards_burned)
        )
    if pc.cards_discarded:
        flag_lines.append(
            "Discarded: " + ", ".join(f"{c.name} ({c.owner})" for c in pc.cards_discarded)
        )
    if flag_lines:
        out.append("Pre-Checks: " + " | ".join(flag_lines))

    out.append("End State:")
    out.extend(_fmt_board_state(t.end_state, include_hand=False, is_end_state=True))
    return "\n".join(out)


def build_coaching_prompt(
    replay: GameReplay,
    *,
    card_db: CardDB | None = None,
) -> tuple[str, str]:
    """Build grounded (system_prompt, user_prompt) for post-game coaching."""
    if card_db is not None:
        replay = canonicalize_replay(replay, card_db)
    schema_json = json.dumps(CoachReportContent.model_json_schema(), indent=2)
    system_prompt = (
        "You are an expert Hearthstone Standard Constructed post-game AI coach.\n"
        "Strict grounding rules:\n"
        "1. Rely ONLY on the provided Match Metadata, Card Glossary, Turn Snapshots, and Python Pre-Checks.\n"
        "2. NEVER invent card text, mana costs, stats, or cards that were not observed in the replay.\n"
        "3. Respect Hearthstone legality and targeting markers in the state snapshots: NEVER suggest attacking "
        "with a `[CANNOT ATTACK THIS TURN]` minion, NEVER suggest bypassing `[TAUNT ACTIVE]` (attacks must hit "
        "Taunt minions first), NEVER suggest targeting `[Stealth]` minions with attacks, spells, or Hero Power, "
        "NEVER suggest targeting `[Elusive]` minions with targeted spells or Hero Power (only attacks or untargeted "
        "AoE can hit `[Elusive]`), and ALWAYS verify what survived or died against `End State` (not guesses).\n"
        "4. Respect deterministic Python pre-checks (`MISSED_LETHAL`, `mana_floated`, `cards_burned`, "
        "`unused_attacks`, `unused_ready_locations`), while checking context such as active enemy Secrets, "
        "Divine Shield, or lethal setups before judging a play a mistake.\n"
        "5. Evaluate EVERY card offered to 'you' in the Mulligan individually.\n"
        "6. Evaluate high-impact `Choices` (`offered [...] -> picked [...]`, including Discover spells, Dark Gift, "
        "and weapon/minion options) when another offered option clearly addressed the board state—such as Divine Shield, "
        "Taunt, or lethal—better than the pick taken.\n"
        "7. Identify up to 3 highest-impact Turning Points ranked 1..3 using severity in "
        "['Missed Lethal', 'Blunder', 'Inaccuracy']. Name exact cards, mana, and lines. Format every habit tag in "
        "concise Title Case (e.g., `Mana Efficiency`, `Threat Assessment`).\n"
        "8. Stay strictly consistent with the match result in `# Match Metadata`: NEVER describe a mid-game play "
        "as 'game-ending', 'game-winning', or 'sealing the win' when the match result is LOSS or TIE.\n"
        "9. Output ONLY a single valid JSON object conforming strictly to the JSON Schema below — no markdown "
        "fences or extra prose.\n\n"
        f"JSON Schema:\n{schema_json}"
    )

    m = replay.metadata
    ps = replay.pre_check_summary
    sections: list[str] = []

    sections.append(
        "# Match Metadata\n"
        f"- Game ID: {m.game_id}\n"
        f"- Format / Mode: {m.format_type} / {m.game_type}\n"
        f"- Matchup: You ({m.your_class} - {m.your_hero}) vs Opponent ({m.opponent_class} - {m.opponent_hero})\n"
        f"- Turn Order: {'Went first' if m.went_first else 'Went second (Had The Coin)'}\n"
        f"- Result: {m.result.upper()} ({m.end_reason}) in {m.total_turns} turns "
        f"({m.your_turns_count} of your turns)\n"
        f"- Start of Game Effects: you={m.start_of_game_effects.get('you', [])}, "
        f"opponent={m.start_of_game_effects.get('opponent', [])}"
    )

    if replay.your_deck_observed or replay.opponent_deck_observed:
        def _fmt_deck(cards: list[Any]) -> str:
            if not cards:
                return "(none observed)"
            return ", ".join(
                f"({c.cost}) {c.name}" + (f" x{c.count}" if c.count > 1 else "")
                if c.cost is not None
                else f"{c.name}" + (f" x{c.count}" if c.count > 1 else "")
                for c in cards
            )

        y_total = sum(c.count for c in replay.your_deck_observed)
        o_total = sum(c.count for c in replay.opponent_deck_observed)
        sections.append(
            "# Observed Decklists (Original Deck Cards Seen — Generated Cards Excluded)\n"
            f"- Your Observed Deck ({y_total} cards seen): {_fmt_deck(replay.your_deck_observed)}\n"
            f"- Opponent Observed Deck ({o_total} cards seen): {_fmt_deck(replay.opponent_deck_observed)}"
        )

    sections.append(
        "# Deterministic Pre-Check Summary\n"
        f"- Missed lethal turns: {ps.missed_lethal_turns or 'None'}\n"
        f"- Total mana floated on your turns: {ps.total_mana_floated}\n"
        f"- Your cards burned: {ps.your_cards_burned or 'None'}\n"
        f"- Your cards discarded: {ps.your_cards_discarded or 'None'}\n"
        f"- Turns with unused face attacks: {ps.turns_with_unused_attacks or 'None'}\n"
        f"- Turns with unused ready locations: {ps.turns_with_unused_ready_locations or 'None'}"
    )

    playable_glossary, _ = partition_glossary(replay.cards_seen)
    glossary_lines = ["# Card Glossary (Playable Cards Seen In This Match)"]
    for ref in playable_glossary:
        cost_s = f"({ref.base_cost}) " if ref.base_cost is not None else ""
        stats_s = (
            f" {ref.base_attack}/{ref.base_health}"
            if ref.base_attack is not None and ref.base_health is not None
            else ""
        )
        meta_bits = [b for b in (ref.card_type, ref.spell_school, "/".join(ref.races)) if b]
        meta_s = f" [{' | '.join(meta_bits)}]" if meta_bits else ""
        pos_s = " <POSITION MATTERS>" if ref.position_matters else ""
        cleaned_ref_txt = clean_hs_display_text(ref.text) if ref.text else ""
        glossary_lines.append(
            f"- {cost_s}{ref.name}{stats_s}{meta_s}{pos_s} [{ref.card_id}]: "
            f"{cleaned_ref_txt or '(no text)'}"
        )
    sections.append("\n".join(glossary_lines))

    mull = replay.mulligan
    mull_lines = [
        "# Mulligan",
        f"- Went first: {mull.went_first} | Has Coin: {mull.has_coin}",
        f"- Opponent kept {mull.opponent_kept_count}, tossed {mull.opponent_tossed_count}",
        "- Offered to you:",
    ]
    for mc in mull.offered:
        status = "KEPT" if mc.kept else "TOSSED"
        cost_s = f"({mc.cost}) " if mc.cost is not None else ""
        mull_lines.append(f"  * [{status}] {cost_s}{mc.name} [{mc.card_id}] -- {mc.text}")
    mull_lines.append(
        "- Final opening hand (before Turn 1 draw): "
        + ", ".join(f"({c.cost}) {c.name}" for c in mull.final_opening_hand)
    )
    sections.append("\n".join(mull_lines))

    turn_lines = ["# Turn-by-Turn Decision Snapshots"]
    turn_lines.extend(_fmt_turn(t) for t in replay.turns)
    sections.append("\n\n".join(turn_lines))

    return system_prompt, "\n\n".join(sections)


def extract_json_object(raw_text: str) -> str:
    """Strip markdown fences or surrounding text and return the outer JSON object."""
    s = raw_text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s


def _call_llm_provider(
    config: CoachConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, int, int, int, float | None]:
    """Call the configured LLM provider and return (text, in_tokens, out_tokens, cached_tokens, cost_usd)."""
    owns_client = client is None
    http = client or httpx.Client(timeout=config.timeout)
    try:
        if config.provider == "anthropic":
            url = f"{config.base_url}/v1/messages"
            headers = {
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": config.model,
                "max_tokens": 4096,
                "temperature": config.temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            resp = http.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                blk.get("text", "")
                for blk in data.get("content", [])
                if blk.get("type") == "text"
            )
            usage = data.get("usage", {})
            return (
                text,
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                int(usage.get("cache_read_input_tokens", 0)),
                None,
            )

        if config.provider in ("openai", "openrouter", "openai_compatible"):
            url = f"{config.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "content-type": "application/json",
            }
            if config.provider == "openrouter":
                headers["X-Title"] = "HS-Coach"
            payload: dict[str, Any] = {
                "model": config.model,
                "temperature": config.temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if not (
                config.provider == "openrouter"
                and config.model.lower().startswith("anthropic/")
            ):
                payload["response_format"] = {"type": "json_object"}
            resp = http.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data:
                err_msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
                # Retry once on transient HTTP-200 upstream provider errors (common on OpenRouter)
                resp2 = http.post(url, headers=headers, json=payload)
                resp2.raise_for_status()
                data = resp2.json()
                if "choices" not in data:
                    raise RuntimeError(f"LLM response missing 'choices': {err_msg or data}")
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            details = usage.get("prompt_tokens_details") or {}
            raw_cost = usage.get("cost")
            reported_cost = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float))
                else None
            )
            return (
                text,
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
                int(details.get("cached_tokens", 0)),
                reported_cost,
            )

        # Gemini generateContent
        url = f"{config.base_url}/v1beta/models/{config.model}:generateContent"
        headers = {
            "x-goog-api-key": config.api_key,
            "content-type": "application/json",
        }
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": config.temperature,
                "responseMimeType": "application/json",
            },
        }
        resp = http.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        meta = data.get("usageMetadata", {})
        return (
            text,
            int(meta.get("promptTokenCount", 0)),
            int(meta.get("candidatesTokenCount", 0)),
            int(meta.get("cachedContentTokenCount", 0)),
            None,
        )
    finally:
        if owns_client:
            http.close()


def dry_run_summary(
    replay: GameReplay,
    *,
    model: str | None = None,
    card_db: CardDB | None = None,
) -> dict[str, Any]:
    """Build prompts and report token/cost estimates without calling any LLM API."""
    sys_p, user_p = build_coaching_prompt(replay, card_db=card_db)
    in_tok = estimate_tokens(sys_p) + estimate_tokens(user_p)
    est_out = 1200
    cost_table: dict[str, float | None] = {}
    targets = [model] if model else ["claude-sonnet-4-5", "gpt-4.1", "gemini-2.5-pro", "gemini-2.5-flash"]
    for m in targets:
        if m:
            cost_table[m] = estimate_cost_usd(m, in_tok, est_out)
    return {
        "game_id": replay.metadata.game_id,
        "system_chars": len(sys_p),
        "user_chars": len(user_p),
        "estimated_input_tokens": in_tok,
        "assumed_output_tokens": est_out,
        "estimated_costs_usd": cost_table,
    }


def coach_game(
    replay: GameReplay,
    config: CoachConfig,
    *,
    client: httpx.Client | None = None,
    card_db: CardDB | None = None,
) -> CoachReport:
    """Call the configured LLM provider and return a strictly validated CoachReport."""
    validate_completed_replay(replay)
    sys_p, user_p = build_coaching_prompt(replay, card_db=card_db)
    t0 = time.monotonic()

    raw_text, in_tok, out_tok, cached_tok, reported_cost = _call_llm_provider(
        config, sys_p, user_p, client=client
    )

    content: CoachReportContent | None = None
    last_err: Exception | None = None
    try:
        content = CoachReportContent.model_validate_json(extract_json_object(raw_text))
    except (ValidationError, ValueError) as exc:
        last_err = exc

    if content is None:
        repair_prompt = (
            f"{user_p}\n\n"
            f"# Validation Error Repair\n"
            f"Your previous JSON output failed schema validation:\n{last_err}\n\n"
            f"Previous output:\n{raw_text}\n\n"
            f"Return ONLY corrected valid JSON conforming strictly to the schema."
        )
        raw_text2, in2, out2, cached2, cost2 = _call_llm_provider(
            config, sys_p, repair_prompt, client=client
        )
        in_tok += in2
        out_tok += out2
        cached_tok += cached2
        if reported_cost is not None or cost2 is not None:
            reported_cost = (reported_cost or 0.0) + (cost2 or 0.0)
        content = CoachReportContent.model_validate_json(extract_json_object(raw_text2))

    latency = round(time.monotonic() - t0, 2)
    final_cost = (
        round(reported_cost, 6)
        if reported_cost is not None
        else estimate_cost_usd(config.model, in_tok, out_tok)
    )
    usage = UsageStats(
        provider=config.provider,
        model=config.model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached_tokens=cached_tok,
        estimated_cost_usd=final_cost,
        latency_seconds=latency,
    )
    return CoachReport(
        **content.model_dump(),
        game_id=replay.metadata.game_id,
        usage=usage,
    )


# ---- Deck & Pocket-Meta Advisor (post-game aggregate review) ----

DECK_SHORTLIST_LIMIT = 120
DECK_SWITCH_MIN_SAMPLE = 10
_TECH_MECHANICS = frozenset({"TAUNT", "LIFESTEAL", "RUSH", "FREEZE", "SILENCE", "DISCOVER"})
_TECH_TEXT_HINTS = ("armor", "destroy", "all enemy minions", "draw")


class DeckReviewError(ValueError):
    """Deck review failed validation after the repair round; carries the usage already spent."""

    def __init__(self, message: str, usage: UsageStats | None = None) -> None:
        super().__init__(message)
        self.usage = usage


def _tech_relevant(rec: dict[str, Any], text: str) -> bool:
    if _TECH_MECHANICS & set(rec.get("mechanics") or []):
        return True
    low = text.lower()
    return any(hint in low for hint in _TECH_TEXT_HINTS)


def deck_candidate_shortlist(
    aggregate: dict[str, Any], card_db: CardDB, *, limit: int = DECK_SHORTLIST_LIMIT
) -> list[dict[str, Any]]:
    """Deterministic <=120 card shortlist of Standard-legal tech candidates for this deck."""
    hero = aggregate["your_class"]
    decklist: dict[str, int] = aggregate.get("decklist", {})

    def _key(rec: dict[str, Any]) -> tuple[Any, ...]:
        is_class = rec.get("cardClass") == hero or hero in (rec.get("classes") or [])
        text = card_db.clean_text(rec["id"], strip_alternate=True)
        cost = rec.get("cost") if isinstance(rec.get("cost"), int) else 99
        # Tech-relevant cards first (class before neutral), then everything else, so common
        # neutral tech cards are never pushed out by a large class pool.
        return (0 if _tech_relevant(rec, text) else 1, 0 if is_class else 1, cost, rec["id"])

    pool = [
        rec
        for rec in card_db.standard_pool(hero)
        if decklist.get(rec["id"], 0) < card_db.max_copies(rec["id"])
    ]
    pool.sort(key=_key)
    return pool[:limit]


def _fmt_deck_card(card_db: CardDB, cid: str, *, max_text: int = 100) -> str:
    rec = card_db.get(cid)
    cost = rec.get("cost")
    text = card_db.clean_text(cid, strip_alternate=True)[:max_text]
    return f"{card_db.name(cid)} ({cost if isinstance(cost, int) else '?'} mana) [{cid}]: {text}"


def build_deck_review_prompt(
    aggregate: dict[str, Any],
    candidates: list[dict[str, Any]],
    card_db: CardDB,
    sample_size: int,
) -> tuple[str, str]:
    """Build (system, user) prompts from aggregated finished-game data. CardDB text only."""
    schema = json.dumps(DeckReviewContent.model_json_schema(), separators=(",", ":"))
    if sample_size < DECK_SWITCH_MIN_SAMPLE:
        sample_rule = (
            f"Sample size is {sample_size} finished games (under {DECK_SWITCH_MIN_SAMPLE}). "
            "You MUST NOT use overall_verdict 'consider_switching_deck', and switch_advice MUST "
            "say the sample is still small for a full deck switch."
        )
    else:
        sample_rule = (
            f"Sample size is {sample_size} finished games, so any overall_verdict is allowed."
        )
    system = (
        "You are a Hearthstone deck advisor reviewing aggregated results of the player's "
        "FINISHED games with one deck. Everything you are given is post-game data; advise "
        "only on deck construction for future games.\n"
        "Rules:\n"
        "- Use only the data below. Refer to the player as 'you' and to opponents as "
        "'opponent'. Never mention player names, BattleTags or deck codes.\n"
        "- Describe the opposing field by archetype family (for example 'faster aggro/tempo', "
        "'board-control'), not by deck codes.\n"
        "- State whether the main leak is execution/mulligan, the pocket meta (the matchups "
        "this player keeps facing), mixed, or unclear (main_leak).\n"
        "- cut_suggestions (max 2) must use card_id values from the OBSERVED DECKLIST and each "
        "reason must cite dead-in-hand, mulligan or matchup evidence from the data.\n"
        "- tech_suggestions (max 2) must use card_id values from the CANDIDATE SHORTLIST only; "
        "replaces_card_id, when set, must be a card_id from the OBSERVED DECKLIST. Each needs "
        "1-4 target_matchups (opponent class or archetype family).\n"
        "- The observed decklist is built from cards seen in games and may be incomplete.\n"
        f"- {sample_rule}\n"
        "- Respond with ONLY one JSON object matching this JSON schema:\n"
        f"{schema}"
    )

    decklist: dict[str, int] = aggregate["decklist"]
    sig = aggregate["signals"]
    pm = aggregate["pocket_meta"]
    lines: list[str] = [
        f"# Deck review data ({aggregate['your_class']}, {sample_size} finished games, "
        f"{aggregate['coached_games']} coached)",
        f"\n## OBSERVED DECKLIST ({aggregate['observed_total']}/30 cards observed)",
    ]
    for cid, cnt in sorted(decklist.items(), key=lambda kv: (card_db.get(kv[0]).get("cost") or 0, kv[0])):
        lines.append(f"{cnt}x {_fmt_deck_card(card_db, cid)}")

    lines.append("\n## POCKET META (your recent finished games with this deck)")
    lines.append(
        f"Record: {pm['wins']}W-{pm['losses']}L-{pm['ties']}T ({pm['win_rate']}% win rate)"
    )
    lines.append(
        "Opponent classes: "
        + (", ".join(f"{o['name']} x{o['count']}" for o in pm["opponent_classes"]) or "none")
    )
    lines.append(
        "Opponent archetypes (coached games): "
        + (", ".join(f"{o['name']} x{o['count']}" for o in pm["opponent_archetypes"]) or "none")
    )

    lines.append("\n## PER-CARD SIGNALS (only cards with a signal)")
    signal_ids = sorted(
        set(sig["mulligan_kept"]) | set(sig["mulligan_tossed"]) | set(sig["dead_in_hand_losses"])
    )
    for cid in signal_ids:
        lines.append(
            f"{card_db.name(cid)} [{cid}]: kept in mulligan {sig['mulligan_kept'].get(cid, 0)}x, "
            f"tossed {sig['mulligan_tossed'].get(cid, 0)}x, "
            f"still in hand at the end of {sig['dead_in_hand_losses'].get(cid, 0)} loss(es)"
        )
    if not signal_ids:
        lines.append("none recorded")

    habits = aggregate["habits"]
    lines.append("\n## HABIT TAGS FROM COACHED GAMES")
    lines.append(
        "Mistakes: " + (", ".join(f"{h['name']} x{h['count']}" for h in habits["mistakes"]) or "none")
    )
    lines.append(
        "Strengths: " + (", ".join(f"{h['name']} x{h['count']}" for h in habits["strengths"]) or "none")
    )

    lines.append(
        f"\n## CANDIDATE SHORTLIST ({len(candidates)} Standard-legal cards: id | name | cost | class | text)"
    )
    for rec in candidates:
        cid = rec["id"]
        cost = rec.get("cost")
        text = card_db.clean_text(cid, strip_alternate=True)[:160]
        lines.append(
            f"{cid} | {card_db.name(cid)} | {cost if isinstance(cost, int) else '?'} | "
            f"{rec.get('cardClass', '')} | {text}"
        )
    return system, "\n".join(lines)


def _check_deck_review(
    content: DeckReviewContent,
    aggregate: dict[str, Any],
    shortlist_ids: set[str],
    card_db: CardDB,
    sample_size: int,
) -> list[str]:
    errs: list[str] = []
    decklist = aggregate["decklist"]
    hero = aggregate["your_class"]
    for cut in content.cut_suggestions:
        if cut.card_id not in decklist:
            errs.append(f"cut card_id {cut.card_id!r} is not in the observed decklist")
    for tech in content.tech_suggestions:
        if tech.card_id not in shortlist_ids:
            errs.append(f"tech card_id {tech.card_id!r} is not in the candidate shortlist")
        if not card_db.is_standard_legal(tech.card_id, hero):
            errs.append(f"tech card_id {tech.card_id!r} is not Standard-legal for {hero}")
        if tech.replaces_card_id is not None and tech.replaces_card_id not in decklist:
            errs.append(f"replaces_card_id {tech.replaces_card_id!r} is not in the observed decklist")
    if sample_size < DECK_SWITCH_MIN_SAMPLE:
        if content.overall_verdict == "consider_switching_deck":
            errs.append(
                f"overall_verdict 'consider_switching_deck' needs {DECK_SWITCH_MIN_SAMPLE}+ games "
                f"(sample is {sample_size})"
            )
        if "small" not in content.switch_advice.lower():
            errs.append("switch_advice must say the sample is still small for a full deck switch")
    return errs


def review_deck(
    aggregate: dict[str, Any],
    config: CoachConfig,
    card_db: CardDB,
    *,
    client: httpx.Client | None = None,
) -> DeckReview:
    """Call the LLM once (plus one repair round) and return a validated DeckReview."""
    sample_size = int(aggregate["sample_size"])
    candidates = deck_candidate_shortlist(aggregate, card_db)
    shortlist_ids = {rec["id"] for rec in candidates}
    sys_p, user_p = build_deck_review_prompt(aggregate, candidates, card_db, sample_size)
    t0 = time.monotonic()

    def _parse(raw: str) -> DeckReviewContent:
        parsed = DeckReviewContent.model_validate_json(extract_json_object(raw))
        errs = _check_deck_review(parsed, aggregate, shortlist_ids, card_db, sample_size)
        if errs:
            raise ValueError("; ".join(errs))
        return parsed

    raw_text, in_tok, out_tok, cached_tok, reported_cost = _call_llm_provider(
        config, sys_p, user_p, client=client
    )
    content: DeckReviewContent | None = None
    last_err: Exception | None = None
    try:
        content = _parse(raw_text)
    except ValueError as exc:  # includes pydantic ValidationError
        last_err = exc

    def _usage() -> UsageStats:
        final_cost = (
            round(reported_cost, 6)
            if reported_cost is not None
            else estimate_cost_usd(config.model, in_tok, out_tok)
        )
        return UsageStats(
            provider=config.provider,
            model=config.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            estimated_cost_usd=final_cost,
            latency_seconds=round(time.monotonic() - t0, 2),
        )

    if content is None:
        repair_prompt = (
            f"{user_p}\n\n"
            f"# Validation Error Repair\n"
            f"Your previous JSON output failed validation:\n{last_err}\n\n"
            f"Previous output:\n{raw_text}\n\n"
            f"Return ONLY corrected valid JSON conforming strictly to the schema and the rules."
        )
        raw_text2, in2, out2, cached2, cost2 = _call_llm_provider(
            config, sys_p, repair_prompt, client=client
        )
        in_tok += in2
        out_tok += out2
        cached_tok += cached2
        if reported_cost is not None or cost2 is not None:
            reported_cost = (reported_cost or 0.0) + (cost2 or 0.0)
        try:
            content = _parse(raw_text2)
        except ValueError as exc:
            raise DeckReviewError(f"Deck review failed validation: {exc}", _usage()) from exc

    data = content.model_dump()
    for cut in data["cut_suggestions"]:
        cut["card_name"] = card_db.name(cut["card_id"])
    for tech in data["tech_suggestions"]:
        tech["card_name"] = card_db.name(tech["card_id"])
    return DeckReview(
        **data,
        deck_key=aggregate["deck_key"],
        your_class=aggregate["your_class"],
        sample_size=sample_size,
        usage=_usage(),
    )
