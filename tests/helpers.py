"""Builders for small synthetic rekordbox files used by the tests."""

import struct
import subprocess

from rekordbox_usb import find_libraries

PAGE = 4096


def short_ascii(text: str) -> bytes:
    """Encode a DeviceSQL short ASCII string."""
    return bytes([((len(text) + 1) << 1) | 1]) + text.encode("ascii")


def utf16_string(text: str) -> bytes:
    """Encode a DeviceSQL long UTF-16LE string."""
    body = text.encode("utf-16-le")
    return b"\x90" + struct.pack("<H", len(body) + 4) + b"\x00" + body


def device_string(text: str) -> bytes:
    """Encode text the way rekordbox does: short ASCII if possible, else UTF-16."""
    return short_ascii(text) if text.isascii() else utf16_string(text)


def build_pdb(path, rows):
    """Write a PDB with a single track page holding `rows` = [(file_path, filename, analyze)]."""
    heap = bytearray()
    offsets = []
    for file_path, filename, analyze in rows:
        row = bytearray(94 + 42)
        struct.pack_into("<I", row, 8, 44100)           # sample rate
        struct.pack_into("<I", row, 16, 1000)           # file size
        struct.pack_into("<I", row, 48, 0)              # bitrate
        struct.pack_into("<H", row, 82, 16)             # sample depth
        struct.pack_into("<H", row, 90, 5)              # file type: FLAC
        strings = {14: device_string(analyze), 19: device_string(filename), 20: device_string(file_path)}
        pos = len(row)
        blob = bytearray()
        string_offsets = [0] * 21
        for idx in range(21):
            data = strings.get(idx, short_ascii(""))
            string_offsets[idx] = pos + len(blob)
            blob += data
        struct.pack_into("<21H", row, 94, *string_offsets)
        offsets.append(len(heap))
        heap += row + blob
    page = bytearray(PAGE)
    struct.pack_into("<IIII", page, 0, 0, 1, 0, 0)       # gap, page_index, type=tracks, next
    page[24] = len(rows)
    page[34:36] = struct.pack("<H", len(rows))
    page[40:40 + len(heap)] = heap
    end = PAGE
    for r, off in enumerate(offsets):
        struct.pack_into("<H", page, end - 6 - 2 * r, off)
    struct.pack_into("<H", page, end - 4, (1 << len(rows)) - 1)
    header = bytearray(PAGE)
    struct.pack_into("<7I", header, 0, 0, PAGE, 1, 2, 0, 1, 0)
    struct.pack_into("<4I", header, 28, 0, 0, 1, 1)
    path.write_bytes(bytes(header) + bytes(page))


def anlz_file(stored_path: str) -> bytes:
    """Build a minimal ANLZ file with a PPTH section followed by a dummy section."""
    body = (stored_path + "\0").encode("utf-16-be")
    ppth = b"PPTH" + struct.pack(">III", 16, 16 + len(body), len(body)) + body
    other = b"PQTZ" + struct.pack(">II", 12, 16) + b"\x01\x02\x03\x04"
    return b"PMAI" + struct.pack(">I", 12) + b"\0\0\0\0" + ppth + other


TRACKS = ["/Contents/A/x", "/Contents/A/y"]


def make_stick(root, tracks=TRACKS, audio="aiff"):
    """Build a legacy-library stick whose library says FLAC.

    Args:
        root: Folder to build the stick in.
        tracks: Track paths without extension, e.g. `/Contents/A/x`.
        audio: "aiff" if the tracks were already converted (only the AIFF exists), or "flac"
            if they were not (only the FLAC exists).
    """
    rows = []
    for i, base in enumerate(tracks):
        audio_file = root / f"{base}.{audio}".lstrip("/")
        audio_file.parent.mkdir(parents=True, exist_ok=True)
        codec = "pcm_s16be" if audio == "aiff" else "flac"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.2",
                        "-ar", "8000", "-ac", "1", "-c:a", codec, str(audio_file)], check=True)
        folder = root / f"PIONEER/USBANLZ/P000/{i + 1:08X}"
        folder.mkdir(parents=True)
        for ext in ("DAT", "EXT", "2EX"):
            (folder / f"ANLZ0000.{ext}").write_bytes(anlz_file(f"{base}.flac"))
        rows.append((f"{base}.flac", f"{base.rsplit('/', 1)[-1]}.flac",
                     f"/PIONEER/USBANLZ/P000/{i + 1:08X}/ANLZ0000.DAT"))
    (root / "PIONEER/rekordbox").mkdir(parents=True)
    build_pdb(root / "PIONEER/rekordbox/export.pdb", rows)
    return find_libraries(root)
