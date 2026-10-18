"""Game specific constants for Monster Hunter Wilds."""

import re

APP_TITLE = "몬스터 헌터 와일즈 모드 매니저"

GAME_EXE = "MonsterHunterWilds.exe"
STEAM_APP_ID = "2246340"
STEAM_INSTALL_DIR = "MonsterHunterWilds"

# PAK mods are loaded as numbered patches of this archive chain.  The game
# reads re_chunk_000.pak.sub_000.pak.patch_001.pak, _002, ... and stops at the
# first missing number, so mod patches have to stay contiguous.
DEFAULT_PAK_CHAIN = "re_chunk_000.pak.sub_000.pak"
PAK_CHAINS = ("re_chunk_000.pak.sub_000.pak", "re_chunk_000.pak")
PAK_PATCH_RE = re.compile(
    r"^(?P<chain>re_chunk_\d{3}\.pak(?:\.sub_\d{3}\.pak)?)\.patch_(?P<num>\d{3})\.pak$",
    re.IGNORECASE,
)
PAK_CHAIN_RE = re.compile(r"^re_chunk_\d{3}\.pak(?:\.sub_\d{3}\.pak)?$", re.IGNORECASE)
MAX_PAK_PATCH = 999

# REFramework is installed as dinput8.dll next to the game executable.
REFRAMEWORK_DLL = "dinput8.dll"

REFRAMEWORK_URL = "https://github.com/praydog/REFramework-nightly/releases"
NEXUS_URL = "https://www.nexusmods.com/games/monsterhunterwilds/mods"
