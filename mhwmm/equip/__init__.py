"""Equipment appearance: which in-game equipment each mod changes the look of.

Mod files are matched to equipment by their game paths (model folders such as
Art/Model/Character/ch03/<model>/<design>/<part>/ and Art/Model/Item/it<type>/...); files
inside .pak mods are recognised by their path hashes against a list of the game's
equipment paths.  Names, rarity and the in-game preview pictures are read at runtime
from the user's own game (its data tables, .msg text and GUI textures) and cached in the
app's data folder - nothing of the game is shipped with the app.

File formats follow, without code taken from them:
- .msg text: dtlnor's REMSG_Converter (MIT, https://github.com/dtlnor/REMSG_Converter) and
  REasy by Battlezone (MIT, https://github.com/seifhassine/REasy) - layout and string-pool key.
- .tex textures: REasy (MIT) - header and mip table; GDeflate (the DirectStorage tile stream the
  game compresses textures with): microsoft/DirectStorage (Apache-2.0) and NVIDIA's libdeflate
  GDeflate decoder (MIT / Apache-2.0) - re-implemented in pure Python.
- The equipment path list (equip_paths.txt.xz, tools/make_equip_paths.py) is taken from REasy's
  MHWS_STM.list (MIT), the community MH Wilds file list (dtlnor, Ekey's REE.PAK.Tool).
"""


class EquipError(Exception):
    """Something the equipment viewer could not read."""
