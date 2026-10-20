"""Game specific constants for Monster Hunter Wilds."""

import re

APP_TITLE = "몬스터 헌터 와일즈 모드 매니저"

GAME_EXE = "MonsterHunterWilds.exe"
STEAM_APP_ID = "2246340"
STEAM_INSTALL_DIR = "MonsterHunterWilds"

# The game only reads its own numbered patches (re_chunk_000.pak.sub_000.pak.patch_NNN.pak),
# so mod PAKs go to this folder instead: REFramework mounts every .pak in it (lower-case
# extension, any name) into a free patch slot, in name order.
PAK_MODS_DIR = "pak_mods"
# Names of patch files, used to recognise PAKs older versions put into the patch chain.
PAK_PATCH_RE = re.compile(
    r"^(?P<chain>re_chunk_\d{3}\.pak(?:\.sub_\d{3}\.pak)?)\.patch_(?P<num>\d{3})\.pak$",
    re.IGNORECASE,
)

# REFramework is installed as dinput8.dll next to the game executable.
REFRAMEWORK_DLL = "dinput8.dll"
REFW_ORIGIN = "reframework"  # Mod.origin of the REFramework installed through the updater

REFRAMEWORK_URL = "https://github.com/praydog/REFramework-nightly/releases"
NEXUS_URL = "https://www.nexusmods.com/games/monsterhunterwilds/mods"
NEXUS_SEARCH_URL = "https://www.nexusmods.com/games/monsterhunterwilds/mods?keyword={}"

# Releases of this program (used by the in-app updater).
GITHUB_REPO = "skagks/asdijofosjifjsioiofasojif"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
EXE_NAME = "MHWildsModManager.exe"
