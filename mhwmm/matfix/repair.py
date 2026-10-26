"""Find a mod's stale materials and rebuild them against the current game.

A material is stale when the current material it is compared with has a texture slot,
shader parameter or gpbf slot the mod's lacks (MHWmodfixer's rule: any other difference -
fewer fields on the game's side, another shader spelling - is not a reason to touch a
working file).  What it is compared with depends on the match:

* The very material the mod copied - the same name in the current version of the mod's
  own file, or the one same-named candidate the donor search found - is compared
  directly: a slot an update gave that material is what the mod lacks, whatever other
  materials of its shader look like.
* A donor picked from other materials is one of many sharing the shader (121 for the
  hunter's base body), so it alone does not decide: the material is stale only when
  every current material using that shader (the current versions of the mod's own files
  and the whole-game index; a "_NoMultiBlend" sibling's when the match was one) has a
  slot it lacks.  The note names the closest of them.  Each is compared as a whole, so
  slots missing from different materials do not add up to a combination none has.
* Without the whole-game index the donor alone is the reference, as in MHWmodfixer.

A material left as shipped because no current material uses its shader (no donor) is listed
with that shader when the current game does not have the shader file itself either: it is one
another mod puts in (a shader pack) or one that is missing, which the caller tells apart
(``FileResult.shaders``).

What the game's paks hold counts as the current game (game.discover_paks: up to the first
gap of a patch chain, without the PAKs an older version of the manager put in the chain),
so a mod PAK another program numbered right after the game's own patches is read as a
game file too.  A material
named just "null" is what mods give the parts they hide and is left as shipped.  A stale
material is rebuilt from the donor that was picked.

Rebuilt materials take their structure from the current game's material and everything
the author chose from the mod: texture paths, shader parameter values, render state
(alpha / two-sided), gpbf buffers and the material name.  A loose file whose version
number is outdated only gets the new number when its content already fits the current
layout.

Output is patched in place in a copy of the current file when the mod's materials
are exactly that file's materials (same names); otherwise it is assembled from the
rebuilt materials.  Either way it is parsed again and compared with what was meant
to be written before it is used.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import MatfixError, rsz
from .donors import SAME_FILE_NAME, candidate_paths, find_donor
from .game import GameIndex, ShaderIndex
from .mdf2 import Mdf2File, assemble_mdf2, detect_version, extract_material, version_from_name
from .pak import PakFile, compress_as, path_hash, write_pak

MDF2_RE = re.compile(r"\.mdf2\.(\d+)$", re.IGNORECASE)
RSZ_RE = re.compile(r"\.(user|pfb|scn)\.(\d+)$", re.IGNORECASE)
SAMPLE_PFB = 0x93538AED5435EFA9  # a game pfb MHWmodfixer checks its type registry against
CURRENT_VERSION_HINT = 45  # what the game uses now; tried first when a pak entry has no name
# .mmtr (shader) version numbers of the game's file list (REasy MHWS_STM.list; the paks store hashes only)
MMTR_VERSIONS = (240906211, 250206176)
SHADER_PROBE = "MaterialShader/Variation/Base_Equip.mmtr"  # a shader of every game version (most armour uses it)

# A slot that is new to the mod gates an optional feature the mod never used, and the
# donor's own texture there was made for another model's UVs: switch it off the way the
# game itself does instead (MHWmodfixer #29/#48 - texture bleed, and a crash).
NULL_MASK = "MasterMaterial/Textures/NullBlack_Alpha_MSK4.tex"
NULL_DETAIL_ALBD = "systems/rendering/NullGray.tex"
NULL_DETAIL_NRRH = "systems/rendering/NullNormalRoughnessOcclusion.tex"


def _is_null(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower().startswith("null")


def _inert_default(slot: str) -> str | None:
    low = slot.lower()
    if "mask" in low:
        return NULL_MASK
    if low.startswith("detail_albd"):
        return NULL_DETAIL_ALBD
    if low.startswith("detail_nrrh"):
        return NULL_DETAIL_NRRH
    return None


# Material names that mark a part the mod hides (its mesh still names a material, so the
# .mdf2 keeps one). Only the bare name, in any case: "null_mat", "Null01" and the like can
# be real, visible materials, and the slot rule below already covers them.
PLACEHOLDER_NAMES = frozenset({"null"})
NOTE_NAMES = 6  # slot names a note lists before "외 N개"


def is_placeholder(name: str) -> bool:
    return name.strip().casefold() in PLACEHOLDER_NAMES


def _gpbf_slots(mat: dict) -> set[str]:
    return {e[0] for e in mat["gpbf_entries"][:mat["gpbf_split"][0]]}


@dataclass(frozen=True)
class Slots:
    """What the staleness rule reads of a current material: texture types, shader parameters,
    gpbf slots and the gpbf split."""
    textures: frozenset
    props: frozenset
    gpbf: frozenset
    split: tuple

    @classmethod
    def of(cls, mat: dict) -> "Slots":
        return cls(frozenset(t["type"] for t in mat["textures"]), frozenset(p["name"] for p in mat["props"]),
                   frozenset(_gpbf_slots(mat)), tuple(mat["gpbf_split"]))


def missing_slots(mod_mat: dict, current) -> tuple[list[str], list[str], list[str]]:
    """(texture types, shader parameters, gpbf slots) the current material (or its Slots) has and
    ``mod_mat`` lacks. gpbf slots count only when the gpbf splits differ (MHWmodfixer's rule)."""
    ref = current if isinstance(current, Slots) else Slots.of(current)
    tex = ref.textures - {t["type"] for t in mod_mat["textures"]}
    props = ref.props - {p["name"] for p in mod_mat["props"]}
    gpbf = set() if tuple(mod_mat["gpbf_split"]) == ref.split else ref.gpbf - _gpbf_slots(mod_mat)
    return sorted(tex, key=str.casefold), sorted(props, key=str.casefold), sorted(gpbf, key=str.casefold)


def needs_fix(mod_mat: dict, donor: dict) -> bool:
    """True when the current material has a slot the mod's lacks (something to gain)."""
    return any(missing_slots(mod_mat, donor))


@dataclass(frozen=True)
class References:
    """The current materials using one shader: how many there are and their distinct slot sets,
    each with the first material that has it (file, material name)."""
    count: int
    kinds: tuple  # ((Slots, file, name), ...)

    def closest(self, mod_mat: dict, first=None) -> tuple[tuple[list[str], list[str], list[str]], str, str]:
        """(missing slots, file, material name) of the one ``mod_mat`` lacks the fewest slots of (nothing
        missing: the mod's material is one the current game has). ``first`` (Slots, file, name) - the
        donor - is looked at first and so wins a tie."""
        best = None
        for slots, src, name in ([first] if first else []) + list(self.kinds):
            missing = missing_slots(mod_mat, slots)
            size = sum(map(len, missing))
            if best is None or size < best[0]:
                best = (size, missing, src, name)
                if not size:
                    break
        return best[1], best[2], best[3]


@lru_cache(maxsize=1 << 16)
def _file_key(path: str) -> int:
    """A game file as the paks key it (the whole-game index lists one under its path, a mod PAK's
    current entry has a "<pak>#<hash>" label)."""
    return path_hash(path)


def slot_list(missing: tuple[list[str], list[str], list[str]]) -> str:
    """"텍스처 A, B · 파라미터 C": at most NOTE_NAMES names, the rest as counts."""
    parts, budget = [], NOTE_NAMES
    for label, names in zip(("텍스처", "파라미터", "gpbf 버퍼"), missing):
        if not names:
            continue
        shown = names[:max(budget, 0)]
        budget -= len(shown)
        if not shown:
            parts.append(f"{label} {len(names)}개")
            continue
        more = f" 외 {len(names) - len(shown)}개" if len(names) > len(shown) else ""
        parts.append(f"{label} {', '.join(shown)}{more}")
    return " · ".join(parts)


def _gpbf_paths(mat: dict) -> dict[str, str]:
    """gpbf slot name -> buffer path (slot names first, then paths in the same order)."""
    n = mat["gpbf_split"][0]
    names, paths = mat["gpbf_entries"][:n], mat["gpbf_entries"][n:]
    return {a[0]: b[0] for a, b in zip(names, paths)} if len(names) == len(paths) else {}


def apply_overrides(donor: dict, mod_mat: dict, notes: list[str]) -> dict:
    """The donor's structure with the mod's choices put back."""
    new = copy.deepcopy(donor)
    new["name"] = mod_mat["name"]  # names must stay unique per file; donors reuse "lambert2" etc.
    new["shading_type"] = mod_mat["shading_type"]
    new["alpha_flags_raw"] = mod_mat["alpha_flags_raw"]
    mod_tex = {t["type"]: t["path"] for t in mod_mat["textures"]}
    for t in new["textures"]:
        path = mod_tex.get(t["type"])
        if path is not None:
            t["path"] = path
        elif not _is_null(t["path"]):
            inert = _inert_default(t["type"])
            if inert is not None:
                t["path"] = inert
    dropped = sorted(set(mod_tex) - {t["type"] for t in new["textures"]})
    if dropped:
        notes.append(f"재질 '{mod_mat['name']}': 현재 게임에 없는 텍스처 칸 {', '.join(dropped)}은(는) 뺐습니다")
    mod_props = {p["name"]: p["values"] for p in mod_mat["props"]}
    for p in new["props"]:
        values = mod_props.get(p["name"])
        if values is not None and len(values) == len(p["values"]):
            p["values"] = list(values)
    mod_gpbf = _gpbf_paths(mod_mat)
    n = new["gpbf_split"][0]
    names, paths = new["gpbf_entries"][:n], new["gpbf_entries"][n:]
    if mod_gpbf and len(names) == len(paths):
        for i, (slot, _h1, _h2) in enumerate(names):
            path = mod_gpbf.get(slot)
            if path is not None:
                paths[i] = (path, paths[i][1], paths[i][2])
        new["gpbf_entries"] = names + paths
    return new


@dataclass
class MaterialPlan:
    mod: dict
    donor: dict | None = None
    donor_file: str | None = None
    match: str | None = None
    stale: bool = False
    # slots the mod's material lacks of the current material it was compared with (the donor, or
    # the closest same-shader material of the current game) and which one that was
    missing: tuple = ((), (), ())
    reference: str = ""
    placeholder: bool = False  # a hidden part's "null" material: never stale for its slots
    picks: list[str] = field(default_factory=list)  # how the donor was chosen (said only when it is used)

    def notes(self) -> list[str]:
        name = self.mod["name"]
        if self.stale:
            out = list(self.picks)
            if any(self.missing):
                out.append(f"재질 '{name}': {self.reference}: {slot_list(self.missing)}")
            return out
        if self.placeholder and any(self.missing):
            return [f"숨김용 재질 '{name}'은 그대로 둡니다"]
        return []


@dataclass
class EntryPlan:
    """One .mdf2: a loose file or an entry inside a mod's pak."""
    label: str
    mod_bytes: bytes
    mod_version: int
    donor_path: str | None
    donor_bytes: bytes | None
    donor_version: int | None
    materials: list[MaterialPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rename_only: bool = False  # content fits the current layout; only the version number is old
    error: str = ""
    donor_hash: int | None = None  # the current file's path hash when donor_path is a label (a mod PAK's entry)

    @property
    def unresolved(self) -> list[str]:
        return [m.mod["name"] for m in self.materials if m.donor is None]

    @property
    def unresolved_shaders(self) -> list[tuple[str, str]]:
        """(material, shader) of the materials left as shipped."""
        return [(m.mod["name"], m.mod["mmtr_path"]) for m in self.materials if m.donor is None]

    @property
    def stale(self) -> list[MaterialPlan]:
        return [m for m in self.materials if m.donor is not None and m.stale]

    @property
    def needs_work(self) -> bool:
        return not self.error and (bool(self.stale) or self.rename_only)

    @property
    def out_version(self) -> int:
        if self.donor_version:
            return self.donor_version
        for m in self.materials:
            if m.donor_file and MDF2_RE.search(m.donor_file):
                return version_from_name(m.donor_file)
        return self.mod_version


def plan_entries(units: list[EntryPlan], whole_game, progress=None) -> None:
    """Match every material of ``units`` (one mod's .mdf2 files) to current ones."""
    progress = progress or (lambda done, total, msg: None)
    pool: list[tuple[str, dict]] = []
    owns = []
    for unit in units:
        own = []
        if unit.donor_bytes is not None and unit.donor_version:
            try:
                donor = Mdf2File(unit.donor_bytes, unit.donor_version)
                own = [(unit.donor_path or unit.label, extract_material(donor, i)) for i in range(len(donor.materials))]
            except Exception as exc:  # the game's own file did not parse: no own donor for this one
                unit.notes.append(f"현재 게임 파일을 읽지 못했습니다: {exc}")
                unit.donor_bytes = unit.donor_version = None
        owns.append(own)
        pool.extend(own)
    keys = {src: unit.donor_hash if unit.donor_hash is not None else _file_key(src)
            for unit, own in zip(units, owns) for src, _mat in own}
    shared: dict[str, References | None] = {}

    def same_shader(mmtr: str) -> References | None:
        """Every current material using ``mmtr``: this mod's current files and the whole game, each once."""
        if mmtr not in shared:
            seen, kinds = set(), {}
            for src, mat in [c for c in pool if c[1]["mmtr_path"] == mmtr] + list(whole_game(mmtr)):
                key = (keys[src] if src in keys else _file_key(src), mat["name"])
                if key not in seen:  # (the mod's own current files are in both)
                    seen.add(key)
                    kinds.setdefault(Slots.of(mat), (src, mat["name"]))
            shared[mmtr] = References(len(seen), tuple((k, *v) for k, v in kinds.items())) if seen else None
        return shared[mmtr]

    for i, (unit, own) in enumerate(zip(units, owns)):
        progress(i, len(units), f"재질 비교 중... {i + 1}/{len(units)}")
        try:
            mod = Mdf2File(unit.mod_bytes, unit.mod_version)
            for j in range(len(mod.materials)):
                mod_mat = extract_material(mod, j)
                picks: list[str] = []
                hit = find_donor(mod_mat, own, pool, whole_game, picks)
                if hit is None:
                    unit.materials.append(MaterialPlan(mod_mat))
                    continue
                donor, src, how = hit
                plan = MaterialPlan(mod_mat, donor, src, how, picks=picks,
                                    placeholder=is_placeholder(mod_mat["name"]))
                plan.missing = missing_slots(mod_mat, donor)
                plan.reference = f"현재 게임의 재질({src}의 '{donor['name']}')에 있는 칸이 없습니다"
                # The material the mod copied (its name in the current version of the mod's own file, or the
                # one same-named candidate) decides by itself. A donor picked from others is one of many with
                # its shader: stale only when no current material of the shader has all the mod's slots
                # (looked at, and the whole-game index built, only when the donor alone says stale - the
                # donor is one of them).
                copied = how == SAME_FILE_NAME or (donor["name"] == mod_mat["name"] and not picks)
                refs = same_shader(donor["mmtr_path"]) if any(plan.missing) and not copied \
                    and whole_game is not None else None
                if refs is not None:  # (the shader the match used: a "_NoMultiBlend" sibling's, when it was one)
                    plan.missing, ref_file, ref_name = refs.closest(mod_mat, (Slots.of(donor), src, donor["name"]))
                    plan.reference = (
                        f"현재 게임의 같은 셰이더 재질 {refs.count}개 중 가장 가까운 재질({ref_file}의 '{ref_name}')에도 "
                        "있는 칸이 없습니다" if refs.count > 1 else
                        f"현재 게임에서 같은 셰이더를 쓰는 유일한 재질({ref_file}의 '{ref_name}')에 있는 칸이 없습니다")
                plan.stale = any(plan.missing) and not plan.placeholder
                unit.materials.append(plan)
        except Exception as exc:
            unit.error, unit.materials = f"재질 파일을 읽지 못했습니다: {exc}", []
            continue
        if not unit.stale and unit.donor_version and unit.mod_version != unit.donor_version:
            unit.rename_only = _fits_layout(unit.mod_bytes, unit.mod_version, unit.donor_version)
            if not unit.rename_only:  # the layout itself changed: rebuild everything that has a donor
                if any(m.donor is not None for m in unit.materials):
                    unit.notes.append(f"재질 파일 형식이 바뀌어(.mdf2.{unit.mod_version} → .mdf2.{unit.donor_version}) "
                                      "같은 셰이더를 찾은 재질을 모두 현재 게임의 재질로 다시 만듭니다")
                for m in unit.materials:
                    m.stale = m.donor is not None
        for m in unit.materials:
            unit.notes.extend(m.notes())


def _fits_layout(data: bytes, old: int, new: int) -> bool:
    try:
        a, b = Mdf2File(data, old), Mdf2File(data, new)
        return [extract_material(a, i) for i in range(len(a.materials))] == \
            [extract_material(b, i) for i in range(len(b.materials))]
    except Exception:
        return False


def _comparable(mat: dict) -> dict:
    """What the game sees of a material, floats as their stored bytes (NaN != NaN)."""
    import struct

    out = {k: v for k, v in mat.items() if k != "header_version"}
    out["props"] = [(p["name"], struct.pack(f"<{len(p['values'])}f", *p["values"]), p.get("count_flags", 0))
                    for p in mat["props"]]
    return out


def build_entry(plan: EntryPlan) -> tuple[bytes, int]:
    """Bytes of the repaired .mdf2 and its layout version."""
    version = plan.out_version
    if plan.rename_only:
        return plan.mod_bytes, version
    wanted = [apply_overrides(m.donor, m.mod, plan.notes) if m.donor is not None and m.stale else m.mod
              for m in plan.materials]
    data = _patch_in_place(plan, wanted)
    how = "in-place"
    if data is None:
        data = assemble_mdf2(wanted, version)
        how = "assembled"
    check = Mdf2File(data, version)
    got = [extract_material(check, i) for i in range(len(check.materials))]
    if [_comparable(g) for g in got] != [_comparable(w) for w in wanted]:
        if how == "in-place":  # should not happen; the assembled file is the reference
            data = assemble_mdf2(wanted, version)
            check = Mdf2File(data, version)
            got = [extract_material(check, i) for i in range(len(check.materials))]
        if [_comparable(g) for g in got] != [_comparable(w) for w in wanted]:
            raise MatfixError(f"{plan.label}: 고친 재질 파일을 다시 읽어 보니 내용이 다릅니다")
    return data, version


def _patch_in_place(plan: EntryPlan, wanted: list[dict]) -> bytes | None:
    """Patch a copy of the current file when the mod's materials are exactly its materials
    (same names, same order) and every one of them came from it: everything the game
    added stays byte-identical, including the blocks the rebuild path leaves out."""
    if plan.donor_bytes is None or not plan.donor_version:
        return None
    donor = Mdf2File(plan.donor_bytes, plan.donor_version)
    names = [m.name.text for m in donor.materials]
    if names != [w["name"] for w in wanted] or len(set(names)) != len(names):
        return None
    if any(m.donor is None or m.donor_file != (plan.donor_path or plan.label) or m.donor["name"] != m.mod["name"]
           for m in plan.materials):
        return None
    for mat, want in zip(donor.materials, wanted):
        current = extract_material(donor, mat.index)
        if [t["type"] for t in current["textures"]] != [t["type"] for t in want["textures"]] \
                or [(p["name"], len(p["values"])) for p in current["props"]] != \
                [(p["name"], len(p["values"])) for p in want["props"]] \
                or [e[0] for e in current["gpbf_entries"][:current["gpbf_split"][0]]] != \
                [e[0] for e in want["gpbf_entries"][:want["gpbf_split"][0]]] \
                or current["mmtr_path"] != want["mmtr_path"]:
            return None  # a material that kept the mod's own structure: assemble instead
        for (_kind, path), t in zip(mat.textures, want["textures"]):
            donor.set_string(path, t["path"])
        for i, p in enumerate(want["props"]):
            donor.set_prop_values(mat, i, p["values"])
        donor.set_render_state(mat, want["shading_type"], want["alpha_flags_raw"])
        n = want["gpbf_split"][0]
        for ref, entry in zip(mat.gpbf[n:], want["gpbf_entries"][n:]):
            donor.set_string(ref, entry[0])
    return donor.to_bytes()


# ---- whole mods ------------------------------------------------------------------
@dataclass
class FileResult:
    key: str  # the caller's name for the file
    rel: str  # game path of a loose file, or the pak's path
    kind: str  # "mdf2" | "user" | "pfb" | "scn" | "pak"
    state: str  # "current" | "fixable" | "unresolved" | "stuck" | "error"
    new_rel: str | None = None  # where the repaired file goes (the version number may change)
    file: Path | None = None  # the repaired file, once written
    fixed: list[str] = field(default_factory=list)  # material names rebuilt
    unresolved: list[str] = field(default_factory=list)  # materials left as shipped (no current shader)
    notes: list[str] = field(default_factory=list)
    error: str = ""
    # RSZ files (.user/.pfb/.scn): the loose file itself, or each such entry of a pak:
    # {"label", "kind", "method" ("CRC 갱신" | "재구성" | "원본 교체" | "버전 번호"), "reason" (when stuck)}
    rsz: list[dict] = field(default_factory=list)
    # (material, shader as materials name it) of the unresolved materials whose shader file the current
    # game does not have: it has to come from another mod (or is missing)
    shaders: list[tuple[str, str]] = field(default_factory=list)

    @property
    def stuck(self) -> list[dict]:
        """RSZ files that no longer fit the game and could not be repaired."""
        return [r for r in self.rsz if r.get("reason")]

    def to_dict(self) -> dict:
        return {"key": self.key, "rel": self.rel, "kind": self.kind, "state": self.state,
                "new_rel": self.new_rel, "fixed": list(self.fixed), "unresolved": list(self.unresolved),
                "notes": list(self.notes), "error": self.error, "rsz": [dict(r) for r in self.rsz],
                "shaders": [[name, mmtr] for name, mmtr in self.shaders]}

    @classmethod
    def from_dict(cls, data: dict) -> "FileResult | None":
        try:
            return cls(str(data["key"]), str(data["rel"]), str(data["kind"]), str(data["state"]),
                       data.get("new_rel") or None, None,
                       [str(x) for x in data.get("fixed") or []], [str(x) for x in data.get("unresolved") or []],
                       [str(x) for x in data.get("notes") or []], str(data.get("error") or ""),
                       [{k: str(v) for k, v in r.items()} for r in data.get("rsz") or [] if isinstance(r, dict)],
                       [(str(x[0]), str(x[1])) for x in data.get("shaders") or []
                        if isinstance(x, (list, tuple)) and len(x) == 2])
        except (KeyError, TypeError, AttributeError):
            return None


def shader_file(mmtr: str, version: int) -> str:
    """The game path of a material's shader (materials name it under natives/STM/, without the version)."""
    path = mmtr.replace("\\", "/").strip("/")
    return f"natives/STM/{path}.{version}"


class GameShaders:
    """Whether the current game's paks have a shader file. They store hashes only, so the file is looked
    for under the known version numbers: unknown (None) when the game has none of them for a shader
    every version has (it numbers them some other way now)."""

    def __init__(self, game: GameIndex):
        self.game = game
        self._versions: tuple[int, ...] | None = None
        self._has: dict[str, bool] = {}

    @property
    def versions(self) -> tuple[int, ...]:
        if self._versions is None:
            self._versions = tuple(v for v in MMTR_VERSIONS if self.game.has(shader_file(SHADER_PROBE, v)))
        return self._versions

    def has(self, mmtr: str) -> bool | None:
        if not self.versions:
            return None
        if mmtr not in self._has:
            self._has[mmtr] = any(self.game.has(shader_file(mmtr, v)) for v in MMTR_VERSIONS)
        return self._has[mmtr]

    def foreign(self, pairs) -> list[tuple[str, str]]:
        """The (material, shader) pairs whose shader the game surely does not have."""
        return [(name, mmtr) for name, mmtr in pairs if mmtr and self.has(mmtr) is False]


def _loose_unit(rel: str, data: bytes, game: GameIndex) -> EntryPlan:
    m = MDF2_RE.search(rel)
    version = int(m.group(1))
    base = rel[:m.start()]
    donor_path = donor_bytes = donor_version = None
    for candidate in candidate_paths(base):
        found = game.read_versioned(candidate, "mdf2", prefer=(version, CURRENT_VERSION_HINT))
        if found is not None:
            donor_path, donor_bytes = found
            donor_version = version_from_name(donor_path)
            break
    return EntryPlan(rel, data, version, donor_path, donor_bytes, donor_version)


def is_material_file(rel: str) -> bool:
    """Files check_mod looks at: loose .mdf2/.user/.pfb/.scn and PAKs for the pak_mods folder."""
    rel = rel.replace("\\", "/")
    return bool(MDF2_RE.search(rel) or RSZ_RE.search(rel)) or (rel.lower().endswith(".pak") and "/" not in rel)


class RszContext:
    """The type registry and whether its CRCs are this game version's (read once per check run)."""

    def __init__(self, game: GameIndex):
        self.game = game
        self._registry = self._trust = None
        self.game_unreadable = 0  # game files the registry could not read: it may be out of date
        self.registry_outdated = 0  # game files with CRCs the registry does not have as current

    @property
    def registry(self) -> rsz.TypeRegistry:
        if self._registry is None:
            self._registry = rsz.TypeRegistry.load()
        return self._registry

    @property
    def trust(self) -> bool:
        return self.matches_game() is True

    def matches_game(self) -> bool | None:
        """Whether the registry's CRCs are this game version's, by a game pfb they all know
        (None when that file is not in the game)."""
        if self._trust is None:
            sample = self.game.read_hash(SAMPLE_PFB)
            info = rsz.parse(sample) if sample else None
            if info is None or not info.types:
                self._trust = "unknown"
            else:
                self._trust = all(self.registry.crc(t) == c for t, c in rsz.crc_map(info).items())
        return None if self._trust == "unknown" else self._trust

    def plan(self, label, data, donor, provided=None):
        p = rsz.plan(label, data, donor, self.registry, provided, self.trust)
        if p is not None and p.game_unreadable:
            self.game_unreadable += 1
        if p is not None and p.registry_outdated:
            self.registry_outdated += 1
        return p


def _rsz_item(plan: rsz.RszPlan, label: str) -> dict:
    item = {"label": label, "kind": plan.kind, "method": plan.method}
    if plan.state == "stuck":
        item["reason"] = plan.reason or "안전하게 고칠 방법을 찾지 못했습니다"
        if plan.detail:
            item["detail"] = plan.detail
    return item


def _rsz_loose(key: str, rel: str, data: bytes, game: GameIndex, rszs: RszContext,
               provided) -> tuple[FileResult, bytes | None]:
    """The result for a loose .user/.pfb/.scn file and the repaired bytes (None: nothing to write)."""
    m = RSZ_RE.search(rel)
    ext, version = m.group(1).lower(), int(m.group(2))
    base = rel[:m.start()]
    donor_path = donor = None
    for candidate in candidate_paths(base):
        found = game.read_versioned(candidate, ext, prefer=(version,))
        if found is not None:
            donor_path, donor = found
            break
    result = FileResult(key, rel, ext, "current")
    p = rszs.plan(rel, data, donor, provided)
    if p is None:
        return result, None  # not an RSZ file after all
    result.notes = list(p.notes)
    new_version = int(donor_path.rsplit(".", 1)[1]) if donor_path else version
    if p.state == "current" and new_version != version:
        p.state, p.method, p.data = "fixable", "버전 번호", data  # the game reads another version number
    if p.state == "stuck":
        result.state = "stuck"
    elif p.state == "fixable":
        result.state, result.new_rel = "fixable", RSZ_RE.sub(f".{m.group(1)}.{new_version}", rel)
    if p.state != "current":
        result.rsz.append(_rsz_item(p, rel))
    return result, (p.data if p.state == "fixable" else None)


def _pak_units(rel: str, pak: PakFile, game: GameIndex, rszs: RszContext):
    """(mdf2 units, RSZ plans) of a mod pak's entries."""
    units, plans = [], []
    for i, h in enumerate(pak.hashes):
        try:
            head = pak.head(i)
            if head != b"MDF\x00" and head not in rsz.MAGICS:  # textures and meshes: skip without reading them
                continue
            raw = pak.read(i)
        except MatfixError:  # an entry encrypted on its own: nothing we can read
            continue
        label = f"{rel}#{h:016X}"
        if head in rsz.MAGICS:
            p = rszs.plan(f"{label}.{rsz.MAGICS[head]}", raw, game.read_hash(h))
            if p is not None and p.state != "current":
                plans.append((h, p))
            continue
        version = detect_version(raw, prefer=CURRENT_VERSION_HINT)
        if version is None:
            continue
        donor = game.read_hash(h)
        donor_version = detect_version(donor, prefer=version) if donor is not None else None
        units.append((h, EntryPlan(label, raw, version, label if donor else None, donor, donor_version,
                                   donor_hash=h if donor else None)))
    return units, plans


def check_mod(files: list[tuple[str, str, Path]], game: GameIndex, shaders: ShaderIndex | None,
              out=None, progress=None, provided: set[str] | None = None,
              rszs: RszContext | None = None) -> list[FileResult]:
    """Look at (key, game path, stored file) of one mod. With ``out`` (result -> path)
    the repaired files are also written there. ``provided``: lower-cased names (without
    version number) of every file the mod ships, for mods in custom character slots.
    Files other than .mdf2, .user, .pfb, .scn and .pak are ignored."""
    progress = progress or (lambda done, total, msg: None)
    whole_game = shaders.find if shaders is not None else None
    rszs = rszs or RszContext(game)
    results: list[FileResult] = []
    rsz_files: list[tuple[FileResult, bytes | None]] = []
    loose: list[tuple[str, EntryPlan]] = []
    paks: list[tuple[str, str, PakFile, list[tuple[int, EntryPlan]], list]] = []
    for key, rel, path in files:
        if not is_material_file(rel):
            continue
        try:
            if MDF2_RE.search(rel):
                loose.append((key, _loose_unit(rel, path.read_bytes(), game)))
            elif RSZ_RE.search(rel):
                rsz_files.append(_rsz_loose(key, rel, path.read_bytes(), game, rszs, provided))
            else:
                pak = PakFile(path)
                units, plans = _pak_units(rel, pak, game, rszs)
                if units or plans:
                    paks.append((key, rel, pak, units, plans))
        except Exception as exc:  # one unreadable file must not stop the rest
            m = RSZ_RE.search(rel)
            kind = "mdf2" if MDF2_RE.search(rel) else (m.group(1).lower() if m else "pak")
            results.append(FileResult(key, rel, kind, "error", error=str(exc)))
    units = [u for _, u in loose] + [u for *_, us, _ps in paks for _, u in us]
    plan_entries(units, whole_game, progress)
    game_shaders = GameShaders(game)

    def write(result: FileResult, make) -> None:
        if out is None:
            return
        try:
            target = Path(out(result))
            target.parent.mkdir(parents=True, exist_ok=True)
            make(target)
            result.file = target
        except Exception as exc:
            result.state, result.error = "error", str(exc)

    for result, data in rsz_files:
        if result.state == "fixable":
            write(result, lambda target, data=data: target.write_bytes(data))
        results.append(result)

    for key, unit in loose:
        result = FileResult(key, unit.label, "mdf2", "current", notes=unit.notes, unresolved=unit.unresolved,
                            shaders=game_shaders.foreign(unit.unresolved_shaders))
        if unit.error:
            result.state, result.error = "error", unit.error
        elif unit.needs_work:
            result.state = "fixable"
            result.new_rel = MDF2_RE.sub(f".mdf2.{unit.out_version}", unit.label)
            result.fixed = [m.mod["name"] for m in unit.stale]
            write(result, lambda target, unit=unit: target.write_bytes(build_entry(unit)[0]))
        elif unit.unresolved:
            result.state = "unresolved"
        results.append(result)

    for key, rel, pak, entries, plans in paks:
        result = FileResult(key, rel, "pak", "current")
        for _h, unit in entries:
            result.notes += unit.notes + ([unit.error] if unit.error else [])
            result.unresolved += unit.unresolved
            result.shaders += game_shaders.foreign(unit.unresolved_shaders)
            result.fixed += [m.mod["name"] for m in unit.stale]
        for _h, p in plans:
            result.rsz.append(_rsz_item(p, p.label))
            result.notes += [f"{p.label}: {n}" for n in p.notes]
        work = [(h, u) for h, u in entries if u.needs_work]
        fixed_rsz = {h: p.data for h, p in plans if p.state == "fixable"}
        if work or fixed_rsz:
            result.state, result.new_rel = "fixable", rel

            def make(target, pak=pak, work=work, fixed_rsz=fixed_rsz):
                _rebuild_pak(pak, {**fixed_rsz, **{h: build_entry(u)[0] for h, u in work}}, target)

            write(result, make)
        elif result.stuck:
            result.state = "stuck"
        elif result.unresolved:
            result.state = "unresolved"
        results.append(result)
    return results


def rsz_sources(files: list[tuple[str, str, Path]], game: GameIndex):
    """(label, kind, the mod's bytes, the game file's path or None, its bytes or None) of every
    .user/.pfb/.scn of a mod, loose or inside its PAKs - for a diagnostics report."""
    for _key, rel, path in files:
        try:
            m = RSZ_RE.search(rel)
            if m:
                ext, version = m.group(1).lower(), int(m.group(2))
                found = None
                for candidate in candidate_paths(rel[:m.start()]):
                    found = game.read_versioned(candidate, ext, prefer=(version,))
                    if found is not None:
                        break
                yield rel, ext, path.read_bytes(), found[0] if found else None, found[1] if found else None
            elif is_material_file(rel) and not MDF2_RE.search(rel):
                pak = PakFile(path)
                for i, h in enumerate(pak.hashes):
                    try:
                        head = pak.head(i)
                        if head in rsz.MAGICS:
                            kind = rsz.MAGICS[head]
                            yield f"{rel}#{h:016X}.{kind}", kind, pak.read(i), f"#{h:016X}", game.read_hash(h)
                    except MatfixError:
                        continue
        except (OSError, MatfixError):
            continue


def _rebuild_pak(pak: PakFile, fixed: dict[int, bytes], target: Path) -> None:
    """Write the mod's pak with ``fixed`` entries replaced to ``target``: same order,
    other entries copied byte for byte, each entry compressed the way it was."""
    if pak.chunk_offsets is not None:
        raise MatfixError(f"{pak.path.name}: 이 형식의 pak은 다시 만들 수 없습니다")
    entries = []
    for h in pak.order:
        i = pak.find(h)
        _off, _csize, dsize, attrs = pak.entry(i)
        compression = attrs & 0xF
        if h in fixed:
            entries.append((h, compress_as(compression, fixed[h]), len(fixed[h]), compression))
        else:
            entries.append((h, (lambda i=i: pak.read_raw(i)), dsize, compression))
    try:
        write_pak(entries, target)
        check = PakFile(target)
        if list(check.order) != list(pak.order):
            raise MatfixError(f"{pak.path.name}: 다시 만든 pak의 항목이 원본과 다릅니다")
        for h, data in fixed.items():
            if check.read_hash(h) != data:
                raise MatfixError(f"{pak.path.name}: 다시 만든 pak을 읽어 보니 내용이 다릅니다")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
