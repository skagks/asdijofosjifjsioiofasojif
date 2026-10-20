"""Repair costume mods whose materials (.mdf2) went stale after a game update.

When Capcom changes a material's layout (new texture slots or shader parameters, a
new file version), a mod built against the old layout shows wrong colours or
textures, renders black/white, or REFramework reports [Invalid file]/[Missing file].
The repair reads the CURRENT game's own material for the same model straight out
of the game's .pak files, keeps its structure, and puts the mod's textures, shader
parameters and render state back into it.

Based on MHWmodfixer by Littlefish (PoeticJustice79), MIT License:
https://github.com/PoeticJustice79/MHWmodfixer - its pak reader (itself ported from
Ekey's REE.PAK.Tool), its MDF2 reader/writer (from the RE-Engine-010-Templates by
alphaZomega and others), and the donor matching and override rules it found through
in-game testing.  Changes here: mods are repaired where they are installed and the
originals are kept (a repair always starts again from them), materials are patched
in place whenever one current file matches the mod's file, and that path keeps the
mod's shader parameters and render state too.  RSZ (.pfb/.user/.scn) repair and the
retargeting tools are not included.

    MIT License

    Copyright (c) 2026 Littlefish (PoeticJustice79)

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

CREDIT = "재질 복구는 MHWmodfixer(Littlefish, MIT 라이선스)를 바탕으로 만들었습니다."


class MatfixError(Exception):
    pass


def _zstd():
    """(decompress(data, max_size), compress(data), start(data, size)) from whichever zstd module is
    installed; start() returns the first bytes of a frame from as much of it as is given."""
    try:
        import zstandard

        return (lambda data, size: zstandard.ZstdDecompressor().decompress(data, max_output_size=size),
                lambda data: zstandard.ZstdCompressor(level=5).compress(data),
                lambda data, size: zstandard.ZstdDecompressor().decompressobj().decompress(data)[:size])
    except ImportError:
        pass
    for name in ("compression.zstd", "backports.zstd"):  # Python 3.14+, or its backport
        try:
            module = __import__(name, fromlist=["decompress"])
            return (lambda data, size: module.decompress(data), lambda data: module.compress(data, level=5),
                    lambda data, size: module.ZstdDecompressor().decompress(data, max_length=size))
        except ImportError:
            continue
    return None


_ZSTD = _zstd()


def available() -> bool:
    """Game files are zstd compressed: without a zstd module nothing can be read."""
    return _ZSTD is not None


def zstd_decompress(data: bytes, max_size: int) -> bytes:
    if _ZSTD is None:
        raise MatfixError("zstd 압축을 풀 수 있는 모듈이 없습니다 (pip install zstandard).")
    return _ZSTD[0](data, max_size)


def zstd_start(data: bytes, size: int) -> bytes:
    """First ``size`` bytes of a zstd frame, of which ``data`` may be only the beginning."""
    if _ZSTD is None:
        raise MatfixError("zstd 압축을 풀 수 있는 모듈이 없습니다 (pip install zstandard).")
    return _ZSTD[2](data, size)


def zstd_compress(data: bytes) -> bytes:
    if _ZSTD is None:
        raise MatfixError("zstd 압축을 할 수 있는 모듈이 없습니다 (pip install zstandard).")
    return _ZSTD[1](data)
