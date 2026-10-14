"""Tests for the free-space estimate and its warnings."""

import shutil
import subprocess
from collections import namedtuple

import pytest

import space_check
from flac2aiff_common import convert, estimate_aiff_size
from space_check import (
    TAGS_ALLOWANCE, assess_space, check_free_space, peak_usage, require_space,
)

MIB = 1024 ** 2
Usage = namedtuple("Usage", "total used free")


def test_peak_is_growth_so_far_plus_the_file_being_written():
    # two files, each 10 -> 15: the second is written while 5 of growth already exists
    assert peak_usage([(15, 10), (15, 10)]) == (20, 10)
    assert peak_usage([]) == (0, 0)


def test_verdicts():
    flac, pcm = [100 * MIB] * 10, [150 * MIB] * 10            # +500 MiB growth, peak ~ 600 MiB
    assert assess_space(flac, pcm, 10 * 1024 * MIB).status == "ok"
    assert assess_space(flac, pcm, 550 * MIB).status == "too_small"      # below even the optimistic peak
    assert assess_space(flac, pcm, 1000 * MIB).status == "tight"         # fits, but headroom < 512 MiB
    assert assess_space(flac, pcm, 1500 * MIB).status == "ok"


def test_pessimistic_estimate_adds_tag_allowance_per_file():
    verdict = assess_space([100 * MIB] * 4, [150 * MIB] * 4, 10 ** 12)
    assert verdict.growth_high - verdict.growth_low == 4 * TAGS_ALLOWANCE


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed")
def test_estimate_is_a_lower_bound_close_to_the_real_aiff(tmp_path):
    flac = tmp_path / "a.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=3", "-ar", "44100",
                    "-ac", "2", "-c:a", "flac", str(flac)], check=True)
    estimate = estimate_aiff_size(flac)
    actual = convert(flac).stat().st_size
    assert 0 <= actual - estimate <= TAGS_ALLOWANCE


def fake_estimates(monkeypatch, tmp_path, pcm_sizes, free):
    files = []
    for i in range(len(pcm_sizes)):
        f = tmp_path / f"{i}.flac"
        f.write_bytes(b"x" * 100)
        files.append(f)
    monkeypatch.setattr(space_check, "estimate_sizes",
                        lambda flacs: (files, [100] * len(files), pcm_sizes, []))
    monkeypatch.setattr(space_check.shutil, "disk_usage", lambda p: Usage(0, 0, free))
    return files


def test_too_small_exits_before_anything_is_converted(tmp_path, monkeypatch, capsys):
    files = fake_estimates(monkeypatch, tmp_path, [500 * MIB], free=100 * MIB)
    verdict = check_free_space(files, tmp_path)
    with pytest.raises(SystemExit, match="not enough free space"):
        require_space(verdict)


def test_tight_warns_but_does_not_exit(tmp_path, monkeypatch, capsys):
    files = fake_estimates(monkeypatch, tmp_path, [500 * MIB], free=800 * MIB)
    verdict = check_free_space(files, tmp_path)
    require_space(verdict)
    assert verdict.status == "tight" and "could get tight" in capsys.readouterr().out


def test_files_that_cannot_exist_on_fat32_are_flagged(tmp_path, monkeypatch, capsys):
    files = fake_estimates(monkeypatch, tmp_path, [5 * 1024 * MIB], free=100 * 1024 * MIB)
    assert check_free_space(files, tmp_path).oversize == files
    assert "FAT32" in capsys.readouterr().out


def test_unreadable_files_are_reported(tmp_path, capsys):
    bad = tmp_path / "broken.flac"
    bad.write_bytes(b"not audio")
    verdict = check_free_space([bad], tmp_path)
    assert verdict.unreadable == [bad] and "could not be read" in capsys.readouterr().out
