"""What a game file belongs to: the shared vocabulary of the equipment viewer.

A ``Target`` is one piece of equipment (or another model) and, where it has parts, the part
a file belongs to.  Keys are plain strings so that they survive a JSON cache:

    kind      key                    part                        example game path
    armor     "<m|f>/<MMM>/<SSS>"    helm body arm waist leg slinger  Art/Model/Character/ch03/004/001/3/...
    weapon    "<TT>/<MMMMMM>"        main sub                    Art/Model/Item/it02/00/0029/it0200_0029_1...
    kinsect   "<NNNN>"               ""                          Art/Model/Item/it10/03/0005/...
    palico    "<NNN>"                body helm weapon            Art/Model/Character/ch05/062/0000/...
    seikret   "equip/<NNN>"          body saddle                 GameDesign/Porter/_Prefab/Equip/012/Body_012.pfb
              "model/<GGG>/<NNNN>"   ""                          Art/Model/Character/ch07/901/0010/...
    npc       "<ABB>_<CCDD>"         ""                          Art/Model/Character/ch04/0/00/00/00/ch04_000_0000...
              "face/<NNN>", "hair/<NNN>"                         Art/Model/Character/ch00/500/..., ch01/500/...
    monster   "em<0|1><NNN>"         ""                          Art/Model/Character/ch90/001/...
    hunter    "face/<NNN>", "hair|eyebrow|beard/<m|f>/<K><NNN>", "inner/<m|f>/<SSS>", "skin/<m|f>"
    other     "charm/<SS>/<NNNN>", "mantle/<NN>", "amulet/<NNNN>"

- armor: m = ch02 (male body), f = ch03 (female body); MMM = ArmorSeriesData._ModId, SSS = the
  design folder (_ModSubMaleId = type 1, _ModSubFemaleId = type 2); the path digit is the part
  (1 arm, 2 body, 3 helm, 4 leg, 5 waist, 6 slinger).
- weapon: TT = app.WeaponDef.TYPE (0..13, see WEAPON_TYPES), MMMMMM = WeaponData._ModelId
  (folder it<TT>/<SS>/<NNNN> = SS * 10000 + NNNN); main = the *_0 model, sub = *_1 (shield,
  second blade, scabbard, quiver...).
- palico: NNN = ch05 series folder; slot folder 0000 body, 1000 helm, 2000 weapon.
"""

from __future__ import annotations

from dataclasses import dataclass

KINDS = ("armor", "weapon", "kinsect", "palico", "seikret", "npc", "monster", "hunter", "other")

KIND_LABELS = {
    "armor": "방어구",
    "weapon": "무기",
    "kinsect": "사냥벌레",
    "palico": "오토모",
    "seikret": "세크레트",
    "npc": "NPC",
    "monster": "몬스터",
    "hunter": "헌터 외모",
    "other": "기타 장식",
}

# app.WeaponDef.TYPE order: (data/msg file name, Korean, English).  The game's own names are read
# from GameDesign/Text/Reference/RefEquipment.msg.23 when the game is available.
WEAPON_TYPES = (
    ("LongSword", "대검", "Great Sword"),
    ("ShortSword", "한손검", "Sword & Shield"),
    ("TwinSword", "쌍검", "Dual Blades"),
    ("Tachi", "태도", "Long Sword"),
    ("Hammer", "해머", "Hammer"),
    ("Whistle", "수렵피리", "Hunting Horn"),
    ("Lance", "랜스", "Lance"),
    ("GunLance", "건랜스", "Gunlance"),
    ("SlashAxe", "슬래시액스", "Switch Axe"),
    ("ChargeAxe", "차지액스", "Charge Blade"),
    ("Rod", "조충곤", "Insect Glaive"),
    ("Bow", "활", "Bow"),
    ("HeavyBowgun", "헤비보우건", "Heavy Bowgun"),
    ("LightBowgun", "라이트보우건", "Light Bowgun"),
)

ARMOR_PARTS = ("helm", "body", "arm", "waist", "leg", "slinger")  # app.ArmorDef.ARMOR_PARTS order
ARMOR_DIGITS = {"1": "arm", "2": "body", "3": "helm", "4": "leg", "5": "waist", "6": "slinger"}
ARMOR_FOLDERS = {"arm": "arm", "body": "body", "helm": "helm", "leg": "leg", "waist": "waist", "slinger": "slinger"}
PALICO_SLOTS = {"0": "body", "1": "helm", "2": "weapon"}
PALICO_PARTS = ("helm", "body", "weapon")
WEAPON_PARTS = ("main", "sub")
SEIKRET_PARTS = ("body", "saddle")

PART_LABELS = {
    "helm": "머리", "body": "몸통", "arm": "팔", "waist": "허리", "leg": "다리", "slinger": "슬링어",
    "main": "본체", "sub": "보조", "weapon": "무기", "saddle": "안장",
}
PART_ORDER = {kind: parts for kind, parts in (("armor", ARMOR_PARTS), ("weapon", WEAPON_PARTS),
                                               ("palico", PALICO_PARTS), ("seikret", SEIKRET_PARTS))}

BODIES = {"m": "남성", "f": "여성"}
CH_BODY = {"ch02": "m", "ch03": "f"}


@dataclass(frozen=True, order=True)
class Target:
    kind: str
    key: str
    part: str = ""

    @property
    def item(self) -> "Target":
        """The equipment without the part."""
        return Target(self.kind, self.key) if self.part else self

    def with_part(self, part: str) -> "Target":
        return Target(self.kind, self.key, part)


def armor_key(body: str, model: int, design: int) -> str:
    return f"{body}/{model:03d}/{design:03d}"


def weapon_key(wtype: int, model_id: int) -> str:
    return f"{wtype:02d}/{model_id:06d}"


def parse_armor_key(key: str) -> tuple[str, int, int]:
    body, model, design = key.split("/")
    return body, int(model), int(design)


def parse_weapon_key(key: str) -> tuple[int, int]:
    wtype, model = key.split("/")
    return int(wtype), int(model)


def weapon_type_label(wtype: int) -> str:
    return WEAPON_TYPES[wtype][1] if 0 <= wtype < len(WEAPON_TYPES) else f"무기 {wtype:02d}"


def fallback_label(target: Target) -> str:
    """A name for a target without the game's data (no game folder, or the game could not be read)."""
    kind, key = target.kind, target.key
    try:
        if kind == "armor":
            _body, model, design = parse_armor_key(key)  # the body is in the subtitle
            return f"방어구 {model:03d}-{design:03d}"
        if kind == "weapon":
            wtype, model = parse_weapon_key(key)
            return f"{weapon_type_label(wtype)} 모델 {model // 10000:02d}-{model % 10000:04d}"
        if kind == "kinsect":
            return f"사냥벌레 {key}"
        if kind == "palico":
            return f"오토모 장비 {key}"
        if kind == "seikret":
            what, _, rest = key.partition("/")
            return f"세크레트 장비 {rest}" if what == "equip" else f"세크레트 모델 {rest.replace('/', '-')}"
        if kind == "npc":
            what, _, rest = key.partition("/")
            if what == "face":
                return f"NPC 얼굴 {rest}"
            if what == "hair":
                return f"NPC 머리카락 {rest}"
            return f"NPC 모델 {key}"
        if kind == "monster":
            return f"몬스터 {key.upper()}"
        if kind == "hunter":
            what, _, rest = key.partition("/")
            parts = rest.split("/")
            body = BODIES.get(parts[0], "") if parts and parts[0] in BODIES else ""
            label = {"face": "얼굴", "hair": "헤어", "eyebrow": "눈썹", "beard": "수염", "inner": "이너웨어",
                     "skin": "피부"}.get(what, what)
            tail = " ".join(p for p in parts if p not in BODIES)
            return " ".join(x for x in (label, tail, f"({body})" if body else "") if x)
        if kind == "other":
            what, _, rest = key.partition("/")
            label = {"charm": "무기 장식", "mantle": "망토", "amulet": "부적"}.get(what, what)
            return f"{label} {rest.replace('/', '-')}".strip()
    except (ValueError, IndexError):
        pass
    return f"{KIND_LABELS.get(kind, kind)} {key}"
