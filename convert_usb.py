#!/usr/bin/env python3
"""Convert the FLAC files of a rekordbox USB/SD export to AIFF and keep the export working.

Usage:
    convert_usb.py STICK [--dry-run] [--no-eject] [--id3v2-version {3,4}] [--backup-dir DIR]

STICK is the mount point of the stick (e.g. /run/media/you/MYSTICK or /Volumes/MYSTICK).
Works with the legacy library (PIONEER/rekordbox/export.pdb), with Device Library Plus
(exportLibrary.db), and with sticks that have both. Per track it
  * converts the FLAC to a bit-identical AIFF next to it, then deletes the FLAC
  * repoints the library rows to the AIFF and moves the track's analysis (ANLZ) files to the
    folder the players compute from the new path, so waveforms, beatgrids and cues keep working
Cues, beatgrids, playlists etc. are kept. A backup of every library/analysis file that is
modified is written to ~/flac2aiff-backups/ (not onto the stick) before anything changes. It is
deleted again when the run finishes cleanly (no failures, no consistency problems), because the
FLACs it refers to are gone by then; otherwise it is kept. A --backup-dir is always kept.

If the run is interrupted, run it again: it converts what is left and then repoints
everything whose FLAC is gone. After a clean run the stick is unmounted and powered off, so it
can be unplugged right away (--no-eject keeps it mounted). Otherwise it stays mounted.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flac2aiff_common import (  # noqa: E402
    ConversionError, OutOfSpaceError, check_ffmpeg, convert, find_flacs,
)
from rekordbox_usb import (  # noqa: E402
    LibraryReadError, apply_plan, check_stick, default_backup_dir, find_libraries,
    plan_repoints,
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
                        help="where to put the backup and keep it (default: ~/flac2aiff-backups/"
                             "stick-<timestamp>, deleted after a clean run)")
    parser.add_argument("--no-eject", action="store_true",
                        help="leave the stick mounted after a clean run (default: unmount and power it off)")
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

    try:  # read the libraries before converting anything: a damaged one stops the run here
        plan = plan_repoints(root, libs)
    except LibraryReadError as e:
        sys.exit(f"error: {e}. Nothing was changed.")

    if args.dry_run:
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
    backup = None
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
              f"finish.", file=sys.stderr)
    else:
        print(f"\nDone: {len(flacs) - len(failed)} converted, {len(failed)} failed, "
              f"{len(problems)} consistency problem(s).")
    if failed:
        print("Failed files (originals kept):", file=sys.stderr)
        for rel in failed:
            print(f"  {rel}", file=sys.stderr)
    clean = not (stopped or failed or problems)
    if discard_backup(backup, clean=clean, requested=args.backup_dir is not None):
        print("The run was clean, so the backup of the library files was deleted.")
    elif backup:
        print(f"Kept the backup of the library files: {backup}")
    if not clean:
        print("The stick is still mounted. Eject it before unplugging it.", file=sys.stderr)
    elif args.no_eject:
        print("The stick is still mounted (--no-eject). Eject it before unplugging it.")
    else:
        error = eject(root)
        if error:
            print(f"Could not eject the stick ({error}). Eject it yourself before unplugging it.",
                  file=sys.stderr)
        else:
            print("The stick was ejected: you can unplug it now.")
    sys.exit(0 if clean else 1)


def eject(root: Path) -> str | None:
    """Unmount the stick and power it off, so it can be unplugged safely.

    Uses `udisksctl` on Linux (no root needed) and `diskutil` on macOS. Does nothing to a
    folder that is not itself a mounted device, e.g. a copy of a stick on this computer.

    Args:
        root: Mount point of the stick.

    Returns:
        None on success, otherwise why the stick could not be ejected.
    """
    if not os.path.ismount(root):
        return f"{root} is not a mount point"
    if platform.system() == "Darwin":
        commands = [["diskutil", "eject", str(root)]]
    else:
        source = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(root)],
                                capture_output=True, text=True)
        device = source.stdout.strip()
        if source.returncode != 0 or not device.startswith("/dev/"):
            return f"no device found for {root}"
        parent = subprocess.run(["lsblk", "-n", "-d", "-o", "PKNAME", device],
                                capture_output=True, text=True).stdout.strip()
        disk = f"/dev/{parent}" if parent else device  # power off the whole drive, not a partition
        commands = [["udisksctl", "unmount", "-b", device],
                    ["udisksctl", "power-off", "-b", disk]]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            return f"{command[0]} is not installed"
        if result.returncode != 0:
            return result.stderr.strip() or f"{' '.join(command)} failed"
    return None


def discard_backup(backup: Path | None, clean: bool, requested: bool) -> bool:
    """Delete the per-run backup after a clean run, unless the user chose where to keep it.

    After a clean run the FLACs the backup refers to are gone, so restoring it would only
    break the stick; it is useful only while the libraries are being written or when
    something went wrong.

    Args:
        backup: The backup folder written by `apply_plan`, or None if nothing was backed up.
        clean: True if nothing failed, the run was not stopped and the stick check passed.
        requested: True if the user asked for the backup with --backup-dir.

    Returns:
        True if the backup was deleted.
    """
    if backup is None or not clean or requested:
        return False
    shutil.rmtree(backup)
    if not any(backup.parent.iterdir()):  # ~/flac2aiff-backups held only this backup
        backup.parent.rmdir()
    return True


if __name__ == "__main__":
    main()
