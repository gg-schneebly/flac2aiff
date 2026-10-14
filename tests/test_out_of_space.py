"""Tests for what happens when the disk is too full or fills up during a run."""

import shutil
import subprocess
import sys
from collections import namedtuple

import pytest

import convert_library
import convert_usb
import flac2aiff_common
from flac2aiff_common import OutOfSpaceError, convert
from helpers import TRACKS, make_stick
from rekordbox_common import anlz_folder
from rekordbox_pdb import PdbFile
from rekordbox_usb import find_libraries

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed")
Usage = namedtuple("Usage", "total used free")


def make_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.5",
                    "-ar", "8000", "-ac", "1", "-c:a", "flac", str(path)], check=True)
    return path


def nothing_left_behind(flac):
    siblings = {p.name for p in flac.parent.iterdir()}
    return flac.exists() and siblings == {flac.name}


def test_refuses_up_front_when_the_next_file_does_not_fit(tmp_path, monkeypatch):
    flac = make_flac(tmp_path / "a.flac")
    monkeypatch.setattr(flac2aiff_common.shutil, "disk_usage", lambda p: Usage(0, 0, 1024))
    with pytest.raises(OutOfSpaceError):
        convert(flac)
    assert nothing_left_behind(flac)


def test_reserve_is_kept_free(tmp_path, monkeypatch):
    flac = make_flac(tmp_path / "a.flac")
    monkeypatch.setattr(flac2aiff_common.shutil, "disk_usage", lambda p: Usage(0, 0, 10 * 1024 ** 2))
    with pytest.raises(OutOfSpaceError):
        convert(flac, min_free=50 * 1024 ** 2)   # fits on its own, but not with the reserve
    assert nothing_left_behind(flac)
    assert convert(flac).suffix == ".aiff"


def test_disk_filling_up_mid_write_keeps_the_flac(tmp_path, monkeypatch):
    flac = make_flac(tmp_path / "a.flac")
    real_run = subprocess.run

    def full_disk(cmd, *args, **kwargs):
        if cmd[0] == "ffmpeg" and "-write_id3v2" in cmd:   # the conversion, not the checksum runs
            open(cmd[-1], "wb").write(b"half an AIFF")
            return subprocess.CompletedProcess(cmd, 1, "", "av_interleaved_write_frame(): No space left on device")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(flac2aiff_common.subprocess, "run", full_disk)
    with pytest.raises(OutOfSpaceError):
        convert(flac)
    assert nothing_left_behind(flac)


def run_main(module, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    with pytest.raises(SystemExit) as stop:
        module.main()
    return stop.value.code


def stop_after_first(real_convert, calls):
    """A convert() that works once and then reports a full disk."""
    def convert_then_fill_up(src, **kwargs):
        calls.append(src.name)
        if len(calls) > 1:
            raise OutOfSpaceError("the disk filled up")
        return real_convert(src, **kwargs)
    return convert_then_fill_up


def test_library_run_stops_at_the_first_full_disk(tmp_path, monkeypatch, capsys):
    flacs = [make_flac(tmp_path / f"{n}.flac") for n in "abc"]
    calls = []
    monkeypatch.setattr(convert_library, "convert", stop_after_first(convert, calls))

    assert run_main(convert_library, monkeypatch, str(tmp_path)) == 1
    assert calls == ["a.flac", "b.flac"]                    # c.flac was never attempted
    assert not flacs[0].exists() and (tmp_path / "a.aiff").exists()
    assert flacs[1].exists() and flacs[2].exists()          # untouched
    assert "Stopped early" in capsys.readouterr().err


def test_stick_run_stops_but_still_finishes_the_converted_tracks(tmp_path, monkeypatch, capsys):
    stick = tmp_path / "stick"
    tracks = TRACKS + ["/Contents/A/z"]
    make_stick(stick, tracks, audio="flac")
    calls = []
    monkeypatch.setattr(convert_usb, "convert", stop_after_first(convert, calls))

    code = run_main(convert_usb, monkeypatch, str(stick), "--backup-dir", str(tmp_path / "backup"))

    assert code == 1 and calls == ["x.flac", "y.flac"]
    rows = {t.file_path: t for t in PdbFile(find_libraries(stick).pdb).tracks()}
    done = rows["/Contents/A/x.aiff"]                       # converted: library and analysis updated
    assert done.analyze_path == f"{anlz_folder('/Contents/A/x.aiff')}/ANLZ0000.DAT"
    assert (stick / done.analyze_path.lstrip("/")).is_file()
    for left in ("y", "z"):                                 # not converted: FLAC and entry unchanged
        assert (stick / f"Contents/A/{left}.flac").is_file()
        assert rows[f"/Contents/A/{left}.flac"].analyze_path.startswith("/PIONEER/USBANLZ/P000/")
    assert "STOPPED EARLY" in capsys.readouterr().err
