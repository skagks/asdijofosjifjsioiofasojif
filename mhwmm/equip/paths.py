"""Which equipment a game path belongs to.

``classify`` reads only the path: the folder a file lives in names the model, the way the game
lays its files out (natives/STM/ and streaming/ prefixes, case and version suffixes do not
matter).  Keys follow model.py; where the table there leaves a detail open:

- armor parts come from the part digit folder (Art/Model) or the part folder (_Prefab); files
  directly in a prefab design folder (``MMM_SSS_avp.user``) have part "", SlingerRope prefabs
  inside a design folder are the slinger.  Model 002 is the innerwear: hunter ``inner/<b>/<SSS>``.
- loose-file armor mods that use a custom character code mh02/mh03 count as ch02/ch03.
- weapon parts come from the file name: ``it<TT><SS>_<NNNN>_0*`` main, ``_1*`` sub, else "".
  The it<TT>/99 test folder and the WeaponConst/Gimmick data of palico weapons are not
  equipment looks (None).
- hunter: face ch00/000-499 (``face/<NNN>``), hair/eyebrow/beard ch01/<000|001>/<K>/<NNN>
  (``hair|eyebrow|beard/<m|f>/<K><NNN>`` for K 0, 1, 2), body skin = everything under
  ch02|ch03/CommonTextures (skin, hands, innerwear and cage textures) and
  ch00/CommonTextures/FaceSkin/<m|f> (``skin/<m|f>``).  Other shared textures are None.
- npc: faces ch00/500-999 (``face/<NNN>``), hair ch01/500-999 (``hair/<NNN>``, every style of
  that group), bodies ch04/<A>/<BB>/<CC>/<DD> (``<ABB>_<CCDD>``); prefabs in
  GameDesign/NPC/_Prefab/Model/<group>/ by their file name.
- palico: only the slot folders 0000/1000/2000 (ch05/000/0001 fur and 9000 skeleton are None);
  Otomo<Head|Body>PartsEnableData_NNN.user count as the helm/body.
- seikret: ch07/<GGG>/<NNNN> models (``model/<GGG>/<NNNN>``), Porter/_Prefab/Equip/<NNN>/
  Body|Saddle_<NNN>.pfb (``equip/<NNN>``).
- other: weapon charms it48/<SS>/<NNNN> and _Prefab/Charm/<SS>/<NNNN> (``charm/<SS>/<NNNN>``),
  mantles it52/<NN> and _Prefab/Mantle/52/<NN> (``mantle/<NN>``), the amulet it50/15/<NNNN>
  and _Prefab/Amulet/it5015_<NNNN>_<k> (``amulet/<NNNN>``).
"""

from __future__ import annotations

import re

from .model import ARMOR_DIGITS, PALICO_SLOTS, Target, armor_key, weapon_key

_PREFIX = re.compile(r"^[@/]*(?:natives/stm/)?(?:streaming/)?")
# .mesh.241111606, .sbnk.1.x64, .motlist.992.en, .tex.241106027.x64.ja ...  The language codes are
# listed so that a name without a version keeps its extension ("hat.001.mdf2" is not "hat").
_LANGUAGES = ("ja|en|fr|it|de|es|es419|ru|pl|nl|pt|ptbr|ko|zhtw|zhcn|fi|sv|da|no|cs|hu|sk|ar|tr|bu|gr|ro|th|uk|"
              "vi|id|fc|hi|ms")
_VERSION = re.compile(rf"(?:\.\d+)+(?:\.(?:x64|stm))?(?:\.(?:{_LANGUAGES}))?$")

BODY_CH = {"2": "m", "3": "f"}  # ch02 / ch03 (and mh02 / mh03)
BODY_PREFAB = {"male": "m", "female": "f"}
BODY_HAIR = {"000": "m", "001": "f"}  # ch01/<000|001>
INNER_MODEL = 2  # armor model 002 is the innerwear
NPC_FROM = 500  # ch00 / ch01 groups from 500 up belong to NPCs


def normalize(path: str) -> str:
    """``natives/STM/streaming/Art/X.mesh.241111606`` -> ``art/x.mesh``: forward slashes, lower
    case, without the natives/stm/ and streaming/ prefix (or a resource string's leading @) and
    without the version, platform and language suffixes."""
    p = _PREFIX.sub("", path.replace("\\", "/").strip().lower(), count=1)
    return _VERSION.sub("", p)


# ---- builders --------------------------------------------------------------------------
def _armor(body: str, model: str, design: str, part: str) -> Target:
    if int(model) == INNER_MODEL:
        return Target("hunter", f"inner/{body}/{design}")
    return Target("armor", armor_key(body, int(model), int(design)), part)


def _face(group: str) -> Target:
    return Target("hunter" if int(group) < NPC_FROM else "npc", f"face/{group}")


def _hair(group: str, k: str, number: str) -> Target | None:
    body = BODY_HAIR.get(group)
    if body:
        return Target("hunter", f"{('hair', 'eyebrow', 'beard')[int(k)]}/{body}/{k}{number}")
    return Target("npc", f"hair/{group}") if int(group) >= NPC_FROM else None


_WEAPON_PART = re.compile(r"^it\d+_\d+_(\d)(?:[._]|$)")


def _weapon_part(name: str) -> str:
    m = _WEAPON_PART.match(name)
    return {"0": "main", "1": "sub"}.get(m.group(1), "") if m else ""


def _weapon(wtype: str, band: str, number: str, name: str) -> Target:
    if wtype == "10" and band == "03":
        return Target("kinsect", number)
    return Target("weapon", weapon_key(int(wtype), int(band) * 10000 + int(number)), _weapon_part(name))


# ---- Art/Model/Character/ ------------------------------------------------------------------
_CHARACTER = [
    (re.compile(r"[cm]h0([23])/(\d{3})/(\d{3})/([1-6])/"),
     lambda m, name: _armor(BODY_CH[m[1]], m[2], m[3], ARMOR_DIGITS[m[4]])),
    (re.compile(r"ch0([23])/commontextures/"), lambda m, name: Target("hunter", f"skin/{BODY_CH[m[1]]}")),
    (re.compile(r"ch00/commontextures/faceskin/([mf])/"), lambda m, name: Target("hunter", f"skin/{m[1]}")),
    (re.compile(r"ch00/(\d{3})/"), lambda m, name: _face(m[1])),
    (re.compile(r"ch01/(00[01])/([0-2])/(\d{3})/"), lambda m, name: _hair(m[1], m[2], m[3])),
    (re.compile(r"ch01/([5-9]\d\d)/"), lambda m, name: Target("npc", f"hair/{m[1]}")),
    (re.compile(r"ch04/(\d)/(\d\d)/(\d\d)/(\d\d)/"), lambda m, name: Target("npc", f"{m[1]}{m[2]}_{m[3]}{m[4]}")),
    (re.compile(r"ch05/(\d{3})/([012])000/"), lambda m, name: Target("palico", m[1], PALICO_SLOTS[m[2]])),
    (re.compile(r"ch07/(\d{3})/(\d{4})/"), lambda m, name: Target("seikret", f"model/{m[1]}/{m[2]}")),
    (re.compile(r"ch9([01])/(\d{3})/"), lambda m, name: Target("monster", f"em{m[1]}{m[2]}")),
]

# ---- Art/Model/Item/ -------------------------------------------------------------------------
_ITEM = [
    (re.compile(r"it(0\d|1[0-3])/(\d\d)/(\d{4})/"), lambda m, name: _weapon(m[1], m[2], m[3], name)),
    (re.compile(r"it48/(\d\d)/(\d{4})/"), lambda m, name: Target("other", f"charm/{m[1]}/{m[2]}")),
    (re.compile(r"it50/15/(\d{4})/"), lambda m, name: Target("other", f"amulet/{m[1]}")),
    (re.compile(r"it52/(\d\d)/"), lambda m, name: Target("other", f"mantle/{m[1]}")),
]

# ---- GameDesign/Equip/_Prefab/ -----------------------------------------------------------------
_EQUIP_PREFAB = [
    (re.compile(r"armor/(male|female)/(\d{3})/(\d{3})/(arm|body|helm|leg|waist|slinger)/[^/]+$"),
     lambda m, name: _armor(BODY_PREFAB[m[1]], m[2], m[3], m[4])),
    (re.compile(r"armor/(male|female)/(\d{3})/(\d{3})/slingerrope[^/]*$"),
     lambda m, name: _armor(BODY_PREFAB[m[1]], m[2], m[3], "slinger")),
    (re.compile(r"armor/(male|female)/(\d{3})/(\d{3})/[^/]+$"),
     lambda m, name: _armor(BODY_PREFAB[m[1]], m[2], m[3], "")),
    (re.compile(r"weapon/wp(0\d|1[0-3])/(\d\d)/(\d{4})/[^/]+$"), lambda m, name: _weapon(m[1], m[2], m[3], name)),
    (re.compile(r"charm/(\d\d)/(\d{4})/[^/]+$"), lambda m, name: Target("other", f"charm/{m[1]}/{m[2]}")),
    (re.compile(r"amulet/it5015_(\d{4})_\d+\.pfb$"), lambda m, name: Target("other", f"amulet/{m[1]}")),
    (re.compile(r"mantle/52/(\d\d)/[^/]+$"), lambda m, name: Target("other", f"mantle/{m[1]}")),
    (re.compile(r"(?:hair|eyebrow|beard)/ch01_(\d{3})_(\d)(\d{3})\.pfb$"), lambda m, name: _hair(m[1], m[2], m[3])),
    (re.compile(r"face/ch00_(\d{3})_\d{4}\.pfb$"), lambda m, name: _face(m[1])),
]

_OTOMO_PARTS = {"head": "helm", "body": "body"}
_OTOMO_PREFAB = [
    (re.compile(r"(\d{3})/ch05_\d{3}_([012])000\.pfb$"), lambda m, name: Target("palico", m[1], PALICO_SLOTS[m[2]])),
    (re.compile(r"(\d{3})/otomo(head|body)partsenabledata_?\d{3}\.user$"),
     lambda m, name: Target("palico", m[1], _OTOMO_PARTS[m[2]])),
]

_PORTER_PREFAB = [
    (re.compile(r"(\d{3})/(body|saddle)_\d{3}\.pfb$"), lambda m, name: Target("seikret", f"equip/{m[1]}", m[2])),
]

_NPC_PREFAB = [
    (re.compile(r"[^/]+/ch04_(\d{3})_(\d{4})(?:_[^/]*)?\.pfb$"), lambda m, name: Target("npc", f"{m[1]}_{m[2]}")),
    (re.compile(r"[^/]+/ch00_(\d{3})_\d{4}\.pfb$"), lambda m, name: _face(m[1])),
    (re.compile(r"[^/]+/ch01_(\d{3})_(\d)(\d{3})\.pfb$"), lambda m, name: _hair(m[1], m[2], m[3])),
]

_AREAS = (
    ("art/model/character/", _CHARACTER),
    ("art/model/item/", _ITEM),
    ("gamedesign/equip/_prefab/", _EQUIP_PREFAB),
    ("gamedesign/otomo/_prefab/equip/", _OTOMO_PREFAB),
    ("gamedesign/porter/_prefab/equip/", _PORTER_PREFAB),
    ("gamedesign/npc/_prefab/model/", _NPC_PREFAB),
)


def classify(path: str) -> Target | None:
    """The equipment (or other model) a game file belongs to, from its path alone; None for
    anything else (UI, sounds, stages, shared files...)."""
    p = normalize(path)
    for prefix, rules in _AREAS:
        if p.startswith(prefix):
            rest = p[len(prefix):]
            name = rest.rpartition("/")[2]
            for rx, build in rules:
                m = rx.match(rest)
                if m:
                    return build(m, name)
            return None
    return None


_NPC_VISUAL = re.compile(r"^gamedesign/npc/character/[^/]+/[^/]+/data/[^/]+_visualsetting\.user$")


def is_npc_visual_setting(path: str) -> bool:
    """GameDesign/NPC/Character/<group>/<NPC id>/Data/*_VisualSetting.user: which models an NPC wears."""
    return bool(_NPC_VISUAL.match(normalize(path)))
