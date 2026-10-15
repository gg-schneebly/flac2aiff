"""Tests for ejecting the stick after a clean run."""

import subprocess
from types import SimpleNamespace

import convert_usb
from convert_usb import eject


def fake_run(outputs, calls):
    """Return a subprocess.run stand-in that answers commands from `outputs` and records them."""
    def run(command, **kwargs):
        calls.append(command)
        stdout, code = outputs.get(command[0], ("", 0))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=code)
    return run


def test_a_folder_that_is_not_mounted_is_left_alone(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", fake_run({}, calls))
    assert "not a mount point" in eject(tmp_path)
    assert calls == []


def test_linux_unmounts_the_partition_and_powers_off_the_drive(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(convert_usb.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(convert_usb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run({"findmnt": ("/dev/sdb1\n", 0),
                                                     "lsblk": ("sdb\n", 0)}, calls))
    assert eject(tmp_path) is None
    assert ["udisksctl", "unmount", "-b", "/dev/sdb1"] in calls
    assert ["udisksctl", "power-off", "-b", "/dev/sdb"] in calls


def test_a_failing_unmount_is_reported(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(convert_usb.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(convert_usb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run({"findmnt": ("/dev/sdb1\n", 0),
                                                     "lsblk": ("sdb\n", 0),
                                                     "udisksctl": ("", 1)}, calls))
    assert "failed" in eject(tmp_path)
    assert not any("power-off" in c for c in calls)


def test_macos_uses_diskutil(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(convert_usb.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(convert_usb.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(subprocess, "run", fake_run({}, calls))
    assert eject(tmp_path) is None
    assert calls == [["diskutil", "eject", str(tmp_path)]]
