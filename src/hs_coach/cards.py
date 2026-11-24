"""HearthstoneJSON cards.json loader, text cleaner, and ETag sync helper."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from hs_coach.schemas import CardReference

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


def clean_card_text(raw: str, *, strip_alternate: bool = False) -> str:
    """Strip Hearthstone HTML/markup, [x], and proxy noise from card text."""
    if not raw:
        return ""
    text = raw.split("@", 1)[0] if strip_alternate else raw
    text = re.sub(r"<[^>]+>|\[x\]|[$#]", "", text)
    text = text.replace("\n", " ").replace("\u00a0", " ")
    text = DEV_NOISE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


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

    def clean_text(self, card_id: str | None, *, strip_alternate: bool = False) -> str:
        return clean_card_text(self.raw_text(card_id), strip_alternate=strip_alternate)

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
