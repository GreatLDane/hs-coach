"""HearthstoneJSON cards.json loader, text cleaner, and ETag sync helper."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from hs_coach.schemas import (
    ActionType,
    BoardEntitySnapshot,
    BoardStateSnapshot,
    CardLossEvent,
    CardReference,
    ChoiceOption,
    ChoiceRecord,
    DamageMathSnapshot,
    DeckCard,
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
    SecretOrQuestState,
    Side,
    TurnAction,
    TurnPreChecks,
    TurnSnapshot,
    WeaponState,
)

HEARTHSTONEJSON_URL = "https://api.hearthstonejson.com/v1/latest/enUS/cards.json"

STANDARD_SECRET_SETS = frozenset(
    {
        "BE",
        "ESCAPEFROM_VIOLET_HOLD",
        "CATACLYSM",
        "EMERALD_DREAM",
        "THE_LOST_CITY",
        "TIME_TRAVEL",
        "CORE",
        "EVENT",
        "END",
        "CAP",
    }
)

RACE_NAMES: dict[str, str] = {
    "ALL": "All",
    "BEAST": "Beast",
    "DEMON": "Demon",
    "DRAENEI": "Draenei",
    "DRAGON": "Dragon",
    "ELEMENTAL": "Elemental",
    "MECHANICAL": "Mech",
    "MURLOC": "Murloc",
    "NAGA": "Naga",
    "PIRATE": "Pirate",
    "QUILBOAR": "Quilboar",
    "TOTEM": "Totem",
    "UNDEAD": "Undead",
}

HERALD_NAME: dict[str, str] = {
    "DEATHKNIGHT": "Onyxia",
    "DEMONHUNTER": "Azshara",
    "ROGUE": "Sinestra",
    "SHAMAN": "Al'Akir",
    "WARLOCK": "Cho'gall",
    "WARRIOR": "Ragnaros",
}

DEV_NOISE_RE = re.compile(r"\s*~GAMEPLAY\s+ASPROXY.*$")
PLACE_RE = re.compile(
    r"\b(adjacent|left|right|left-\s*most|right-\s*most|leftmost|rightmost|next to|neighbou?rs?|middle|either side)\b",
    re.I,
)
_INTERNAL_TEXT_RE = re.compile(
    r"(?:Executes nightmare bonus|This was played on the friendly side|For internal use|\bDEV ONLY\b|\bDO NOT RELEASE\b|\bPlaceholder\b|\bDummy\b)",
    re.I,
)
_INTERNAL_NAMES = frozenset({"Detected!", "Detected", "Dark Gift"})


def clean_hs_display_text(text: str, *, replace_placeholders: bool = True) -> str:
    """Clean &, numeric d-prefixes, and optional unresolved {0}/{1} placeholders without touching HTML."""
    if not text:
        return ""
    text = re.sub(r"([.:!?])\s*&\s*", r"\1 ", text)
    text = re.sub(r"(?<!\s)&|&(?!\s)", " ", text)
    text = re.sub(r"(?<![A-Za-z0-9])d(?=(?:\{\d+\}|\d+\b))", "", text)
    if replace_placeholders:
        text = re.sub(r"\{\d+\}", "X", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_card_text(
    raw: str, *, strip_alternate: bool = False, replace_placeholders: bool = True
) -> str:
    """Strip Hearthstone HTML/markup, [x], &, numeric d-prefixes, placeholders, and proxy noise."""
    if not raw:
        return ""
    text = raw.split("@", 1)[0] if strip_alternate else raw
    text = re.sub(r"<[^>]+>|\[x\]|[$#]", "", text)
    text = text.replace("\n", " ").replace("\u00a0", " ")
    text = DEV_NOISE_RE.sub("", text)
    return clean_hs_display_text(text, replace_placeholders=replace_placeholders)


class CardDB:
    """In-memory index over HearthstoneJSON card records."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_dbf_id: dict[int, str] = {}
        self.spell_school: dict[str, str] = {}
        self.races: dict[str, list[str]] = {}
        self._base_durability: dict[str, int] = {}
        self.secret_pool_by_class: dict[str, dict[str, str]] = {}
        self.all_secret_names: set[str] = set()

        for rec in records or []:
            cid = rec.get("id")
            if not cid:
                continue
            self.by_id[cid] = rec
            dbf_id = rec.get("dbfId")
            if isinstance(dbf_id, int):
                self.by_dbf_id[dbf_id] = cid
            if rec.get("spellSchool"):
                self.spell_school[cid] = str(rec["spellSchool"]).title()

            rlist = rec.get("races") or ([rec["race"]] if rec.get("race") else [])
            rdisp = [RACE_NAMES[r] for r in rlist if r in RACE_NAMES]
            if rdisp:
                self.races[cid] = rdisp

            if rec.get("type") == "WEAPON":
                dur = rec.get("durability") or rec.get("health") or 0
                if isinstance(dur, int):
                    self._base_durability[cid] = dur

            mechs = rec.get("mechanics") or []
            if "SECRET" in mechs:
                cname = rec.get("name", cid)
                self.all_secret_names.add(cname)
                if rec.get("collectible") and rec.get("set") in STANDARD_SECRET_SETS:
                    cls_name = rec.get("cardClass")
                    if cls_name:
                        trig = clean_card_text(rec.get("text", ""), strip_alternate=True)
                        trig = re.sub(r"^\s*Secret:\s*", "", trig)
                        self.secret_pool_by_class.setdefault(cls_name, {})[cname] = (
                            trig.split(".")[0][:90].strip()
                        )

    @classmethod
    def from_records(cls, records: list[dict[str, Any]]) -> CardDB:
        return cls(records)

    @classmethod
    def from_file(cls, path: str | Path) -> CardDB:
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh)
        if not isinstance(records, list):
            raise ValueError(f"Expected JSON array in cards file: {path}")
        return cls(records)

    def get(self, card_id: str | None) -> dict[str, Any]:
        if not card_id:
            return {}
        return self.by_id.get(card_id, {})

    def name(self, card_id: str | None) -> str:
        if not card_id:
            return "?"
        rec = self.by_id.get(card_id)
        return str(rec.get("name", card_id)) if rec else card_id

    def raw_text(self, card_id: str | None) -> str:
        if not card_id:
            return ""
        rec = self.by_id.get(card_id)
        return str(rec.get("text", "")) if rec else ""

    def clean_text(
        self,
        card_id: str | None,
        *,
        strip_alternate: bool = False,
        replace_placeholders: bool = True,
    ) -> str:
        return clean_card_text(
            self.raw_text(card_id),
            strip_alternate=strip_alternate,
            replace_placeholders=replace_placeholders,
        )

    def base_durability(self, card_id: str | None) -> int:
        if not card_id:
            return 0
        return self._base_durability.get(card_id, 0)

    def position_matters(self, card_id: str | None) -> bool:
        raw = self.raw_text(card_id).split("@", 1)[0]
        if not raw:
            return False
        stripped = re.sub(r"<[^>]+>|\[x\]|\((?:\d+|\{\d\})[^)]*\)", "", raw).replace("\n", " ")
        return bool(PLACE_RE.search(stripped))

    def is_old_god(self, card_id: str | None) -> bool:
        return "Old God" in self.raw_text(card_id)

    def is_shattered_half(self, card_id: str | None) -> bool:
        return "<b>Shattered</b>" in self.raw_text(card_id)

    def format_race(self, card_id: str | None, fallback_race: str | None = None) -> list[str]:
        if card_id and card_id in self.races:
            return list(self.races[card_id])
        if fallback_race and fallback_race in RACE_NAMES:
            return [RACE_NAMES[fallback_race]]
        return []

    def to_reference(self, card_id: str) -> CardReference:
        rec = self.by_id.get(card_id, {})
        health_val = rec.get("health")
        if health_val is None and rec.get("type") == "WEAPON":
            health_val = rec.get("durability")
        return CardReference(
            card_id=card_id,
            name=self.name(card_id),
            card_type=str(rec.get("type", "")),
            base_cost=rec.get("cost") if isinstance(rec.get("cost"), int) else None,
            base_attack=rec.get("attack") if isinstance(rec.get("attack"), int) else None,
            base_health=health_val if isinstance(health_val, int) else None,
            card_class=str(rec.get("cardClass", "")),
            spell_school=self.spell_school.get(card_id),
            races=self.format_race(card_id),
            text=self.clean_text(card_id, strip_alternate=True),
            position_matters=self.position_matters(card_id),
        )


def resolve_cards_path(explicit_path: str | Path | None = None) -> Path:
    """Resolve cards.json path from CLI arg, env var, or ./data/cards.json."""
    if explicit_path is not None:
        return Path(explicit_path)
    env_path = os.environ.get("HS_COACH_CARDS_JSON")
    if env_path:
        return Path(env_path)
    return Path("data") / "cards.json"


def sync_cards_json(
    dest_path: str | Path,
    *,
    force: bool = False,
    timeout: float = 60.0,
    url: str = HEARTHSTONEJSON_URL,
) -> dict[str, Any]:
    """Download or update cards.json from HearthstoneJSON with ETag caching."""
    dest = Path(dest_path)
    meta_path = dest.with_name(dest.name + ".meta.json")
    dest.parent.mkdir(parents=True, exist_ok=True)

    prev_meta: dict[str, Any] = {}
    if meta_path.exists() and not force:
        try:
            prev_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            prev_meta = {}

    headers: dict[str, str] = {"User-Agent": "hs-coach/0.1.0"}
    if dest.exists() and not force and prev_meta.get("etag"):
        headers["If-None-Match"] = str(prev_meta["etag"])

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url, headers=headers)

    if resp.status_code == 304 and dest.exists():
        count = prev_meta.get("card_count")
        if not isinstance(count, int):
            count = len(json.loads(dest.read_text(encoding="utf-8")))
        return {
            "updated": False,
            "path": str(dest),
            "card_count": count,
            "etag": prev_meta.get("etag"),
        }

    resp.raise_for_status()
    records = resp.json()
    if not isinstance(records, list):
        raise ValueError("HearthstoneJSON response did not return a JSON array")

    fd, tmp_name = tempfile.mkstemp(
        prefix=".cards_", suffix=".json.tmp", dir=str(dest.parent)
    )
    try:
        with os.fdopen(fd, "wb") as tmp_fh:
            tmp_fh.write(resp.content)
        os.replace(tmp_name, dest)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    etag = resp.headers.get("ETag")
    new_meta = {
        "url": url,
        "etag": etag,
        "last_modified": resp.headers.get("Last-Modified"),
        "card_count": len(records),
    }
    meta_path.write_text(json.dumps(new_meta, indent=2), encoding="utf-8")

    return {
        "updated": True,
        "path": str(dest),
        "card_count": len(records),
        "etag": etag,
    }


_CARD_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_DORMANT_KW_RE = re.compile(r"^Dormant \(wakes T\d+, now T\d+\)$")

_ALLOWED_FORMAT_TYPES = frozenset(
    {"FT_STANDARD", "FT_WILD", "FT_CLASSIC", "FT_TWIST", "FT_UNKNOWN"}
)
_ALLOWED_GAME_TYPES = frozenset(
    {
        "GT_RANKED",
        "GT_CASUAL",
        "GT_VS_FRIEND",
        "GT_VS_AI",
        "GT_ARENA",
        "GT_TAVERNBRAWL",
        "GT_UNKNOWN",
    }
)
_ALLOWED_CLASSES = frozenset(
    {
        "DEATHKNIGHT",
        "DEMONHUNTER",
        "DRUID",
        "HUNTER",
        "MAGE",
        "PALADIN",
        "PRIEST",
        "ROGUE",
        "SHAMAN",
        "WARLOCK",
        "WARRIOR",
        "NEUTRAL",
        "DREAM",
        "WHIZBANG",
        "UNKNOWN",
    }
)
_ALLOWED_CARD_TYPES = frozenset(
    {"MINION", "SPELL", "WEAPON", "HERO", "HERO_POWER", "LOCATION"}
)
_ALLOWED_HERO_FLAGS = frozenset({"Divine Shield", "Immune", "Stealth", "Frozen"})
_ALLOWED_KEYWORDS = frozenset(
    {
        "Taunt",
        "Divine Shield",
        "Stealth",
        "Frozen",
        "Windfury",
        "Lifesteal",
        "Poisonous",
        "Rush",
        "Charge",
        "Reborn",
        "Elusive",
        "Immune",
        "Rush (minions only)",
        "Rush (minions only this turn)",
        "Dormant",
    }
)
_SAFE_GENERIC_LABELS = frozenset(
    {"Hidden Card", "Hidden Secret", "Your Hero", "Opponent Hero", "Unknown Card"}
)
_CANONICAL_OLD_GOD_ABILITIES: dict[str, str] = {
    "BE_013t1": "Wrath (6 dmg split)",
    "BE_013t2": "Domain (+2/+2 x2)",
    "BE_013t3": "Glare (5 Lifesteal to minion)",
    "BE_013t6": "Glare (5 Lifesteal to minion)",
    "BE_013t4": "Servants (2x 2/2 Taunt)",
}


def _validate_card_id(card_id: str | None) -> str:
    cleaned = (card_id or "").strip()
    if not cleaned:
        return ""
    if not _CARD_ID_RE.match(cleaned):
        raise ValueError(f"Invalid card_id format: {card_id!r}")
    return cleaned


def _norm_enum(value: str | None, allowlist: frozenset[str], default: str) -> str:
    norm = (value or "").strip().upper()
    return norm if norm in allowlist else default


_SAFE_OUT_OF_TURN_PREFIX_RE = re.compile(
    r"^\[(Your|Opponent's)(?:\s+(Deathrattle|Triggered|Trigger))?\]\s+"
)


def format_action_summary(
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
    tgt_disp = tgt_name
    if tgt_disp and tgt_side and "Hero" not in tgt_disp and tgt_name:
        tgt_disp = f"{tgt_name} ({tgt_side})"

    prefix = ""
    if actor is not None and active_player is not None and actor != active_player:
        owner = "Your" if actor == "you" else "Opponent's"
        prefix = f"[{owner} {cause}] " if cause else f"[{owner}] "

    if action_type == "HERO_POWER":
        body = f"Hero Power: {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
    elif action_type == "ACTIVATE":
        body = f"Activates {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
    elif action_type == "TRADE":
        body = f"Trades {src_name}"
    elif action_type == "PREPARE":
        body = f"Prepares {src_name}"
    elif action_type == "DECK_ACTION":
        body = f"Deck action: {src_name}"
    elif action_type == "ABILITY":
        body = f"Uses {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
    elif action_type == "ATTACK":
        body = f"Attacks with {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
    elif action_type == "TRIGGER":
        if is_spell_trigger:
            body = f"Triggered cast: {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
        else:
            body = f"Triggers {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
    else:
        body = f"Plays {src_name}" + (f" -> {tgt_disp}" if tgt_disp else "")
    return f"{prefix}{body}"


def is_internal_card_reference(ref: CardReference) -> bool:
    """Return True for starting hero portraits, internal helper spells, and empty placeholders."""
    name = (ref.name or "").strip()
    ctype = (ref.card_type or "").strip().upper()
    cleaned_txt = clean_hs_display_text(ref.text or "")

    if ctype == "HERO" and (
        ref.card_id.startswith("HERO_")
        or ref.base_cost is None
        or not cleaned_txt
        or cleaned_txt == "-"
    ):
        return True

    if name in _INTERNAL_NAMES and (
        not cleaned_txt or bool(_INTERNAL_TEXT_RE.search(cleaned_txt)) or ref.base_cost is None
    ):
        return True

    if _INTERNAL_TEXT_RE.search(cleaned_txt):
        return True

    if not ctype and (not cleaned_txt or cleaned_txt == "-"):
        return True

    if ref.card_id.endswith("e") and ctype in ("", "ENCHANTMENT") and (not cleaned_txt or cleaned_txt == "-"):
        return True

    return False


def partition_glossary(
    cards_seen: dict[str, CardReference],
) -> tuple[list[CardReference], list[CardReference]]:
    """Filter internal/hero stubs, deduplicate duplicate token IDs, and separate enchantments."""
    playable: list[CardReference] = []
    enchantments: list[CardReference] = []
    seen_playable: set[tuple[str, str, int | None, str]] = set()
    seen_ench: set[tuple[str, str, int | None, str]] = set()

    ordered = sorted(
        cards_seen.values(),
        key=lambda r: (r.name, len(r.card_id), r.card_id),
    )
    for ref in ordered:
        if is_internal_card_reference(ref):
            continue
        norm_txt = clean_hs_display_text(ref.text or "")
        key = (
            ref.name.strip(),
            ref.card_type.strip().upper(),
            ref.base_cost,
            norm_txt,
        )
        if key[1] == "ENCHANTMENT":
            if key in seen_ench:
                continue
            seen_ench.add(key)
            enchantments.append(ref)
        else:
            if key in seen_playable:
                continue
            seen_playable.add(key)
            playable.append(ref)

    return playable, enchantments


class _ReplayCanonicalizer:
    """Rewrites a replay's descriptive fields strictly from trusted CardDB data."""

    def __init__(self, card_db: CardDB) -> None:
        self.db = card_db
        self.known_card_ids: set[str] = set()
        self.known_card_names: set[str] = {
            str(rec["name"])
            for rec in card_db.by_id.values()
            if rec.get("name")
        }

    def lookup(
        self, card_id: str | None, *, fallback_name: str = "Unknown Card"
    ) -> tuple[str, str, CardReference | None]:
        cid = _validate_card_id(card_id)
        if not cid or cid not in self.db.by_id:
            return cid, fallback_name, None
        self.known_card_ids.add(cid)
        ref = self.db.to_reference(cid)
        return cid, ref.name, ref

    def canonical_hero(self, hero: HeroState) -> HeroState:
        h_class = _norm_enum(hero.hero_class, _ALLOWED_CLASSES, "UNKNOWN")
        cid, name, ref = self.lookup(
            hero.card_id,
            fallback_name=f"{h_class.title()} Hero" if h_class != "UNKNOWN" else "Unknown Hero",
        )
        if ref is not None and ref.card_class in _ALLOWED_CLASSES and h_class == "UNKNOWN":
            h_class = ref.card_class

        weapon: WeaponState | None = None
        if hero.weapon is not None:
            w = hero.weapon
            w_cid, w_name, w_ref = self.lookup(w.card_id, fallback_name="Unknown Weapon")
            weapon = WeaponState(
                entity_id=w.entity_id,
                card_id=w_cid,
                name=w_name,
                attack=w.attack,
                durability=w.durability,
                effective_text=(w_ref.text or None) if w_ref is not None else None,
            )

        hero_power: HeroPowerState | None = None
        if hero.hero_power is not None:
            hp = hero.hero_power
            hp_cid, hp_name, hp_ref = self.lookup(hp.card_id, fallback_name="Hero Power")
            hero_power = HeroPowerState(
                card_id=hp_cid,
                name=hp_name,
                cost=hp.cost,
                used=hp.used,
                effective_text=hp_ref.text if hp_ref is not None else "",
            )

        return HeroState(
            hero_class=h_class,
            card_id=cid,
            name=name,
            hp=hero.hp,
            max_hp=hero.max_hp,
            armor=hero.armor,
            effective_hp=hero.effective_hp,
            attack=hero.attack,
            can_attack=hero.can_attack,
            flags=[f for f in hero.flags if f in _ALLOWED_HERO_FLAGS],
            weapon=weapon,
            hero_power=hero_power,
        )

    def canonical_hand_card(self, hc: HandCardSnapshot) -> HandCardSnapshot:
        cid, name, ref = self.lookup(hc.card_id)
        ctype = hc.card_type if hc.card_type in _ALLOWED_CARD_TYPES else ""
        return HandCardSnapshot(
            position=hc.position,
            entity_id=hc.entity_id,
            card_id=cid,
            name=name,
            card_type=(ref.card_type or ctype) if ref is not None else ctype,
            cost=hc.cost,
            base_cost=ref.base_cost if ref is not None else None,
            attack=hc.attack,
            health=hc.health,
            spell_school=ref.spell_school if ref is not None else None,
            races=list(ref.races) if ref is not None else [],
            outcast_active=hc.outcast_active,
            spell_damage_bonus=hc.spell_damage_bonus,
            enchantments=[],
            assembled_note=None,
            countdown=hc.countdown,
            prepare=hc.prepare,
            shattered_half=hc.shattered_half,
            effective_text=ref.text if ref is not None else "",
            position_matters=ref.position_matters if ref is not None else False,
        )

    def canonical_board_entity(self, ent: BoardEntitySnapshot) -> BoardEntitySnapshot:
        cid, name, ref = self.lookup(ent.card_id)
        keywords = [
            kw
            for kw in ent.keywords
            if kw in _ALLOWED_KEYWORDS or _DORMANT_KW_RE.match(kw)
        ]
        og_abils: list[OldGodAbilityState] = []
        for ab in ent.old_god_abilities:
            ab_cid, ab_name, ab_ref = self.lookup(ab.card_id, fallback_name="Ability")
            static_name = _CANONICAL_OLD_GOD_ABILITIES.get(ab_cid)
            if ab_ref is None and static_name is None:
                continue
            og_abils.append(
                OldGodAbilityState(
                    card_id=ab_cid,
                    name=static_name or ab_name,
                    text=ab_ref.text if ab_ref is not None else "",
                    used=ab.used,
                )
            )
        return BoardEntitySnapshot(
            position=ent.position,
            entity_id=ent.entity_id,
            card_id=cid,
            name=name,
            card_type=ent.card_type,
            attack=ent.attack,
            health=ent.health,
            max_health=ent.max_health,
            damage=ent.damage,
            races=list(ref.races) if ref is not None else [],
            keywords=keywords,
            spell_damage=ent.spell_damage,
            dormant=ent.dormant,
            dormant_wakes_turn=ent.dormant_wakes_turn,
            dormant_wakes_exhausted=ent.dormant_wakes_exhausted,
            location_ready=ent.location_ready,
            can_attack=ent.can_attack,
            attacks_remaining=ent.attacks_remaining,
            old_god_abilities=og_abils,
            assembled_note=None,
            dark_gift=None,
            effective_text=(ref.text or None) if ref is not None else None,
            position_matters=ref.position_matters if ref is not None else False,
        )

    def canonical_secret_or_quest(self, sq: SecretOrQuestState) -> SecretOrQuestState:
        cid, name, ref = self.lookup(sq.card_id, fallback_name="")
        return SecretOrQuestState(
            entity_id=sq.entity_id,
            kind=sq.kind,
            owner=sq.owner,
            card_class=_norm_enum(sq.card_class, _ALLOWED_CLASSES, ""),
            revealed=sq.revealed,
            card_id=cid or None,
            name=(name if ref is not None else None) if sq.revealed else None,
            possible_secrets=[
                s for s in sq.possible_secrets if s in self.db.all_secret_names
            ],
            ruled_out_secrets=[
                s for s in sq.ruled_out_secrets if s in self.db.all_secret_names
            ],
            quest_progress=sq.quest_progress,
            quest_total=sq.quest_total,
            quest_reward=None,
        )

    def canonical_board_state(self, state: BoardStateSnapshot) -> BoardStateSnapshot:
        opp_slots: list[OpponentHandSlot] = []
        for slot in state.opponent_hand_slots:
            rev_cid, rev_name, rev_ref = self.lookup(slot.revealed_card_id, fallback_name="")
            opp_slots.append(
                OpponentHandSlot(
                    position=slot.position,
                    turn_entered=slot.turn_entered,
                    created_by=None,
                    is_minion_known=slot.is_minion_known,
                    is_buffed=slot.is_buffed,
                    revealed_card_id=rev_cid or None,
                    revealed_name=(rev_name if rev_ref is not None else ("Unknown Card" if rev_cid else None)),
                )
            )
        return BoardStateSnapshot(
            your_mana_available=state.your_mana_available,
            your_mana_max=state.your_mana_max,
            your_overload_locked=state.your_overload_locked,
            opponent_mana_available=state.opponent_mana_available,
            opponent_mana_max=state.opponent_mana_max,
            your_hero=self.canonical_hero(state.your_hero),
            opponent_hero=self.canonical_hero(state.opponent_hero),
            your_board=[self.canonical_board_entity(e) for e in state.your_board],
            opponent_board=[self.canonical_board_entity(e) for e in state.opponent_board],
            your_hand=[self.canonical_hand_card(hc) for hc in state.your_hand],
            opponent_hand_size=state.opponent_hand_size,
            opponent_hand_slots=opp_slots,
            your_deck_size=state.your_deck_size,
            opponent_deck_size=state.opponent_deck_size,
            your_secrets_and_quests=[
                self.canonical_secret_or_quest(sq) for sq in state.your_secrets_and_quests
            ],
            opponent_secrets_and_quests=[
                self.canonical_secret_or_quest(sq) for sq in state.opponent_secrets_and_quests
            ],
            opponent_triggered_secrets=[
                s for s in state.opponent_triggered_secrets if s in self.db.all_secret_names
            ],
            special_notes=[],
        )

    def canonical_deck(self, cards: list[DeckCard]) -> list[DeckCard]:
        out: list[DeckCard] = []
        for dc in cards:
            cid, name, ref = self.lookup(dc.card_id)
            ctype = dc.card_type if dc.card_type in _ALLOWED_CARD_TYPES else ""
            out.append(
                DeckCard(
                    card_id=cid,
                    name=name,
                    cost=ref.base_cost if ref is not None else None,
                    count=dc.count,
                    card_type=(ref.card_type or ctype) if ref is not None else ctype,
                )
            )
        return out

    def canonical_mulligan(self, mull: MulliganSnapshot) -> MulliganSnapshot:
        offered: list[MulliganCard] = []
        kept: list[str] = []
        tossed: list[str] = []
        for mc in mull.offered:
            cid, name, ref = self.lookup(mc.card_id)
            offered.append(
                MulliganCard(
                    entity_id=mc.entity_id,
                    card_id=cid,
                    name=name,
                    cost=ref.base_cost if ref is not None else None,
                    text=ref.text if ref is not None else "",
                    kept=mc.kept,
                )
            )
            if mc.kept:
                kept.append(name)
            else:
                tossed.append(name)
        return MulliganSnapshot(
            went_first=mull.went_first,
            has_coin=mull.has_coin,
            offered=offered,
            kept_names=kept,
            tossed_names=tossed,
            final_opening_hand=[
                self.canonical_hand_card(hc) for hc in mull.final_opening_hand
            ],
            opponent_kept_count=mull.opponent_kept_count,
            opponent_tossed_count=mull.opponent_tossed_count,
        )

    def canonical_turn(self, turn: TurnSnapshot) -> TurnSnapshot:
        start_state = self.canonical_board_state(turn.start_state)
        end_state = self.canonical_board_state(turn.end_state)

        canon_actions: list[TurnAction] = []
        for act in turn.actions:
            s_cid, s_name, s_ref = self.lookup(act.source_card_id, fallback_name="")
            if s_cid:
                resolved_src = s_name if s_ref is not None else "Unknown Card"
            elif act.source_name in _SAFE_GENERIC_LABELS:
                resolved_src = act.source_name
            else:
                resolved_src = "Hidden Card"

            resolved_tgt: str | None = None
            t_cid = ""
            if act.target_card_id:
                t_cid, t_name, t_ref = self.lookup(act.target_card_id)
                resolved_tgt = t_name if t_ref is not None else "Unknown Card"
            elif act.target_name in _SAFE_GENERIC_LABELS:
                resolved_tgt = act.target_name

            safe_cause: str | None = None
            pm = _SAFE_OUT_OF_TURN_PREFIX_RE.match(act.summary or "")
            if pm and pm.group(2):
                safe_cause = pm.group(2)
            body_part = act.summary[pm.end():] if pm else (act.summary or "")
            is_spell_trigger = act.action_type == "TRIGGER" and body_part.startswith("Triggered cast:")

            canon_actions.append(
                TurnAction(
                    seq=act.seq,
                    actor=act.actor,
                    action_type=act.action_type,
                    source_entity_id=act.source_entity_id,
                    source_card_id=s_cid or None,
                    source_name=resolved_src,
                    target_entity_id=act.target_entity_id,
                    target_card_id=t_cid or None,
                    target_name=resolved_tgt,
                    target_side=act.target_side,
                    mana_cost=(
                        act.mana_cost
                        if act.action_type in ("PLAY", "HERO_POWER")
                        else None
                    ),
                    summary=format_action_summary(
                        act.action_type,
                        resolved_src,
                        resolved_tgt,
                        act.target_side,
                        actor=act.actor,
                        active_player=turn.active_player,
                        cause=safe_cause,
                        is_spell_trigger=is_spell_trigger,
                    ),
                )
            )

        canon_choices: list[ChoiceRecord] = []
        for ch in turn.choices:
            src_cid, src_name, src_ref = self.lookup(ch.source_card_id, fallback_name="")
            opts: list[ChoiceOption] = []
            for opt in ch.options:
                ocid, oname, oref = self.lookup(opt.card_id)
                opts.append(
                    ChoiceOption(
                        entity_id=opt.entity_id,
                        card_id=ocid,
                        name=oname,
                        cost=oref.base_cost if oref is not None else None,
                        text=oref.text if oref is not None else "",
                        dark_gift=None,
                    )
                )
            chosen_ids: list[str] = []
            chosen_names: list[str] = []
            for ch_cid in ch.chosen_card_ids:
                ccid, cname, cref = self.lookup(ch_cid)
                if ccid:
                    chosen_ids.append(ccid)
                    chosen_names.append(cname if cref is not None else "Unknown Card")
            canon_choices.append(
                ChoiceRecord(
                    choice_id=ch.choice_id,
                    actor=ch.actor,
                    kind=ch.kind,
                    source_card_id=src_cid or None,
                    source_name=(
                        src_name if src_ref is not None else ("Unknown Card" if src_cid else None)
                    ),
                    options_visible=ch.options_visible,
                    options=opts,
                    chosen_card_ids=chosen_ids,
                    chosen_names=chosen_names,
                    assemble_middle_card=None,
                )
            )

        pc = turn.pre_checks

        def _clean_dm(dm: DamageMathSnapshot | None) -> DamageMathSnapshot | None:
            if dm is None:
                return None
            return DamageMathSnapshot(
                window=dm.window,
                mana_available=dm.mana_available,
                enemy_effective_hp=dm.enemy_effective_hp,
                enemy_taunt_blocking=dm.enemy_taunt_blocking,
                enemy_hero_divine_shield=dm.enemy_hero_divine_shield,
                enemy_hero_immune=dm.enemy_hero_immune,
                enemy_has_active_secret=dm.enemy_has_active_secret,
                max_face_damage=dm.max_face_damage,
                sources=[],
                unmodelled_notes=[],
                lethal_available=dm.lethal_available,
                confidence=dm.confidence,
            )

        start_dm = _clean_dm(pc.start_damage_math)
        peak_dm = _clean_dm(pc.peak_damage_math)

        missed_detail: str | None = None
        if pc.missed_lethal:
            lethal_dm = (
                peak_dm
                if (peak_dm and peak_dm.lethal_available)
                else (start_dm if (start_dm and start_dm.lethal_available) else None)
            )
            if lethal_dm is not None:
                missed_detail = (
                    f"{lethal_dm.window}: {lethal_dm.max_face_damage} max dmg "
                    f"vs {lethal_dm.enemy_effective_hp} eff HP [{lethal_dm.confidence}]"
                )
            else:
                missed_detail = "missed lethal flagged"

        allowed_atk_labels: set[str] = {
            f"Hero ({int(end_state.your_hero.attack or 0)} atk)",
            "Hero",
        }
        allowed_loc_names: set[str] = set()
        for ent in end_state.your_board:
            if ent.card_type == "MINION":
                allowed_atk_labels.add(ent.name)
                allowed_atk_labels.add(f"{ent.name} ({int(ent.attack or 0)} atk)")
            elif ent.card_type == "LOCATION":
                allowed_loc_names.add(ent.name)

        def _clean_losses(items: list[CardLossEvent]) -> list[CardLossEvent]:
            out_losses: list[CardLossEvent] = []
            for ev in items:
                lcid, lname, lref = self.lookup(ev.card_id, fallback_name="Unknown Card")
                out_losses.append(
                    CardLossEvent(
                        owner=ev.owner,
                        reason=ev.reason,
                        entity_id=ev.entity_id,
                        card_id=lcid or None,
                        name=lname if lref is not None else "Unknown Card",
                    )
                )
            return out_losses

        return TurnSnapshot(
            turn_number=turn.turn_number,
            player_turn_number=turn.player_turn_number,
            active_player=turn.active_player,
            start_state=start_state,
            actions=canon_actions,
            choices=canon_choices,
            end_state=end_state,
            pre_checks=TurnPreChecks(
                start_damage_math=start_dm,
                peak_damage_math=peak_dm,
                missed_lethal=pc.missed_lethal,
                missed_lethal_detail=missed_detail,
                mana_floated=pc.mana_floated,
                prepare_was_available=pc.prepare_was_available,
                unused_attacks=[u for u in pc.unused_attacks if u in allowed_atk_labels],
                unused_ready_locations=[
                    loc for loc in pc.unused_ready_locations if loc in allowed_loc_names
                ],
                unused_hero_power=pc.unused_hero_power,
                cards_burned=_clean_losses(pc.cards_burned),
                cards_discarded=_clean_losses(pc.cards_discarded),
            ),
        )


def canonicalize_replay(replay: GameReplay, card_db: CardDB) -> GameReplay:
    """Canonicalize replay card fields from CardDB and strip untrusted free text."""
    canon = _ReplayCanonicalizer(card_db)

    for raw_cid, ref in replay.cards_seen.items():
        _validate_card_id(raw_cid)
        ref_cid = _validate_card_id(ref.card_id)
        if ref_cid and ref_cid in card_db.by_id:
            canon.known_card_ids.add(ref_cid)

    turns = [canon.canonical_turn(t) for t in replay.turns]
    mulligan = canon.canonical_mulligan(replay.mulligan)
    your_deck = canon.canonical_deck(replay.your_deck_observed)
    opp_deck = canon.canonical_deck(replay.opponent_deck_observed)

    m = replay.metadata
    your_class = _norm_enum(m.your_class, _ALLOWED_CLASSES, "UNKNOWN")
    opp_class = _norm_enum(m.opponent_class, _ALLOWED_CLASSES, "UNKNOWN")

    your_hero_name = (
        f"{your_class.title()} Hero" if your_class != "UNKNOWN" else "Unknown Hero"
    )
    opp_hero_name = (
        f"{opp_class.title()} Hero" if opp_class != "UNKNOWN" else "Unknown Hero"
    )
    for t in turns:
        if t.start_state.your_hero.card_id in card_db.by_id:
            your_hero_name = t.start_state.your_hero.name
            break
    for t in turns:
        if t.start_state.opponent_hero.card_id in card_db.by_id:
            opp_hero_name = t.start_state.opponent_hero.name
            break

    sog_effects: dict[Side, list[str]] = {
        "you": [
            n
            for n in m.start_of_game_effects.get("you", [])
            if n in canon.known_card_names
        ],
        "opponent": [
            n
            for n in m.start_of_game_effects.get("opponent", [])
            if n in canon.known_card_names
        ],
    }

    metadata = MatchMetadata(
        game_id=m.game_id,
        started_at=m.started_at,
        ended_at=m.ended_at,
        duration_seconds=m.duration_seconds,
        build_number=m.build_number,
        format_type=_norm_enum(m.format_type, _ALLOWED_FORMAT_TYPES, "FT_UNKNOWN"),
        game_type=_norm_enum(m.game_type, _ALLOWED_GAME_TYPES, "GT_UNKNOWN"),
        scenario_id=m.scenario_id,
        is_standard_constructed=m.is_standard_constructed,
        your_class=your_class,
        opponent_class=opp_class,
        your_hero=your_hero_name,
        opponent_hero=opp_hero_name,
        went_first=m.went_first,
        has_coin=m.has_coin,
        result=m.result,
        your_terminal_playstate=m.your_terminal_playstate,
        opponent_terminal_playstate=m.opponent_terminal_playstate,
        end_reason=m.end_reason,
        total_turns=m.total_turns,
        your_turns_count=m.your_turns_count,
        start_of_game_effects=sog_effects,
    )

    your_burned = [
        ev.name
        for t in turns
        for ev in t.pre_checks.cards_burned
        if ev.owner == "you"
    ]
    your_discarded = [
        ev.name
        for t in turns
        for ev in t.pre_checks.cards_discarded
        if ev.owner == "you"
    ]
    missed_turns = (
        [t.turn_number for t in turns if t.pre_checks.missed_lethal]
        if turns
        else list(replay.pre_check_summary.missed_lethal_turns)
    )
    unused_atk_turns = (
        [t.turn_number for t in turns if t.pre_checks.unused_attacks]
        if turns
        else list(replay.pre_check_summary.turns_with_unused_attacks)
    )
    unused_loc_turns = (
        [t.turn_number for t in turns if t.pre_checks.unused_ready_locations]
        if turns
        else list(replay.pre_check_summary.turns_with_unused_ready_locations)
    )
    total_floated = (
        sum(t.pre_checks.mana_floated for t in turns if t.active_player == "you")
        if turns
        else replay.pre_check_summary.total_mana_floated
    )

    pre_summary = MatchPreCheckSummary(
        missed_lethal_turns=missed_turns,
        total_mana_floated=total_floated,
        your_cards_burned=your_burned,
        your_cards_discarded=your_discarded,
        turns_with_unused_attacks=unused_atk_turns,
        turns_with_unused_ready_locations=unused_loc_turns,
    )

    cards_seen = {
        cid: card_db.to_reference(cid) for cid in sorted(canon.known_card_ids)
    }

    return GameReplay(
        schema_version=replay.schema_version,
        metadata=metadata,
        mulligan=mulligan,
        turns=turns,
        your_deck_observed=your_deck,
        opponent_deck_observed=opp_deck,
        cards_seen=cards_seen,
        pre_check_summary=pre_summary,
    )
