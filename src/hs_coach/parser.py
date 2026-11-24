"""Streaming Power.log parser producing anonymized Decision-Snapshot GameReplay objects."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, TextIO

from hs_coach.cards import (
    HERALD_NAME,
    CardDB,
    clean_card_text,
    format_action_summary,
    is_internal_card_reference,
)
from hs_coach.schemas import (
    ActionType,
    BoardEntitySnapshot,
    BoardStateSnapshot,
    CardLossEvent,
    CardReference,
    ChoiceOption,
    ChoiceRecord,
    CountdownState,
    DamageMathSnapshot,
    DeckCard,
    EndReason,
    GameReplay,
    HandCardSnapshot,
    HeroPowerState,
    HeroState,
    MatchMetadata,
    MatchPreCheckSummary,
    MulliganCard,
    MulliganSnapshot,
    OldGodAbilityState,
    OpponentHandSlot,
    PrepareState,
    Result,
    SecretOrQuestState,
    Side,
    TerminalPlaystate,
    TurnAction,
    TurnPreChecks,
    TurnSnapshot,
    WeaponState,
)

CONSTRUCTED_GAME_TYPES = frozenset(
    {"GT_RANKED", "GT_CASUAL", "GT_VS_FRIEND", "GT_VS_AI"}
)

COIN_CARD_ID = "GAME_005"

LINE_RE = re.compile(
    r"^D (\d\d:\d\d:\d\d(?:\.\d+)?)\s+(GameState\.[A-Za-z0-9_]+)\(\)\s+-\s+(.*)$"
)
BRACKET_ENT_RE = re.compile(
    r"^\[entityName=.*? id=(\d+) .*?cardId=(\S*) player=(\d+)\]$"
)
PLAYER_EID_RE = re.compile(r"^Player EntityID=(\d+) PlayerID=(\d+)")
PLAYER_NAME_RE = re.compile(r"^PlayerID=(\d+), PlayerName=(.+)$")
FULL_ENT_RE = re.compile(r"^FULL_ENTITY - Creating ID=(\d+) CardID=(\S*)$")
SHOW_CHANGE_RE = re.compile(
    r"^(SHOW_ENTITY|CHANGE_ENTITY) - Updating Entity=(.+?) CardID=(\S*)$"
)
TAG_CHANGE_RE = re.compile(r"^TAG_CHANGE Entity=(.+) tag=(\w+) value=(\S+)$")
HIDE_ENT_RE = re.compile(r"^HIDE_ENTITY - Entity=(.+) tag=(\w+) value=(\S+)$")
TAG_VAL_RE = re.compile(r"^tag=(\w+) value=(\S+)$")
GAME_ENT_RE = re.compile(r"^GameEntity EntityID=(\d+)$")
BLOCK_START_RE = re.compile(
    r"^BLOCK_START BlockType=(\w+) Entity=(.+?) EffectCardId=.*? Target=(.+?) SubOption=(-?\d+)"
)
TRIGGER_LASH_RE = re.compile(
    r"^BLOCK_START BlockType=TRIGGER Entity=\[entityName=.*? id=\d+ .*?cardId=BE_771 player=(\d+)\]"
)
TRIGGER_SOG_RE = re.compile(
    r"^BLOCK_START BlockType=TRIGGER Entity=\[entityName=.*? id=(\d+) .*?cardId=(\S*) player=(\d+)\].*TriggerKeyword=START_OF_GAME_KEYWORD"
)
CHOICE_HDR_RE = re.compile(
    r"^id=(\d+)\s+Player=(.+?)\s+TaskList=\d+\s+ChoiceType=(\w+)"
)
CHOSEN_HDR_RE = re.compile(r"^id=(\d+)\s+Player=(.+?)\s+EntitiesCount=(\d+)")
SEND_CHOICE_HDR_RE = re.compile(r"^id=(\d+)\s+ChoiceType=(\w+)")
CHOICE_SOURCE_RE = re.compile(r"^Source=(.+)$")
CHOICE_ENT_RE = re.compile(
    r"^(?:Entities|m_chosenEntities)\[\d+\]=\[entityName=.*?id=(\d+).*?cardId=(\S*) player=(\d+)\]"
)
META_BURNED_RE = re.compile(r"^META_DATA - Meta=BURNED_CARD\b")
META_INFO_RE = re.compile(
    r"^Info\[\d+\]\s*=\s*(?:\[entityName=.*? id=(\d+).*?cardId=(\S*) player=(\d+)\]|(\d+))"
)

ALT_SPLIT_RE = re.compile(r"@|(?<=</i>)1(?=[A-Z])")
PREPARE_RE = re.compile(r"^(\[x\])?<b>Prepare\b")
LEFT_RE = re.compile(r"\((?:\d+|\{0\})((?:\s+\w+)?\s+left!)\)")
FRESH_COUNTDOWN_N1 = {"FIR_911": 1}

CTHUN_ABIL = {
    "BE_013t1": "Wrath (6 dmg split)",
    "BE_013t2": "Domain (+2/+2 x2)",
    "BE_013t3": "Glare (5 Lifesteal to minion)",
    "BE_013t6": "Glare (5 Lifesteal to minion)",
    "BE_013t4": "Servants (2x 2/2 Taunt)",
}


@dataclass(frozen=True)
class LogEvent:
    ts_str: str
    ts_sec: float
    method: str
    payload: str


@dataclass
class ParseResult:
    replays: list[GameReplay] = field(default_factory=list)
    total_games_found: int = 0
    skipped_incomplete: int = 0
    skipped_non_standard: int = 0
    warnings: list[str] = field(default_factory=list)


def _ts_to_seconds(ts_str: str) -> float:
    parts = ts_str.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def iter_game_chunks(stream: TextIO) -> Iterator[list[LogEvent]]:
    """Stream a Power.log text stream and yield lists of GameState LogEvents per CREATE_GAME."""
    current: list[LogEvent] | None = None
    for raw_line in stream:
        m = LINE_RE.match(raw_line.rstrip("\r\n"))
        if not m:
            continue
        ts_str, method, payload = m.group(1), m.group(2), m.group(3)
        ev = LogEvent(
            ts_str=ts_str,
            ts_sec=_ts_to_seconds(ts_str),
            method=method,
            payload=payload,
        )
        if method == "GameState.DebugPrintPower" and payload.strip() == "CREATE_GAME":
            if current:
                yield current
            current = [ev]
        elif current is not None:
            current.append(ev)
    if current:
        yield current


@dataclass
class _PrescanInfo:
    build_number: int | None
    game_type: str
    format_type: str
    scenario_id: int | None
    started_at: str | None
    ended_at: str | None
    duration_seconds: float | None
    completed: bool
    is_standard_constructed: bool
    me_pid: str
    opp_pid: str
    game_id: str


def _prescan_game(events: list[LogEvent]) -> _PrescanInfo:
    """First pass over one game chunk: mode filtering, completion check, and mirror-safe side detection."""
    build_number: int | None = None
    game_type = ""
    format_type = ""
    scenario_id: int | None = None
    unknown_pids: set[str] = set()
    pid_to_eid: dict[int, int] = {}
    name_to_eid: dict[str, int] = {}
    terminal_seen = False

    # Fallback side scoring when neither or both players are UNKNOWN HUMAN PLAYER
    ents_meta: dict[int, dict[str, Any]] = {}
    drawn_known: dict[str, int] = {}
    known_hand_ever: dict[str, set[int]] = {}
    deck_revealed: set[int] = set()
    cur_ent: int | None = None
    card_seq_hash = hashlib.sha256()

    def _peid(tok: str) -> int | str | None:
        tok = tok.strip()
        bm = BRACKET_ENT_RE.match(tok)
        if bm:
            eid = int(bm.group(1))
            ent = ents_meta.setdefault(eid, {"card": "", "ctrl": "", "zone": ""})
            if bm.group(2) and not ent["card"]:
                ent["card"] = bm.group(2)
                card_seq_hash.update(bm.group(2).encode("ascii", "ignore"))
            return eid
        if tok.isdigit():
            return int(tok)
        if tok == "GameEntity":
            return "game"
        if tok not in name_to_eid:
            named = {v for k, v in name_to_eid.items() if k != "UNKNOWN HUMAN PLAYER"}
            free = [e for e in pid_to_eid.values() if e not in named]
            if len(free) == 1:
                name_to_eid[tok] = free[0]
        return name_to_eid.get(tok)

    def _update_zone(eid: int, new_zone: str) -> None:
        ent = ents_meta.setdefault(eid, {"card": "", "ctrl": "", "zone": ""})
        old_zone = ent["zone"]
        ent["zone"] = new_zone
        ctrl = ent["ctrl"]
        if new_zone == "HAND" and old_zone != "HAND":
            if ent["card"] and ctrl:
                known_hand_ever.setdefault(ctrl, set()).add(eid)
            if old_zone == "DECK" and eid in deck_revealed and ctrl:
                drawn_known[ctrl] = drawn_known.get(ctrl, 0) + 1
                deck_revealed.discard(eid)
        elif new_zone != "DECK":
            deck_revealed.discard(eid)

    for ev in events:
        s = ev.payload.strip()
        if ev.method == "GameState.DebugPrintGame":
            if s.startswith("BuildNumber="):
                val = s.split("=", 1)[1].strip()
                if val.isdigit():
                    build_number = int(val)
            elif s.startswith("GameType="):
                game_type = s.split("=", 1)[1].strip()
            elif s.startswith("FormatType="):
                format_type = s.split("=", 1)[1].strip()
            elif s.startswith("ScenarioID="):
                val = s.split("=", 1)[1].strip()
                if val.lstrip("-").isdigit():
                    scenario_id = int(val)
            else:
                pm = PLAYER_NAME_RE.match(s)
                if pm:
                    pid_str, pname = pm.group(1), pm.group(2).strip()
                    if int(pid_str) in pid_to_eid:
                        name_to_eid[pname] = pid_to_eid[int(pid_str)]
                    if pname == "UNKNOWN HUMAN PLAYER":
                        unknown_pids.add(pid_str)
            continue

        if ev.method != "GameState.DebugPrintPower":
            continue

        m = PLAYER_EID_RE.match(s)
        if m:
            pid_to_eid[int(m.group(2))] = int(m.group(1))
            continue

        m = FULL_ENT_RE.match(s)
        if m:
            cur_ent = int(m.group(1))
            cid = m.group(2)
            ent = ents_meta.setdefault(cur_ent, {"card": "", "ctrl": "", "zone": ""})
            if cid:
                ent["card"] = cid
                card_seq_hash.update(cid.encode("ascii", "ignore"))
            continue

        m = SHOW_CHANGE_RE.match(s)
        if m:
            ent_ref = _peid(m.group(2))
            if isinstance(ent_ref, int):
                cur_ent = ent_ref
                ent = ents_meta.setdefault(cur_ent, {"card": "", "ctrl": "", "zone": ""})
                if m.group(1) == "SHOW_ENTITY" and ent["zone"] == "DECK":
                    deck_revealed.add(cur_ent)
                if m.group(3):
                    ent["card"] = m.group(3)
                    card_seq_hash.update(m.group(3).encode("ascii", "ignore"))
            else:
                cur_ent = None
            continue

        m = TAG_CHANGE_RE.match(s)
        if m:
            cur_ent = None
            ref = _peid(m.group(1))
            tag, val = m.group(2), m.group(3)
            if isinstance(ref, int):
                if tag == "PLAYSTATE" and val in ("WON", "LOST", "TIED"):
                    terminal_seen = True
                ent = ents_meta.setdefault(ref, {"card": "", "ctrl": "", "zone": ""})
                if tag == "CONTROLLER":
                    ent["ctrl"] = val
                    if ent["zone"] == "HAND" and ent["card"]:
                        known_hand_ever.setdefault(val, set()).add(ref)
                elif tag == "ZONE":
                    _update_zone(ref, val)
            continue

        m = HIDE_ENT_RE.match(s)
        if m:
            cur_ent = None
            ref = _peid(m.group(1))
            if isinstance(ref, int) and m.group(2) == "ZONE":
                _update_zone(ref, m.group(3))
            continue

        m = TAG_VAL_RE.match(s)
        if m and cur_ent is not None:
            tag, val = m.group(1), m.group(2)
            ent = ents_meta.setdefault(cur_ent, {"card": "", "ctrl": "", "zone": ""})
            if tag == "CONTROLLER":
                ent["ctrl"] = val
                if ent["zone"] == "HAND" and ent["card"]:
                    known_hand_ever.setdefault(val, set()).add(cur_ent)
            elif tag == "ZONE":
                _update_zone(cur_ent, val)
            continue

        if GAME_ENT_RE.match(s):
            cur_ent = None
        elif s.startswith(("BLOCK_", "META_DATA", "SUB_SPELL", "Info[", "Source=", "Targets")):
            cur_ent = None

    if len(unknown_pids) == 1:
        opp_pid = next(iter(unknown_pids))
        me_pid = "2" if opp_pid == "1" else "1"
    else:
        score = {
            c: len(known_hand_ever.get(c, set())) + 3 * drawn_known.get(c, 0)
            for c in ("1", "2")
        }
        me_pid = max(("1", "2"), key=lambda c: score.get(c, 0)) if any(score.values()) else "1"
        opp_pid = "2" if me_pid == "1" else "1"

    started_at = events[0].ts_str if events else None
    ended_at = events[-1].ts_str if events else None
    duration: float | None = None
    if events:
        dt = events[-1].ts_sec - events[0].ts_sec
        if dt < 0:
            dt += 86400.0
        duration = round(dt, 2)

    id_seed = (
        f"{started_at}|{ended_at}|{build_number}|{format_type}|{game_type}|"
        f"{scenario_id}|{me_pid}|{card_seq_hash.hexdigest()}"
    )
    game_id = hashlib.sha256(id_seed.encode("utf-8")).hexdigest()[:16]
    is_std = format_type == "FT_STANDARD" and game_type in CONSTRUCTED_GAME_TYPES

    return _PrescanInfo(
        build_number=build_number,
        game_type=game_type,
        format_type=format_type,
        scenario_id=scenario_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        completed=terminal_seen,
        is_standard_constructed=is_std,
        me_pid=me_pid,
        opp_pid=opp_pid,
        game_id=game_id,
    )


def _int_tag(ent: dict[str, Any], key: str, default: int = 0) -> int:
    val = ent.get("tags", {}).get(key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _str_tag(ent: dict[str, Any], key: str, default: str = "") -> str:
    val = ent.get("tags", {}).get(key)
    return str(val) if val is not None else default


@dataclass
class _OpenChoice:
    choice_id: int
    player_pid: str
    choice_type: str
    source_id: int = 0
    source_cid: str = ""
    opt_ids: list[int] = field(default_factory=list)
    opt_cids: list[str] = field(default_factory=list)
    chosen_ids: list[int] = field(default_factory=list)
    chosen_cids: list[str] = field(default_factory=list)
    expected_count: int = 1
    turn_opened: int = 0


@dataclass
class _ActiveBlock:
    block_type: str
    source_eid: int | None
    target_eid: int | None
    suboption: int
    action_idx: int | None = None
    play_card_type: str | None = None
    play_had_my_minion: bool = False
    play_opp_lt7: bool = False
    atk_card_type: str | None = None
    atk_target_type: str | None = None
    triggered_secrets: set[str] = field(default_factory=set)
    trigger_key: tuple[int, str] | None = None


@dataclass
class _TurnWork:
    turn_number: int
    player_turn_number: int
    active_player: Side
    start_state: BoardStateSnapshot
    start_damage_math: DamageMathSnapshot | None = None
    peak_damage_math: DamageMathSnapshot | None = None
    actions: list[TurnAction] = field(default_factory=list)
    choices: list[ChoiceRecord] = field(default_factory=list)
    cards_burned: list[CardLossEvent] = field(default_factory=list)
    cards_discarded: list[CardLossEvent] = field(default_factory=list)
    eot_mana_floated: int = 0
    eot_prepare_available: bool = False
    eot_unused_attacks: list[str] = field(default_factory=list)
    eot_unused_ready_locations: list[str] = field(default_factory=list)
    eot_unused_hero_power: bool = False
    eot_captured: bool = False


class _GameSimulator:
    """Chronological state machine for one completed Hearthstone game."""

    def __init__(self, events: list[LogEvent], prescan: _PrescanInfo, db: CardDB) -> None:
        self.events = events
        self.prescan = prescan
        self.db = db
        self.me = prescan.me_pid
        self.opp = prescan.opp_pid

        self.ents: dict[int, dict[str, Any]] = {}
        self.pid_to_eid: dict[int, int] = {}
        self.name_to_eid: dict[str, int] = {}
        self.game_tags: dict[str, str] = {}

        self.dormant_since: dict[int, int] = {}
        self.hand_entered: dict[int, int] = {}
        self.active_hero: dict[str, int] = {}
        self.end_reason_by_eid: dict[int, str] = {}

        # Special mechanics tracking
        self.cthun_trig: list[tuple[int, str]] = []
        self.lash_trig: list[str] = []
        self.start_of_game_raw: list[tuple[str, int, str]] = []

        # Secret tracking (chronological)
        self.active_opp_secrets: set[int] = set()
        self.secret_overlap: dict[int, set[int]] = {}
        self.secret_ruled_actions: dict[int, set[str]] = {}
        self.opp_triggered_secrets: list[str] = []
        self.plays_this_turn = 0

        # Mulligan tracking
        self.mulligan_offers: dict[str, _OpenChoice] = {}
        self.mulligan_chosen: dict[str, list[int]] = {}
        self.final_opening_hand: list[HandCardSnapshot] = []
        self.opening_hand_captured = False

        # General choices tracking
        self.choices_by_id: dict[int, _OpenChoice] = {}
        self.recorded_choice_ids: set[int] = set()
        self.pending_choices: list[ChoiceRecord] = []
        self.pending_burned: list[CardLossEvent] = []
        self.pending_discarded: list[CardLossEvent] = []

        # Block & action resolution state
        self.block_stack: list[_ActiveBlock] = []
        self.pending_atk: tuple[_TurnWork, int] | None = None
        self.pending_abil: tuple[_TurnWork, int, str] | None = None
        self.pending_da: tuple[_TurnWork, int, int] | None = None
        self._action_meta: dict[tuple[int, int], tuple[str | None, bool]] = {}

        # Turn lifecycle
        self.player_turn_counts: dict[Side, int] = {"you": 0, "opponent": 0}
        self.turns: list[TurnSnapshot] = []
        self.current_turn_work: _TurnWork | None = None

        # Card glossary & observed decklist collectors
        self.seen_card_ids: set[str] = set()
        self.initial_deck_candidates: set[int] = set()
        self.original_card_id: dict[int, str] = {}

        # Parser transient cursors
        self._cur_ent: int | str | None = None
        self._reading_choice_id: int | None = None
        self._reading_choice_mode: str | None = None  # "choices" | "chosen" | "send"
        self._reading_burned_meta = False

    def _side_of(self, ctrl: str | None) -> Side:
        return "you" if ctrl == self.me else "opponent"

    def _eid_of(self, tok: str) -> int | str:
        tok = tok.strip()
        m = BRACKET_ENT_RE.match(tok)
        if m:
            eid = int(m.group(1))
            ent = self.ents.setdefault(eid, {"card": "", "tags": {}})
            if m.group(2):
                self.original_card_id.setdefault(eid, m.group(2))
                if not ent["card"]:
                    ent["card"] = m.group(2)
                    self.seen_card_ids.add(m.group(2))
            return eid
        if tok.isdigit():
            return int(tok)
        if tok == "GameEntity":
            return "game"
        if tok not in self.name_to_eid:
            named = {v for k, v in self.name_to_eid.items() if k != "UNKNOWN HUMAN PLAYER"}
            free = [e for e in self.pid_to_eid.values() if e not in named]
            if len(free) == 1:
                self.name_to_eid[tok] = free[0]
        return self.name_to_eid.get(tok, tok)

    def _ent(self, eid: int) -> dict[str, Any]:
        return self.ents.setdefault(eid, {"card": "", "tags": {}})

    def _player_ent(self, ctrl: str) -> dict[str, Any]:
        eid = self.pid_to_eid.get(int(ctrl)) if ctrl.isdigit() else None
        return self.ents.get(eid, {"card": "", "tags": {}}) if eid is not None else {"card": "", "tags": {}}

    def _cur_game_turn(self) -> int:
        try:
            return int(self.game_tags.get("TURN", 0))
        except (TypeError, ValueError):
            return 0

    def _active_side(self) -> Side:
        for c in (self.me, self.opp):
            if _int_tag(self._player_ent(c), "CURRENT_PLAYER") == 1:
                return self._side_of(c)
        return "you" if self._cur_game_turn() % 2 == 1 else "opponent"

    def _zone(self, ctrl: str, zone_name: str, types: tuple[str, ...] | None = None) -> list[tuple[int, dict[str, Any]]]:
        out = [
            (eid, ent)
            for eid, ent in self.ents.items()
            if ent["tags"].get("CONTROLLER") == ctrl
            and ent["tags"].get("ZONE") == zone_name
            and (types is None or ent["tags"].get("CARDTYPE") in types)
        ]
        return sorted(out, key=lambda x: (_int_tag(x[1], "ZONE_POSITION"), x[0]))

    def _hero_class(self, ctrl: str) -> str:
        heroes = self._zone(ctrl, "PLAY", ("HERO",))
        if not heroes:
            if ctrl in self.active_hero:
                he = self.ents.get(self.active_hero[ctrl], {"card": "", "tags": {}})
            else:
                return ""
        else:
            he = heroes[-1][1]
        return str(
            he["tags"].get("CLASS")
            or self.db.get(he.get("card")).get("cardClass", "")
        )

    def _weapon_durability(self, ent: dict[str, Any]) -> int:
        full = (
            _int_tag(ent, "DURABILITY")
            or _int_tag(ent, "HEALTH")
            or self.db.base_durability(ent.get("card"))
        )
        return full - _int_tag(ent, "DAMAGE")

    def _fill_placeholders(
        self, text: str, cid: str, ent: dict[str, Any] | None = None, ctrl: str | None = None
    ) -> str:
        if "{" not in text:
            return text
        c_ctrl = ctrl or (ent["tags"].get("CONTROLLER") if ent else None)
        herald_used = False
        if re.search(r"\bHerald\s+\{0\}", text):
            cls_name = (
                (ent["tags"].get("CLASS") if ent else None)
                or self.db.get(cid).get("cardClass", "")
            )
            hname = HERALD_NAME.get(cls_name) or (
                HERALD_NAME.get(self._hero_class(c_ctrl)) if c_ctrl else None
            )
            if hname:
                text = re.sub(r"\b(Herald\s+)\{0\}", rf"\1{hname}", text)
                herald_used = True
        if ent is not None and "{0}" in text:
            card_ref: str | None = None
            mo = _int_tag(ent, "DISPLAY_CARD_ON_MOUSEOVER")
            if mo and mo in self.db.by_dbf_id:
                card_ref = self.db.name(self.db.by_dbf_id[mo])
            if not card_ref and "'{0}'" in text:
                n1 = _int_tag(ent, "TAG_SCRIPT_DATA_NUM_1")
                if n1 in self.ents and self.ents[n1].get("card"):
                    card_ref = self.db.name(self.ents[n1]["card"])
            if card_ref and (
                "'{0}'" in text
                or re.search(r"\b(cast|summon|from|with)\s+\{0\}", text, re.I)
            ):
                text = text.replace("{0}", card_ref)
        if "{0}" in text and re.search(r"\{0\}\s+(?:Silver Hand )?Recruit", text):
            text = text.replace("{0}", "1/1")
        if ent is not None:
            n1 = ent["tags"].get("TAG_SCRIPT_DATA_NUM_1")
            n2 = ent["tags"].get("TAG_SCRIPT_DATA_NUM_2")
            if "{0}" in text and n1 is not None and str(n1).lstrip("-").isdigit():
                text = text.replace("{0}", str(n1))
            if "{1}" in text:
                v1 = (
                    n2
                    if (n2 is not None and str(n2).lstrip("-").isdigit())
                    else (
                        n1
                        if herald_used and n1 is not None and str(n1).lstrip("-").isdigit()
                        else None
                    )
                )
                if v1 is not None:
                    text = text.replace("{1}", str(v1))
        return text

    def _short_text(
        self, cid: str, ent: dict[str, Any] | None = None, ctrl: str | None = None
    ) -> str:
        raw = self.db.raw_text(cid).split("@", 1)[0]
        if not raw:
            return ""
        t = clean_card_text(raw, strip_alternate=False, replace_placeholders=False)
        if ent is not None and "TAG_SCRIPT_DATA_NUM_1" in ent["tags"]:
            t = LEFT_RE.sub(rf"({ent['tags']['TAG_SCRIPT_DATA_NUM_1']}\1)", t)
        t = self._fill_placeholders(t, cid, ent=ent, ctrl=ctrl)
        t = re.sub(r"\(\{\d\}[^)]*\)", "", t)
        t = re.sub(r"\{\d+\}", "X", t)
        return re.sub(r"\s+", " ", t).strip()

    def _assembled(self, ent: dict[str, Any]) -> tuple[str, str] | None:
        h1 = _int_tag(ent, "CRAFTABLE_FIRST_HALF_DBID")
        h2 = _int_tag(ent, "CRAFTABLE_SECOND_HALF_DBID")
        d1 = self.db.by_dbf_id.get(h1) if h1 > 1 else None
        d2 = self.db.by_dbf_id.get(h2) if h2 > 1 else None
        if not (d1 or d2):
            return None
        self.seen_card_ids.add(ent["card"])
        if d1:
            self.seen_card_ids.add(d1)
        if d2:
            self.seen_card_ids.add(d2)

        def halves(card_id: str) -> list[str]:
            t = self.db.clean_text(card_id, strip_alternate=True)
            return [s.strip() for s in t.split("&", 1)] if "&" in t else [t, ""]

        top, bot = halves(ent["card"])
        if d1:
            top = halves(d1)[0]
        if d2:
            bot = halves(d2)[1]
        src = [f"top from {self.db.name(d1)}"] if d1 else []
        if d2:
            src.append(f"bottom from {self.db.name(d2)}")
        return f"{top} {bot}".strip(), f"ASSEMBLED: {', '.join(src)}"

    def _dark_gift_note(self, eid: int) -> str | None:
        ent = self.ents.get(eid)
        if not ent:
            return None
        cids: list[str] = []
        dg_id = _int_tag(ent, "DARK_GIFT_ENTITY")
        if dg_id and dg_id in self.ents and self.ents[dg_id].get("card"):
            cids.append(self.ents[dg_id]["card"])
        for en_id, en in self.ents.items():
            if (
                en["tags"].get("CARDTYPE") == "ENCHANTMENT"
                and en["tags"].get("ATTACHED") == str(eid)
                and en["tags"].get("ZONE") in ("PLAY", None)
                and _int_tag(en, "IS_NIGHTMARE_BONUS") == 1
            ):
                cr = self.db.by_dbf_id.get(_int_tag(en, "CREATOR_DBID"))
                dc = en.get("card") or ""
                if (
                    cr
                    and cr in self.db.by_id
                    and self.db.clean_text(cr)
                    and (dc.startswith(cr) or self.db.get(cr).get("type") == "SPELL")
                ):
                    dc = cr
                elif dc.endswith("e") and dc[:-1] in self.db.by_id and self.db.clean_text(dc[:-1]):
                    dc = dc[:-1]
                elif not dc or not self.db.clean_text(dc):
                    dc = cr or dc
                if dc:
                    cids.append(dc)
        if not cids:
            return None
        parts: list[str] = []
        seen_keys: set[str] = set()
        for dc in dict.fromkeys(cids):
            self.seen_card_ids.add(dc)
            dt = self.db.clean_text(dc)
            key = dt.lower().rstrip(".") if dt else self.db.name(dc).lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            parts.append(f"{self.db.name(dc)} - {dt}" if dt else self.db.name(dc))
        return "; ".join(parts) if parts else None

    def _old_god_abils(self, ent: dict[str, Any]) -> list[OldGodAbilityState]:
        cid = ent.get("card", "")
        if not cid or not self.db.is_old_god(cid):
            return []
        ctrl = ent["tags"].get("CONTROLLER")
        out: list[OldGodAbilityState] = []
        seen: set[str] = set()
        for aid, aent in sorted(self.ents.items()):
            acard = aent.get("card", "")
            if (
                acard.startswith(cid + "t")
                and aent["tags"].get("CARDTYPE") == "SPELL"
                and aent["tags"].get("CONTROLLER") == ctrl
                and aent["tags"].get("ZONE") == "SETASIDE"
                and acard not in seen
            ):
                seen.add(acard)
                self.seen_card_ids.add(acard)
                out.append(
                    OldGodAbilityState(
                        card_id=acard,
                        name=self.db.name(acard),
                        text=self._short_text(acard, ent=aent, ctrl=ctrl),
                        used=_int_tag(aent, "LITERALLY_UNPLAYABLE") == 1,
                    )
                )
        return out

    def _minion_can_attack_face(
        self, ent: dict[str, Any], og_abils: list[OldGodAbilityState] | None = None
    ) -> bool:
        abils = og_abils if og_abils is not None else self._old_god_abils(ent)
        if abils and not all(a.used for a in abils):
            return False
        if (
            _int_tag(ent, "ATK") <= 0
            or _int_tag(ent, "FROZEN") == 1
            or _int_tag(ent, "CANT_ATTACK") == 1
            or _int_tag(ent, "CANT_ATTACK_HERO") == 1
            or _int_tag(ent, "DORMANT") == 1
        ):
            return False
        limit = 2 if _int_tag(ent, "WINDFURY") == 1 else 1
        if _int_tag(ent, "NUM_ATTACKS_THIS_TURN") >= limit:
            return False
        if _int_tag(ent, "EXHAUSTED") == 1 and _int_tag(ent, "CHARGE") != 1:
            return False
        return True

    def _hero_can_attack(self, ent: dict[str, Any]) -> bool:
        if (
            _int_tag(ent, "ATK") <= 0
            or _int_tag(ent, "FROZEN") == 1
            or _int_tag(ent, "CANT_ATTACK") == 1
        ):
            return False
        limit = 2 if _int_tag(ent, "WINDFURY") == 1 else 1
        if _int_tag(ent, "NUM_ATTACKS_THIS_TURN") >= limit:
            return False
        if _int_tag(ent, "EXHAUSTED") == 1:
            return False
        return True

    def _countdown(self, ent: dict[str, Any]) -> tuple[str, CountdownState] | None:
        raw = self.db.raw_text(ent.get("card"))
        if "{1}" not in raw or "discards" not in raw.lower():
            return None
        parts = ALT_SPLIT_RE.split(raw, 1)
        alt = _int_tag(ent, "USE_ALTERNATE_CARD_TEXT") == 1 and len(parts) > 1
        n1 = ent["tags"].get("TAG_SCRIPT_DATA_NUM_1")
        n2 = ent["tags"].get("TAG_SCRIPT_DATA_NUM_2")
        v0: int | str = int(n1) if n1 is not None and str(n1).lstrip("-").isdigit() else FRESH_COUNTDOWN_N1.get(ent.get("card", ""), "?")
        left = 1 if alt else (int(n2) if n2 is not None and str(n2).lstrip("-").isdigit() else 3)
        text = (parts[1] if alt else parts[0]).replace("{0}", str(v0)).replace("{1}", str(left))
        if v0 != 1:
            text = re.sub(r"\bcard\.", "cards.", text)
        return text, CountdownState(
            current_value=v0,
            turns_left_including_this=left,
            discards_this_turn=left <= 1,
        )

    def _prepare_state(self, ent: dict[str, Any]) -> PrepareState | None:
        raw = self.db.raw_text(ent.get("card"))
        has_prep = bool(PREPARE_RE.search(raw)) or _int_tag(ent, "PREPARE") == 1
        prepared = _int_tag(ent, "PREPARED")
        if prepared > 0:
            return PrepareState(
                available=False,
                prepared_discount=prepared,
                locked_this_turn=_int_tag(ent, "LITERALLY_UNPLAYABLE") == 1,
            )
        if not has_prep or _int_tag(ent, "PREPARE", 1) == 0:
            return None
        return PrepareState(available=True, prepared_discount=0, locked_this_turn=False)

    def _enchantment_spell_damage(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for _, en in self.ents.items():
            att = str(en["tags"].get("ATTACHED", ""))
            if (
                en["tags"].get("CARDTYPE") == "ENCHANTMENT"
                and att.isdigit()
                and en["tags"].get("ZONE") in ("PLAY", None)
            ):
                m = re.search(r"Spell Damage \+(\d+)", self.db.raw_text(en.get("card")))
                if m:
                    eid = int(att)
                    out[eid] = out.get(eid, 0) + int(m.group(1))
        return out

    def _attached_enchantments(self, eid: int) -> list[str]:
        names: list[str] = []
        for _, en in sorted(self.ents.items()):
            if (
                en["tags"].get("CARDTYPE") == "ENCHANTMENT"
                and en["tags"].get("ATTACHED") == str(eid)
                and en["tags"].get("ZONE") in ("PLAY", None)
            ):
                cid = en.get("card")
                if cid:
                    self.seen_card_ids.add(cid)
                    names.append(self.db.name(cid))
        return names

    def _effective_hand_text(
        self,
        eid: int,
        ent: dict[str, Any],
        board_sp: int,
        ench_sp: dict[int, int],
    ) -> tuple[str, int, CountdownState | None]:
        cid = ent.get("card", "")
        raw = self.db.raw_text(cid)
        cd = self._countdown(ent)
        cd_state: CountdownState | None = None
        if cd:
            raw, cd_state = cd
        if ent["tags"].get("CARDTYPE") != "SPELL":
            asm = self._assembled(ent)
            if asm:
                return asm[0], 0, cd_state
            return self._short_text(cid, ent=ent, ctrl=ent["tags"].get("CONTROLLER")), 0, cd_state

        sp = ench_sp.get(eid, 0) + board_sp
        n1 = _int_tag(ent, "TAG_SCRIPT_DATA_NUM_1")
        if cid == "CATA_585" and n1 > 0:
            t = re.sub(r"\$(\d+)", lambda m: str(n1 + sp), raw, count=1)
        else:
            t = re.sub(r"\$(\d+)", lambda m: str(int(m.group(1)) + sp), raw)
        t = clean_card_text(t, strip_alternate=False, replace_placeholders=False)
        t = self._fill_placeholders(t, cid, ent=ent, ctrl=ent["tags"].get("CONTROLLER"))
        return re.sub(r"\{\d+\}", "X", t), sp, cd_state

    def _hp_text(self, ent: dict[str, Any]) -> str:
        cid = ent.get("card", "")
        t = self._fill_placeholders(
            self.db.clean_text(cid, replace_placeholders=False),
            cid,
            ent=ent,
            ctrl=ent["tags"].get("CONTROLLER"),
        )
        n1 = _int_tag(ent, "TAG_SCRIPT_DATA_NUM_1")
        if n1 > 0:
            if "{0}" in t:
                t = t.replace("{0}", str(n1))
            elif "@" in t:
                t = t.replace("@", str(n1))
            elif re.search(r"\bDeal \d+ damage\b", t):
                t = re.sub(r"\bDeal \d+ damage\b", f"Deal {n1} damage", t, count=1)
        return re.sub(r"\{\d+\}", "X", t)

    def _quest_info(self, ent: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
        prog = _int_tag(ent, "QUEST_PROGRESS")
        tot = _int_tag(ent, "QUEST_PROGRESS_TOTAL")
        rid = _int_tag(ent, "QUEST_REWARD_DATABASE_ID")
        rcid = self.db.by_dbf_id.get(rid) if rid > 0 else None
        rstr: str | None = None
        if rcid and rcid != ent.get("card"):
            self.seen_card_ids.add(rcid)
            rc = self.db.get(rcid)
            stats = (
                f" {rc['attack']}/{rc['health']}"
                if rc.get("type") == "MINION" and "attack" in rc and "health" in rc
                else ""
            )
            cost = f"({rc['cost']}) " if "cost" in rc else ""
            rtxt = self.db.clean_text(rcid)
            rstr = f"{cost}{self.db.name(rcid)}{stats}" + (f" -- {rtxt}" if rtxt else "")
        return (prog if tot > 0 else None), (tot if tot > 0 else None), rstr

    def _secret_states(self, ctrl: str) -> list[SecretOrQuestState]:
        side = self._side_of(ctrl)
        out: list[SecretOrQuestState] = []
        for eid, ent in self._zone(ctrl, "SECRET"):
            cid = ent.get("card") or None
            if cid:
                self.seen_card_ids.add(cid)
            is_quest = (
                ent["tags"].get("CARDTYPE") == "QUEST"
                or _int_tag(ent, "QUEST") == 1
                or _int_tag(ent, "SIDE_QUEST") == 1
            )
            if is_quest:
                kind = "side_quest" if _int_tag(ent, "SIDE_QUEST") == 1 else "quest"
                prog, tot, reward = self._quest_info(ent)
                out.append(
                    SecretOrQuestState(
                        entity_id=eid,
                        kind=kind,
                        owner=side,
                        card_class=_str_tag(ent, "CLASS") or str(self.db.get(cid).get("cardClass", "")),
                        revealed=bool(cid),
                        card_id=cid,
                        name=self.db.name(cid) if cid else None,
                        quest_progress=prog,
                        quest_total=tot,
                        quest_reward=reward,
                    )
                )
                continue

            if cid:
                out.append(
                    SecretOrQuestState(
                        entity_id=eid,
                        kind="secret",
                        owner=side,
                        card_class=_str_tag(ent, "CLASS") or str(self.db.get(cid).get("cardClass", "")),
                        revealed=True,
                        card_id=cid,
                        name=self.db.name(cid),
                    )
                )
                continue

            cls_name = _str_tag(ent, "CLASS")
            pool = self.db.secret_pool_by_class.get(cls_name, {})
            ruled_overlap = {
                self.db.name(self.ents[o]["card"])
                for o in self.secret_overlap.get(eid, set())
                if self.ents.get(o, {}).get("card")
                and self._is_secret_entity(o)
            }
            ruled = (ruled_overlap | self.secret_ruled_actions.get(eid, set())) & set(pool)
            cands = sorted(n for n in pool if n not in ruled)
            out.append(
                SecretOrQuestState(
                    entity_id=eid,
                    kind="secret",
                    owner=side,
                    card_class=cls_name,
                    revealed=False,
                    possible_secrets=cands,
                    ruled_out_secrets=sorted(ruled),
                )
            )
        return out

    def _is_secret_entity(self, eid: int) -> bool:
        ent = self.ents.get(eid, {})
        cid = ent.get("card", "")
        if not cid:
            return (
                ent.get("tags", {}).get("CARDTYPE") != "QUEST"
                and _int_tag(ent, "QUEST") != 1
                and _int_tag(ent, "SIDE_QUEST") != 1
            )
        return self.db.name(cid) in self.db.all_secret_names or "SECRET" in (
            self.db.get(cid).get("mechanics") or []
        )

    def _build_hand_snapshot(self, ctrl: str) -> list[HandCardSnapshot]:
        hand = self._zone(ctrl, "HAND")
        board_sp = sum(_int_tag(e, "SPELLPOWER") for _, e in self._zone(ctrl, "PLAY", ("MINION",)))
        ench_sp = self._enchantment_spell_damage()
        out: list[HandCardSnapshot] = []
        for idx, (eid, ent) in enumerate(hand, 1):
            cid = ent.get("card", "")
            if cid:
                self.seen_card_ids.add(cid)
            ctype = _str_tag(ent, "CARDTYPE") or str(self.db.get(cid).get("type", ""))
            eff_txt, sp_bonus, cd_state = self._effective_hand_text(eid, ent, board_sp, ench_sp)
            asm = self._assembled(ent)
            prep = self._prepare_state(ent)
            rec = self.db.get(cid)
            base_cost = rec.get("cost") if isinstance(rec.get("cost"), int) else None
            atk = _int_tag(ent, "ATK") if ctype in ("MINION", "WEAPON") else None
            hp = (
                (_int_tag(ent, "HEALTH") - _int_tag(ent, "DAMAGE"))
                if ctype == "MINION"
                else (self._weapon_durability(ent) if ctype == "WEAPON" else None)
            )
            out.append(
                HandCardSnapshot(
                    position=idx,
                    entity_id=eid,
                    card_id=cid,
                    name=self.db.name(cid) if cid else "Unknown",
                    card_type=ctype,
                    cost=_int_tag(ent, "COST"),
                    base_cost=base_cost,
                    attack=atk,
                    health=hp,
                    spell_school=self.db.spell_school.get(cid),
                    races=self.db.format_race(cid, _str_tag(ent, "CARDRACE")),
                    outcast_active=(idx == 1 or idx == len(hand)),
                    spell_damage_bonus=sp_bonus,
                    enchantments=self._attached_enchantments(eid),
                    assembled_note=asm[1] if asm else None,
                    countdown=cd_state,
                    prepare=prep,
                    shattered_half=self.db.is_shattered_half(cid),
                    effective_text=eff_txt,
                    position_matters=self.db.position_matters(cid),
                )
            )
        return out

    def _build_board_snapshot(self, ctrl: str) -> list[BoardEntitySnapshot]:
        items = self._zone(ctrl, "PLAY", ("MINION", "LOCATION"))
        now_turn = self._cur_game_turn()
        out: list[BoardEntitySnapshot] = []
        for pos, (eid, ent) in enumerate(items, 1):
            cid = ent.get("card", "")
            if cid:
                self.seen_card_ids.add(cid)
            ctype = _str_tag(ent, "CARDTYPE")
            asm = self._assembled(ent)
            dg = self._dark_gift_note(eid)
            eff_txt = asm[0] if asm else self._short_text(cid, ent=ent, ctrl=ctrl)

            if ctype == "LOCATION":
                ready = _int_tag(ent, "EXHAUSTED") == 0
                out.append(
                    BoardEntitySnapshot(
                        position=pos,
                        entity_id=eid,
                        card_id=cid,
                        name=self.db.name(cid),
                        card_type="LOCATION",
                        health=_int_tag(ent, "HEALTH") - _int_tag(ent, "DAMAGE"),
                        max_health=_int_tag(ent, "HEALTH"),
                        damage=_int_tag(ent, "DAMAGE"),
                        location_ready=ready,
                        can_attack=False,
                        attacks_remaining=0,
                        effective_text=eff_txt or None,
                        position_matters=self.db.position_matters(cid),
                    )
                )
                continue

            kws = [
                label
                for label, tag in (
                    ("Taunt", "TAUNT"),
                    ("Divine Shield", "DIVINE_SHIELD"),
                    ("Stealth", "STEALTH"),
                    ("Frozen", "FROZEN"),
                    ("Windfury", "WINDFURY"),
                    ("Lifesteal", "LIFESTEAL"),
                    ("Poisonous", "POISONOUS"),
                    ("Rush", "RUSH"),
                    ("Charge", "CHARGE"),
                    ("Reborn", "REBORN"),
                    ("Elusive", "ELUSIVE"),
                    ("Immune", "IMMUNE"),
                )
                if _int_tag(ent, tag) == 1
            ]
            dormant = _int_tag(ent, "DORMANT") == 1
            wake_turn: int | None = None
            wakes_exhausted = False
            if dormant:
                wakes_exhausted = _int_tag(ent, "CHARGE") != 1 and _int_tag(ent, "RUSH") != 1
                nm = re.search(r"Dormant for (\d+) turn", self.db.clean_text(cid))
                if nm and eid in self.dormant_since:
                    wake_turn = self.dormant_since[eid] + 2 * int(nm.group(1))
            og_abils = self._old_god_abils(ent)
            can_atk = self._minion_can_attack_face(ent, og_abils)
            limit = 2 if _int_tag(ent, "WINDFURY") == 1 else 1
            atks_left = max(0, limit - _int_tag(ent, "NUM_ATTACKS_THIS_TURN")) if can_atk else 0
            # Also handle Rush minions summoned this turn (can attack minions only, not face)
            if (
                not can_atk
                and _int_tag(ent, "EXHAUSTED") == 1
                and _int_tag(ent, "RUSH") == 1
                and _int_tag(ent, "CANT_ATTACK") != 1
                and _int_tag(ent, "FROZEN") != 1
                and not dormant
                and _int_tag(ent, "ATK") > 0
                and _int_tag(ent, "NUM_ATTACKS_THIS_TURN") < limit
            ):
                if "Rush" in kws:
                    kws[kws.index("Rush")] = "Rush (minions only)"
                else:
                    kws.append("Rush (minions only)")

            if dormant and wake_turn is not None:
                kws.append(f"Dormant (wakes T{wake_turn}, now T{now_turn})")
            elif dormant:
                kws.append("Dormant")

            out.append(
                BoardEntitySnapshot(
                    position=pos,
                    entity_id=eid,
                    card_id=cid,
                    name=self.db.name(cid),
                    card_type="MINION",
                    attack=_int_tag(ent, "ATK"),
                    health=_int_tag(ent, "HEALTH") - _int_tag(ent, "DAMAGE"),
                    max_health=_int_tag(ent, "HEALTH"),
                    damage=_int_tag(ent, "DAMAGE"),
                    races=self.db.format_race(cid, _str_tag(ent, "CARDRACE")),
                    keywords=kws,
                    spell_damage=_int_tag(ent, "SPELLPOWER"),
                    dormant=dormant,
                    dormant_wakes_turn=wake_turn,
                    dormant_wakes_exhausted=wakes_exhausted,
                    can_attack=can_atk,
                    attacks_remaining=atks_left,
                    old_god_abilities=og_abils,
                    assembled_note=asm[1] if asm else None,
                    dark_gift=dg,
                    effective_text=eff_txt or None,
                    position_matters=self.db.position_matters(cid),
                )
            )
        return out

    def _build_hero_state(self, ctrl: str) -> HeroState:
        heroes = self._zone(ctrl, "PLAY", ("HERO",))
        if heroes:
            he = heroes[-1][1]
        elif ctrl in self.active_hero:
            he = self.ents.get(self.active_hero[ctrl], {"card": "", "tags": {}})
        else:
            he = {"card": "", "tags": {}}
        h_cid = he.get("card", "")
        if h_cid:
            self.seen_card_ids.add(h_cid)
        max_hp = _int_tag(he, "HEALTH", 30)
        hp = max_hp - _int_tag(he, "DAMAGE")
        armor = _int_tag(he, "ARMOR")

        flags: list[str] = []
        for label, tag in (
            ("Divine Shield", "DIVINE_SHIELD"),
            ("Immune", "IMMUNE"),
            ("Stealth", "STEALTH"),
            ("Frozen", "FROZEN"),
        ):
            if _int_tag(he, tag) == 1:
                flags.append(label)

        weap_list = self._zone(ctrl, "PLAY", ("WEAPON",))
        weap_state: WeaponState | None = None
        if weap_list:
            weid, went = weap_list[-1]
            wcid = went.get("card", "")
            if wcid:
                self.seen_card_ids.add(wcid)
            weap_state = WeaponState(
                entity_id=weid,
                card_id=wcid,
                name=self.db.name(wcid),
                attack=_int_tag(went, "ATK"),
                durability=self._weapon_durability(went),
                effective_text=self._short_text(wcid, ent=went, ctrl=ctrl) or None,
            )

        hp_list = self._zone(ctrl, "PLAY", ("HERO_POWER",))
        hp_state: HeroPowerState | None = None
        if hp_list:
            _, hpent = hp_list[-1]
            hpcid = hpent.get("card", "")
            if hpcid:
                self.seen_card_ids.add(hpcid)
            hp_state = HeroPowerState(
                card_id=hpcid,
                name=self.db.name(hpcid),
                cost=_int_tag(hpent, "COST"),
                used=_int_tag(hpent, "EXHAUSTED") == 1,
                effective_text=self._hp_text(hpent),
            )

        return HeroState(
            hero_class=self._hero_class(ctrl),
            card_id=h_cid,
            name=self.db.name(h_cid) if h_cid else "Unknown Hero",
            hp=hp,
            max_hp=max_hp,
            armor=armor,
            effective_hp=hp + armor,
            attack=_int_tag(he, "ATK"),
            can_attack=self._hero_can_attack(he),
            flags=flags,
            weapon=weap_state,
            hero_power=hp_state,
        )

    def _opponent_hand_slots(self) -> list[OpponentHandSlot]:
        opp_hand = self._zone(self.opp, "HAND")
        slots: list[OpponentHandSlot] = []
        for idx, (eid, ent) in enumerate(opp_hand, 1):
            tags = ent["tags"]
            cr = tags.get("DISPLAYED_CREATOR") or tags.get("CREATOR")
            cr_name: str | None = None
            if cr and str(cr).isdigit() and int(cr) in self.ents:
                cr_cid = self.ents[int(cr)].get("card")
                if cr_cid:
                    self.seen_card_ids.add(cr_cid)
                    cr_name = self.db.name(cr_cid)
            is_buffed = any(
                en["tags"].get("ATTACHED") == str(eid)
                and en["tags"].get("ZONE") != "GRAVEYARD"
                for en in self.ents.values()
            )
            rev_cid = ent.get("card") or None
            if rev_cid:
                self.seen_card_ids.add(rev_cid)
            slots.append(
                OpponentHandSlot(
                    position=idx,
                    turn_entered=self.hand_entered.get(eid, 0),
                    created_by=cr_name,
                    is_minion_known=tags.get("CARDTYPE") == "MINION",
                    is_buffed=is_buffed,
                    revealed_card_id=rev_cid,
                    revealed_name=self.db.name(rev_cid) if rev_cid else None,
                )
            )
        return slots

    def _special_notes(self) -> list[str]:
        notes: list[str] = []
        # Setesh timers
        for who, ctrl in (("your", self.me), ("opponent", self.opp)):
            heroes = self._zone(ctrl, "PLAY", ("HERO",))
            arm = _int_tag(heroes[-1][1], "ARMOR") if heroes else 0
            for eid, ent in sorted(self.ents.items()):
                if (
                    ent.get("card") == "BE_557e2"
                    and ent["tags"].get("CONTROLLER") == ctrl
                    and ent["tags"].get("ZONE") == "PLAY"
                ):
                    done = _int_tag(ent, "TAG_SCRIPT_DATA_NUM_1")
                    left = max(1, 2 - done)
                    notes.append(
                        f"Setesh timer ({who}): converts {arm} Armor -> {arm}/{arm} Taunt Steel Golem in {left} turn(s)."
                    )
        # Opponent C'Thun tracker
        opp_abils = list(
            dict.fromkeys(
                a
                for eid, a in self.cthun_trig
                if self.ents.get(eid, {}).get("tags", {}).get("CONTROLLER") == self.opp
            )
        )
        opp_lash = sum(1 for p in self.lash_trig if p == self.opp)
        opp_cthun_played = any(
            e.get("card") == "BE_013"
            and e["tags"].get("CONTROLLER") == self.opp
            and e["tags"].get("ZONE") in ("PLAY", "GRAVEYARD")
            for e in self.ents.values()
        )
        if (opp_abils or opp_lash) and not opp_cthun_played:
            cost = max(0, 10 - opp_lash)
            notes.append(
                f"Opponent C'Thun progress: {len(opp_abils)}/4 abilities ({', '.join(opp_abils) or 'none'}), ~{cost} mana."
            )
        # Non-adjacent Shattered halves
        halves: dict[str, list[int]] = {}
        for _, ent in self._zone(self.me, "HAND"):
            cid = ent.get("card", "")
            if cid and self.db.is_shattered_half(cid):
                halves.setdefault(self.db.name(cid), []).append(_int_tag(ent, "ZONE_POSITION"))
        for cname, positions in halves.items():
            if len(positions) >= 2 and min(abs(a - b) for a in positions for b in positions if a != b) > 1:
                notes.append(f"Shattered halves of {cname} are not adjacent in hand.")
        return notes

    def _capture_board_state(self) -> BoardStateSnapshot:
        pm = self._player_ent(self.me)
        po = self._player_ent(self.opp)
        my_mana = max(
            0,
            _int_tag(pm, "RESOURCES")
            - _int_tag(pm, "RESOURCES_USED")
            + _int_tag(pm, "TEMP_RESOURCES"),
        )
        opp_mana = max(
            0,
            _int_tag(po, "RESOURCES")
            - _int_tag(po, "RESOURCES_USED")
            + _int_tag(po, "TEMP_RESOURCES"),
        )
        opp_hand = self._zone(self.opp, "HAND")
        return BoardStateSnapshot(
            your_mana_available=my_mana,
            your_mana_max=_int_tag(pm, "RESOURCES"),
            your_overload_locked=_int_tag(pm, "OVERLOAD_LOCKED"),
            opponent_mana_available=opp_mana,
            opponent_mana_max=_int_tag(po, "RESOURCES"),
            your_hero=self._build_hero_state(self.me),
            opponent_hero=self._build_hero_state(self.opp),
            your_board=self._build_board_snapshot(self.me),
            opponent_board=self._build_board_snapshot(self.opp),
            your_hand=self._build_hand_snapshot(self.me),
            opponent_hand_size=len(opp_hand),
            opponent_hand_slots=self._opponent_hand_slots(),
            your_deck_size=len(self._zone(self.me, "DECK")),
            opponent_deck_size=len(self._zone(self.opp, "DECK")),
            your_secrets_and_quests=self._secret_states(self.me),
            opponent_secrets_and_quests=self._secret_states(self.opp),
            opponent_triggered_secrets=list(self.opp_triggered_secrets),
            special_notes=self._special_notes(),
        )

    def _spell_face_damage(
        self, ent: dict[str, Any], eff_text: str
    ) -> tuple[int, str, list[int]]:
        if ent["tags"].get("CARDTYPE") != "SPELL":
            return 0, "", []
        s = eff_text.lower()
        total = 0
        hits: list[int] = []
        notes: list[str] = []
        opp_minions = self._zone(self.opp, "PLAY", ("MINION",))
        for m in re.finditer(r"deal (\d+) damage([^.]*)", s):
            dmg = int(m.group(1))
            rest = m.group(2)
            if "more" in rest:
                continue
            if "minion" in rest and "enemies" not in rest:
                continue
            if "randomly split" in rest:
                if not opp_minions:
                    total += dmg
                    hits.append(dmg)
                else:
                    notes.append(f"up to {dmg} random")
                continue
            total += dmg
            hits.append(dmg)
        return total, "; ".join(notes), hits

    def _compute_damage_math(self, window: str) -> DamageMathSnapshot:
        pm = self._player_ent(self.me)
        mana = max(
            0,
            _int_tag(pm, "RESOURCES")
            - _int_tag(pm, "RESOURCES_USED")
            + _int_tag(pm, "TEMP_RESOURCES"),
        )
        opp_minions = self._zone(self.opp, "PLAY", ("MINION",))
        taunt = any(
            _int_tag(e, "TAUNT") == 1
            and _int_tag(e, "STEALTH") != 1
            and _int_tag(e, "DORMANT") != 1
            and _int_tag(e, "IMMUNE") != 1
            for _, e in opp_minions
        )
        opp_heroes = self._zone(self.opp, "PLAY", ("HERO",))
        oh = opp_heroes[-1][1] if opp_heroes else {"tags": {}}
        opp_eff_hp = _int_tag(oh, "HEALTH", 30) - _int_tag(oh, "DAMAGE") + _int_tag(oh, "ARMOR")
        opp_shield = _int_tag(oh, "DIVINE_SHIELD") == 1
        opp_immune = _int_tag(oh, "IMMUNE") == 1
        has_active_secret = any(
            self._is_secret_entity(eid)
            for eid in self.active_opp_secrets
        )

        free_dmg = 0
        free_src: list[str] = []
        free_hits: list[int] = []
        if not taunt:
            for _, e in self._zone(self.me, "PLAY", ("MINION",)):
                if self._minion_can_attack_face(e):
                    hits = (2 if _int_tag(e, "WINDFURY") == 1 else 1) - _int_tag(
                        e, "NUM_ATTACKS_THIS_TURN"
                    )
                    atk = _int_tag(e, "ATK")
                    if hits > 0 and atk > 0:
                        free_dmg += atk * hits
                        free_hits.extend([atk] * hits)
                        free_src.append(
                            f"{self.db.name(e.get('card'))} attack {atk}"
                            + (f"x{hits}" if hits > 1 else "")
                        )
            for _, e in self._zone(self.me, "PLAY", ("HERO",)):
                if self._hero_can_attack(e):
                    hits = (2 if _int_tag(e, "WINDFURY") == 1 else 1) - _int_tag(
                        e, "NUM_ATTACKS_THIS_TURN"
                    )
                    atk = _int_tag(e, "ATK")
                    if hits > 0 and atk > 0:
                        free_dmg += atk * hits
                        free_hits.extend([atk] * hits)
                        free_src.append(
                            f"Hero attack {atk}" + (f"x{hits}" if hits > 1 else "")
                        )

        board_sp = sum(
            _int_tag(e, "SPELLPOWER") for _, e in self._zone(self.me, "PLAY", ("MINION",))
        )
        ench_sp = self._enchantment_spell_damage()
        items: list[tuple[int, int, str, list[int]]] = []
        notes: list[str] = []
        shattered: dict[str, list[tuple[int, int, int, str, list[int]]]] = {}

        for eid, ent in self._zone(self.me, "HAND"):
            cid = ent.get("card", "")
            if not cid or _int_tag(ent, "LITERALLY_UNPLAYABLE") == 1:
                continue
            eff_txt, _, _ = self._effective_hand_text(eid, ent, board_sp, ench_sp)
            fd, note, fd_hits = self._spell_face_damage(ent, eff_txt)
            if note:
                notes.append(f"{self.db.name(cid)}: {note}")
            if self.db.is_shattered_half(cid):
                shattered.setdefault(self.db.name(cid), []).append(
                    (_int_tag(ent, "COST"), fd, ench_sp.get(eid, 0), cid, fd_hits)
                )
                continue
            if fd > 0:
                items.append((_int_tag(ent, "COST"), fd, self.db.name(cid), fd_hits))

        for name, parts in shattered.items():
            distinct_ids = list(dict.fromkeys(p[3] for p in parts))
            if len(distinct_ids) >= 2:
                a = next(p for p in parts if p[3] == distinct_ids[0])
                b = next(p for p in parts if p[3] == distinct_ids[1])
                dmg = a[1] + b[1] + (b[2] if a[1] else 0) + (a[2] if b[1] else 0)
                if dmg > 0:
                    items.append((a[0], dmg, f"{name} (merged)", a[4] + b[4]))
            else:
                for p in parts:
                    if p[1] > 0:
                        items.append((p[0], p[1], f"{name} (half)", p[4]))

        hp_list = self._zone(self.me, "PLAY", ("HERO_POWER",))
        if hp_list:
            hp_ent = hp_list[-1][1]
            ht = self._hp_text(hp_ent)
            m = re.search(r"Deal (\d+) damage([^.]*)", ht)
            if m and _int_tag(hp_ent, "EXHAUSTED") != 1:
                hdmg = int(m.group(1))
                hrest = m.group(2).lower()
                if "random" in hrest and opp_minions:
                    notes.append(f"{self.db.name(hp_ent.get('card'))}: {hdmg} random")
                elif "minion" not in hrest or "enemies" in hrest:
                    items.append(
                        (
                            _int_tag(hp_ent, "COST"),
                            hdmg,
                            self.db.name(hp_ent.get("card")),
                            [hdmg],
                        )
                    )

        # 0-1 knapsack over mana budget
        best: dict[int, tuple[int, list[str], list[int]]] = {0: (0, [], [])}
        for cost, dmg, nm, hits in items:
            for spent, (cur_d, cur_used, cur_hits) in sorted(best.items(), reverse=True):
                ns = spent + cost
                if ns <= mana and (ns not in best or best[ns][0] < cur_d + dmg):
                    best[ns] = (
                        cur_d + dmg,
                        cur_used + [f"{nm} ({dmg})"],
                        cur_hits + hits,
                    )

        d, used, spell_hits = max(best.values(), key=lambda x: x[0])
        total = free_dmg + d
        all_hits = free_hits + spell_hits
        if opp_shield and all_hits:
            # First damage instance is absorbed by hero Divine Shield; remove smallest hit optimistically
            total = max(0, total - min(all_hits))

        lethal = not opp_immune and opp_eff_hp > 0 and total >= opp_eff_hp
        if not lethal:
            confidence = "none"
        elif opp_shield or has_active_secret:
            confidence = "check_secret_or_shield"
        else:
            confidence = "certain"

        return DamageMathSnapshot(
            window=window,  # type: ignore[arg-type]
            mana_available=mana,
            enemy_effective_hp=opp_eff_hp,
            enemy_taunt_blocking=taunt,
            enemy_hero_divine_shield=opp_shield,
            enemy_hero_immune=opp_immune,
            enemy_has_active_secret=has_active_secret,
            max_face_damage=total,
            sources=free_src + used,
            unmodelled_notes=notes,
            lethal_available=lethal,
            confidence=confidence,  # type: ignore[arg-type]
        )

    def _open_turn_if_needed(self) -> _TurnWork:
        if self.current_turn_work is not None:
            return self.current_turn_work
        active_side = self._active_side()
        self.player_turn_counts[active_side] += 1
        start_state = self._capture_board_state()
        start_dm = self._compute_damage_math("start_of_turn") if active_side == "you" else None
        tw = _TurnWork(
            turn_number=self._cur_game_turn() or 1,
            player_turn_number=self.player_turn_counts[active_side],
            active_player=active_side,
            start_state=start_state,
            start_damage_math=start_dm,
            peak_damage_math=start_dm,
            choices=list(self.pending_choices),
            cards_burned=list(self.pending_burned),
            cards_discarded=list(self.pending_discarded),
        )
        self.pending_choices.clear()
        self.pending_burned.clear()
        self.pending_discarded.clear()
        self.current_turn_work = tw
        return tw

    def _capture_eot_checks(self, tw: _TurnWork) -> None:
        if tw.eot_captured or tw.active_player != "you":
            return
        tw.eot_captured = True
        pm = self._player_ent(self.me)
        mana_left = max(
            0,
            _int_tag(pm, "RESOURCES")
            - _int_tag(pm, "RESOURCES_USED")
            + _int_tag(pm, "TEMP_RESOURCES"),
        )
        tw.eot_mana_floated = mana_left
        tw.eot_prepare_available = any(
            (p := self._prepare_state(e)) is not None and p.available
            for _, e in self._zone(self.me, "HAND")
        )
        unused_atks: list[str] = []
        for _, e in self._zone(self.me, "PLAY", ("MINION",)):
            if self._minion_can_attack_face(e):
                unused_atks.append(f"{self.db.name(e.get('card'))} ({_int_tag(e, 'ATK')} atk)")
        for _, e in self._zone(self.me, "PLAY", ("HERO",)):
            if self._hero_can_attack(e):
                unused_atks.append(f"Hero ({_int_tag(e, 'ATK')} atk)")
        tw.eot_unused_attacks = unused_atks

        tw.eot_unused_ready_locations = [
            self.db.name(e.get("card"))
            for _, e in self._zone(self.me, "PLAY", ("LOCATION",))
            if _int_tag(e, "EXHAUSTED") == 0
        ]
        hp_list = self._zone(self.me, "PLAY", ("HERO_POWER",))
        if hp_list:
            hp_ent = hp_list[-1][1]
            tw.eot_unused_hero_power = (
                _int_tag(hp_ent, "EXHAUSTED") != 1
                and mana_left >= _int_tag(hp_ent, "COST")
            )

    def _finalize_turn(self, game_ended_win: bool = False) -> None:
        tw = self.current_turn_work
        if tw is None:
            return
        self.current_turn_work = None
        end_state = self._capture_board_state()

        if tw.active_player == "you" and not tw.eot_captured:
            self._capture_eot_checks(tw)

        # Flush any SendChoices-only choices before finalizing the turn
        for ch in self.choices_by_id.values():
            if ch.choice_id not in self.recorded_choice_ids and ch.chosen_ids:
                self._record_choice_if_ready(ch)

        if game_ended_win and tw.active_player == "you":
            # Winning turn: never penalize unused lethal/mana/attacks after winning!
            missed = False
            missed_detail = None
            mana_floated = 0
            unused_attacks: list[str] = []
            unused_locs: list[str] = []
            unused_hp = False
        elif tw.active_player == "you":
            s_dm = tw.start_damage_math
            p_dm = tw.peak_damage_math
            lethal_dm: DamageMathSnapshot | None = None
            if s_dm and s_dm.lethal_available and p_dm and p_dm.lethal_available:
                lethal_dm = (
                    s_dm
                    if (s_dm.confidence == "certain" or p_dm.confidence != "certain")
                    else p_dm
                )
            elif p_dm and p_dm.lethal_available:
                lethal_dm = p_dm
            elif s_dm and s_dm.lethal_available:
                lethal_dm = s_dm

            missed = lethal_dm is not None
            missed_detail = (
                f"{lethal_dm.window}: {lethal_dm.max_face_damage} max dmg "
                f"({' + '.join(lethal_dm.sources)}) vs {lethal_dm.enemy_effective_hp} eff HP "
                f"[{lethal_dm.confidence}]"
                if lethal_dm
                else None
            )
            mana_floated = tw.eot_mana_floated
            unused_attacks = tw.eot_unused_attacks
            unused_locs = tw.eot_unused_ready_locations
            unused_hp = tw.eot_unused_hero_power
        else:
            missed = False
            missed_detail = None
            mana_floated = 0
            unused_attacks = []
            unused_locs = []
            unused_hp = False

        # Flush any trailing choices/losses that arrived during MAIN_END/MAIN_CLEANUP
        if self.pending_choices:
            tw.choices.extend(self.pending_choices)
            self.pending_choices.clear()
        if self.pending_burned:
            tw.cards_burned.extend(self.pending_burned)
            self.pending_burned.clear()
        if self.pending_discarded:
            tw.cards_discarded.extend(self.pending_discarded)
            self.pending_discarded.clear()

        self.turns.append(
            TurnSnapshot(
                turn_number=tw.turn_number,
                player_turn_number=tw.player_turn_number,
                active_player=tw.active_player,
                start_state=tw.start_state,
                actions=tw.actions,
                choices=tw.choices,
                end_state=end_state,
                pre_checks=TurnPreChecks(
                    start_damage_math=tw.start_damage_math,
                    peak_damage_math=(
                        tw.peak_damage_math
                        if tw.peak_damage_math != tw.start_damage_math
                        else None
                    ),
                    missed_lethal=missed,
                    missed_lethal_detail=missed_detail,
                    mana_floated=mana_floated,
                    prepare_was_available=tw.eot_prepare_available and mana_floated > 0,
                    unused_attacks=unused_attacks,
                    unused_ready_locations=unused_locs,
                    unused_hero_power=unused_hp,
                    cards_burned=tw.cards_burned,
                    cards_discarded=tw.cards_discarded,
                ),
            )
        )

    def _record_choice_if_ready(self, ch: _OpenChoice) -> None:
        if ch.choice_type == "MULLIGAN" or ch.choice_id in self.recorded_choice_ids:
            return
        self.recorded_choice_ids.add(ch.choice_id)
        actor: Side = "you" if ch.player_pid == self.me else "opponent"
        is_assemble = (
            len(ch.opt_cids) == 4
            and ch.opt_cids[0]
            and ch.opt_cids[0] == ch.opt_cids[1]
            and ch.opt_cids[2] == ch.opt_cids[3]
        )
        src_cid = ch.source_cid or self.ents.get(ch.source_id, {}).get("card", "") or None
        if src_cid:
            self.seen_card_ids.add(src_cid)
        src_name = self.db.name(src_cid) if src_cid else None
        src_raw = self.db.raw_text(src_cid) if src_cid else ""
        if is_assemble:
            kind = "assemble"
        elif "Choose One" in src_raw:
            kind = "choose_one"
        elif "Discover" in src_raw or len(ch.opt_ids) == 3:
            kind = "discover"
        else:
            kind = "general"

        options_visible = any(
            bool(c or self.ents.get(oid, {}).get("card"))
            for oid, c in zip(ch.opt_ids, ch.opt_cids)
        )
        opts: list[ChoiceOption] = []
        if is_assemble and options_visible:
            for idx in (0, 2):
                oid = ch.opt_ids[idx]
                cid = ch.opt_cids[idx] or self.ents.get(oid, {}).get("card", "")
                if cid:
                    self.seen_card_ids.add(cid)
                ent = self.ents.get(oid, {"tags": {}})
                cost = ent["tags"].get("COST")
                cost_i = int(cost) if cost is not None and str(cost).isdigit() else self.db.get(cid).get("cost")
                opts.append(
                    ChoiceOption(
                        entity_id=oid,
                        card_id=cid,
                        name=self.db.name(cid),
                        cost=cost_i if isinstance(cost_i, int) else None,
                        text=self.db.clean_text(cid, strip_alternate=True),
                        dark_gift=self._dark_gift_note(oid),
                    )
                )
        elif options_visible:
            for oid, cid in zip(ch.opt_ids, ch.opt_cids):
                rcid = cid or self.ents.get(oid, {}).get("card", "")
                if rcid:
                    self.seen_card_ids.add(rcid)
                ent = self.ents.get(oid, {"tags": {}})
                cost = ent["tags"].get("COST")
                cost_i = int(cost) if cost is not None and str(cost).isdigit() else self.db.get(rcid).get("cost")
                opts.append(
                    ChoiceOption(
                        entity_id=oid,
                        card_id=rcid,
                        name=self.db.name(rcid) if rcid else "Unknown",
                        cost=cost_i if isinstance(cost_i, int) else None,
                        text=self.db.clean_text(rcid, strip_alternate=True) if rcid else "",
                        dark_gift=self._dark_gift_note(oid),
                    )
                )

        chosen_cids: list[str] = []
        chosen_names: list[str] = []
        for idx, cid in enumerate(ch.chosen_cids):
            oid = ch.chosen_ids[idx] if idx < len(ch.chosen_ids) else 0
            rcid = cid or self.ents.get(oid, {}).get("card", "")
            if rcid:
                self.seen_card_ids.add(rcid)
                chosen_cids.append(rcid)
                chosen_names.append(self.db.name(rcid))

        mid_card: str | None = None
        if is_assemble and ch.source_id in self.ents:
            mid_id = _int_tag(self.ents[ch.source_id], "ASSEMBLE_TARGET")
            if mid_id in self.ents and self.ents[mid_id].get("card"):
                mcid = self.ents[mid_id]["card"]
                self.seen_card_ids.add(mcid)
                mid_card = self.db.name(mcid)

        rec = ChoiceRecord(
            choice_id=ch.choice_id,
            actor=actor,
            kind=kind,  # type: ignore[arg-type]
            source_card_id=src_cid,
            source_name=src_name,
            options_visible=options_visible,
            options=opts,
            chosen_card_ids=chosen_cids,
            chosen_names=chosen_names,
            assemble_middle_card=mid_card,
        )
        if self.current_turn_work is not None:
            self.current_turn_work.choices.append(rec)
        else:
            self.pending_choices.append(rec)

    def _format_action_summary(
        self,
        action_type: ActionType,
        src_name: str,
        tgt_name: str | None,
        tgt_side: Side | None,
        *,
        actor: Side | None = None,
        active_player: Side | None = None,
        cause: str | None = None,
        is_spell_trigger: bool = False,
    ) -> str:
        return format_action_summary(
            action_type,
            src_name,
            tgt_name,
            tgt_side,
            actor=actor,
            active_player=active_player,
            cause=cause,
            is_spell_trigger=is_spell_trigger,
        )

    def _unknown_label(self, ent: dict[str, Any]) -> str:
        if ent["tags"].get("ZONE") == "SECRET" or _int_tag(ent, "SECRET") == 1:
            cls_name = _str_tag(ent, "CLASS")
            return f"Hidden {cls_name.title()} Secret" if cls_name else "Hidden Secret"
        return "Hidden Card"

    def _refresh_action(self, tw: _TurnWork, action_idx: int) -> None:
        if action_idx < 0 or action_idx >= len(tw.actions):
            return
        act = tw.actions[action_idx]
        if act.source_entity_id is not None and act.source_entity_id in self.ents:
            se = self.ents[act.source_entity_id]
            cid = se.get("card") or act.source_card_id
            if cid:
                self.seen_card_ids.add(cid)
                act.source_card_id = cid
                act.source_name = self.db.name(cid)
            else:
                act.source_name = self._unknown_label(se)
            if act.action_type == "PLAY" and se["tags"].get("CARDTYPE") == "HERO_POWER":
                act.action_type = "HERO_POWER"
            if act.action_type in ("PLAY", "HERO_POWER"):
                if act.mana_cost is None and "COST" in se["tags"]:
                    act.mana_cost = _int_tag(se, "COST")
            else:
                act.mana_cost = None
        if act.action_type not in ("PLAY", "HERO_POWER"):
            act.mana_cost = None
        if act.target_entity_id is not None and act.target_entity_id in self.ents:
            te = self.ents[act.target_entity_id]
            tcid = te.get("card") or act.target_card_id
            if tcid:
                self.seen_card_ids.add(tcid)
                act.target_card_id = tcid
                act.target_name = self.db.name(tcid)
            tctrl = te["tags"].get("CONTROLLER")
            if tctrl:
                act.target_side = self._side_of(tctrl)
        cause, is_spell_trigger = self._action_meta.get(
            (tw.turn_number, action_idx), (None, False)
        )
        act.summary = self._format_action_summary(
            act.action_type,
            act.source_name,
            act.target_name,
            act.target_side,
            actor=act.actor,
            active_player=tw.active_player,
            cause=cause,
            is_spell_trigger=is_spell_trigger,
        )

    def _set_entity_tag(self, eid: int, tag: str, val: str) -> None:
        ent = self._ent(eid)
        old_zone = ent["tags"].get("ZONE")
        ent["tags"][tag] = val

        if tag == "CARDTYPE" or tag == "ZONE" or tag == "CONTROLLER":
            if (
                ent["tags"].get("CARDTYPE") == "HERO"
                and ent["tags"].get("ZONE") == "PLAY"
                and ent["tags"].get("CONTROLLER")
            ):
                self.active_hero[ent["tags"]["CONTROLLER"]] = eid

        if tag == "DORMANT" and val == "1":
            self.dormant_since.setdefault(eid, self._cur_game_turn())

        if tag == "ZONE":
            if val == "DECK" and not self.game_tags.get("STEP"):
                self.initial_deck_candidates.add(eid)
            if val == "HAND" and old_zone != "HAND":
                self.hand_entered[eid] = self._cur_game_turn()
            # Track opponent secrets
            if val == "SECRET" and ent["tags"].get("CONTROLLER") == self.opp:
                if eid not in self.active_opp_secrets:
                    for other in self.active_opp_secrets:
                        self.secret_overlap.setdefault(eid, set()).add(other)
                        self.secret_overlap.setdefault(other, set()).add(eid)
                    self.active_opp_secrets.add(eid)
            elif val != "SECRET" and eid in self.active_opp_secrets:
                self.active_opp_secrets.discard(eid)
                if ent.get("card") and self._is_secret_entity(eid):
                    sname = self.db.name(ent["card"])
                    self.opp_triggered_secrets.append(sname)
                    for blk in self.block_stack:
                        blk.triggered_secrets.add(sname)

            # Track hand -> graveyard discards (not played, not mulligan)
            if (
                old_zone == "HAND"
                and val == "GRAVEYARD"
                and self.game_tags.get("STEP") != "BEGIN_MULLIGAN"
            ):
                in_own_play = any(
                    b.block_type == "PLAY" and b.source_eid == eid
                    for b in self.block_stack
                )
                if not in_own_play:
                    cid = ent.get("card") or None
                    if cid:
                        self.seen_card_ids.add(cid)
                    ev = CardLossEvent(
                        owner=self._side_of(ent["tags"].get("CONTROLLER")),
                        reason="discarded",
                        entity_id=eid,
                        card_id=cid,
                        name=self.db.name(cid) if cid else "Unknown Card",
                    )
                    if self.current_turn_work is not None:
                        self.current_turn_work.cards_discarded.append(ev)
                    else:
                        self.pending_discarded.append(ev)

    def _build_observed_deck(self, ctrl: str) -> list[DeckCard]:
        counts: dict[str, int] = {}
        for eid in self.initial_deck_candidates:
            ent = self.ents.get(eid)
            if not ent:
                continue
            tags = ent["tags"]
            if tags.get("CONTROLLER") != ctrl:
                continue
            if tags.get("CREATOR") or tags.get("DISPLAYED_CREATOR") or tags.get("CREATOR_DBID"):
                continue
            cid = self.original_card_id.get(eid) or ent.get("card", "")
            if not cid or cid == COIN_CARD_ID or self.db.name(cid) == "The Coin":
                continue
            ctype = str(tags.get("CARDTYPE") or self.db.get(cid).get("type", ""))
            if ctype == "ENCHANTMENT":
                continue
            self.seen_card_ids.add(cid)
            counts[cid] = counts.get(cid, 0) + 1

        out: list[DeckCard] = []
        for cid, cnt in counts.items():
            rec = self.db.get(cid)
            cost = rec.get("cost") if isinstance(rec.get("cost"), int) else None
            ctype = str(rec.get("type", ""))
            out.append(
                DeckCard(
                    card_id=cid,
                    name=self.db.name(cid),
                    cost=cost,
                    count=cnt,
                    card_type=ctype,
                )
            )
        return sorted(out, key=lambda d: (d.cost if d.cost is not None else 99, d.name, d.card_id))

    def _resolve_end_reason(self, result: Result) -> EndReason:
        for ctrl in (self.me, self.opp):
            peid = self.pid_to_eid.get(int(ctrl))
            if peid is not None and peid in self.end_reason_by_eid:
                reason = self.end_reason_by_eid[peid]
                if reason == "conceded":
                    return "concede"
                if reason == "disconnected":
                    return "disconnect"
        if result == "tie":
            return "tie"
        return "lethal"

    def _build_mulligan_snapshot(self, went_first: bool) -> MulliganSnapshot:
        my_offer = self.mulligan_offers.get(self.me)
        my_kept_ids = set(self.mulligan_chosen.get(self.me, []))
        offered_cards: list[MulliganCard] = []
        kept_names: list[str] = []
        tossed_names: list[str] = []

        if my_offer:
            for idx, (oid, ocid) in enumerate(zip(my_offer.opt_ids, my_offer.opt_cids)):
                ent = self.ents.get(oid, {"card": "", "tags": {}})
                cid = ocid or ent.get("card", "")
                if cid == COIN_CARD_ID or (cid and self.db.name(cid) == "The Coin"):
                    continue
                if len(my_offer.opt_ids) == 5 and idx == 4 and not cid:
                    continue
                if cid:
                    self.seen_card_ids.add(cid)
                kept = oid in my_kept_ids
                name = self.db.name(cid) if cid else "Unknown"
                rec = self.db.get(cid)
                cost = rec.get("cost") if isinstance(rec.get("cost"), int) else None
                offered_cards.append(
                    MulliganCard(
                        entity_id=oid,
                        card_id=cid,
                        name=name,
                        cost=cost,
                        text=self.db.clean_text(cid, strip_alternate=True) if cid else "",
                        kept=kept,
                    )
                )
                if kept:
                    kept_names.append(name)
                else:
                    tossed_names.append(name)

        opp_offer = self.mulligan_offers.get(self.opp)
        opp_total = 3 if not went_first else 4
        if opp_offer:
            # Exclude Coin slot if 5 entities offered to opponent
            opp_total = min(4, len(opp_offer.opt_ids)) if len(opp_offer.opt_ids) >= 4 else len(opp_offer.opt_ids)
        opp_kept = len(self.mulligan_chosen.get(self.opp, []))
        opp_tossed = max(0, opp_total - opp_kept)

        return MulliganSnapshot(
            went_first=went_first,
            has_coin=not went_first,
            offered=offered_cards,
            kept_names=kept_names,
            tossed_names=tossed_names,
            final_opening_hand=self.final_opening_hand,
            opponent_kept_count=opp_kept,
            opponent_tossed_count=opp_tossed,
        )

    def run(self) -> GameReplay:
        for ev in self.events:
            s = ev.payload.strip()
            if ev.method == "GameState.DebugPrintGame":
                m = PLAYER_NAME_RE.match(s)
                if m:
                    pid_i, pname = int(m.group(1)), m.group(2).strip()
                    if pid_i in self.pid_to_eid:
                        self.name_to_eid[pname] = self.pid_to_eid[pid_i]
                continue

            if ev.method == "GameState.DebugPrintEntityChoices":
                self._handle_entity_choices_line(s)
                continue
            if ev.method == "GameState.DebugPrintEntitiesChosen":
                self._handle_entities_chosen_line(s)
                continue
            if ev.method == "GameState.SendChoices":
                self._handle_send_choices_line(s)
                continue
            if ev.method != "GameState.DebugPrintPower":
                continue

            self._handle_power_line(s)

        # Flush any unrecorded GENERAL choices before finalizing the turn
        for ch in self.choices_by_id.values():
            if ch.choice_id not in self.recorded_choice_ids and (ch.chosen_ids or ch.opt_ids):
                self._record_choice_if_ready(ch)

        # Finalize any open turn at end of log
        my_playstate = _str_tag(self._player_ent(self.me), "PLAYSTATE")
        opp_playstate = _str_tag(self._player_ent(self.opp), "PLAYSTATE")
        if my_playstate == "WON":
            result: Result = "win"
            your_tps: TerminalPlaystate = "WON"
            opp_tps: TerminalPlaystate = "LOST"
        elif my_playstate == "LOST":
            result = "loss"
            your_tps = "LOST"
            opp_tps = "WON"
        elif my_playstate == "TIED":
            result = "tie"
            your_tps = "TIED"
            opp_tps = "TIED"
        elif opp_playstate == "LOST":
            result = "win"
            your_tps = "WON"
            opp_tps = "LOST"
        elif opp_playstate == "WON":
            result = "loss"
            your_tps = "LOST"
            opp_tps = "WON"
        elif opp_playstate == "TIED":
            result = "tie"
            your_tps = "TIED"
            opp_tps = "TIED"
        else:
            raise ValueError("Game did not reach a terminal PLAYSTATE")

        self._finalize_turn(game_ended_win=(result == "win"))

        went_first = _int_tag(self._player_ent(self.me), "FIRST_PLAYER") == 1
        start_of_game: dict[Side, list[str]] = {"you": [], "opponent": []}
        for p, eid, cid in self.start_of_game_raw:
            resolved = cid or self.ents.get(eid, {}).get("card", "")
            if resolved:
                self.seen_card_ids.add(resolved)
                side = self._side_of(p)
                name = self.db.name(resolved)
                if name not in start_of_game[side]:
                    start_of_game[side].append(name)

        your_hero = self._build_hero_state(self.me)
        opp_hero = self._build_hero_state(self.opp)

        your_deck_observed = self._build_observed_deck(self.me)
        opp_deck_observed = self._build_observed_deck(self.opp)

        cards_seen = {
            cid: self.db.to_reference(cid)
            for cid in sorted(self.seen_card_ids)
            if cid
        }

        missed_turns = [t.turn_number for t in self.turns if t.pre_checks.missed_lethal]
        total_floated = sum(
            t.pre_checks.mana_floated for t in self.turns if t.active_player == "you"
        )
        your_burned = [
            c.name
            for t in self.turns
            for c in t.pre_checks.cards_burned
            if c.owner == "you"
        ]
        your_discarded = [
            c.name
            for t in self.turns
            for c in t.pre_checks.cards_discarded
            if c.owner == "you"
        ]
        unused_atk_turns = [
            t.turn_number for t in self.turns if t.pre_checks.unused_attacks
        ]
        unused_loc_turns = [
            t.turn_number for t in self.turns if t.pre_checks.unused_ready_locations
        ]

        metadata = MatchMetadata(
            game_id=self.prescan.game_id,
            started_at=self.prescan.started_at,
            ended_at=self.prescan.ended_at,
            duration_seconds=self.prescan.duration_seconds,
            build_number=self.prescan.build_number,
            format_type=self.prescan.format_type,
            game_type=self.prescan.game_type,
            scenario_id=self.prescan.scenario_id,
            is_standard_constructed=self.prescan.is_standard_constructed,
            your_class=your_hero.hero_class or "UNKNOWN",
            opponent_class=opp_hero.hero_class or "UNKNOWN",
            your_hero=your_hero.name,
            opponent_hero=opp_hero.name,
            went_first=went_first,
            has_coin=not went_first,
            result=result,
            your_terminal_playstate=your_tps,
            opponent_terminal_playstate=opp_tps,
            end_reason=self._resolve_end_reason(result),
            total_turns=self._cur_game_turn() or len(self.turns),
            your_turns_count=self.player_turn_counts["you"],
            start_of_game_effects=start_of_game,
        )

        return GameReplay(
            metadata=metadata,
            mulligan=self._build_mulligan_snapshot(went_first),
            turns=self.turns,
            your_deck_observed=your_deck_observed,
            opponent_deck_observed=opp_deck_observed,
            cards_seen=cards_seen,
            pre_check_summary=MatchPreCheckSummary(
                missed_lethal_turns=missed_turns,
                total_mana_floated=total_floated,
                your_cards_burned=your_burned,
                your_cards_discarded=your_discarded,
                turns_with_unused_attacks=unused_atk_turns,
                turns_with_unused_ready_locations=unused_loc_turns,
            ),
        )

    def _handle_entity_choices_line(self, s: str) -> None:
        hm = CHOICE_HDR_RE.match(s)
        if hm:
            cid = int(hm.group(1))
            player_tok = hm.group(2).strip()
            ctype = hm.group(3)
            peid = self._eid_of(player_tok)
            p_pid = next(
                (str(pid) for pid, eid in self.pid_to_eid.items() if eid == peid),
                self.me,
            )
            ch = _OpenChoice(
                choice_id=cid,
                player_pid=p_pid,
                choice_type=ctype,
                turn_opened=self._cur_game_turn(),
            )
            self._reading_choice_id = cid
            self._reading_choice_mode = "choices"
            if ctype == "MULLIGAN":
                self.mulligan_offers[p_pid] = ch
            else:
                self.choices_by_id[cid] = ch
            return
        if self._reading_choice_mode != "choices" or self._reading_choice_id is None:
            return
        ch = (
            self.choices_by_id.get(self._reading_choice_id)
            or next(
                (v for v in self.mulligan_offers.values() if v.choice_id == self._reading_choice_id),
                None,
            )
        )
        if ch is None:
            return
        sm = CHOICE_SOURCE_RE.match(s)
        if sm:
            src_ref = self._eid_of(sm.group(1))
            if isinstance(src_ref, int):
                ch.source_id = src_ref
                ch.source_cid = self.ents.get(src_ref, {}).get("card", "")
            return
        em = CHOICE_ENT_RE.match(s)
        if em:
            oid = int(em.group(1))
            ocid = em.group(2)
            ch.opt_ids.append(oid)
            ch.opt_cids.append(ocid)
            if ocid:
                self.seen_card_ids.add(ocid)
            return
        self._reading_choice_id = None
        self._reading_choice_mode = None

    def _handle_entities_chosen_line(self, s: str) -> None:
        hm = CHOSEN_HDR_RE.match(s)
        if hm:
            cid = int(hm.group(1))
            player_tok = hm.group(2).strip()
            count = int(hm.group(3))
            peid = self._eid_of(player_tok)
            p_pid = next(
                (str(pid) for pid, eid in self.pid_to_eid.items() if eid == peid),
                self.me,
            )
            self._reading_choice_id = cid
            self._reading_choice_mode = "chosen"
            if cid in self.choices_by_id:
                self.choices_by_id[cid].chosen_ids.clear()
                self.choices_by_id[cid].chosen_cids.clear()
                self.choices_by_id[cid].expected_count = count
            elif any(v.choice_id == cid for v in self.mulligan_offers.values()):
                self.mulligan_chosen[p_pid] = []
            if count == 0 and cid in self.choices_by_id:
                self._record_choice_if_ready(self.choices_by_id[cid])
            return
        if self._reading_choice_mode != "chosen" or self._reading_choice_id is None:
            return
        em = CHOICE_ENT_RE.match(s)
        if em:
            oid = int(em.group(1))
            ocid = em.group(2)
            p_pid = em.group(3)
            if ocid:
                self.seen_card_ids.add(ocid)
            if any(v.choice_id == self._reading_choice_id for v in self.mulligan_offers.values()):
                self.mulligan_chosen.setdefault(p_pid, []).append(oid)
            elif self._reading_choice_id in self.choices_by_id:
                ch = self.choices_by_id[self._reading_choice_id]
                ch.chosen_ids.append(oid)
                ch.chosen_cids.append(ocid)
                if len(ch.chosen_ids) >= ch.expected_count:
                    self._record_choice_if_ready(ch)
            return
        if self._reading_choice_id in self.choices_by_id:
            ch = self.choices_by_id[self._reading_choice_id]
            if ch.chosen_ids:
                self._record_choice_if_ready(ch)
        self._reading_choice_id = None
        self._reading_choice_mode = None

    def _handle_send_choices_line(self, s: str) -> None:
        hm = SEND_CHOICE_HDR_RE.match(s)
        if hm:
            self._reading_choice_id = int(hm.group(1))
            self._reading_choice_mode = "send"
            return
        if self._reading_choice_mode != "send" or self._reading_choice_id is None:
            return
        em = CHOICE_ENT_RE.match(s)
        if em:
            oid = int(em.group(1))
            ocid = em.group(2)
            p_pid = em.group(3)
            if any(v.choice_id == self._reading_choice_id for v in self.mulligan_offers.values()):
                self.mulligan_chosen.setdefault(p_pid, []).append(oid)
            elif self._reading_choice_id in self.choices_by_id:
                ch = self.choices_by_id[self._reading_choice_id]
                if oid not in ch.chosen_ids:
                    ch.chosen_ids.append(oid)
                    ch.chosen_cids.append(ocid)
            return
        self._reading_choice_id = None
        self._reading_choice_mode = None

    def _handle_power_line(self, s: str) -> None:
        if self._reading_burned_meta:
            im = META_INFO_RE.match(s)
            if im:
                eid = int(im.group(1) or im.group(4))
                ent = self._ent(eid)
                cid = im.group(2) or ent.get("card") or None
                if cid:
                    self.seen_card_ids.add(cid)
                ctrl = im.group(3) or ent["tags"].get("CONTROLLER")
                ev = CardLossEvent(
                    owner=self._side_of(ctrl),
                    reason="burned",
                    entity_id=eid,
                    card_id=cid,
                    name=self.db.name(cid) if cid else "Unknown Card",
                )
                if self.current_turn_work is not None:
                    self.current_turn_work.cards_burned.append(ev)
                else:
                    self.pending_burned.append(ev)
                return
            self._reading_burned_meta = False

        if META_BURNED_RE.match(s):
            self._reading_burned_meta = True
            self._cur_ent = None
            return

        m = PLAYER_EID_RE.match(s)
        if m:
            eid, pid = int(m.group(1)), int(m.group(2))
            self.pid_to_eid[pid] = eid
            self._ent(eid)
            return

        m = FULL_ENT_RE.match(s)
        if m:
            eid = int(m.group(1))
            cid = m.group(2)
            self._cur_ent = eid
            prev_tags = self.ents.get(eid, {}).get("tags", {})
            self.ents[eid] = {"card": cid, "tags": prev_tags}
            if cid:
                self.original_card_id.setdefault(eid, cid)
                self.seen_card_ids.add(cid)
            return

        m = SHOW_CHANGE_RE.match(s)
        if m:
            ref = self._eid_of(m.group(2))
            cid = m.group(3)
            if isinstance(ref, int):
                self._cur_ent = ref
                ent = self._ent(ref)
                if cid:
                    self.original_card_id.setdefault(ref, cid)
                    ent["card"] = cid
                    self.seen_card_ids.add(cid)
                if (
                    cid in CTHUN_ABIL
                    and not any(
                        x.get("card") == "BE_013"
                        and x["tags"].get("ZONE") in ("PLAY", "GRAVEYARD")
                        for x in self.ents.values()
                    )
                ):
                    self.cthun_trig.append((ref, CTHUN_ABIL[cid]))
            else:
                self._cur_ent = None
            return

        lm = TRIGGER_LASH_RE.match(s)
        if lm:
            self.lash_trig.append(lm.group(1))
            self._cur_ent = None
            self.block_stack.append(_ActiveBlock("TRIGGER", None, None, -1))
            return

        sog_m = TRIGGER_SOG_RE.match(s)
        if sog_m:
            self.start_of_game_raw.append(
                (sog_m.group(3), int(sog_m.group(1)), sog_m.group(2))
            )
            self._cur_ent = None
            self.block_stack.append(_ActiveBlock("TRIGGER", int(sog_m.group(1)), None, -1))
            return

        bm = BLOCK_START_RE.match(s)
        if bm:
            self._cur_ent = None
            bt = bm.group(1)
            src = self._eid_of(bm.group(2))
            tgt_raw = bm.group(3).strip()
            tgt = self._eid_of(tgt_raw) if tgt_raw != "0" else None
            subopt = int(bm.group(4))
            src_eid = src if isinstance(src, int) else None
            tgt_eid = tgt if isinstance(tgt, int) else None

            blk = _ActiveBlock(bt, src_eid, tgt_eid, subopt)
            if bt in ("PLAY", "ATTACK", "DECK_ACTION") and src_eid is not None:
                se = self._ent(src_eid)
                ctrl = se["tags"].get("CONTROLLER")
                side = self._side_of(ctrl)
                # Ensure turn snapshot exists even on unusual logs
                tw = self._open_turn_if_needed()
                seq = len(tw.actions) + 1
                scid = se.get("card") or None
                if scid:
                    self.seen_card_ids.add(scid)
                sname = self.db.name(scid) if scid else "Unknown"
                tcid = self._ent(tgt_eid).get("card") or None if tgt_eid is not None else None
                if tcid:
                    self.seen_card_ids.add(tcid)
                tname = self.db.name(tcid) if tcid else None
                tside = (
                    self._side_of(self._ent(tgt_eid)["tags"].get("CONTROLLER"))
                    if tgt_eid is not None
                    else None
                )
                mana_cost = _int_tag(se, "COST") if "COST" in se["tags"] else None

                self.pending_atk = None
                self.pending_abil = None
                self.pending_da = None

                if bt == "DECK_ACTION":
                    action_type: ActionType = "DECK_ACTION"
                    tw.actions.append(
                        TurnAction(
                            seq=seq,
                            actor=side,
                            action_type=action_type,
                            source_entity_id=src_eid,
                            source_card_id=scid,
                            source_name=sname,
                            mana_cost=None,
                            summary=self._format_action_summary(
                                action_type,
                                sname,
                                None,
                                None,
                                actor=side,
                                active_player=tw.active_player,
                            ),
                        )
                    )
                    blk.action_idx = len(tw.actions) - 1
                    self._action_meta[(tw.turn_number, blk.action_idx)] = (None, False)
                    self.pending_da = (tw, blk.action_idx, src_eid)
                elif (
                    bt == "PLAY"
                    and subopt >= 0
                    and se["tags"].get("ZONE") == "PLAY"
                    and se["tags"].get("CARDTYPE") == "MINION"
                ):
                    abils = [
                        aid
                        for aid, ae in sorted(self.ents.items())
                        if scid
                        and ae.get("card", "").startswith(scid + "t")
                        and ae["tags"].get("CARDTYPE") == "SPELL"
                        and ae["tags"].get("CONTROLLER") == ctrl
                        and ae["tags"].get("ZONE") == "SETASIDE"
                    ]
                    guess_eid = abils[subopt] if 0 <= subopt < len(abils) else src_eid
                    g_cid = self._ent(guess_eid).get("card") or scid
                    if g_cid:
                        self.seen_card_ids.add(g_cid)
                    g_name = self.db.name(g_cid) if g_cid else sname
                    tw.actions.append(
                        TurnAction(
                            seq=seq,
                            actor=side,
                            action_type="ABILITY",
                            source_entity_id=guess_eid,
                            source_card_id=g_cid,
                            source_name=g_name,
                            target_entity_id=tgt_eid,
                            target_card_id=tcid,
                            target_name=tname,
                            target_side=tside,
                            mana_cost=None,
                            summary=self._format_action_summary(
                                "ABILITY",
                                g_name,
                                tname,
                                tside,
                                actor=side,
                                active_player=tw.active_player,
                            ),
                        )
                    )
                    blk.action_idx = len(tw.actions) - 1
                    self._action_meta[(tw.turn_number, blk.action_idx)] = (None, False)
                    if scid:
                        self.pending_abil = (tw, blk.action_idx, scid)
                else:
                    cause: str | None = None
                    if bt == "PLAY":
                        if se["tags"].get("CARDTYPE") == "HERO_POWER":
                            action_type = "HERO_POWER"
                        elif (
                            se["tags"].get("CARDTYPE") == "LOCATION"
                            and se["tags"].get("ZONE") == "PLAY"
                        ):
                            action_type = "ACTIVATE"
                        else:
                            action_type = "PLAY"
                        if ctrl == self.me and action_type == "PLAY":
                            self.plays_this_turn += 1
                            blk.play_card_type = se["tags"].get("CARDTYPE")
                            blk.play_had_my_minion = bool(
                                self._zone(self.me, "PLAY", ("MINION",))
                            )
                            blk.play_opp_lt7 = (
                                len(self._zone(self.opp, "PLAY", ("MINION", "LOCATION"))) < 7
                            )
                    else:
                        action_type = "ATTACK"
                        if any(b.block_type == "DEATHS" for b in self.block_stack):
                            cause = "Deathrattle"
                        elif any(b.block_type == "TRIGGER" for b in self.block_stack):
                            cause = "Triggered"
                        if ctrl == self.me:
                            blk.atk_card_type = se["tags"].get("CARDTYPE")
                            blk.atk_target_type = (
                                self._ent(tgt_eid)["tags"].get("CARDTYPE")
                                if tgt_eid is not None
                                else None
                            )
                    tw.actions.append(
                        TurnAction(
                            seq=seq,
                            actor=side,
                            action_type=action_type,
                            source_entity_id=src_eid,
                            source_card_id=scid,
                            source_name=sname,
                            target_entity_id=tgt_eid,
                            target_card_id=tcid,
                            target_name=tname,
                            target_side=tside,
                            mana_cost=(
                                mana_cost
                                if action_type in ("PLAY", "HERO_POWER")
                                else None
                            ),
                            summary=self._format_action_summary(
                                action_type,
                                sname,
                                tname,
                                tside,
                                actor=side,
                                active_player=tw.active_player,
                                cause=cause,
                            ),
                        )
                    )
                    blk.action_idx = len(tw.actions) - 1
                    self._action_meta[(tw.turn_number, blk.action_idx)] = (cause, False)
                    if action_type == "ATTACK" and tgt_eid is None:
                        self.pending_atk = (tw, blk.action_idx)
            elif (
                bt == "TRIGGER"
                and src_eid is not None
                and self.current_turn_work is not None
                and self.game_tags.get("STEP") not in (None, "", "BEGIN_MULLIGAN")
            ):
                se = self._ent(src_eid)
                scid = se.get("card") or ""
                if scid:
                    ctype = (
                        se["tags"].get("CARDTYPE")
                        or str(self.db.get(scid).get("type", ""))
                    ).upper()
                    sname = self.db.name(scid)
                    step = self.game_tags.get("STEP", "")
                    is_relevant_trigger = (
                        ctype in ("SPELL", "WEAPON")
                        or (
                            ctype == "ENCHANTMENT"
                            and (
                                "aura" in sname.lower()
                                or "TriggerKeyword=END_OF_TURN" in s
                                or "TriggerKeyword=START_OF_TURN" in s
                                or step == "MAIN_END"
                            )
                        )
                    )
                    already_playing_same = any(
                        b.block_type == "PLAY" and b.source_eid == src_eid
                        for b in self.block_stack
                    )
                    trig_key = (src_eid, scid)
                    enclosing_dup = any(
                        b.trigger_key == trig_key for b in self.block_stack
                    )
                    tw = self.current_turn_work
                    last_act = tw.actions[-1] if tw.actions else None
                    consecutive_dup = (
                        last_act is not None
                        and last_act.action_type == "TRIGGER"
                        and last_act.source_entity_id == src_eid
                        and last_act.source_card_id == scid
                    )
                    if (
                        is_relevant_trigger
                        and ctype not in ("", "GAME", "PLAYER", "HERO", "HERO_POWER")
                        and not is_internal_card_reference(self.db.to_reference(scid))
                        and not already_playing_same
                        and not enclosing_dup
                        and not consecutive_dup
                    ):
                        self.seen_card_ids.add(scid)
                        ctrl = se["tags"].get("CONTROLLER")
                        side = self._side_of(ctrl)
                        tcid = (
                            self._ent(tgt_eid).get("card") or None
                            if tgt_eid is not None
                            else None
                        )
                        if tcid:
                            self.seen_card_ids.add(tcid)
                        tname = self.db.name(tcid) if tcid else None
                        tside = (
                            self._side_of(self._ent(tgt_eid)["tags"].get("CONTROLLER"))
                            if tgt_eid is not None
                            else None
                        )
                        trig_cause = "Trigger" if side != tw.active_player else None
                        is_spell_trig = ctype == "SPELL"
                        tw.actions.append(
                            TurnAction(
                                seq=len(tw.actions) + 1,
                                actor=side,
                                action_type="TRIGGER",
                                source_entity_id=src_eid,
                                source_card_id=scid,
                                source_name=sname,
                                target_entity_id=tgt_eid,
                                target_card_id=tcid,
                                target_name=tname,
                                target_side=tside,
                                mana_cost=None,
                                summary=self._format_action_summary(
                                    "TRIGGER",
                                    sname,
                                    tname,
                                    tside,
                                    actor=side,
                                    active_player=tw.active_player,
                                    cause=trig_cause,
                                    is_spell_trigger=is_spell_trig,
                                ),
                            )
                        )
                        blk.action_idx = len(tw.actions) - 1
                        blk.trigger_key = trig_key
                        self._action_meta[(tw.turn_number, blk.action_idx)] = (
                            trig_cause,
                            is_spell_trig,
                        )

            self.block_stack.append(blk)
            return

        if s == "BLOCK_END":
            self._cur_ent = None
            if not self.block_stack:
                return
            blk = self.block_stack.pop()
            if blk.action_idx is not None and self.current_turn_work is not None:
                self._refresh_action(self.current_turn_work, blk.action_idx)

            # Secret ruling-out on your PLAY / ATTACK blocks
            if blk.block_type == "PLAY" and blk.play_card_type is not None:
                if blk.play_card_type == "SPELL":
                    for sid in self.active_opp_secrets:
                        r = self.secret_ruled_actions.setdefault(sid, set())
                        r.add("Counterspell")
                        if blk.play_had_my_minion and "Counterspell" not in blk.triggered_secrets:
                            r.add("Pressure Plate")
                elif blk.play_card_type == "MINION":
                    for sid in self.active_opp_secrets:
                        self.secret_ruled_actions.setdefault(sid, set()).add("Explosive Runes")
                if (
                    self.plays_this_turn >= 3
                    and blk.play_opp_lt7
                    and "Counterspell" not in blk.triggered_secrets
                ):
                    for sid in self.active_opp_secrets:
                        self.secret_ruled_actions.setdefault(sid, set()).add("Rat Trap")
            elif blk.block_type == "ATTACK" and blk.atk_card_type is not None:
                if blk.atk_card_type == "MINION":
                    for sid in self.active_opp_secrets:
                        self.secret_ruled_actions.setdefault(sid, set()).update(
                            ("Mystic Misdirection", "Freezing Trap")
                        )
                if not (blk.triggered_secrets & {"Mystic Misdirection", "Freezing Trap"}):
                    if blk.atk_target_type == "HERO":
                        for sid in self.active_opp_secrets:
                            self.secret_ruled_actions.setdefault(sid, set()).update(
                                ("Ice Barrier", "Explosive Trap")
                            )
                    elif blk.atk_target_type == "MINION":
                        for sid in self.active_opp_secrets:
                            self.secret_ruled_actions.setdefault(sid, set()).add("Oasis Ally")

            # Mid-turn peak lethal re-evaluation after your actions resolve
            if (
                blk.block_type in ("PLAY", "ATTACK", "DECK_ACTION")
                and self.current_turn_work is not None
                and self.current_turn_work.active_player == "you"
                and self.game_tags.get("STEP") == "MAIN_ACTION"
                and not self._game_is_over()
            ):
                dm = self._compute_damage_math("mid_turn_peak")
                prev = self.current_turn_work.peak_damage_math
                if prev is None or self._is_better_damage_math(dm, prev):
                    self.current_turn_work.peak_damage_math = dm
            return

        hm = HIDE_ENT_RE.match(s)
        if hm:
            self._cur_ent = None
            ref = self._eid_of(hm.group(1))
            if isinstance(ref, int):
                self._set_entity_tag(ref, hm.group(2), hm.group(3))
            return

        tm = TAG_CHANGE_RE.match(s)
        if tm:
            self._cur_ent = None
            ref = self._eid_of(tm.group(1))
            tag, val = tm.group(2), tm.group(3)
            if ref == "game":
                self._handle_game_tag_change(tag, val)
            elif isinstance(ref, int):
                self._set_entity_tag(ref, tag, val)
                if tag == "PLAYSTATE" and val in ("CONCEDED", "DISCONNECTED"):
                    self.end_reason_by_eid[ref] = val.lower()
                if self.pending_atk is not None and tag == "DEFENDING" and val == "1":
                    tw, idx = self.pending_atk
                    tw.actions[idx].target_entity_id = ref
                    self._refresh_action(tw, idx)
                    self.pending_atk = None
                if (
                    self.pending_abil is not None
                    and tag == "LITERALLY_UNPLAYABLE"
                    and val == "1"
                ):
                    tw, idx, god_cid = self.pending_abil
                    if (
                        self._ent(ref)["tags"].get("ZONE") == "SETASIDE"
                        and self._ent(ref).get("card", "").startswith(god_cid + "t")
                    ):
                        tw.actions[idx].source_entity_id = ref
                        self._refresh_action(tw, idx)
                        self.pending_abil = None
                if self.pending_da is not None and ref == self.pending_da[2]:
                    tw, idx, _ = self.pending_da
                    if tag == "IS_USING_TRADE_OPTION" and val == "1":
                        tw.actions[idx].action_type = "TRADE"
                        self._refresh_action(tw, idx)
                    elif tag == "IS_USING_PREPARE_OPTION" and val == "1":
                        tw.actions[idx].action_type = "PREPARE"
                        self._refresh_action(tw, idx)
                if tag == "CURRENT_PLAYER" and val == "0":
                    # Check Flames of Infinity rule-out if our turn just ended
                    me_peid = self.pid_to_eid.get(int(self.me)) if self.me.isdigit() else None
                    if ref == me_peid and self._zone(self.me, "PLAY", ("MINION",)):
                        for sid in self.active_opp_secrets:
                            self.secret_ruled_actions.setdefault(sid, set()).add(
                                "Flames of Infinity"
                            )
            return

        tvm = TAG_VAL_RE.match(s)
        if tvm and self._cur_ent is not None:
            tag, val = tvm.group(1), tvm.group(2)
            if self._cur_ent == "game":
                self.game_tags[tag] = val
            elif isinstance(self._cur_ent, int):
                self._set_entity_tag(self._cur_ent, tag, val)
            return

        if GAME_ENT_RE.match(s):
            self._cur_ent = "game"
            return

        if s.startswith(("META_DATA", "SUB_SPELL", "Info[", "Source=", "Targets", "CREATE_GAME")):
            if not s.startswith("CREATE_GAME"):
                self._cur_ent = None

    def _handle_game_tag_change(self, tag: str, val: str) -> None:
        self.game_tags[tag] = val
        if tag == "TURN":
            self.plays_this_turn = 0
        elif tag == "NUM_TURNS_IN_PLAY" and val == "1" and "TURN" not in self.game_tags:
            self.game_tags["TURN"] = "1"
        elif tag == "PROPOSED_DEFENDER" and self.pending_atk is not None:
            if val.isdigit() and int(val) > 0:
                tw, idx = self.pending_atk
                tw.actions[idx].target_entity_id = int(val)
                self._refresh_action(tw, idx)
                for blk in self.block_stack:
                    if blk.block_type == "ATTACK" and blk.action_idx == idx:
                        blk.atk_target_type = self._ent(int(val))["tags"].get("CARDTYPE")
        elif tag == "STEP":
            if val == "MAIN_READY":
                if not self.opening_hand_captured and self._cur_game_turn() <= 1:
                    self.final_opening_hand = self._build_hand_snapshot(self.me)
                    self.opening_hand_captured = True
            elif val == "MAIN_ACTION":
                self._open_turn_if_needed()
            elif val == "MAIN_END":
                if self.current_turn_work is not None and not self._game_is_over():
                    self._capture_eot_checks(self.current_turn_work)
            elif val in ("MAIN_CLEANUP", "MAIN_NEXT"):
                if self.current_turn_work is not None:
                    self._finalize_turn(game_ended_win=False)

    def _game_is_over(self) -> bool:
        return any(
            _str_tag(self._player_ent(c), "PLAYSTATE") in ("WON", "LOST", "TIED")
            for c in (self.me, self.opp)
        )

    @staticmethod
    def _is_better_damage_math(cand: DamageMathSnapshot, prev: DamageMathSnapshot) -> bool:
        if cand.lethal_available and not prev.lethal_available:
            return True
        if cand.lethal_available == prev.lethal_available:
            if cand.confidence == "certain" and prev.confidence != "certain":
                return True
            return cand.max_face_damage > prev.max_face_damage
        return False


def parse_power_log(
    log_path: str | Path,
    card_db: CardDB,
    *,
    standard_only: bool = True,
) -> ParseResult:
    """Stream a Power.log file and parse every completed matching game."""
    path = Path(log_path)
    result = ParseResult()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for chunk in iter_game_chunks(fh):
            result.total_games_found += 1
            prescan = _prescan_game(chunk)
            if not prescan.completed:
                result.skipped_incomplete += 1
                continue
            if standard_only and not prescan.is_standard_constructed:
                result.skipped_non_standard += 1
                continue
            try:
                replay = _GameSimulator(chunk, prescan, card_db).run()
                result.replays.append(replay)
            except Exception as exc:  # pragma: no cover - defensive guard
                result.warnings.append(f"Game {prescan.game_id} failed: {exc}")
    return result
