"""Estimate how much disk space a FLAC -> AIFF conversion needs and warn before it starts.

AIFF is uncompressed, so converting makes files roughly 1.5x larger. Each FLAC is only deleted
after its AIFF has been verified, so the peak need is slightly above the final growth.

Two estimates bracket the real size (calibrated on 239 real tracks: the PCM size was never above
the real AIFF and low by 40 KiB to 2.4 MiB per file, the tags and cover art):
  * optimistic: PCM size only. If even this does not fit, the conversion surely fails.
  * pessimistic: PCM size plus a per-file allowance for tags and cover art. If this leaves
    little headroom, the user gets a warning.
"""

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from flac2aiff_common import TAGS_ALLOWANCE, ConversionError, estimate_aiff_size

MIN_FREE_FLOOR = 256 * 1024 ** 2        # a conversion never fills the disk below this
LIBRARY_RESERVE = 16 * 1024 ** 2        # stick: rewriting the two track libraries
ANLZ_RESERVE_PER_TRACK = 1024 ** 2      # stick: new analysis files exist before the old are removed
TIGHT_MIN_HEADROOM = 512 * 1024 ** 2    # warn if less than this is left at the peak ...
TIGHT_HEADROOM_FRACTION = 0.05          # ... or less than this share of the growth
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1      # largest file a FAT32 stick can hold
PROBE_WORKERS = 8


@dataclass
class SpaceVerdict:
    """Outcome of a space check.

    Attributes:
        status: "ok", "tight" (may not leave enough headroom) or "too_small" (will not fit).
        free: Free bytes on the target.
        growth_low: Optimistic total growth in bytes.
        growth_high: Pessimistic total growth in bytes.
        peak_low: Optimistic extra bytes needed at the worst moment of the run.
        peak_high: Pessimistic extra bytes needed at the worst moment of the run.
        reserve: Bytes kept free on top of the AIFF files (floor plus what finishing the run needs).
        flac_total: Total size of the FLAC files that could be estimated.
        oversize: Files whose AIFF would reach 4 GiB (too big for FAT32).
        unreadable: Files that could not be probed and are not counted.
    """

    status: str
    free: int
    growth_low: int
    growth_high: int
    peak_low: int
    peak_high: int
    reserve: int
    flac_total: int
    oversize: list[Path] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)


def peak_usage(sizes: list[tuple[int, int]]) -> tuple[int, int]:
    """Return (peak extra bytes, total growth) for converting files one after another.

    While a file is converted, its new version exists next to the old one, so the peak at that
    moment is the growth so far plus the new size.

    Args:
        sizes: (new size, old size) per file, in conversion order.
    """
    growth = peak = 0
    for new, old in sizes:
        peak = max(peak, growth + new)
        growth += new - old
    return peak, growth


def finalization_reserve(tracks: int) -> int:
    """Return the bytes a stick conversion needs after the audio: library and analysis updates.

    Args:
        tracks: Number of tracks being converted.
    """
    return LIBRARY_RESERVE + tracks * ANLZ_RESERVE_PER_TRACK


def assess_space(flac_sizes: list[int], pcm_sizes: list[int], free: int,
                 reserve: int = MIN_FREE_FLOOR) -> SpaceVerdict:
    """Compare the estimated space needed with the free space.

    Args:
        flac_sizes: Current size of each FLAC in bytes, in conversion order.
        pcm_sizes: Estimated PCM (optimistic AIFF) size of each file, same order.
        free: Free bytes on the target.
        reserve: Bytes that must stay free on top of the AIFF files.
    """
    low = list(zip(pcm_sizes, flac_sizes))
    high = [(pcm + TAGS_ALLOWANCE, flac) for pcm, flac in low]
    peak_low, growth_low = peak_usage(low)
    peak_high, growth_high = peak_usage(high)
    peak_low, peak_high = peak_low + reserve, peak_high + reserve
    margin = max(TIGHT_MIN_HEADROOM, TIGHT_HEADROOM_FRACTION * growth_high)
    if free < peak_low:
        status = "too_small"
    elif free < peak_high + margin:
        status = "tight"
    else:
        status = "ok"
    return SpaceVerdict(status, free, growth_low, growth_high, peak_low, peak_high, reserve,
                        sum(flac_sizes))


def estimate_sizes(flacs: list[Path]) -> tuple[list[Path], list[int], list[int], list[Path]]:
    """Probe the files (in parallel) for their estimated AIFF size.

    Args:
        flacs: FLAC files in conversion order.

    Returns:
        (readable files, their FLAC sizes, their estimated PCM sizes, unreadable files).
    """
    def probe(path: Path) -> int | None:
        try:
            return estimate_aiff_size(path)
        except (ConversionError, KeyError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        estimates = list(pool.map(probe, flacs))
    ok = [(f, e) for f, e in zip(flacs, estimates) if e is not None]
    return ([f for f, _ in ok], [f.stat().st_size for f, _ in ok], [e for _, e in ok],
            [f for f, e in zip(flacs, estimates) if e is None])


def gib(n: float) -> str:
    """Format a byte count as GiB with one decimal."""
    return f"{n / 1024 ** 3:.1f} GiB"


def check_free_space(flacs: list[Path], target: Path, reserve: int = 0) -> SpaceVerdict:
    """Estimate the space the conversion needs, print the result and any warning.

    The caller decides what to do with a "too_small" verdict (the scripts exit). The scripts
    also pass `verdict.reserve` to `convert()`, which stops the run before the disk gets fuller.

    Args:
        flacs: FLAC files to convert, in conversion order.
        target: Any path on the filesystem that receives the AIFF files.
        reserve: Extra bytes the run needs after the audio (see `finalization_reserve`).
    """
    files, flac_sizes, pcm_sizes, unreadable = estimate_sizes(flacs)
    free = shutil.disk_usage(target).free
    verdict = assess_space(flac_sizes, pcm_sizes, free, MIN_FREE_FLOOR + reserve)
    verdict.unreadable = unreadable
    verdict.oversize = [f for f, pcm in zip(files, pcm_sizes) if pcm + TAGS_ALLOWANCE > FAT32_MAX_FILE]

    print(f"Space check: {len(files)} FLAC file(s), {gib(verdict.flac_total)}, become about "
          f"{gib(verdict.flac_total + verdict.growth_low)}-{gib(verdict.flac_total + verdict.growth_high)} "
          f"as AIFF (growth {gib(verdict.growth_low)}-{gib(verdict.growth_high)}); "
          f"{gib(free)} free on {target}")
    if unreadable:
        print(f"WARNING: {len(unreadable)} file(s) could not be read and are not counted "
              f"(they will fail during conversion).")
    if verdict.oversize:
        print(f"WARNING: {len(verdict.oversize)} file(s) would be 4 GiB or larger as AIFF. FAT32 "
              f"drives cannot store such files; they will fail to convert and their FLACs stay.")
    if verdict.status == "tight":
        print(f"WARNING: free space could get tight. At the busiest moment the conversion needs "
              f"{gib(verdict.peak_low)}-{gib(verdict.peak_high)}, leaving "
              f"{gib(free - verdict.peak_high)}-{gib(free - verdict.peak_low)} free. Free up some "
              f"space first or press Ctrl-C to cancel.")
    return verdict


def require_space(verdict: SpaceVerdict) -> None:
    """Exit with an error if the conversion surely does not fit.

    Args:
        verdict: Result of `check_free_space`.
    """
    if verdict.status == "too_small":
        raise SystemExit(f"error: not enough free space: the conversion needs at least "
                         f"{gib(verdict.peak_low)} at its busiest moment but only "
                         f"{gib(verdict.free)} is free. Nothing was changed.")
