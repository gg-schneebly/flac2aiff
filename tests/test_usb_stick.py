"""End-to-end tests of the stick repointing on a small synthetic legacy-library stick."""

import shutil

import pytest

import rekordbox_usb
from helpers import TRACKS, anlz_file, make_stick
from rekordbox_common import anlz_folder, anlz_stored_path
from rekordbox_pdb import PdbFile
from rekordbox_usb import apply_plan, check_stick, plan_repoints

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed")

OLD_DIR = "/PIONEER/USBANLZ/P000/00000001"



def test_analysis_files_move_to_the_folder_of_the_new_path(tmp_path):
    stick = tmp_path / "stick"
    libs = make_stick(stick)
    plan = plan_repoints(stick, libs)
    assert plan.count == 2
    apply_plan(stick, libs, plan, tmp_path / "backup")

    for track, row in zip(TRACKS, PdbFile(libs.pdb).tracks()):
        folder = anlz_folder(f"{track}.aiff")
        assert row.file_path == f"{track}.aiff"
        assert row.analyze_path == f"{folder}/ANLZ0000.DAT"
        for ext in ("DAT", "EXT", "2EX"):
            moved = stick / f"{folder}/ANLZ0000.{ext}".lstrip("/")
            assert anlz_stored_path(moved.read_bytes()) == f"{track}.aiff"
    assert not (stick / OLD_DIR.lstrip("/")).exists()
    assert check_stick(stick, libs) == []
    assert (tmp_path / "backup/PIONEER/USBANLZ/P000/00000001/ANLZ0000.DAT").is_file()


def test_tracks_sharing_a_folder_get_numbered_files(tmp_path, monkeypatch):
    stick = tmp_path / "stick"
    libs = make_stick(stick)
    shared = "/PIONEER/USBANLZ/P001/00000002"
    monkeypatch.setattr(rekordbox_usb, "anlz_folder", lambda path: shared)
    foreign = stick / shared.lstrip("/")
    foreign.mkdir(parents=True)
    (foreign / "ANLZ0000.DAT").write_bytes(anlz_file("/Contents/other.mp3"))

    apply_plan(stick, libs, plan_repoints(stick, libs), tmp_path / "backup")

    stems = {}
    for f in sorted(foreign.glob("*.DAT")):
        stems[f.stem] = anlz_stored_path(f.read_bytes())
    assert stems == {"ANLZ0000": "/Contents/other.mp3", "ANLZ0001": "/Contents/A/x.aiff",
                     "ANLZ0002": "/Contents/A/y.aiff"}
    assert [t.analyze_path for t in PdbFile(libs.pdb).tracks()] == [
        f"{shared}/ANLZ0001.DAT", f"{shared}/ANLZ0002.DAT"]


def test_failure_restores_the_stick(tmp_path, monkeypatch):
    stick = tmp_path / "stick"
    libs = make_stick(stick)
    before = libs.pdb.read_bytes()
    plan = plan_repoints(stick, libs)

    def explode(*args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rekordbox_usb, "_apply_pdb", explode)
    with pytest.raises(RuntimeError):
        apply_plan(stick, libs, plan, tmp_path / "backup")

    assert libs.pdb.read_bytes() == before
    assert (stick / OLD_DIR.lstrip("/") / "ANLZ0000.DAT").is_file()
    for track in TRACKS:
        assert not (stick / anlz_folder(f"{track}.aiff").lstrip("/")).exists()
