"""Shared FLAC -> AIFF conversion logic (used by convert_library.py and convert_file.py).

Conversion is done with ffmpeg:
  * audio is decoded losslessly and written as big-endian PCM at the source bit depth
    (16-bit FLAC -> 16-bit AIFF, 24-bit FLAC -> 24-bit AIFF, ...); sample rate and
    channel layout are unchanged
  * all tags (Vorbis comments) are copied into an ID3v2 chunk inside the AIFF
  * embedded cover art is copied as an ID3 attached picture

Before the original FLAC is deleted, the decoded audio of both files is hashed (MD5)
and compared. The FLAC is only removed if the audio is bit-identical.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path


class ConversionError(Exception):
    pass


class NotFlacError(ConversionError):
    pass


# bits per sample -> ffmpeg AIFF codec
PCM_CODECS = {8: "pcm_s8", 16: "pcm_s16be", 24: "pcm_s24be", 32: "pcm_s32be"}

# Vorbis comment keys that ffmpeg does not map to a standard ID3 frame on its own
# (it would write them as custom TXXX frames, which e.g. DJ software ignores)
ID3_FRAMES = {
    "BPM": "TBPM",
    "INITIALKEY": "TKEY",
    "KEY": "TKEY",
    "ISRC": "TSRC",
    "LABEL": "TPUB",
    "ORGANIZATION": "TPUB",
    "PUBLISHER": "TPUB",
}


def check_ffmpeg():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise SystemExit(
            f"error: {', '.join(missing)} not found on PATH. Install with: brew install ffmpeg"
        )


def is_flac_path(path):
    return path.suffix.lower() == ".flac"


def has_flac_signature(path):
    """True if the file starts with the FLAC marker 'fLaC' (optionally after an ID3v2 tag)."""
    with open(path, "rb") as f:
        header = f.read(10)
        if header[:3] == b"ID3" and len(header) == 10:
            # ID3v2 size is a 28-bit "syncsafe" integer; +10 header, +10 if a footer is present
            size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
            f.seek(10 + size + (10 if header[5] & 0x10 else 0))
            return f.read(4) == b"fLaC"
        return header[:4] == b"fLaC"


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConversionError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def audio_stream(info, path):
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise ConversionError(f"no audio stream found in {path}")
    return streams[0]


def bit_depth(stream):
    # FLAC reports its real depth in bits_per_raw_sample (e.g. 24 while sample_fmt is s32)
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            bits = int(stream.get(key) or 0)
        except ValueError:
            bits = 0
        if bits:
            break
    else:
        bits = {"s16": 16, "s16p": 16, "s32": 32, "s32p": 32, "u8": 8, "u8p": 8}.get(
            stream.get("sample_fmt"), 16
        )
    # round odd depths (e.g. 20-bit) up to the next depth AIFF/ffmpeg can store
    for supported in sorted(PCM_CODECS):
        if bits <= supported:
            return supported
    raise ConversionError(f"unsupported bit depth: {bits}")


def audio_md5(path):
    """MD5 of the decoded audio, normalised to 32-bit PCM so FLAC and AIFF are comparable."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s32le", "-f", "md5", "-"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConversionError(f"could not decode {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def convert(src, id3v2_version=3, dry_run=False):
    """Convert one FLAC file to AIFF next to it, then delete the FLAC.

    Returns the path of the new AIFF file.
    """
    src = Path(src)
    if not src.is_file():
        raise ConversionError(f"not a file: {src}")
    if not is_flac_path(src):
        raise NotFlacError(f"not a FLAC file (extension is '{src.suffix}'): {src}")
    if not has_flac_signature(src):
        raise NotFlacError(f"not a valid FLAC file (has .flac extension but no FLAC header): {src}")

    info = probe(src)
    stream = audio_stream(info, src)
    if stream.get("codec_name") != "flac":
        raise NotFlacError(f"not a FLAC file (audio codec is '{stream.get('codec_name')}'): {src}")

    dst = src.with_suffix(".aiff")
    if dst.exists():
        raise ConversionError(f"target already exists, refusing to overwrite: {dst}")

    codec = PCM_CODECS[bit_depth(stream)]
    if dry_run:
        return dst

    tmp = dst.with_name(f".{dst.name}.part")
    has_cover = any(
        s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic")
        for s in info.get("streams", [])
    )

    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src), "-map", "0:a:0"]
    if has_cover:
        cmd += ["-map", "0:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    cmd += ["-c:a", codec, "-map_metadata", "0"]
    for key, value in info.get("format", {}).get("tags", {}).items():
        frame = ID3_FRAMES.get(key.upper())
        if frame:
            # write the standard frame and drop the original key
            cmd += ["-metadata", f"{frame}={value}", "-metadata", f"{key}="]
    cmd += [
        "-write_id3v2", "1",
        "-id3v2_version", str(id3v2_version),
        "-f", "aiff",
        str(tmp),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ConversionError(f"ffmpeg failed on {src}: {result.stderr.strip()}")

        if audio_md5(src) != audio_md5(tmp):
            raise ConversionError(f"audio verification failed (output differs from source): {src}")

        st = src.stat()
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
    finally:
        # also runs on Ctrl-C, so no half-written files are left behind
        if tmp.exists():
            tmp.unlink()

    src.unlink()
    return dst
