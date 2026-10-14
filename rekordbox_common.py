"""Helpers shared by the rekordbox repointing scripts (computer library and USB sticks)."""

import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from flac2aiff_common import audio_stream, probe

FILETYPE_FLAC = 5
FILETYPE_AIFF = 12
ANLZ_SUFFIXES = (".DAT", ".EXT", ".2EX")


ANLZ_HASH_MODULUS = 200003  # prime; also the table size in USBANLZ/USBMNG.DAT
ANLZ_BUCKET_BITS = (0, 2, 6, 7, 9, 13, 16)  # bits of the hash that form the "Pxxx" folder


def anlz_path_hash(path: str) -> int:
    """Return the number Pioneer players derive from a track path to locate its analysis folder.

    The USB analysis folder `PIONEER/USBANLZ/Pxxx/yyyyyyyy/` is named from this value, not
    taken from the library, so a track whose path changes needs its analysis files moved to the
    folder of the new path. The function is a two-multiplier hash over the path's UTF-16 code
    units. It was documented by the community (https://github.com/morizkraemer/fourfour,
    pioneer-usb-writer/reference-code/PIONEER.md) and verified here against 1,400+ folders
    written by rekordbox and by a CDJ-850.

    Args:
        path: Track path relative to the stick root as stored in the library
            (e.g. `/Contents/Artist/Album/Artist - Title.aiff`).
    """
    raw = path.encode("utf-16-le")
    h = 0
    for unit in struct.unpack(f"<{len(raw) // 2}H", raw):
        t = (h * 0x5BC9 + unit) & 0xFFFFFFFF
        h = (t * 0x93B5 + unit) & 0xFFFFFFFF
    return h % ANLZ_HASH_MODULUS


def anlz_folder(path: str) -> str:
    """Return the stick-relative analysis folder for a track path.

    Args:
        path: Track path as stored in the library, e.g. `/Contents/A/x.aiff`.

    Returns:
        Folder such as `/PIONEER/USBANLZ/P00E/000281CE` (no trailing slash).
    """
    value = anlz_path_hash(path)
    bucket = sum(((value >> bit) & 1) << i for i, bit in enumerate(ANLZ_BUCKET_BITS))
    return f"/PIONEER/USBANLZ/P{bucket:03X}/{value:08X}"


def nfc(s: str) -> str:
    """Return `s` in Unicode NFC form.

    macOS file names and rekordbox paths may encode accents differently (NFD vs NFC).

    Args:
        s: Any string, typically a path.
    """
    return unicodedata.normalize("NFC", s)


@dataclass
class AiffInfo:
    """Properties of an AIFF file as rekordbox stores them.

    Attributes:
        codec: Audio codec name reported by ffprobe (`pcm_*` for a real AIFF).
        size: File size in bytes.
        bitrate: Bit rate in kbps, computed as rekordbox does for uncompressed audio.
        sample_rate: Sample rate in Hz.
        bit_depth: Bits per sample.
    """

    codec: str
    size: int
    bitrate: int
    sample_rate: int
    bit_depth: int


def read_aiff_info(path) -> AiffInfo:
    """Probe an audio file and return the values rekordbox stores for it.

    Args:
        path: Path of the audio file.

    Raises:
        ConversionError: If ffprobe fails or the file has no audio stream.
    """
    stream = audio_stream(probe(path), path)
    rate = int(stream.get("sample_rate") or 0)
    bits = int(stream.get("bits_per_sample") or 0)
    channels = int(stream.get("channels") or 0)
    return AiffInfo(
        codec=stream.get("codec_name", ""),
        size=Path(path).stat().st_size,
        bitrate=rate * bits * channels // 1000,
        sample_rate=rate,
        bit_depth=bits,
    )


def _find_ppth(data: bytes) -> tuple[int, int, str] | None:
    """Locate the PPTH (file path) section of an ANLZ file.

    Args:
        data: Contents of an ANLZ (.DAT/.EXT/.2EX) file.

    Returns:
        (offset of the path bytes, length of the path bytes, decoded path incl. NUL padding),
        or None if the file has no PPTH section.

    Raises:
        ValueError: If `data` is not a well-formed ANLZ file.
    """
    if data[:4] != b"PMAI":
        raise ValueError("not an ANLZ file")
    i = struct.unpack(">I", data[4:8])[0]
    while i + 12 <= len(data):
        tag = data[i:i + 4]
        len_header, len_tag = struct.unpack(">II", data[i + 4:i + 12])
        if len_tag <= 0:
            raise ValueError("corrupt ANLZ section")
        if tag == b"PPTH":
            len_path = struct.unpack(">I", data[i + 12:i + 16])[0]
            start = i + len_header
            return start, len_path, data[start:start + len_path].decode("utf-16-be")
        i += len_tag
    return None


def anlz_stored_path(data: bytes) -> str | None:
    """Return the file path stored in an ANLZ file, or None if it has none.

    Args:
        data: Contents of an ANLZ (.DAT/.EXT/.2EX) file.
    """
    found = _find_ppth(data)
    return found[2].rstrip("\0") if found else None


def patch_anlz_extension(data: bytes) -> bytes | None:
    """Return the ANLZ file bytes with the stored file name's .flac changed to .aiff.

    The file is patched byte-for-byte instead of being re-built with pyrekordbox, because
    pyrekordbox drops sections it doesn't know (e.g. rekordbox 7's PVB2 waveforms).
    '.flac' and '.aiff' have the same length, so no section lengths change.

    Args:
        data: Contents of an ANLZ (.DAT/.EXT/.2EX) file.

    Returns:
        The patched bytes, or None if the file has no PPTH section ending in '.flac'.

    Raises:
        ValueError: If `data` is not a well-formed ANLZ file.
    """
    found = _find_ppth(data)
    if found is None:
        return None
    start, len_path, path = found
    name = path.rstrip("\0")
    if not name.lower().endswith(".flac"):
        return None
    new = (name[:-5] + ".aiff" + path[len(name):]).encode("utf-16-be")
    assert len(new) == len_path
    return data[:start] + new + data[start + len_path:]
