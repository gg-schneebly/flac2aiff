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
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flac2aiff_common import ConversionError, check_ffmpeg  # noqa: E402
from rekordbox_common import (  # noqa: E402
    ANLZ_SUFFIXES, FILETYPE_AIFF, nfc, patch_anlz_extension, read_aiff_info,
)

from pyrekordbox import Rekordbox6Database  # noqa: E402
from pyrekordbox.config import get_config  # noqa: E402
from pyrekordbox.utils import get_rekordbox_pid  # noqa: E402

DB_FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")


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
            aiff = read_aiff_info(new)
        except ConversionError as e:
            skipped.append((old, str(e)))
            continue
        if not aiff.codec.startswith("pcm_"):
            skipped.append((old, f"{new} is not an uncompressed AIFF ({aiff.codec})"))
            continue
        if c.SampleRate and aiff.sample_rate != c.SampleRate:
            skipped.append((old, f"sample rate differs: rekordbox {c.SampleRate}, aiff {aiff.sample_rate}"))
            continue
        info = {"size": aiff.size, "bitrate": aiff.bitrate}
        todo.append((c, new, info))
    return todo, skipped


def anlz_files_for(db, content):
    if not content.AnalysisDataPath:
        return []
    anlz_dir = db.get_anlz_dir(content)
    if not anlz_dir.is_dir():
        return []  # track was never analysed
    return sorted(p for p in anlz_dir.iterdir() if p.suffix.upper() in ANLZ_SUFFIXES)


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
