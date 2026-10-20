"""Names, rarity and preview pictures of the game's equipment, read from the user's own game.

``build(game)`` reads the game's data tables (.user), its text (.msg) and the GUI's thumbnail
tables, and returns a JSON-serialisable dict that the app caches per game version; ``Catalog``
answers questions about it by ``Target`` (model.py).  Each section (armor, weapons, kinsects,
palico, seikret, NPC, monsters) is read on its own: one that fails leaves a note in
"problems" and the others intact.  Names are in the first of ``languages`` an entry has.

How the tables connect (enum fields hold the enum's Fixed value; rarity is stored as
Fixed = 18 - n and shown as n + 1):
- armor: Common/Equip/ArmorSeriesData (_ModId = model folder MMM, _ModSubMaleId /
  _ModSubFemaleId = design folder of type 1 / 2, the same when the series has one design), its
  pieces from ArmorData and the layered OuterArmorData.  Which body has a prefab comes from the
  Catalog's PlayerArmorList (_DataList male, _FemaleDataList female), both bodies when it cannot
  be read.  Thumbnails: GUI/Common/_UserData/ArmorThumbnailTextureData, TextureId =
  B * 10^7 + S * 100 + G * 10 + P with B 2|3 the body, S the series' plain enum value
  (Player/EnumMaker/ArmorSeries maps Fixed -> plain), P the ARMOR_PARTS index (helm..leg) and
  G = 0 for type 1, 1 for type 2 - unverified: every series has both G, single-design ones too.
- weapons: Common/Weapon/<Type> (WeaponData: TYPE = 14 - _Type, _ModelId -> folder
  it<TT>/<id // 10000>/<id % 10000>, _<Type> = the weapon's Fixed id) and Outer<Type>Data
  (layered), names from Excel_Equip/<Type> and Outer<Type>; the tree name from <Type>Tree (the
  row a weapon id sits in -> WeaponSeriesData).  Thumbnails: WeaponThumbnailTextureData,
  TextureId = TYPE * 10^6 + the Fixed id (kinsects: 10 * 10^6 + _ModelID).
- palico: Otomo/DataParam/OtomoEquipSeriesData, whose _ModelId is the Fixed value of
  app.OtEquipDef.EQUIP_SERIES_ID; no game file maps that to the ch05 folder number, so the
  numbers below are embedded: taken from dtlnor's enum dump (Enums_Internal.json,
  github.com/dtlnor/MHWs-in-json).  Pieces: OtArmorData / OtWeaponData / OtomoOuterArmorData;
  thumbnails: OtEquipThumnailTextureData (sic), TextureId = 5 * 10^5 + NNN * 100 + X * 10 with
  X 0 body, 1 helm, 2 weapon (a folder the embedded numbers do not know yet gets its pictures only).
- seikret (best effort): Facility/SeikretEquipData (_VisualSettingType) -> the visual setting
  in Porter/Data/PorterVisualSettingPack/PT00_00_000 -> its _EquipSeriesId -> the Catalog's
  PorterEquipList -> Porter/_Prefab/Equip/<NNN>/Body|Saddle_<NNN>.pfb, whose meshes name the
  ch07 model folders (aliases); thumbnails from CharaMake/CharacterEditThumbnailsSeikretEquip.
- NPC: each VisualSetting file passed in names the ch04 body prefab (and ch00 face) an NPC
  wears; the NPC id in its path -> NPC/EnumMaker/NpcID -> Dialogue/CommonData/NpcNameData ->
  Excel_Data/NpcName.
- monsters: Excel_Data/EnemyText entries EnemyText_NAME_EM<0|1><NNN>_<VV>_<K> -> "em0NNN".
GUI thumbnail prefabs GUI/Common/_Prefab/Thumbnail/<dir>/Thumbnail_<X>.pfb draw
GUI/ui_texture/tex080000/tex_thumbnail/<Character|Item>/<dir>/tex_<X>_IMLM4.tex (checked to exist;
otherwise a .tex resource of the prefab itself).
"""

from __future__ import annotations

import json
import logging
import re
import struct
from collections import Counter
from dataclasses import dataclass

from ..matfix import rsz
from . import EquipError, msg
from .model import ARMOR_PARTS, PART_LABELS, WEAPON_TYPES, Target, armor_key, weapon_key, weapon_type_label
from .userdata import USER, read_user, rows

log = logging.getLogger(__name__)

CATALOG_FORMAT = 3  # raise when the result of build() changes, so that cached catalogs are made again
# 2: thumbnails also without the GUI thumbnail tables (their names follow the TextureId); 3: rarity per name
NATIVES = "natives/STM/"
USER_VERSION, MSG_VERSION, PFB_VERSION, TEX_VERSION = ".3", ".23", ".18", ".241106027"
USER_EXT, MSG_EXT = ".user" + USER_VERSION, ".msg" + MSG_VERSION
TEX_EXT = ".tex" + TEX_VERSION
GUI_THUMBS = "GUI/ui_texture/tex080000/tex_thumbnail/"
MAX_NAMES = 12
MAX_NPC_NAMES = 5
ARMOR_THUMB_PARTS = ARMOR_PARTS[:5]  # helm..leg: the parts that have a thumbnail
ITEM_THUMB_ORDER = ("body", "helm", "arm", "waist", "leg", "weapon", "main", "saddle")
CATALOG_PACKS = tuple(dict.fromkeys(["First", "00_00"] + [f"{a:02d}_{b:02d}" for a in range(16) for b in range(0, 100, 10)]))
OTOMO_PARTS = {0: "weapon", 1: "helm", 2: "body"}  # app.OtEquipDef.EQUIP_TYPE
OTOMO_THUMB = {"body": 0, "helm": 1, "weapon": 2}
NPC_FROM = 500  # ch00 / ch01 groups from 500 up belong to NPCs

# app.OtEquipDef.EQUIP_SERIES_ID: Fixed value -> plain value = the ch05 folder number (dtlnor's dump;
# INVALID -978216832 and MAX 1132691456 left out: no folder)
OTOMO_SERIES = {
    -316533632: 0, -1972441856: 1, 95804296: 2, -1963750272: 3, 1613199232: 4, -893399104: 5, 1692420864: 6,
    -1707190784: 7, -1572577024: 8, 895444992: 9, 706803520: 10, -1996820224: 11, -381985984: 12, 909250176: 13,
    -512282656: 14, 1297603712: 15, -665552832: 16, -1439663104: 17, -574091008: 18, -528465152: 19, 436710528: 20,
    205645760: 21, -462046464: 22, -1349842176: 23, 1275033728: 24, 1859540736: 25, 831486016: 26, 1431748480: 27,
    1893861888: 28, 556009664: 29, -1563718400: 30, 658066624: 31, 515860960: 32, 637271168: 33, 1938585984: 34,
    1827823872: 35, 1757640960: 36, -1182100224: 37, 158860960: 38, 1645478656: 39, 1972078208: 40, 1909112448: 41,
    8803: 42, 14968: 43, 7597: 44, 12368: 45, 1462: 46, 13320: 47, 28738: 48, 2707: 49, 29866: 50, 28819: 51,
    30113: 52, 20664: 53, 11922: 54, 20607: 55, 6913: 56, 28205: 57, 2218: 58, 9022: 59, 6360: 60, 28893: 61,
    3665: 62, 17593: 63, 22504: 64, 28562: 65, 22361: 66, 8191: 67, 18915: 68, 16613: 69, 22436: 70, 27698: 71,
    20376: 72, -635231232: 506,
}

SECTIONS = (("labels", "장비 분류 이름"), ("armor", "방어구"), ("weapon", "무기"), ("kinsect", "사냥벌레"),
            ("palico", "오토모 장비"), ("seikret", "세크레트 장비"), ("npc", "NPC"), ("monster", "몬스터"))

_GUI_THUMB = re.compile(r"^GameDesign/GUI/Common/_Prefab/Thumbnail/((?:ch|it)\d+)/Thumbnail_(\w+)\.pfb$", re.IGNORECASE)
_PORTER_PREFAB = re.compile(r"Porter/_Prefab/Equip/(\d{3})/(Body|Saddle)_\d{3}\.pfb$", re.IGNORECASE)
_PORTER_EQUIP = re.compile(r"_EQUIP_(\d{3})_", re.IGNORECASE)
_CH07 = re.compile(r"Art/Model/Character/ch07/(\d{3})/(\d{4})/", re.IGNORECASE)
_NPC_ID = re.compile(r"(npc\d{3}_\d{2}_\d{3})", re.IGNORECASE)
_ENEMY = re.compile(r"^EnemyText_NAME_EM([01])(\d{3})_(\d{2})_(\d+)$")


# prefab names inside an NPC's VisualSetting file (its strings, read as UTF-16): body ch04_ABB_CCDD,
# face ch00_NNN_*, hair ch01_NNN_*
_NPC_BODY = re.compile(r"ch04_(\d{3})_(\d{4})(?=[._])", re.IGNORECASE)
_NPC_FACE = re.compile(r"ch00_(\d{3})_\d{4}\.pfb", re.IGNORECASE)
_NPC_HAIR = re.compile(r"ch01_(\d{3})_\d{4}\.pfb", re.IGNORECASE)


class _Missing(Exception):
    """A table a section cannot do without."""


@dataclass
class _Series:
    fixed: int
    model: int
    male: int
    female: int
    name: str
    rarity: int | None
    order: tuple


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rarity(value) -> int | None:
    """In-game rarity (1..12) from a stored app.ItemDef.RARE Fixed value (18 - n)."""
    v = _int(value)
    return 19 - v if v is not None and 7 <= v <= 18 else None


def _path(value) -> str:
    """A resource path from a via.Prefab object (its Path) or a plain string."""
    if isinstance(value, dict):
        value = value.get("Path")
    return value if isinstance(value, str) else ""


def _user_path(value) -> str:
    return value.get(USER, "") if isinstance(value, dict) and isinstance(value.get(USER), str) else ""


# GUI thumbnail prefab of a TextureId, as the game names them (used when a thumbnail table cannot be read)
_THUMB_PREFAB = "GameDesign/GUI/Common/_Prefab/Thumbnail/{0}/Thumbnail_{1}.pfb"


def thumb_texture(prefab: str) -> str:
    """The texture a GUI thumbnail prefab is named after (full path with version); "" for another prefab."""
    m = _GUI_THUMB.match(prefab[1:] if prefab.startswith("@") else prefab)
    if not m:
        return ""
    folder = "Character" if m.group(1)[:2].lower() == "ch" else "Item"
    return f"{NATIVES}{GUI_THUMBS}{folder}/{m.group(1)}/tex_{m.group(2)}_IMLM4{TEX_EXT}"


def armor_thumb_prefab(tid: int) -> str:
    """B*1e7 + XX*1e5 + SERIES*100 + G*10 + P -> Thumbnail_ch0B_XX_SSS_G_P."""
    body = tid // 10 ** 7
    name = f"ch0{body}_{tid // 10 ** 5 % 100:02d}_{tid // 100 % 1000:03d}_{tid // 10 % 10}_{tid % 10}"
    return _THUMB_PREFAB.format(f"ch0{body}", name)


def item_thumb_prefab(tid: int) -> str:
    """TYPE*1e6 + SS*1e4 + NNNN (weapons, kinsects) -> Thumbnail_it<TT><SS>_<NNNN>."""
    wtype, rest = divmod(tid, 10 ** 6)
    return _THUMB_PREFAB.format(f"it{wtype:02d}", f"it{wtype:02d}{rest // 10 ** 4:02d}_{rest % 10 ** 4:04d}")


def palico_thumb_prefab(tid: int) -> str:
    """5e5 + NNN*100 + X*10 -> Thumbnail_ch05_NNN_X0."""
    number, slot = divmod(tid - 500000, 100)
    return _THUMB_PREFAB.format("ch05", f"ch05_{number:03d}_{slot:02d}")


def _named(pairs, limit: int = MAX_NAMES) -> dict:
    """"names" (unique, in order) and the in-game rarity of each ("rarities"; the lowest when a name
    occurs more than once), "rarity" and "rarity_max": the lowest and highest of all."""
    rarity: dict[str, int | None] = {}
    every = []
    for name, rare in pairs:
        if rare:
            every.append(rare)
        if not name:
            continue
        if name not in rarity:
            rarity[name] = rare
        elif rare and (rarity[name] is None or rare < rarity[name]):
            rarity[name] = rare
    names = list(rarity)[:limit]
    return {"names": names, "rarities": [rarity[n] for n in names], "rarity": min(every, default=None),
            "rarity_max": max(every, default=None)}


def _unique(names) -> list[str]:
    return [n for n in dict.fromkeys(names) if n]


def _order(row: dict, *fields: str) -> tuple:
    return tuple(_int(row.get(f)) or 0 for f in fields)


def build(game, registry: rsz.TypeRegistry | None = None, progress=None, languages=msg.DEFAULT_LANGUAGES,
          npc_settings=()) -> dict:
    """The catalog of the game behind ``game`` (read(path) -> bytes | None, has(path) -> bool;
    paths with natives/STM/ and the version).  ``npc_settings``: paths of the NPCs'
    VisualSetting .user files.  Never raises for bad game data: what could not be read is
    listed in "problems"."""
    return _Builder(game, registry or rsz.TypeRegistry.load(), progress, tuple(languages), tuple(npc_settings)).run()


class _Builder:
    def __init__(self, game, registry, progress, languages, npc_settings):
        self.game = game
        self.registry = registry
        self.progress = progress or (lambda done, total, message: None)
        self.languages = languages
        self.npc_settings = npc_settings
        self.items: dict[str, dict] = {}
        self.aliases: dict[str, dict] = {}
        self.labels: dict[str, dict] = {}
        self.problems: list[str] = []
        self.missing: list[str] = []
        self.step = 0
        self._users: dict[str, dict | None] = {}
        self._msgs: dict[str, msg.MsgFile | None] = {}
        self._thumbs: dict[str, str | None] = {}
        self._packs: dict[str, list[str]] = {}

    # ---- running ---------------------------------------------------------------------
    def run(self) -> dict:
        for i, (name, label) in enumerate(SECTIONS):
            self.step = i
            self.report(0, f"게임에서 장비 정보 읽는 중... {label}")
            self.missing = []
            try:
                getattr(self, "_" + name)()
            except _Missing:
                pass
            except Exception:  # one section must never cost the others
                log.warning("장비 정보(%s)를 만들지 못했습니다", label, exc_info=True)
                self.problems.append(f"{label}: 정보를 읽는 중 오류가 났습니다")
            if self.missing:
                files = list(dict.fromkeys(self.missing))
                self.problems.append(f"{label}: 게임 파일을 읽지 못했습니다 ({', '.join(files[:4])}"
                                     f"{f' 외 {len(files) - 4}개' if len(files) > 4 else ''})")
        self.progress(len(SECTIONS) * 10, len(SECTIONS) * 10, "장비 정보를 다 읽었습니다")
        log.info("장비 정보: %s", ", ".join(f"{k} {len(v)}" for k, v in self.items.items()))
        return {"format": CATALOG_FORMAT, "languages": list(self.languages), "problems": self.problems,
                "labels": self.labels, "items": self.items, "aliases": self.aliases}

    def report(self, fraction: float, message: str) -> None:
        total = len(SECTIONS) * 10
        self.progress(min(total, int((self.step + fraction) * 10)), total, message)

    # ---- reading game files ------------------------------------------------------------
    def read(self, path: str) -> bytes | None:
        try:
            if not self.game.has(path):
                return None
            return self.game.read(path)
        except Exception as exc:  # an encrypted or damaged entry, a vanished pak
            log.info("게임 파일을 읽지 못했습니다 (%s): %s", path, exc)
            return None

    def user_at(self, path: str, note: bool = True) -> dict | None:
        if path not in self._users:
            data = self.read(path)
            root = read_user(data, self.registry) if data else None
            if data and root is None:
                log.info("형식 정보로 읽히지 않는 파일: %s", path)
            self._users[path] = root
        root = self._users[path]
        if root is None and note:
            self.missing.append(path.rsplit("/", 1)[-1])
        return root

    def user(self, rel: str, required: bool = False) -> dict | None:
        root = self.user_at(NATIVES + rel + USER_EXT)
        if root is None and required:
            raise _Missing(rel)
        return root

    def messages(self, rel: str, required: bool = False) -> msg.MsgFile | None:
        path = NATIVES + rel + MSG_EXT
        if path not in self._msgs:
            data = self.read(path)
            try:
                self._msgs[path] = msg.parse(data) if data else None
            except EquipError as exc:
                log.info("%s: %s", path, exc)
                self._msgs[path] = None
        found = self._msgs[path]
        if found is None:
            self.missing.append(path.rsplit("/", 1)[-1])
            if required:
                raise _Missing(rel)
        return found

    def text(self, names: msg.MsgFile | None, guid) -> str:
        if names is None or not isinstance(guid, bytes):
            return ""
        return names.name_of(guid, self.languages)

    def catalog_files(self, name: str) -> list[str]:
        """The Catalog's <pack>/Data/<name>[_<pack>].user.3 files the game has."""
        if name not in self._packs:
            found = []
            for pack in CATALOG_PACKS:
                for file in (name, f"{name}_{pack}"):
                    path = f"{NATIVES}GameDesign/Catalog/{pack}/Data/{file}{USER_EXT}"
                    try:
                        if self.game.has(path):
                            found.append(path)
                    except Exception:
                        pass
            self._packs[name] = found
        return self._packs[name]

    def thumb_table(self, rel: str) -> dict[int, str]:
        """TextureId -> GUI thumbnail prefab."""
        out = {}
        for r in rows(self.user(rel)):
            tid, prefab = _int(r.get("_TextureId")), _path(r.get("_Prefab"))
            if tid is not None and prefab:
                out[tid] = prefab
        return out

    def thumb(self, prefab: str) -> str | None:
        """The texture (full path with version) a GUI thumbnail prefab shows."""
        prefab = prefab[1:] if prefab.startswith("@") else prefab
        if prefab not in self._thumbs:
            path = thumb_texture(prefab)
            tex = path if path and self.read_has(path) else None
            self._thumbs[prefab] = tex or self.texture(prefab)
        return self._thumbs[prefab]

    def read_has(self, path: str) -> bool:
        try:
            return bool(self.game.has(path))
        except Exception:
            return False

    def resources(self, rel: str) -> list[str]:
        """Resource paths a game prefab (path as a resource names it: without natives/STM/ and the
        version) declares, without "@"."""
        data = self.read(NATIVES + rel + PFB_VERSION)
        try:
            info = rsz.parse(data) if data else None
            found = info.resources() if info is not None else ()
        except (struct.error, ValueError, IndexError):
            return []
        return sorted(s[1:] if s.startswith("@") else s for s in found)

    def texture(self, prefab: str) -> str | None:
        """The first .tex a prefab declares that the game has."""
        for s in self.resources(prefab):
            if s.lower().endswith(".tex") and self.read_has(NATIVES + s + TEX_VERSION):
                return NATIVES + s + TEX_VERSION
        return None

    def section(self, kind: str) -> dict:
        return self.items.setdefault(kind, {})

    # ---- sections ------------------------------------------------------------------------
    def _labels(self):
        ref = self.messages("GameDesign/Text/Reference/RefEquipment", required=True)
        weapon_types = {}
        for t in range(len(WEAPON_TYPES)):
            text = ref.text(ref.by_name(f"RefEquipment_0001_{t:03d}"), self.languages)
            if text:
                weapon_types[str(t)] = text
        parts = {}
        for i, part in enumerate(ARMOR_THUMB_PARTS):
            text = ref.text(ref.by_name(f"RefEquipment_0002_{i:03d}"), self.languages)
            if text:
                parts[part] = text
        self.labels = {"weapon_types": weapon_types, "parts": parts}

    def _armor(self):
        names = self.messages("GameDesign/Text/Excel_Equip/ArmorSeries")
        series = []
        for r in rows(self.user("GameDesign/Common/Equip/ArmorSeriesData", required=True)):
            fixed, model = _int(r.get("_Series")), _int(r.get("_ModId"))
            male, female = _int(r.get("_ModSubMaleId")), _int(r.get("_ModSubFemaleId"))
            if not fixed or model is None or model < 0 or male is None or female is None:
                continue
            series.append(_Series(fixed, model, male, female, self.text(names, r.get("_Name")),
                                  _rarity(r.get("_Rare")), _order(r, "_SortId", "_Index")))
        series.sort(key=lambda s: s.order)
        self.report(0.2, "게임에서 장비 정보 읽는 중... 방어구 부위")
        pieces: dict[int, dict[str, tuple[str, str]]] = {}  # series -> part -> (male name, female name)
        armor_names = self.messages("GameDesign/Text/Excel_Equip/Armor")
        for r in rows(self.user("GameDesign/Common/Equip/ArmorData")):
            fixed, part = _int(r.get("_Series")), _part(r.get("_PartsType"))
            name = self.text(armor_names, r.get("_Name"))
            if fixed and part and name:
                pieces.setdefault(fixed, {}).setdefault(part, (name, name))
        outer_names = self.messages("GameDesign/Text/Excel_Equip/OuterArmor")
        for r in rows(self.user("GameDesign/Common/Equip/OuterArmorData")):
            fixed, part = _int(r.get("_Series")), _part(r.get("_PartsType"))
            male, female = self.text(outer_names, r.get("_NameMale")), self.text(outer_names, r.get("_NameFemale"))
            if fixed and part and (male or female):
                pieces.setdefault(fixed, {}).setdefault(part, (male or female, female or male))
        self.report(0.5, "게임에서 장비 정보 읽는 중... 방어구 그림")
        plain = {}
        for r in rows(self.user("GameDesign/Player/EnumMaker/ArmorSeries")):
            fixed, value = _int(r.get("_FixedID")), _int(r.get("_EnumValue"))
            if fixed is not None and value is not None:
                plain[fixed] = value
        thumbs = self.thumb_table("GameDesign/GUI/Common/_UserData/ArmorThumbnailTextureData")
        listed = self._armor_list()
        groups: dict[tuple[int, int], list[_Series]] = {}
        for s in series:
            for sub in dict.fromkeys((s.male, s.female)):
                groups.setdefault((s.model, sub), []).append(s)
        out = self.section("armor")
        for (model, sub), members in groups.items():
            first = members[0]
            design = 1 if sub == first.male else 2
            bodies = [b for b in "mf" if listed is None or (b, model, sub) in listed] or ["m", "f"]
            named = _named((s.name, s.rarity) for s in members)
            for body in bodies:
                entry = {**named, "design": design, "single_design": first.male == first.female,
                         "pieces": {}, "thumbs": {}}
                for p, part in enumerate(ARMOR_THUMB_PARTS):
                    for s in members:
                        pair = pieces.get(s.fixed, {}).get(part)
                        if pair:
                            entry["pieces"][part] = pair[0 if body == "m" else 1]
                            break
                    for s in members:
                        value = plain.get(s.fixed)
                        tid = (2 if body == "m" else 3) * 10 ** 7 + (value or 0) * 100 + (design - 1) * 10 + p
                        prefab = (thumbs.get(tid) or armor_thumb_prefab(tid)) if value is not None and value >= 0 else None
                        tex = self.thumb(prefab) if prefab else None
                        if tex:
                            entry["thumbs"][part] = tex
                            break
                entry["thumb"] = _item_thumb(entry["thumbs"])
                if entry["names"] or entry["thumb"]:
                    out[armor_key(body, model, sub)] = entry

    def _armor_list(self) -> set | None:
        """(body, model, design folder) of every armor prefab set the Catalog lists; None when
        no PlayerArmorList could be read."""
        found, read_any = set(), False
        for path in self.catalog_files("PlayerArmorList"):
            root = self.user_at(path, note=False)
            if root is None:
                log.info("방어구 목록을 읽지 못했습니다: %s", path)
                continue
            read_any = True
            for body, field in (("m", "_DataList"), ("f", "_FemaleDataList")):
                for r in rows(root, field):
                    model, sub = _int(r.get("_ArmorID")), _int(r.get("_ArmorSubID"))
                    if model is not None and sub is not None:
                        found.add((body, model, sub))
        return found if read_any else None

    def _weapon(self):
        thumbs = self.thumb_table("GameDesign/GUI/Common/_UserData/WeaponThumbnailTextureData")
        series_names = self.messages("GameDesign/Text/Excel_Equip/WeaponSeries")
        fixed_names = {}
        for r in rows(self.user("GameDesign/Common/Equip/WeaponSeriesData")):
            fixed, name = _int(r.get("_Series")), self.text(series_names, r.get("_Name"))
            if fixed is not None and name:
                fixed_names[fixed] = name
        trees = {}  # a weapon tree names its rows by the plain enum value
        for r in rows(self.user("GameDesign/Player/EnumMaker/WeaponSeries") if fixed_names else None):
            plain, fixed = _int(r.get("_EnumValue")), _int(r.get("_FixedID"))
            if plain is not None and fixed in fixed_names:
                trees[plain] = fixed_names[fixed]
        out = self.section("weapon")
        read_any = False
        for t, (file, ko, _en) in enumerate(WEAPON_TYPES):
            self.report(t / len(WEAPON_TYPES), f"게임에서 장비 정보 읽는 중... 무기 ({ko})")
            data = self.user(f"GameDesign/Common/Weapon/{file}")
            read_any |= data is not None
            names = self.messages(f"GameDesign/Text/Excel_Equip/{file}") if data is not None else None
            tree = self._weapon_tree(file, trees) if data is not None and trees else {}
            found: dict[str, list] = {}
            for r in rows(data):
                stored, model = _int(r.get("_Type")), _int(r.get("_ModelId"))
                wtype = 14 - stored if stored is not None and 0 <= 14 - stored < len(WEAPON_TYPES) else t
                if model is None or model < 0:
                    continue
                wid = _int(r.get("_" + file))
                found.setdefault(weapon_key(wtype, model), []).append(
                    ((0, *_order(r, "_SortId", "_Index")), self.text(names, r.get("_Name")),
                     _rarity(r.get("_Rare")), wtype, wid))
            outer = self.user(f"GameDesign/Common/Weapon/Outer{file}Data")
            outer_names = self.messages(f"GameDesign/Text/Excel_Equip/Outer{file}") if outer is not None else None
            for r in rows(outer):
                model = _int(r.get("_ModelId"))
                if model is None or model < 0:
                    continue
                found.setdefault(weapon_key(t, model), []).append(  # layered: after the weapons themselves
                    ((1, *_order(r, "_SortId", "_Index")), self.text(outer_names, r.get("_Name")),
                     _rarity(r.get("_Rare")), t, _int(r.get(f"_{file}Id"))))  # _<Type>Id: its weapon, if any
            for key, members in found.items():
                members.sort(key=lambda m: m[0])
                entry = {**_named((m[1], m[2]) for m in members), "thumbs": {}, "thumb": None}
                in_tree = Counter(tree[m[4]] for m in members if m[4] in tree)
                if in_tree:
                    entry["tree"] = in_tree.most_common(1)[0][0]
                for _sort, _name, _rare, wtype, wid in members:
                    tid = wtype * 10 ** 6 + wid if wid is not None and wid > 0 else None
                    prefab = (thumbs.get(tid) or item_thumb_prefab(tid)) if tid is not None else None
                    tex = self.thumb(prefab) if prefab else None
                    if tex:
                        entry["thumb"] = tex
                        break
                if entry["names"] or entry["thumb"]:
                    out[key] = entry
        if not read_any:
            raise _Missing("weapons")

    def _weapon_tree(self, file: str, trees: dict[int, str]) -> dict[int, str]:
        """The weapons' Fixed id -> the name of the tree (row) it sits in; ``trees``: plain series
        value (what a tree's rows hold) -> name."""
        root = self.user(f"GameDesign/Common/Weapon/{file}Tree")
        series = {}
        for r in rows(root, "_RowDataList"):
            level, plain = _int(r.get("_RowLevel")), _int(r.get("_Series"))
            if level is not None and plain in trees:
                series[level] = trees[plain]
        out = {}
        for r in rows(root, "_WeaponTreeList"):
            wid, level = _int(r.get("_WeaponID")), _int(r.get("_RowDataLevel"))
            if wid is not None and level in series:
                out.setdefault(wid, series[level])
        return out

    def _kinsect(self):
        names = self.messages("GameDesign/Text/Excel_Equip/RodInsect")
        thumbs = self.thumb_table("GameDesign/GUI/Common/_UserData/WeaponThumbnailTextureData")
        found: dict[str, list] = {}
        for r in rows(self.user("GameDesign/Common/Weapon/RodInsectData", required=True)):
            model = _int(r.get("_ModelID"))
            if model is None or model // 10000 != 3:
                continue
            found.setdefault(f"{model % 10000:04d}", []).append(
                (_order(r, "_SortID", "_Index"), self.text(names, r.get("_Name")), _rarity(r.get("_Rare")), model))
        out = self.section("kinsect")
        for key, members in found.items():
            members.sort(key=lambda m: m[0])
            tid = 10 * 10 ** 6 + members[0][3]
            prefab = thumbs.get(tid) or item_thumb_prefab(tid)
            entry = {**_named((m[1], m[2]) for m in members), "thumbs": {},
                     "thumb": self.thumb(prefab) if prefab else None}
            if entry["names"] or entry["thumb"]:
                out[key] = entry

    def _palico(self):
        names = self.messages("GameDesign/Text/Excel_Equip/OtomoEquipSeries")
        groups: dict[int, list] = {}  # ch05 folder -> [(order, EQUIP_DATA_ID Fixed, name, rarity)]
        for r in rows(self.user("GameDesign/Otomo/DataParam/OtomoEquipSeriesData", required=True)):
            series, number = _int(r.get("_Series")), OTOMO_SERIES.get(_int(r.get("_ModelId")))
            if series is None or number is None:
                continue
            groups.setdefault(number, []).append((_order(r, "_SortId", "_Index"), series, self.text(names, r.get("_Name")),
                                                  _rarity(r.get("_Rare"))))
        pieces: dict[int, dict[str, str]] = {}  # EQUIP_DATA_ID -> part -> name
        for rel, msg_rel, field in (("OtomoArmorData", "OtomoArmor", "_EquipCategory"),
                                    ("OtomoWeaponData", "OtomoWeapon", None),
                                    ("OtomoOuterArmorData", "OtomoOuterArmor", "_EquipType")):
            table = self.user(f"GameDesign/Otomo/DataParam/{rel}")
            texts = self.messages(f"GameDesign/Text/Excel_Equip/{msg_rel}") if table is not None else None
            for r in rows(table):
                series = _int(r.get("_Series"))
                part = OTOMO_PARTS.get(_int(r.get(field))) if field else "weapon"
                name = self.text(texts, r.get("_Name"))
                if series is not None and part and name:
                    pieces.setdefault(series, {}).setdefault(part, name)
        thumbs = self.thumb_table("GameDesign/GUI/Common/_UserData/OtEquipThumnailTextureData")
        for tid in thumbs:  # folders of series the embedded table does not know (newer game data): pictures only
            number, slot = divmod(tid - 500000, 100)
            if 0 <= number < 1000 and slot in (0, 10, 20):
                groups.setdefault(number, [])
        out = self.section("palico")
        for number, members in groups.items():
            members.sort(key=lambda m: m[0])
            entry = {**_named((m[2], m[3]) for m in members), "pieces": {}, "thumbs": {}}
            for part in ("helm", "body", "weapon"):
                name = next((pieces[m[1]][part] for m in members if part in pieces.get(m[1], {})), "")
                if name:
                    entry["pieces"][part] = name
                tid = 500000 + number * 100 + OTOMO_THUMB[part] * 10
                prefab = thumbs.get(tid) or palico_thumb_prefab(tid)
                tex = self.thumb(prefab) if prefab else None
                if tex:
                    entry["thumbs"][part] = tex
            entry["thumb"] = _item_thumb(entry["thumbs"])
            if entry["names"] or entry["thumb"]:
                out[f"{number:03d}"] = entry

    def _seikret(self):
        names = self.messages("GameDesign/Text/Excel_Data/SeikretEquip")
        equips = rows(self.user("GameDesign/Facility/SeikretEquipData", required=True))
        settings = {}  # VISUAL_SETTING_TYPE Fixed -> PorterVisualSetting path
        pack = self.user("GameDesign/Porter/Data/PorterVisualSettingPack/PT00_00_000_PorterVisualSettingPack")
        for r in rows(pack):
            kind, path = _int(r.get("_VisualSettingType")), _user_path(r.get("_PorterVisualSetting"))
            if kind is not None and path:
                settings.setdefault(kind, path)
        prefabs: dict[int, dict[str, str]] = {}  # PorterEquipDef Fixed -> {"body": prefab, "saddle": prefab}
        for path in self.catalog_files("PorterEquipList"):
            for r in rows(self.user_at(path, note=False)):
                sid = _int(r.get("_SeriesID"))
                parts = r.get("_PartsPrefabs") if isinstance(r.get("_PartsPrefabs"), list) else []
                for value in parts:
                    m = _PORTER_PREFAB.search(_path(value))
                    if sid is not None and m:
                        prefabs.setdefault(sid, {}).setdefault(m.group(2).lower(), _path(value))
        thumbs = {}
        for r in rows(self.user("GameDesign/Common/CharaMake/CharacterEditThumbnailsSeikretEquip"), "_Entries"):
            value = r.get("_Value")
            kind, texture = _int(r.get("_Key")), _path(value.get("Texture") if isinstance(value, dict) else None)
            if kind is not None and texture:
                thumbs[kind] = texture
        self.report(0.5, "게임에서 장비 정보 읽는 중... 세크레트 모델")
        out = self.section("seikret")
        owners: dict[str, set] = {}  # ch07 model folder -> {(equip key, part)}
        for r in sorted(equips, key=lambda r: _order(r, "_Index")):
            kind = _int(r.get("_VisualSettingType"))
            setting_path = settings.get(kind, "")
            setting = self.user_at(NATIVES + setting_path + USER_VERSION, note=False) if setting_path else None
            parts = prefabs.get(_int(setting.get("_EquipSeriesId")), {}) if setting else {}
            number = next((m.group(1) for m in map(_PORTER_PREFAB.search, parts.values()) if m), None)  # the body's
            if number is None:
                m = _PORTER_EQUIP.search(setting_path)
                number = m.group(1) if m else None
            if number is None:
                continue
            key = f"equip/{number}"
            name = self.text(names, r.get("_Name"))
            # a piece may come from another folder (EQUIP_001 wears saddle 503): that folder gets the name too
            folders = [m.group(1) for m in map(_PORTER_PREFAB.search, parts.values()) if m]
            for folder in dict.fromkeys([number, *folders]):
                entry = out.setdefault(f"equip/{folder}", {"names": [], "rarity": None, "thumbs": {}, "thumb": None})
                entry["names"] = _unique([*entry["names"], name])[:MAX_NAMES]
            entry = out[key]
            texture = thumbs.get(kind)
            if texture and not entry["thumb"]:
                path = NATIVES + (texture[1:] if texture.startswith("@") else texture) + TEX_VERSION
                entry["thumb"] = path if self.read_has(path) else None
            for part, prefab in parts.items():
                for s in self.resources(prefab):
                    m = _CH07.search(s)
                    if m:
                        owners.setdefault(f"model/{m.group(1)}/{m.group(2)}", set()).add((key, part))
        for key in [k for k, e in out.items() if not e["names"] and not e["thumb"]]:
            del out[key]
        aliases = {}
        for model, users in owners.items():
            keys = {k for k, _p in users}
            if len(keys) == 1:  # a model folder only one equipment uses
                (part,) = {p for _k, p in users} if len(users) == 1 else ("",)
                aliases[model] = [keys.pop(), part]
        if aliases:
            self.aliases["seikret"] = aliases

    def _npc(self):
        if not self.npc_settings:
            return
        ids = {}
        for r in rows(self.user("GameDesign/NPC/EnumMaker/NpcID", required=True)):
            name, fixed = r.get("_EnumName"), _int(r.get("_FixedID"))
            if isinstance(name, str) and fixed is not None:
                ids[name.upper()] = fixed
        guids = {}
        for r in rows(self.user("GameDesign/Dialogue/CommonData/NpcNameData", required=True)):
            fixed = _int(r.get("_NpcID"))
            if fixed is not None and isinstance(r.get("_Name"), bytes):
                guids.setdefault(fixed, r["_Name"])
        texts = self.messages("GameDesign/Text/Excel_Data/NpcName", required=True)
        uses: dict[str, dict[str, int]] = {}  # entry key -> NPC id -> rank (0 main story cast)
        worn: dict[str, set] = {}  # face/hair key -> body keys worn with it
        names: dict[str, str] = {}
        for i, path in enumerate(self.npc_settings):
            if i % 50 == 0:
                self.report(i / len(self.npc_settings),
                            f"게임에서 장비 정보 읽는 중... NPC {i}/{len(self.npc_settings)}")
            m = _NPC_ID.search(path.rsplit("/", 1)[-1]) or _NPC_ID.search(path)
            data = self.read(path) if m else None
            if not data:
                continue
            npc = m.group(1).upper()
            if npc not in names:
                names[npc] = self.text(texts, guids.get(ids.get(npc)))
            rank = 0 if "/main/" in path.replace("\\", "/").lower() else 1
            text = data[:len(data) // 2 * 2].decode("utf-16-le", "replace")  # RSZ strings sit at even offsets
            bodies = {f"{a}_{b}" for a, b in _NPC_BODY.findall(text)}
            heads = {f"{what}/{g}" for what, rx in (("face", _NPC_FACE), ("hair", _NPC_HAIR))
                     for g in rx.findall(text) if int(g) >= NPC_FROM}
            for key in bodies | heads:
                users = uses.setdefault(key, {})
                users[npc] = min(rank, users.get(npc, rank))
            for key in heads:
                worn.setdefault(key, set()).update(bodies)
        common = Counter(n for n in names.values() if n)  # generic villagers share a name
        out = self.section("npc")
        for key, users in uses.items():
            ordered = sorted(users, key=lambda n: (users[n], common[names.get(n, "")], n))
            found = _unique(names.get(n, "") for n in ordered)
            if found:
                out[key] = {"names": found[:MAX_NPC_NAMES], "npcs": len(users), "rarity": None, "thumbs": {},
                            "thumb": None}
        aliases = {key: [body, ""] for key, bodies in worn.items() if len(bodies) == 1 for body in bodies
                   if body in out or key not in out}  # never trade the face's own names for none
        if aliases:
            self.aliases["npc"] = aliases

    def _monster(self):
        texts = self.messages("GameDesign/Text/Excel_Data/EnemyText", required=True)
        found: dict[str, list] = {}
        for e in texts.entries:
            m = _ENEMY.match(e.name)
            text = texts.text(e, self.languages) if m else ""
            if text:
                order = (m.group(3) != "00", m.group(3), m.group(4))  # the plain monster before its variants
                found.setdefault(f"em{m.group(1)}{m.group(2)}", []).append((order, text))
        out = self.section("monster")
        for key, members in found.items():
            members.sort(key=lambda x: x[0])
            out[key] = {"names": _unique(t for _o, t in members)[:MAX_NAMES], "rarity": None, "thumbs": {},
                        "thumb": None}


def _part(value) -> str | None:
    v = _int(value)
    return ARMOR_THUMB_PARTS[v] if v is not None and 0 <= v < len(ARMOR_THUMB_PARTS) else None


def _rare(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _item_thumb(thumbs: dict[str, str]) -> str | None:
    return next((thumbs[p] for p in ITEM_THUMB_ORDER if p in thumbs), None)


# ---- lookups ------------------------------------------------------------------------------
class Catalog:
    """The result of build(), asked by Target.  ``data`` is what is cached."""

    def __init__(self, data: dict):
        self.data = data if isinstance(data, dict) else {}
        problems = self.data.get("problems")
        self.problems: list[str] = [p for p in problems if isinstance(p, str)] if isinstance(problems, list) else []
        self._items = _dict(self.data.get("items"))
        self._aliases = _dict(self.data.get("aliases"))
        self._labels = _dict(self.data.get("labels"))

    def resolve(self, target: Target) -> Target:
        """The equipment a file of ``target`` really belongs to: a seikret model folder -> the equipment
        whose prefab loads it, an NPC face or hair -> the NPC body worn with it (when only one is)."""
        alias = _dict(self._aliases.get(target.kind)).get(target.key)
        if isinstance(alias, list) and len(alias) == 2 and all(isinstance(x, str) for x in alias) and alias[0]:
            return Target(target.kind, alias[0], alias[1] or target.part)
        return target

    def entry(self, target: Target) -> dict | None:
        target = self.resolve(target)
        found = _dict(self._items.get(target.kind)).get(target.key)
        return found if isinstance(found, dict) else None

    def names(self, target: Target) -> list[str]:
        entry = self.entry(target)
        names = entry.get("names") if entry else None
        return [n for n in names if isinstance(n, str) and n] if isinstance(names, list) else []

    def name(self, target: Target) -> str:
        """The piece's name for a target with a part (when the game names it), else the equipment's
        first name; "" when unknown."""
        target = self.resolve(target)
        entry = self.entry(target)
        if entry is None:
            return ""
        piece = _dict(entry.get("pieces")).get(target.part) if target.part else None
        if isinstance(piece, str) and piece:
            return piece
        names = self.names(target)
        return names[0] if names else ""

    def thumb(self, target: Target) -> str | None:
        """The preview texture (full game path with version): the part's when the target has a part
        with its own picture, else the equipment's."""
        target = self.resolve(target)
        entry = self.entry(target)
        if entry is None:
            return None
        tex = _dict(entry.get("thumbs")).get(target.part) if target.part else None
        if not isinstance(tex, str) or not tex:
            tex = entry.get("thumb")
        return tex if isinstance(tex, str) and tex else None

    def rarity(self, target: Target) -> int | None:
        """The lowest in-game rarity of the equipment that looks like this (e.g. the low-rank set)."""
        entry = self.entry(target)
        return _rare(entry.get("rarity")) if entry else None

    def rarity_range(self, target: Target) -> tuple[int | None, int | None]:
        """(lowest, highest) rarity of the equipment that shares this look (low rank ... α, β)."""
        entry = self.entry(target)
        if not entry:
            return None, None
        low = _rare(entry.get("rarity"))
        return low, _rare(entry.get("rarity_max")) or low

    def rarities(self, target: Target) -> list[int | None]:
        """The rarity of each of names(target)."""
        entry = self.entry(target)
        if not entry:
            return []
        names, rares = entry.get("names"), entry.get("rarities")
        if not isinstance(names, list):
            return []
        rares = rares if isinstance(rares, list) and len(rares) == len(names) else [None] * len(names)
        return [_rare(r) for n, r in zip(names, rares) if isinstance(n, str) and n]

    def weapon_type_name(self, wtype: int) -> str:
        found = _dict(self._labels.get("weapon_types")).get(str(wtype))
        return found if isinstance(found, str) and found else weapon_type_label(wtype)

    def part_label(self, part: str) -> str:
        found = _dict(self._labels.get("parts")).get(part)
        return found if isinstance(found, str) and found else PART_LABELS.get(part, part)

    def count(self, kind: str) -> int:
        return len(_dict(self._items.get(kind)))

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Catalog":
        """Raises EquipError for a catalog of another format or a damaged one."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise EquipError(f"장비 정보 캐시를 읽지 못했습니다: {exc}") from None
        if not isinstance(data, dict) or data.get("format") != CATALOG_FORMAT or not isinstance(data.get("items"), dict):
            raise EquipError("장비 정보 캐시의 형식이 다릅니다")
        return cls(data)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}
