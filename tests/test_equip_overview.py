from types import SimpleNamespace

from mhwmm.equip import overview
from mhwmm.equip.model import Target, fallback_label


def scan(targets, redirects=(), errors=()):
    return SimpleNamespace(targets=dict(targets), redirects=list(redirects), errors=list(errors))


class FakeCatalog:
    problems = []

    def __init__(self, names):
        self._names = names

    def resolve(self, target):
        return target

    def names(self, target):
        return self._names.get((target.kind, target.key), [])

    def entry(self, target):
        return {"design": 2} if target.kind == "armor" else {}

    def rarity(self, target):
        return 3 if target.kind == "armor" else None

    def weapon_type_name(self, wtype):
        return "쌍검"


BONE_HELM = Target("armor", "f/004/001", "helm")
BONE_BODY = Target("armor", "f/004/001", "body")
DB = Target("weapon", "02/000029", "main")


def test_items_merge_mods_and_mark_conflicts():
    scans = [
        ("a", "Armor A", True, 0, scan({BONE_HELM: 3, BONE_BODY: 2})),
        ("b", "Armor B", True, 1, scan({BONE_HELM: 1})),
        ("c", "Old helm", False, 2, scan({BONE_HELM: 1})),
        ("d", "Blades", True, 3, scan({DB: 4})),
        ("e", "Script only", True, 4, scan({}, errors=["깨진 pak"])),
    ]
    catalog = FakeCatalog({("armor", "f/004/001"): ["시험", "시험α"], ("weapon", "02/000029"): ["연습검Ⅰ", "연습검Ⅱ"]})
    result = overview.build(scans, catalog)
    armor, blades = result.items
    assert armor.name == "시험 · 시험α" and armor.subtitle == "여성 · 타입 2" and armor.rarity == 3
    assert armor.changed == ["helm", "body"]
    assert [u.name for u in armor.uses] == ["Armor A", "Armor B", "Old helm"]
    assert armor.uses[0].files == 5
    conflicts = armor.conflicts()
    assert list(conflicts) == ["helm"] and [u.name for u in conflicts["helm"]] == ["Armor A", "Armor B"]  # winner last
    assert blades.name == "연습검Ⅰ 외 1" and blades.subtitle == "쌍검" and blades.group == "weapon/02"
    assert result.mods_without == ["Script only"] and result.problems == ["'Script only': 깨진 pak"]
    groups = result.groups()
    assert groups[0] == ("armor", "방어구", 1) and ("armor/f", "여성", 1) in groups and ("weapon/02", "쌍검", 1) in groups
    assert "armor b" in armor.search_text() and "시험α" in armor.search_text()


def test_without_catalog_uses_game_numbers_and_notes_redirects():
    lepus_helm = Target("armor", "f/008/001", "helm")
    scans = [("l", "Lepus", True, 0, scan({lepus_helm: 1}, redirects=[(lepus_helm, Target("armor", "f/022/000", "helm"))]))]
    item, = overview.build(scans, None).items
    assert item.name == fallback_label(item.target) == "방어구 008-001" and item.subtitle == "여성"
    assert item.notes == ["'Lepus': 머리 프리팹이 방어구 022-000 머리 모델을 씁니다"]
    assert item.changed == ["helm"] and not item.conflicts()


def test_fallback_labels():
    assert fallback_label(Target("weapon", "02/100004")) == "쌍검 모델 10-0004"
    assert fallback_label(Target("palico", "062")) == "오토모 장비 062"
    assert fallback_label(Target("monster", "em0001")) == "몬스터 EM0001"
    assert fallback_label(Target("hunter", "hair/f/0001")) == "헤어 0001 (여성)"
