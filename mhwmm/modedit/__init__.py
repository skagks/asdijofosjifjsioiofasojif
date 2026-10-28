"""모드 수정: change an installed mod in place, without touching the files it was installed with.

Edits live in an overlay in the mod's own folder: ``edits.json`` (the recipe: which files are left
out, which values were changed and from what) and ``edits/`` (the files built from it, each under a
new name).  The manager resolves the overlay in ``mod_entries`` in the order

    stored file → (excluded files left out) → matfix repair → value edit

so a value edit is always applied on top of the file the game would otherwise get, and is built
again when that file changes (an update, a repair accepted or declined, a hand edit of the folder).

Modules (none of them imports Tk or the manager; everything is safe on a worker thread):
- model.py: the record (``edits.json``), the plans the window and the CLI ask for, ``Pending`` (the
  window's unsaved changes) and the view types the manager fills.
- engines.py: the value engine protocol (``rsz`` for .user data, ``lua``/``json``/``ini``/``toml`` for
  shipped script settings) and the rebase rule that re-applies a recipe on a changed file.
- pakedit.py: the members of a mod's root PAK and rebuilding it with replaced entries.
- build.py: building one item's output into a temporary stage.
- rszdata.py, textconfig.py: the engines.
"""

from __future__ import annotations

EDITS_FORMAT = 1  # raise when the meaning of a stored recipe changes (older readers then refuse it)
EDITS_RECORD = "edits.json"
EDITS_DIR = "edits"
EDITS_PREFIX = "edits/"  # source keys of outputs in mod_entries
BAD_SUFFIX = ".bad-"  # edits.json.bad-YYYYMMDD-HHMMSS
GC_KEEP_PREVIOUS = True  # keep the previous record's outputs for one generation


class EditError(ValueError):
    """Something 모드 수정 refused or could not do; the message is Korean and shown as it is
    (ValueError is already one of gui.EXPECTED_ERRORS)."""


class StaleError(EditError):
    """The file reads only with an older layout of the game's types (it needs a matfix repair first)."""


class GameCopyChanged(EditError):
    """Loose files whose copy in the game folder changed since it was placed; reverting them needs
    ``force=True`` (the change made in the game would be lost)."""

    def __init__(self, message: str, rels: list[str]):
        super().__init__(message)
        self.rels = list(rels)
