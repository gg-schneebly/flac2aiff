"""Tests for the PDB patcher, ANLZ patching and the path hash, using small synthetic files."""

import pytest

from helpers import anlz_file, build_pdb
from rekordbox_common import anlz_folder, anlz_stored_path, patch_anlz_extension
from rekordbox_pdb import PdbFile

def test_pdb_reads_tracks(tmp_path):
    build_pdb(tmp_path / "export.pdb", [("/Contents/a/x.flac", "x.flac", "/PIONEER/USBANLZ/P000/1/ANLZ0000.DAT")])
    (track,) = PdbFile(tmp_path / "export.pdb").tracks()
    assert (track.file_path, track.filename, track.file_type) == ("/Contents/a/x.flac", "x.flac", 5)
    assert track.analyze_path == "/PIONEER/USBANLZ/P000/1/ANLZ0000.DAT"


def test_pdb_repoint_only_changes_the_track_fields(tmp_path):
    path = tmp_path / "export.pdb"
    build_pdb(path, [("/Contents/ä/x.flac", "ä.flac", "/a"), ("/Contents/y.flac", "y.flac", "/b")])
    before = path.read_bytes()
    pdb = PdbFile(path)
    first = pdb.tracks()[0]
    pdb.repoint(first, "/Contents/ä/x.aiff", 12, 2000, 1411)
    pdb.save()

    after = path.read_bytes()
    assert len(after) == len(before)
    tracks = PdbFile(path).tracks()
    assert (tracks[0].file_path, tracks[0].filename) == ("/Contents/ä/x.aiff", "x.aiff")
    assert (tracks[0].file_type, tracks[0].file_size, tracks[0].bitrate) == (12, 2000, 1411)
    assert (tracks[1].file_path, tracks[1].file_type) == ("/Contents/y.flac", 5)


def test_pdb_repoint_rejects_length_change(tmp_path):
    path = tmp_path / "export.pdb"
    build_pdb(path, [("/Contents/x.flac", "x.flac", "/a")])
    pdb = PdbFile(path)
    with pytest.raises(Exception):
        pdb.repoint(pdb.tracks()[0], "/Contents/x.aif", 12, 1, 1)


def test_anlz_patch_swaps_extension_only():
    data = anlz_file("/Contents/a/x.flac")
    patched = patch_anlz_extension(data)
    assert anlz_stored_path(patched) == "/Contents/a/x.aiff"
    assert len(patched) == len(data)
    assert patched.count(b"PQTZ") == 1 and patched.endswith(data[-16:])


def test_anlz_patch_ignores_other_extensions():
    assert patch_anlz_extension(anlz_file("/Contents/a/x.mp3")) is None


def test_anlz_rejects_garbage():
    with pytest.raises(ValueError):
        patch_anlz_extension(b"not an anlz file")


def test_pdb_repoint_can_update_the_analysis_path(tmp_path):
    path = tmp_path / "export.pdb"
    build_pdb(path, [("/Contents/x.flac", "x.flac", "/PIONEER/USBANLZ/P000/00000001/ANLZ0000.DAT")])
    pdb = PdbFile(path)
    pdb.repoint(pdb.tracks()[0], "/Contents/x.aiff", 12, 2, 3,
                analyze_path="/PIONEER/USBANLZ/P0AB/000000CD/ANLZ0000.DAT")
    pdb.save()
    assert PdbFile(path).tracks()[0].analyze_path == "/PIONEER/USBANLZ/P0AB/000000CD/ANLZ0000.DAT"


@pytest.mark.parametrize("path, folder", [
    # three probe files exported by rekordbox
    ("/Contents/UnknownArtist/UnknownAlbum/a.aiff", "/PIONEER/USBANLZ/P038/0000AE88"),
    ("/Contents/UnknownArtist/UnknownAlbum/t5.aiff", "/PIONEER/USBANLZ/P067/00012D6F"),
    ("/Contents/UnknownArtist/UnknownAlbum/xxxxx.aiff", "/PIONEER/USBANLZ/P078/0001B2B2"),
    # a real stick path, and the example published with the algorithm
    ("/Contents/Schierke/DINA Presents Rituals/Schierke - A Blueprint For The Witch (Origi.flac",
     "/PIONEER/USBANLZ/P03D/00007BDB"),
    ("/Contents/Leo Portela/Bon Vibrant - Leo Portela.flac", "/PIONEER/USBANLZ/P00E/000281CE"),
    # characters outside the BMP are hashed as two UTF-16 code units
    ("/Contents/loveisthetruth/\U0001d4c1\U0001d4c8\U0001d4be/x.flac", None),
])
def test_anlz_folder_matches_rekordbox(path, folder):
    if folder is None:
        assert anlz_folder(path) != anlz_folder(path.replace("\U0001d4c1", "l"))
    else:
        assert anlz_folder(path) == folder
