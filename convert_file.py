#!/usr/bin/env python3
"""Convert a single FLAC file to AIFF, in place.

Usage:
    convert_file.py FILE [--id3v2-version {3,4}]

The .aiff file is written next to the original, which is deleted after the audio has
been verified. Exits with an error if FILE is not a FLAC file.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flac2aiff_common import ConversionError, check_ffmpeg, convert  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", help="FLAC file to convert")
    parser.add_argument("--id3v2-version", type=int, choices=(3, 4), default=3,
                        help="ID3 tag version written into the AIFF file (default: 3, most compatible)")
    args = parser.parse_args()

    check_ffmpeg()
    try:
        dst = convert(Path(args.file).expanduser(), id3v2_version=args.id3v2_version)
    except ConversionError as e:
        sys.exit(f"error: {e}")
    print(f"converted: {args.file} -> {dst}")


if __name__ == "__main__":
    main()
