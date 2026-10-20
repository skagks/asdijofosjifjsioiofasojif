"""Which current game material a mod's material should be rebuilt from.

Ported from MHWmodfixer's slot_merge.py/donor.py; each rule comes from a mod that
broke without it (see the comments).  Tiers, in order:
  1. same name in the current version of the mod's own file (when it uses the same
     shader, or that shader is in use nowhere else)
  2. the same shader in that file
  3. the same shader in the other files of the same mod (pieces of one armour set)
  4. the same shader anywhere in the game
Exact shader matches are tried in every scope before "_NoMultiBlend" spelling
variants, so a material whose shader is still alive is never downgraded to a
narrower sibling (that crashed the game for one mod).
"""

from __future__ import annotations

import re

_MH_SLOT_RE = re.compile(r"(?<![a-z0-9])mh(\d\d)", re.IGNORECASE)
_VARIANT = "_NoMultiBlend"


def candidate_paths(base: str) -> list[str]:
    """The mod's own path first; mod managers install into fake "mhNN" character
    slots that stand in for the game's "chNN" ones."""
    swapped = _MH_SLOT_RE.sub(r"ch\1", base)
    return [base] if swapped == base else [base, swapped]


def mmtr_variants(mmtr: str) -> list[str]:
    """Capcom splits "_NoMultiBlend" siblings out of shaders without changing what a
    material is for, so both spellings are worth trying."""
    suffix = _VARIANT + ".mmtr"
    if mmtr.endswith(suffix):
        return [mmtr, mmtr[:-len(suffix)] + ".mmtr"]
    if mmtr.endswith(".mmtr"):
        return [mmtr, mmtr[:-len(".mmtr")] + suffix]
    return [mmtr]


def _usesc(name: str) -> bool:
    return name.lower().endswith("_usesc")


def category(path: str) -> str | None:
    """.../art/model/item/... -> "item": a weapon should borrow from items, not a
    character (the game silently ignored a mod whose donor was a character)."""
    parts = path.lower().split("/")
    try:
        return parts[parts.index("model") + 1]
    except (ValueError, IndexError):
        return None


def pick_best(candidates: list[tuple[str, dict]], mod_mat: dict, notes: list[str],
              category_hint: str | None = None) -> tuple[str, dict] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # an exact name means the author copied that very material (strongest signal)
    exact = [c for c in candidates if c[1]["name"] == mod_mat["name"]]
    if len(exact) == 1:
        return exact[0]
    pool = exact or candidates
    if category_hint is not None:
        pool = [c for c in pool if category(c[0]) == category_hint] or pool
    want = _usesc(mod_mat["name"])
    same = [c for c in pool if _usesc(c[1]["name"]) == want]
    pool = same if len(same) == 1 else (same or pool)
    if len(pool) > 1:
        notes.append(f"재질 '{mod_mat['name']}': 같은 셰이더를 쓰는 후보가 {len(pool)}개라 {pool[0][0]}의 것을 썼습니다")
    return pool[0]


def find_donor(mod_mat: dict, own: list[tuple[str, dict]], pool: list[tuple[str, dict]], whole_game,
               notes: list[str]) -> tuple[dict, str, str] | None:
    """(donor material, its file, how it was matched) or None.

    own: materials of the current version of the mod's own file; pool: those of every
    file of the same mod; whole_game: callable(mmtr) -> [(file, material)] or None."""
    mmtr = mod_mat["mmtr_path"]
    hint = category(own[0][0]) if own else None

    def search(scope_own, scope_pool, scope_game, mmtrs, label):
        if scope_own:
            hit = pick_best([c for c in own if c[1]["mmtr_path"] in mmtrs], mod_mat, notes)
            if hit:
                return hit[1], hit[0], f"같은 파일 · {label}"
        if scope_pool:
            hit = pick_best([c for c in pool if c[1]["mmtr_path"] in mmtrs], mod_mat, notes, hint)
            if hit:
                return hit[1], hit[0], f"같은 모드의 다른 파일 · {label}"
        if scope_game and whole_game is not None:
            seen, candidates = set(), []
            for m in mmtrs:
                for src, blob in whole_game(m):
                    if (src, blob["name"]) not in seen:  # one file can hold several matching materials
                        seen.add((src, blob["name"]))
                        candidates.append((src, blob))
            hit = pick_best(candidates, mod_mat, notes, hint)
            if hit:
                return hit[1], hit[0], f"게임 전체 · {label}"
        return None

    same_name = next(((src, b) for src, b in own if b["name"] == mod_mat["name"]), None)
    if same_name is not None and same_name[1]["mmtr_path"] == mmtr:
        return same_name[1], same_name[0], "같은 파일 · 이름 일치"
    if same_name is not None:
        # The current file kept the name but moved the material to a narrower shader
        # (a "_NoMultiBlend" split). If the mod's own shader is still used anywhere,
        # rebuild from that instead of dropping the mod's MultiBlend slots.
        hit = search(True, True, True, [mmtr], "같은 셰이더")
        return hit or (same_name[1], same_name[0], "같은 파일 · 이름 일치")
    return search(True, True, True, [mmtr], "같은 셰이더") or \
        search(True, True, True, mmtr_variants(mmtr), "비슷한 셰이더")
