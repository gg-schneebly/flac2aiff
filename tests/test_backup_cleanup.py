"""Tests for deleting the per-run stick backup after a clean run."""

from convert_usb import discard_backup


def make_backup(tmp_path):
    """Create a backup folder with one file inside a backups parent folder."""
    backup = tmp_path / "flac2aiff-backups" / "stick-1"
    backup.mkdir(parents=True)
    (backup / "export.pdb").write_bytes(b"x")
    return backup


def test_clean_run_deletes_backup_and_empty_parent(tmp_path):
    backup = make_backup(tmp_path)
    assert discard_backup(backup, clean=True, requested=False)
    assert not backup.parent.exists()


def test_other_backups_keep_the_parent(tmp_path):
    backup = make_backup(tmp_path)
    (backup.parent / "stick-0").mkdir()
    assert discard_backup(backup, clean=True, requested=False)
    assert not backup.exists() and backup.parent.exists()


def test_problems_keep_the_backup(tmp_path):
    backup = make_backup(tmp_path)
    assert not discard_backup(backup, clean=False, requested=False)
    assert backup.exists()


def test_backup_dir_chosen_by_user_is_kept(tmp_path):
    backup = make_backup(tmp_path)
    assert not discard_backup(backup, clean=True, requested=True)
    assert backup.exists()


def test_no_backup_is_a_no_op():
    assert not discard_backup(None, clean=True, requested=False)
