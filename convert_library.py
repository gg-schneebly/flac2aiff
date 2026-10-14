#!/usr/bin/env python3
"""Convert every FLAC file in a music library (recursively) to AIFF, in place.

Usage:
    convert_library.py [DIRECTORY] [--dry-run] [--id3v2-version {3,4}]

DIRECTORY defaults to the current working directory. Each new .aiff file is written
next to its .flac original, which is deleted after the audio has been verified.
All other files and the directory structure are left untouched.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flac2aiff_common import (  # noqa: E402
    ConversionError, OutOfSpaceError, check_ffmpeg, convert, find_flacs,
)
from space_check import check_free_space, require_space  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", nargs="?", default=".", help="music library root (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be converted")
    parser.add_argument("--id3v2-version", type=int, choices=(3, 4), default=3,
                        help="ID3 tag version written into the AIFF files (default: 3, most compatible)")
    args = parser.parse_args()

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"error: not a directory: {root}")
    check_ffmpeg()

    flacs = list(find_flacs(root))
    print(f"Found {len(flacs)} FLAC file(s) in {root}")
    min_free = 0
    if flacs:
        verdict = check_free_space(flacs, root)
        require_space(verdict)
        min_free = verdict.reserve

    failed, converted, stopped = [], 0, False
    for i, src in enumerate(flacs, 1):
        rel = src.relative_to(root)
        try:
            dst = convert(src, id3v2_version=args.id3v2_version, dry_run=args.dry_run,
                          min_free=min_free)
            verb = "would convert" if args.dry_run else "converted"
            print(f"[{i}/{len(flacs)}] {verb}: {rel} -> {dst.name}")
            converted += 1
        except OutOfSpaceError as e:
            print(f"[{i}/{len(flacs)}] STOPPED: {e}", file=sys.stderr)
            stopped = True
            break
        except ConversionError as e:
            print(f"[{i}/{len(flacs)}] FAILED: {rel}\n    {e}", file=sys.stderr)
            failed.append(rel)
        except KeyboardInterrupt:
            print("\nInterrupted. Files converted so far are done; the current file was left untouched.",
                  file=sys.stderr)
            sys.exit(130)

    if stopped:
        print(f"\nStopped early because the disk is full: {converted} converted, "
              f"{len(flacs) - converted - len(failed)} left untouched as FLAC. Free up space and "
              f"run the same command again to continue.", file=sys.stderr)
        sys.exit(1)
    print(f"\nDone: {len(flacs) - len(failed)} succeeded, {len(failed)} failed.")
    if failed:
        print("Failed files (originals kept):", file=sys.stderr)
        for rel in failed:
            print(f"  {rel}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
