"""The equipment window's content: per-mod scans and the game catalog turned into one entry
per piece of equipment, with the mods that change it."""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import (ARMOR_PARTS, BODIES, KIND_LABELS, KINDS, PART_LABELS, PART_ORDER, Target, fallback_label,
                    parse_armor_key, parse_weapon_key, weapon_type_label)


@dataclass
class ModUse:
    """One mod's files for one piece of equipment."""
    mod_id: str
    name: str
    enabled: bool
    order: int  # place in the load order: a later mod wins a file both change
    parts: dict[str, int] = field(default_factory=dict)  # part ("" = not part-specific) -> file count

    @property
    def files(self) -> int:
        return sum(self.parts.values())


@dataclass
class Item:
    target: Target  # without a part
    name: str
    names: list[str]
    subtitle: str
    group: str  # sidebar group, e.g. "armor/f", "weapon/02"
    rarity: int | None = None
    parts: tuple[str, ...] = ()  # the parts this kind of equipment has, in the game's order
    uses: list[ModUse] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        """Parts changed by an enabled mod (all mods when none is enabled), in display order."""
        uses = [u for u in self.uses if u.enabled] or self.uses
        have = {p for u in uses for p in u.parts}
        return [p for p in self.parts if p in have] + sorted(p for p in have if p not in self.parts and p)

    @property
    def enabled(self) -> bool:
        return any(u.enabled for u in self.uses)

    def conflicts(self) -> dict[str, list[ModUse]]:
        """Parts (or "" for the whole item) that more than one enabled mod changes -> those mods, the
        winner (last in the load order) last."""
        out = {}
        for part in {p for u in self.uses for p in u.parts}:
            users = sorted((u for u in self.uses if u.enabled and part in u.parts), key=lambda u: u.order)
            if len(users) > 1:
                out[part] = users
        return out

    def search_text(self) -> str:
        return " ".join([self.name, *self.names, self.subtitle, *(u.name for u in self.uses)]).lower()


@dataclass
class Overview:
    items: list[Item]
    mods_scanned: int = 0
    mods_without: list[str] = field(default_factory=list)  # names of mods that change no known equipment
    problems: list[str] = field(default_factory=list)
    catalog: object = None  # where group names come from (the game's own weapon names)

    def groups(self) -> list[tuple[str, str, int]]:
        """Sidebar entries (group key, label, count) in display order; kinds first, then sub-groups."""
        counts: dict[str, int] = {}
        for item in self.items:
            kind = item.group.split("/")[0]
            counts[kind] = counts.get(kind, 0) + 1
            if "/" in item.group:
                counts[item.group] = counts.get(item.group, 0) + 1
        out = []
        for kind in KINDS:
            if kind not in counts:
                continue
            out.append((kind, KIND_LABELS[kind], counts[kind]))
            subs = sorted(g for g in counts if g.startswith(kind + "/"))
            if len(subs) > 1 or kind in ("armor", "weapon"):
                out.extend((g, group_label(g, self.catalog), counts[g]) for g in subs)
        return out

    def subset(self, items: list[Item]) -> "Overview":
        return Overview(items, catalog=self.catalog)


def group_label(group: str, catalog=None) -> str:
    kind, _, sub = group.partition("/")
    if kind == "armor":
        return BODIES.get(sub, sub)
    if kind == "weapon" and sub.isdigit():
        return catalog.weapon_type_name(int(sub)) if catalog is not None else weapon_type_label(int(sub))
    return KIND_LABELS.get(kind, kind) if not sub else sub


def _group(target: Target) -> str:
    if target.kind == "armor":
        return "armor/" + target.key.split("/")[0]
    if target.kind == "weapon":
        return "weapon/" + target.key.split("/")[0]
    return target.kind


def _subtitle(target: Target, entry: dict | None, catalog) -> str:
    kind, key = target.kind, target.key
    entry = entry or {}
    try:
        if kind == "armor":
            body, _model, _design = parse_armor_key(key)
            bits = [BODIES.get(body, body)]
            if entry.get("design") and not entry.get("single_design"):
                bits.append(f"타입 {entry['design']}")
            return " · ".join(bits)
        if kind == "weapon":
            wtype, _model = parse_weapon_key(key)
            label = catalog.weapon_type_name(wtype) if catalog is not None else weapon_type_label(wtype)
            tree = entry.get("tree") or ""
            return " · ".join(x for x in (label, tree) if x)
        if kind == "npc":
            if key.startswith(("face/", "hair/")):
                return "NPC 얼굴" if key.startswith("face/") else "NPC 머리카락"
            if len(set(entry.get("names") or ())) > 1:
                return "NPC · 여러 NPC가 함께 쓰는 모습"
    except (ValueError, IndexError):
        pass
    return KIND_LABELS.get(kind, kind)


def describe(target: Target, catalog) -> tuple[str, list[str], dict | None]:
    """(title, all names, catalog entry) for an item."""
    entry = catalog.entry(target) if catalog is not None else None
    names = list(catalog.names(target)) if catalog is not None else []
    if not names:
        return fallback_label(target), [], entry
    if len(names) == 1:
        return names[0], names, entry
    if target.kind == "armor":
        return " · ".join(names[:3]) + (f" 외 {len(names) - 3}" if len(names) > 3 else ""), names, entry
    return f"{names[0]} 외 {len(names) - 1}", names, entry


def build(scans: list[tuple[str, str, bool, int, object]], catalog=None) -> Overview:
    """``scans``: (mod id, mod name, enabled, load-order index, ModScan) per mod."""
    items: dict[Target, Item] = {}
    without = []
    problems = []
    for mod_id, mod_name, enabled, order, scan in scans:
        found = False
        for target, count in scan.targets.items():
            if catalog is not None:
                target = catalog.resolve(target)
            key = target.item
            item = items.get(key)
            if item is None:
                name, names, entry = describe(key, catalog)
                item = items[key] = Item(key, name, names, _subtitle(key, entry, catalog), _group(key),
                                         catalog.rarity(key) if catalog is not None else None,
                                         _parts(key))
            use = next((u for u in item.uses if u.mod_id == mod_id), None)
            if use is None:
                use = ModUse(mod_id, mod_name, enabled, order)
                item.uses.append(use)
            use.parts[target.part] = use.parts.get(target.part, 0) + count
            found = True
        for source, dest in scan.redirects:
            if catalog is not None:
                source, dest = catalog.resolve(source), catalog.resolve(dest)
            item = items.get(source.item)
            if item is not None and dest.item != source.item:
                where = PART_LABELS.get(source.part, "")
                to, _names, entry = describe(dest.item, catalog)
                if to == item.name:  # the same set in another design or body
                    to += f"({_subtitle(dest.item, entry, catalog)})"
                to_part = PART_LABELS.get(dest.part, "")
                note = f"'{mod_name}': {where + ' ' if where else ''}프리팹이 {to}{' ' + to_part if to_part else ''} 모델을 씁니다"
                if note not in item.notes:
                    item.notes.append(note)
        for error in scan.errors:
            problems.append(f"'{mod_name}': {error}")
        if not found:
            without.append(mod_name)
    ordered = sorted(items.values(), key=_sort_key)
    for item in ordered:
        item.uses.sort(key=lambda u: u.order)
    return Overview(ordered, len(scans), without, problems, catalog)


def _parts(target: Target) -> tuple[str, ...]:
    if target.kind == "armor":
        return ARMOR_PARTS[:5]  # the slinger shows only when a mod changes it (see Item.changed)
    return PART_ORDER.get(target.kind, ())


def _sort_key(item: Item):
    kind = item.target.kind
    rank = KINDS.index(kind) if kind in KINDS else len(KINDS)
    try:
        if kind == "armor":
            body, model, design = parse_armor_key(item.target.key)
            return rank, item.group, model, design, item.name
        if kind == "weapon":
            wtype, model = parse_weapon_key(item.target.key)
            return rank, f"weapon/{wtype:02d}", model, 0, item.name
    except (ValueError, IndexError):
        pass
    return rank, item.group, 0, 0, item.name
