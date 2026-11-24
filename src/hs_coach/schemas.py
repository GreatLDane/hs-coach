"""Pydantic v2 contracts for anonymized GameReplay and CoachReport JSON."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Side = Literal["you", "opponent"]
Result = Literal["win", "loss", "tie"]
TerminalPlaystate = Literal["WON", "LOST", "TIED"]
EndReason = Literal["lethal", "concede", "disconnect", "tie", "unknown"]
ChoiceKind = Literal["discover", "assemble", "choose_one", "general"]
ActionType = Literal[
    "PLAY",
    "HERO_POWER",
    "ATTACK",
    "ACTIVATE",
    "TRADE",
    "PREPARE",
    "ABILITY",
    "DECK_ACTION",
    "TRIGGER",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardReference(_StrictModel):
    card_id: str
    name: str
    card_type: str = ""
    base_cost: int | None = None
    base_attack: int | None = None
    base_health: int | None = None
    card_class: str = ""
    spell_school: str | None = None
    races: list[str] = Field(default_factory=list)
    text: str = ""
    position_matters: bool = False


class MatchMetadata(_StrictModel):
    game_id: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    build_number: int | None = None
    format_type: str
    game_type: str
    scenario_id: int | None = None
    is_standard_constructed: bool
    your_class: str
    opponent_class: str
    your_hero: str
    opponent_hero: str
    went_first: bool
    has_coin: bool
    result: Result
    your_terminal_playstate: TerminalPlaystate | None = None
    opponent_terminal_playstate: TerminalPlaystate | None = None
    end_reason: EndReason
    total_turns: int
    your_turns_count: int
    start_of_game_effects: dict[Side, list[str]] = Field(
        default_factory=lambda: {"you": [], "opponent": []}
    )


class MulliganCard(_StrictModel):
    entity_id: int
    card_id: str
    name: str
    cost: int | None = None
    text: str = ""
    kept: bool


class CountdownState(_StrictModel):
    current_value: int | str
    turns_left_including_this: int
    discards_this_turn: bool


class PrepareState(_StrictModel):
    available: bool
    prepared_discount: int = 0
    locked_this_turn: bool = False


class HandCardSnapshot(_StrictModel):
    position: int
    entity_id: int
    card_id: str
    name: str
    card_type: str
    cost: int
    base_cost: int | None = None
    attack: int | None = None
    health: int | None = None
    spell_school: str | None = None
    races: list[str] = Field(default_factory=list)
    outcast_active: bool = False
    spell_damage_bonus: int = 0
    enchantments: list[str] = Field(default_factory=list)
    assembled_note: str | None = None
    countdown: CountdownState | None = None
    prepare: PrepareState | None = None
    shattered_half: bool = False
    effective_text: str = ""
    position_matters: bool = False


class MulliganSnapshot(_StrictModel):
    went_first: bool
    has_coin: bool
    offered: list[MulliganCard] = Field(default_factory=list)
    kept_names: list[str] = Field(default_factory=list)
    tossed_names: list[str] = Field(default_factory=list)
    final_opening_hand: list[HandCardSnapshot] = Field(default_factory=list)
    opponent_kept_count: int = 0
    opponent_tossed_count: int = 0


class WeaponState(_StrictModel):
    entity_id: int
    card_id: str
    name: str
    attack: int
    durability: int
    effective_text: str | None = None


class HeroPowerState(_StrictModel):
    card_id: str
    name: str
    cost: int
    used: bool
    effective_text: str = ""


class HeroState(_StrictModel):
    hero_class: str
    card_id: str
    name: str
    hp: int
    max_hp: int
    armor: int
    effective_hp: int
    attack: int = 0
    can_attack: bool = False
    flags: list[str] = Field(default_factory=list)
    weapon: WeaponState | None = None
    hero_power: HeroPowerState | None = None


class OldGodAbilityState(_StrictModel):
    card_id: str
    name: str
    text: str
    used: bool


class BoardEntitySnapshot(_StrictModel):
    position: int
    entity_id: int
    card_id: str
    name: str
    card_type: Literal["MINION", "LOCATION"]
    attack: int | None = None
    health: int | None = None
    max_health: int | None = None
    damage: int = 0
    races: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    spell_damage: int = 0
    dormant: bool = False
    dormant_wakes_turn: int | None = None
    dormant_wakes_exhausted: bool = False
    location_ready: bool | None = None
    can_attack: bool = False
    attacks_remaining: int = 0
    old_god_abilities: list[OldGodAbilityState] = Field(default_factory=list)
    assembled_note: str | None = None
    dark_gift: str | None = None
    effective_text: str | None = None
    position_matters: bool = False


class OpponentHandSlot(_StrictModel):
    position: int
    turn_entered: int
    created_by: str | None = None
    is_minion_known: bool = False
    is_buffed: bool = False
    revealed_card_id: str | None = None
    revealed_name: str | None = None


class SecretOrQuestState(_StrictModel):
    entity_id: int
    kind: Literal["secret", "quest", "side_quest"]
    owner: Side
    card_class: str = ""
    revealed: bool = False
    card_id: str | None = None
    name: str | None = None
    possible_secrets: list[str] = Field(default_factory=list)
    ruled_out_secrets: list[str] = Field(default_factory=list)
    quest_progress: int | None = None
    quest_total: int | None = None
    quest_reward: str | None = None


class BoardStateSnapshot(_StrictModel):
    your_mana_available: int
    your_mana_max: int
    your_overload_locked: int = 0
    opponent_mana_available: int
    opponent_mana_max: int
    your_hero: HeroState
    opponent_hero: HeroState
    your_board: list[BoardEntitySnapshot] = Field(default_factory=list)
    opponent_board: list[BoardEntitySnapshot] = Field(default_factory=list)
    your_hand: list[HandCardSnapshot] = Field(default_factory=list)
    opponent_hand_size: int = 0
    opponent_hand_slots: list[OpponentHandSlot] = Field(default_factory=list)
    your_deck_size: int = 0
    opponent_deck_size: int = 0
    your_secrets_and_quests: list[SecretOrQuestState] = Field(default_factory=list)
    opponent_secrets_and_quests: list[SecretOrQuestState] = Field(default_factory=list)
    opponent_triggered_secrets: list[str] = Field(default_factory=list)
    special_notes: list[str] = Field(default_factory=list)


class ChoiceOption(_StrictModel):
    entity_id: int
    card_id: str
    name: str
    cost: int | None = None
    text: str = ""
    dark_gift: str | None = None


class ChoiceRecord(_StrictModel):
    choice_id: int
    actor: Side
    kind: ChoiceKind
    source_card_id: str | None = None
    source_name: str | None = None
    options_visible: bool = True
    options: list[ChoiceOption] = Field(default_factory=list)
    chosen_card_ids: list[str] = Field(default_factory=list)
    chosen_names: list[str] = Field(default_factory=list)
    assemble_middle_card: str | None = None


class TurnAction(_StrictModel):
    seq: int
    actor: Side
    action_type: ActionType
    source_entity_id: int | None = None
    source_card_id: str | None = None
    source_name: str
    target_entity_id: int | None = None
    target_card_id: str | None = None
    target_name: str | None = None
    target_side: Side | None = None
    mana_cost: int | None = None
    summary: str


class DamageMathSnapshot(_StrictModel):
    window: Literal["start_of_turn", "mid_turn_peak"]
    mana_available: int
    enemy_effective_hp: int
    enemy_taunt_blocking: bool
    enemy_hero_divine_shield: bool
    enemy_hero_immune: bool
    enemy_has_active_secret: bool
    max_face_damage: int
    sources: list[str] = Field(default_factory=list)
    unmodelled_notes: list[str] = Field(default_factory=list)
    lethal_available: bool
    confidence: Literal["certain", "check_secret_or_shield", "none"]


class CardLossEvent(_StrictModel):
    owner: Side
    reason: Literal["burned", "discarded"]
    entity_id: int
    card_id: str | None = None
    name: str


class TurnPreChecks(_StrictModel):
    start_damage_math: DamageMathSnapshot | None = None
    peak_damage_math: DamageMathSnapshot | None = None
    missed_lethal: bool = False
    missed_lethal_detail: str | None = None
    mana_floated: int = 0
    prepare_was_available: bool = False
    unused_attacks: list[str] = Field(default_factory=list)
    unused_ready_locations: list[str] = Field(default_factory=list)
    unused_hero_power: bool = False
    cards_burned: list[CardLossEvent] = Field(default_factory=list)
    cards_discarded: list[CardLossEvent] = Field(default_factory=list)


class TurnSnapshot(_StrictModel):
    turn_number: int
    player_turn_number: int
    active_player: Side
    start_state: BoardStateSnapshot
    actions: list[TurnAction] = Field(default_factory=list)
    choices: list[ChoiceRecord] = Field(default_factory=list)
    end_state: BoardStateSnapshot
    pre_checks: TurnPreChecks


class MatchPreCheckSummary(_StrictModel):
    missed_lethal_turns: list[int] = Field(default_factory=list)
    total_mana_floated: int = 0
    your_cards_burned: list[str] = Field(default_factory=list)
    your_cards_discarded: list[str] = Field(default_factory=list)
    turns_with_unused_attacks: list[int] = Field(default_factory=list)
    turns_with_unused_ready_locations: list[int] = Field(default_factory=list)


class DeckCard(_StrictModel):
    card_id: str
    name: str
    cost: int | None = None
    count: int
    card_type: str = ""


class GameReplay(_StrictModel):
    schema_version: str = "1.0.0"
    metadata: MatchMetadata
    mulligan: MulliganSnapshot
    turns: list[TurnSnapshot] = Field(default_factory=list)
    your_deck_observed: list[DeckCard] = Field(default_factory=list)
    opponent_deck_observed: list[DeckCard] = Field(default_factory=list)
    cards_seen: dict[str, CardReference] = Field(default_factory=dict)
    pre_check_summary: MatchPreCheckSummary


_GAME_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_BATTLETAG_RE = re.compile(r"\b[A-Za-z0-9_]{2,24}#\d{4,6}\b")
_EXPECTED_PLAYSTATES: dict[Result, tuple[TerminalPlaystate, TerminalPlaystate]] = {
    "win": ("WON", "LOST"),
    "loss": ("LOST", "WON"),
    "tie": ("TIED", "TIED"),
}


def validate_completed_replay(replay: GameReplay) -> None:
    """Enforce post-game terminal PLAYSTATE proof, structural bounds, and BattleTag absence."""
    m = replay.metadata
    if not _GAME_ID_RE.match(m.game_id):
        raise ValueError("Invalid game_id format.")
    if m.total_turns < 1:
        raise ValueError("Completed replay must have total_turns >= 1.")
    if m.duration_seconds is not None and m.duration_seconds < 0:
        raise ValueError("Replay duration_seconds cannot be negative.")
    expected_pair = _EXPECTED_PLAYSTATES.get(m.result)
    if (
        expected_pair is None
        or (m.your_terminal_playstate, m.opponent_terminal_playstate) != expected_pair
    ):
        raise ValueError("Replay lacks valid terminal PLAYSTATE proof matching result.")
    if (
        len(replay.turns) > 120
        or len(replay.cards_seen) > 300
        or len(replay.your_deck_observed) > 60
        or len(replay.opponent_deck_observed) > 60
    ):
        raise ValueError("Replay exceeds allowed structural bounds.")
    replay_json = replay.model_dump_json()
    if _BATTLETAG_RE.search(replay_json) or "PlayerName=" in replay_json:
        raise ValueError("Replay appears to contain un-anonymized player identifier/BattleTag.")


# ---- Coach Report Schemas ----


class MulliganCardVerdict(_StrictModel):
    card_name: str
    card_id: str | None = None
    action_taken: Literal["kept", "tossed"]
    verdict: Literal["good", "questionable", "mistake"]
    recommended_action: Literal["keep", "toss"]
    reasoning: str


class MulliganReview(_StrictModel):
    overall_grade: Literal["A", "B", "C", "D", "F"]
    summary: str
    card_verdicts: list[MulliganCardVerdict] = Field(default_factory=list)


class TurningPoint(_StrictModel):
    rank: int = Field(ge=1, le=3)
    turn_number: int
    severity: Literal["Missed Lethal", "Blunder", "Inaccuracy"]
    title: str
    turn_context: str
    what_was_played: str
    better_line: str
    why_it_mattered: str
    habit_tag: str


class BestPlay(_StrictModel):
    turn_number: int
    title: str
    what_was_played: str
    why_it_was_good: str


class HabitTag(_StrictModel):
    tag: str
    polarity: Literal["mistake", "strength"]
    turns: list[int] = Field(default_factory=list)
    note: str


class UsageStats(_StrictModel):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    estimated_cost_usd: float | None = None
    latency_seconds: float | None = None


class CoachReportContent(_StrictModel):
    """Exact JSON schema returned by the LLM coach."""

    match_summary: str
    your_archetype: str
    opponent_archetype: str
    overall_grade: Literal[
        "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D", "F"
    ]
    key_takeaway: str
    mulligan_review: MulliganReview
    turning_points: list[TurningPoint] = Field(default_factory=list, max_length=3)
    best_plays: list[BestPlay] = Field(default_factory=list)
    habit_tags: list[HabitTag] = Field(default_factory=list)


class CoachReport(CoachReportContent):
    schema_version: str = "1.0.0"
    game_id: str
    usage: UsageStats | None = None


# ---- Deck & Pocket-Meta Advisor Schemas (post-game aggregate review) ----


class DeckCutSuggestion(_StrictModel):
    card_id: str
    card_name: str
    reason: str
    evidence: list[str] = Field(default_factory=list, max_length=4)


class DeckTechSuggestion(_StrictModel):
    card_id: str
    card_name: str
    replaces_card_id: str | None = None
    target_matchups: list[str] = Field(min_length=1, max_length=4)
    reason: str


class DeckReviewContent(_StrictModel):
    """Exact JSON schema returned by the LLM deck advisor."""

    overall_verdict: Literal["keep_list", "tech_adjustments", "consider_switching_deck"]
    executive_summary: str
    cut_suggestions: list[DeckCutSuggestion] = Field(default_factory=list, max_length=2)
    tech_suggestions: list[DeckTechSuggestion] = Field(default_factory=list, max_length=2)
    switch_advice: str
    main_leak: Literal["execution_or_mulligan", "pocket_meta", "mixed", "unclear"]


class DeckReview(DeckReviewContent):
    schema_version: str = "1.0.0"
    deck_key: str
    your_class: str
    sample_size: int
    usage: UsageStats | None = None
