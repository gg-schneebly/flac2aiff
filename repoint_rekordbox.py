#!/usr/bin/env python3
"""Point rekordbox tracks at the AIFF files that replaced their FLAC originals.

Usage:
    repoint_rekordbox.py            # dry run: only report what would change
    repoint_rekordbox.py --apply    # make the changes (rekordbox must be closed)

A track is repointed when its FLAC no longer exists and an .aiff with the same name
exists in the same folder (i.e. it was converted by convert_library.py / convert_file.py).
Playlists, cues, beatgrids, ratings etc. are kept because the existing track entry is
reused. Updated per track:
  * rekordbox database: file path, file name, file type, file size, bit rate
  * the track's analysis (ANLZ) files, which store the file name: only '.flac' is
    changed to '.aiff', every other byte (waveforms, beatgrid, cues) stays identical

Before anything is written, the database and every analysis file that will be modified
are copied to a timestamped backup folder next to the database.
"""

import argparse
import os
import shutil
import struct
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flac2aiff_common import ConversionError, audio_stream, check_ffmpeg, probe  # noqa: E402

from pyrekordbox import Rekordbox6Database  # noqa: E402
from pyrekordbox.config import get_config  # noqa: E402
from pyrekordbox.utils import get_rekordbox_pid  # noqa: E402

FILETYPE_FLAC = 5
FILETYPE_AIFF = 12
DB_FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")


def nfc(s):
    # macOS file names and rekordbox paths may encode accents differently (NFD vs NFC)
    return unicodedata.normalize("NFC", s)


def plan_changes(db):
    """Return (todo, skipped): todo is a list of (content, new_path, aiff_info)."""
    contents = db.get_content().all()
    known_paths = {nfc(c.FolderPath or "") for c in contents}
    todo, skipped = [], []

    for c in contents:
        old = c.FolderPath or ""
        if not old.lower().endswith(".flac") or Path(old).exists():
            continue
        # keep the database's own spelling of the path, only swap the extension
        new = old[: -len(".flac")] + ".aiff"
        if not Path(new).is_file():
            continue  # missing for some other reason (moved/deleted); not ours to fix
        if nfc(new) in known_paths:
            skipped.append((old, "the .aiff already has its own rekordbox entry"))
            continue
        try:
            stream = audio_stream(probe(new), new)
        except ConversionError as e:
            skipped.append((old, str(e)))
            continue
        if not stream.get("codec_name", "").startswith("pcm_"):
            skipped.append((old, f"{new} is not an uncompressed AIFF ({stream.get('codec_name')})"))
            continue
        rate = int(stream.get("sample_rate") or 0)
        if c.SampleRate and rate != c.SampleRate:
            skipped.append((old, f"sample rate differs: rekordbox {c.SampleRate}, aiff {rate}"))
            continue
        bits = int(stream.get("bits_per_sample") or 0)
        channels = int(stream.get("channels") or 0)
        info = {
            "size": Path(new).stat().st_size,
            "bitrate": rate * bits * channels // 1000,  # kbps, as rekordbox stores it for AIFF
        }
        todo.append((c, new, info))
    return todo, skipped


def anlz_files_for(db, content):
    if not content.AnalysisDataPath:
        return []
    anlz_dir = db.get_anlz_dir(content)
    if not anlz_dir.is_dir():
        return []  # track was never analysed
    return sorted(p for p in anlz_dir.iterdir() if p.suffix.upper() in (".DAT", ".EXT", ".2EX"))


def patch_anlz_extension(data):
    """Return the ANLZ file bytes with the stored file name's .flac changed to .aiff.

    The file is patched byte-for-byte instead of being re-built with pyrekordbox, because
    pyrekordbox drops sections it doesn't know (e.g. rekordbox 7's PVB2 waveforms).
    '.flac' and '.aiff' have the same length, so no section lengths change.
    Returns None if the file has no PPTH section ending in '.flac'.
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
            path = data[start:start + len_path].decode("utf-16-be")
            name = path.rstrip("\0")
            if not name.lower().endswith(".flac"):
                return None
            new = (name[:-5] + ".aiff" + path[len(name):]).encode("utf-16-be")
            assert len(new) == len_path
            return data[:start] + new + data[start + len_path:]
        i += len_tag
    return None


def backup(db_dir, anlz_paths):
    dest = db_dir / f"flac2aiff-backup-{datetime.now():%Y%m%d-%H%M%S}"
    dest.mkdir()
    for name in DB_FILES:
        if (db_dir / name).exists():
            shutil.copy2(db_dir / name, dest / name)
    for p in anlz_paths:
        target = dest / p.relative_to(db_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    return dest


def restore_anlz(backup_dir, db_dir, anlz_paths):
    for p in anlz_paths:
        shutil.copy2(backup_dir / p.relative_to(db_dir), p)


def apply_changes(db, db_dir, todo):
    # patch all analysis files in memory first, so we know exactly what to back up
    anlz = {}
    for c, _, _ in todo:
        for path in anlz_files_for(db, c):
            patched = patch_anlz_extension(path.read_bytes())
            if patched is not None:
                anlz[path] = patched

    backup_dir = backup(db_dir, list(anlz))
    print(f"Backup written to {backup_dir}")

    try:
        for c, new, info in todo:
            if c.OrgFolderPath == c.FolderPath:
                c.OrgFolderPath = new
            c.FolderPath = new
            c.FileNameL = Path(new).name
            c.FileType = FILETYPE_AIFF
            c.FileSize = info["size"]
            c.BitRate = info["bitrate"]
        for path, data in anlz.items():
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        db.commit()
    except BaseException:
        db.rollback()
        restore_anlz(backup_dir, db_dir, list(anlz))
        print("Error: nothing was changed (database rolled back, analysis files restored).", file=sys.stderr)
        raise
    return backup_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--db-dir", type=Path,
                        help="rekordbox data folder (default: the one rekordbox uses, "
                             "~/Library/Pioneer/rekordbox on macOS)")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every track")
    args = parser.parse_args()

    check_ffmpeg()
    live_dir = Path((get_config("rekordbox7") or get_config("rekordbox6")).get("db_dir", "")).resolve()
    db_dir = args.db_dir.expanduser().resolve() if args.db_dir else live_dir
    # rekordbox overwrites its database while running; a copy elsewhere is safe to edit
    if args.apply and db_dir == live_dir and get_rekordbox_pid():
        sys.exit("error: rekordbox is running. Quit rekordbox and run this again.")

    db = Rekordbox6Database(db_dir / "master.db", db_dir=db_dir)

    todo, skipped = plan_changes(db)
    flac_total = sum(1 for c in db.get_content().all() if (c.FolderPath or "").lower().endswith(".flac"))
    print(f"rekordbox library: {db_dir}")
    print(f"FLAC tracks in rekordbox: {flac_total}")
    print(f"  to repoint to .aiff: {len(todo)}")
    print(f"  skipped: {len(skipped)}")
    if args.verbose:
        for c, new, _ in todo:
            print(f"    {c.FolderPath}\n      -> {Path(new).name}")
    for old, reason in skipped:
        print(f"    SKIPPED {old}\n      {reason}")

    if not todo:
        return
    if not args.apply:
        print("\nDry run, nothing changed. Quit rekordbox and run with --apply to make these changes.")
        return

    apply_changes(db, db_dir, todo)
    db.close()
    print(f"\nDone: {len(todo)} tracks now point to their .aiff files. You can open rekordbox again.")


if __name__ == "__main__":
    main()
