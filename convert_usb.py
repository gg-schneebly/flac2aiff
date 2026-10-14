#!/usr/bin/env python3
"""Convert the FLAC files of a rekordbox USB/SD export to AIFF and keep the export working.

Usage:
    convert_usb.py STICK [--dry-run] [--id3v2-version {3,4}] [--backup-dir DIR]

STICK is the mount point of the stick (e.g. /run/media/you/MYSTICK or /Volumes/MYSTICK).
Works with the legacy library (PIONEER/rekordbox/export.pdb), with Device Library Plus
(exportLibrary.db), and with sticks that have both. Per track it
  * converts the FLAC to a bit-identical AIFF next to it, then deletes the FLAC
  * repoints the library rows to the AIFF and moves the track's analysis (ANLZ) files to the
    folder the players compute from the new path, so waveforms, beatgrids and cues keep working
Cues, beatgrids, playlists etc. are kept. A backup of every library/analysis file that is
modified is written to ~/flac2aiff-backups/ (not onto the stick) before anything changes.

If the run is interrupted, run it again: it converts what is left and then repoints
everything whose FLAC is gone. Eject the stick afterwards before unplugging.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flac2aiff_common import (  # noqa: E402
    ConversionError, OutOfSpaceError, check_ffmpeg, convert, find_flacs,
)
from rekordbox_usb import (  # noqa: E402
    apply_plan, check_stick, default_backup_dir, find_libraries, plan_repoints,
)
from space_check import check_free_space, finalization_reserve, require_space  # noqa: E402

# folders on a stick that never hold the user's music
SKIP_DIRS = {"PIONEER", "PIONEER REC", "System Volume Information", ".Spotlight-V100",
             "$RECYCLE.BIN", ".Trashes", ".fseventsd"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stick", type=Path, help="mount point of the rekordbox USB/SD stick")
    parser.add_argument("--dry-run", action="store_true", help="only report what would be done")
    parser.add_argument("--id3v2-version", type=int, choices=(3, 4), default=3,
                        help="ID3 tag version written into the AIFF files (default: 3, most compatible)")
    parser.add_argument("--backup-dir", type=Path,
                        help="where to put the backup (default: ~/flac2aiff-backups/stick-<timestamp>)")
    args = parser.parse_args()

    root = args.stick.expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"error: not a directory: {root}")
    libs = find_libraries(root)
    if not (libs.pdb or libs.dlp):
        sys.exit(f"error: no rekordbox export found on {root} (looked in PIONEER/rekordbox)")
    check_ffmpeg()

    found = [name for name, lib in (("legacy export.pdb", libs.pdb), ("Device Library Plus", libs.dlp)) if lib]
    print(f"Stick: {root}\nRekordbox libraries: {' + '.join(found)}")

    flacs = list(find_flacs(root, SKIP_DIRS))
    print(f"{len(flacs)} FLAC file(s) to convert")
    verdict = check_free_space(flacs, root, reserve=finalization_reserve(len(flacs)))
    require_space(verdict)

    if args.dry_run:
        plan = plan_repoints(root, libs)
        print(f"{plan.count} track(s) already converted and waiting to be repointed in the libraries")
        for f in flacs[:20]:
            print(f"  would convert: {f.relative_to(root)}")
        if len(flacs) > 20:
            print(f"  ... and {len(flacs) - 20} more")
        return

    failed, converted, stopped = [], 0, False
    try:
        for i, src in enumerate(flacs, 1):
            try:
                dst = convert(src, id3v2_version=args.id3v2_version, min_free=verdict.reserve)
                print(f"[{i}/{len(flacs)}] converted: {src.relative_to(root)} -> {dst.name}")
                converted += 1
            except OutOfSpaceError as e:
                print(f"[{i}/{len(flacs)}] STOPPED: {e}", file=sys.stderr)
                stopped = True
                break
            except ConversionError as e:
                print(f"[{i}/{len(flacs)}] FAILED: {src.relative_to(root)}\n    {e}", file=sys.stderr)
                failed.append(src.relative_to(root))
    except KeyboardInterrupt:
        print("\nInterrupted. The libraries have NOT been updated yet, so tracks converted so far "
              "won't play from this stick until you run this command again.", file=sys.stderr)
        sys.exit(130)

    plan = plan_repoints(root, libs)
    print(f"\nRepointing {plan.count} track(s) in the rekordbox libraries")
    for old, reason in plan.skipped:
        print(f"  SKIPPED {old}\n    {reason}", file=sys.stderr)
    backup_dir = args.backup_dir or default_backup_dir()
    if plan.count:
        try:
            backup = apply_plan(root, libs, plan, backup_dir)
        except OSError as e:
            sys.exit(f"error: could not update the libraries ({e}). Nothing was changed there "
                     f"(backup: {backup_dir}). The converted music files are on the stick: free up "
                     f"some space and run the same command again, it picks up where it stopped.")
        print(f"Backup of the original library files: {backup}")
    if hasattr(os, "sync"):  # not available on Windows
        os.sync()

    problems = check_stick(root, libs)
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if stopped:
        print(f"\nSTOPPED EARLY because the stick is full: {converted} converted and their library "
              f"entries updated, {len(flacs) - converted - len(failed)} left untouched as FLAC "
              f"(their entries are unchanged). Free up space and run the same command again to "
              f"finish. Eject the stick before unplugging it.", file=sys.stderr)
    else:
        print(f"\nDone: {len(flacs) - len(failed)} converted, {len(failed)} failed, "
              f"{len(problems)} consistency problem(s). Eject the stick before unplugging it.")
    if failed:
        print("Failed files (originals kept):", file=sys.stderr)
        for rel in failed:
            print(f"  {rel}", file=sys.stderr)
    sys.exit(1 if stopped or failed or problems else 0)


if __name__ == "__main__":
    main()
