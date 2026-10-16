"""Tests for sticks where rekordbox exported both the FLAC and the AIFF of a track."""

import shutil
import subprocess

import pytest

from convert_usb import remove_duplicate
from helpers import anlz_file, build_pdb, make_stick
from rekordbox_common import anlz_folder, anlz_stored_path
from rekordbox_pdb import PdbFile
from rekordbox_usb import apply_plan, find_libraries, plan_repoints

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed")

TRACK = "/Contents/A/x"


def make_aiff(src, dst, tone="sine=duration=0.2"):
    """Write an AIFF: the audio of `src`, or a generated tone if `src` is None."""
    source = ["-i", str(src)] if src else ["-f", "lavfi", "-i", tone, "-ar", "8000", "-ac", "1"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *source, "-c:a", "pcm_s16be", str(dst)],
                   check=True)


def test_flac_with_identical_aiff_is_removed(tmp_path):
    make_stick(tmp_path, tracks=[TRACK], audio="flac")
    flac = tmp_path / "Contents/A/x.flac"
    make_aiff(flac, flac.with_suffix(".aiff"))

    assert remove_duplicate(flac)
    assert not flac.exists()


def test_flac_with_different_aiff_is_kept(tmp_path):
    make_stick(tmp_path, tracks=[TRACK], audio="flac")
    flac = tmp_path / "Contents/A/x.flac"
    make_aiff(None, flac.with_suffix(".aiff"), tone="sine=frequency=880:duration=0.2")

    assert not remove_duplicate(flac)
    assert flac.is_file()


def stick_with_aiff_row(root, aiff_analysis: bytes):
    """Stick whose library lists the FLAC (already converted) and the AIFF of one track.

    The AIFF row's analysis sits in the folder players compute for the AIFF path.
    """
    make_stick(root, tracks=[TRACK], audio="aiff")
    folder = anlz_folder(f"{TRACK}.aiff")
    (root / folder.lstrip("/")).mkdir(parents=True)
    for ext in ("DAT", "EXT", "2EX"):
        (root / f"{folder}/ANLZ0000.{ext}".lstrip("/")).write_bytes(aiff_analysis)
    build_pdb(root / "PIONEER/rekordbox/export.pdb", [
        (f"{TRACK}.flac", "x.flac", "/PIONEER/USBANLZ/P000/00000001/ANLZ0000.DAT"),
        (f"{TRACK}.aiff", "x.aiff", f"{folder}/ANLZ0000.DAT"),
    ])
    return find_libraries(root), folder


def test_identical_analysis_of_the_aiff_row_is_shared(tmp_path):
    stick = tmp_path / "stick"
    libs, folder = stick_with_aiff_row(stick, anlz_file(f"{TRACK}.aiff"))

    apply_plan(stick, libs, plan_repoints(stick, libs), tmp_path / "backup")

    rows = PdbFile(libs.pdb).tracks()
    assert [r.file_path for r in rows] == [f"{TRACK}.aiff"] * 2
    assert [r.analyze_path for r in rows] == [f"{folder}/ANLZ0000.DAT"] * 2
    assert sorted(p.name for p in (stick / folder.lstrip("/")).iterdir()) == [
        "ANLZ0000.2EX", "ANLZ0000.DAT", "ANLZ0000.EXT"]
    assert not (stick / "PIONEER/USBANLZ/P000/00000001").exists()


def test_different_analysis_of_the_aiff_row_is_never_overwritten(tmp_path):
    stick = tmp_path / "stick"
    own = anlz_file(f"{TRACK}.aiff") + b"other cues"
    libs, folder = stick_with_aiff_row(stick, own)

    apply_plan(stick, libs, plan_repoints(stick, libs), tmp_path / "backup")

    rows = PdbFile(libs.pdb).tracks()
    assert [r.analyze_path for r in rows] == [f"{folder}/ANLZ0001.DAT", f"{folder}/ANLZ0000.DAT"]
    target = stick / folder.lstrip("/")
    assert (target / "ANLZ0000.DAT").read_bytes() == own
    assert anlz_stored_path((target / "ANLZ0001.DAT").read_bytes()) == f"{TRACK}.aiff"
