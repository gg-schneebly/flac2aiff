"""Minimal reader/patcher for the track table of a rekordbox export.pdb (DeviceSQL).

The legacy USB/SD export format stores tracks in `PIONEER/rekordbox/export.pdb`. Only what is
needed to repoint a track from `.flac` to `.aiff` is implemented. Rows are patched in place
(`.flac` and `.aiff` have the same length) so the page layout and every other byte stay
identical.

Format reference: https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/exports.html
"""

import struct
from dataclasses import dataclass
from pathlib import Path

PAGE_HEADER_SIZE = 0x28
ROW_GROUP_SIZE = 0x24  # 16 row offsets (u2) + presence flags (u2) + unknown (u2)
TABLE_TRACKS = 0
PAGE_FLAG_NO_DATA = 0x40

# byte offsets inside a track row
OFF_SAMPLE_RATE = 8
OFF_FILE_SIZE = 16
OFF_BITRATE = 48
OFF_SAMPLE_DEPTH = 82
OFF_FILE_TYPE = 90
OFF_STRING_OFFSETS = 94
STRING_IDX_ANALYZE_PATH = 14
STRING_IDX_FILENAME = 19
STRING_IDX_FILE_PATH = 20


class PdbError(Exception):
    """Raised when a PDB file does not look like what this module understands."""


@dataclass
class DeviceSqlString:
    """Location and decoding of one DeviceSQL string inside the file.

    Attributes:
        start: Absolute offset of the string's first (header) byte.
        text_start: Absolute offset of the first text byte.
        text_end: Absolute offset one past the last text byte.
        encoding: Python codec name of the text bytes.
        text: Decoded string.
    """

    start: int
    text_start: int
    text_end: int
    encoding: str
    text: str


@dataclass
class PdbTrack:
    """One track row.

    Attributes:
        row_offset: Absolute offset of the row in the file.
        file_path: Path of the audio file on the stick (e.g. `/Contents/Artist/a.flac`).
        filename: File name without directories.
        analyze_path: Path of the track's analysis (ANLZ .DAT) file on the stick.
        file_type: rekordbox file type code (5 = FLAC, 12 = AIFF).
        file_size: File size in bytes.
        bitrate: Bit rate in kbps.
        sample_rate: Sample rate in Hz.
        sample_depth: Bit depth.
        path_str: Location of the `file_path` string.
        name_str: Location of the `filename` string.
        analyze_str: Location of the `analyze_path` string.
    """

    row_offset: int
    file_path: str
    filename: str
    analyze_path: str
    file_type: int
    file_size: int
    bitrate: int
    sample_rate: int
    sample_depth: int
    path_str: DeviceSqlString
    name_str: DeviceSqlString
    analyze_str: DeviceSqlString


def _read_string(data: bytes, pos: int) -> DeviceSqlString:
    """Decode the DeviceSQL string starting at `pos`.

    Args:
        data: Whole file contents.
        pos: Absolute offset of the string header.

    Returns:
        The decoded string with its byte location.

    Raises:
        PdbError: If the header byte is not a known string kind.
    """
    header = data[pos]
    if header & 1:  # short ASCII: length (incl. header byte) in the upper 7 bits
        end = pos + (header >> 1)
        return DeviceSqlString(pos, pos + 1, end, "ascii", data[pos + 1:end].decode("ascii"))
    length = struct.unpack_from("<H", data, pos + 1)[0]  # incl. the 4 header bytes
    if header == 0x40:
        encoding = "ascii"
    elif header == 0x90:
        encoding = "utf-16-le"
    else:
        raise PdbError(f"unknown string kind 0x{header:02x} at offset {pos}")
    end = pos + length
    return DeviceSqlString(pos, pos + 4, end, encoding, data[pos + 4:end].decode(encoding))


class PdbFile:
    """An export.pdb loaded into memory, with read access to tracks and in-place patching.

    Args:
        path: Path of the export.pdb file.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = bytearray(self.path.read_bytes())
        if len(self.data) < 28:
            raise PdbError(f"{path} is too small to be a PDB file")
        self.page_size, num_tables = struct.unpack_from("<II", self.data, 4)
        self._tables = {}
        for i in range(num_tables):
            kind, _, first, last = struct.unpack_from("<4I", self.data, 28 + 16 * i)
            self._tables[kind] = (first, last)
        if TABLE_TRACKS not in self._tables:
            raise PdbError(f"{path} has no track table")

    def _track_pages(self):
        """Yield the offsets of all pages in the track table that hold rows."""
        first, last = self._tables[TABLE_TRACKS]
        index = first
        while True:
            offset = index * self.page_size
            if offset + self.page_size > len(self.data):
                raise PdbError(f"track table points outside the file (page {index})")
            _, page_index, kind, next_page = struct.unpack_from("<IIII", self.data, offset)
            if page_index != index or kind != TABLE_TRACKS:
                raise PdbError(f"unexpected page header at page {index}")
            if not self.data[offset + 27] & PAGE_FLAG_NO_DATA:
                yield offset
            if index == last:
                return
            index = next_page

    def _row_offsets(self, page: int):
        """Yield the absolute offsets of all present rows of the page at offset `page`."""
        small = self.data[page + 24]
        large = struct.unpack_from("<H", self.data, page + 34)[0]
        count = large if large > small and large != 0x1FFF else small
        end = page + self.page_size
        for group in range((count + 15) // 16):
            group_end = end - group * ROW_GROUP_SIZE
            flags = struct.unpack_from("<H", self.data, group_end - 4)[0]
            for r in range(min(16, count - group * 16)):
                if flags & (1 << r):
                    rel = struct.unpack_from("<H", self.data, group_end - 6 - 2 * r)[0]
                    yield page + PAGE_HEADER_SIZE + rel

    def tracks(self) -> list[PdbTrack]:
        """Return all track rows of the file."""
        tracks = []
        for page in self._track_pages():
            for row in self._row_offsets(page):
                offsets = struct.unpack_from("<21H", self.data, row + OFF_STRING_OFFSETS)
                path_str = _read_string(self.data, row + offsets[STRING_IDX_FILE_PATH])
                name_str = _read_string(self.data, row + offsets[STRING_IDX_FILENAME])
                analyze_str = _read_string(self.data, row + offsets[STRING_IDX_ANALYZE_PATH])
                tracks.append(PdbTrack(
                    row_offset=row,
                    file_path=path_str.text,
                    filename=name_str.text,
                    analyze_path=analyze_str.text,
                    file_type=struct.unpack_from("<H", self.data, row + OFF_FILE_TYPE)[0],
                    file_size=struct.unpack_from("<I", self.data, row + OFF_FILE_SIZE)[0],
                    bitrate=struct.unpack_from("<I", self.data, row + OFF_BITRATE)[0],
                    sample_rate=struct.unpack_from("<I", self.data, row + OFF_SAMPLE_RATE)[0],
                    sample_depth=struct.unpack_from("<H", self.data, row + OFF_SAMPLE_DEPTH)[0],
                    path_str=path_str,
                    name_str=name_str,
                    analyze_str=analyze_str,
                ))
        return tracks

    def _replace_string(self, string: DeviceSqlString, new_text: str) -> None:
        """Overwrite a string with a new text of identical encoded length."""
        new = new_text.encode(string.encoding)
        if len(new) != string.text_end - string.text_start:
            raise PdbError(f"replacement changes the length of {string.text!r}")
        self.data[string.text_start:string.text_end] = new

    def repoint(self, track: PdbTrack, new_path: str, file_type: int, file_size: int,
                bitrate: int, analyze_path: str | None = None) -> None:
        """Change a track to point at a different file of the same name length.

        Args:
            track: Track row as returned by `tracks()`.
            new_path: New file path; must have the same encoded length as the old one.
            file_type: New rekordbox file type code.
            file_size: New file size in bytes.
            bitrate: New bit rate in kbps.
            analyze_path: New analysis file path (same encoded length as the old one), or
                None to keep it.
        """
        self._replace_string(track.path_str, new_path)
        if analyze_path is not None:
            self._replace_string(track.analyze_str, analyze_path)
        self._replace_string(track.name_str, new_path.rsplit("/", 1)[-1])
        struct.pack_into("<H", self.data, track.row_offset + OFF_FILE_TYPE, file_type)
        struct.pack_into("<I", self.data, track.row_offset + OFF_FILE_SIZE, file_size)
        struct.pack_into("<I", self.data, track.row_offset + OFF_BITRATE, bitrate)

    def save(self) -> None:
        """Write the (patched) data back to the original path atomically."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(self.data)
        tmp.replace(self.path)
