"""Repoint the tracks of a rekordbox USB/SD export from .flac to .aiff.

A rekordbox export can hold two track libraries side by side, and a stick may have either
or both:
  * legacy `PIONEER/rekordbox/export.pdb` (DeviceSQL), read by older players
  * Device Library Plus `PIONEER/rekordbox/exportLibrary.db` (encrypted SQLite)
Both, plus the per-track analysis (ANLZ) files in `PIONEER/USBANLZ/`, store the track's path.
Cues, beatgrids, playlists and everything else are kept because the existing rows and files
are patched, never rebuilt.

Players do not trust the analysis pointer in the library: they compute the analysis folder
from a hash of the track path (see `rekordbox_common.anlz_folder`). Renaming `.flac` to
`.aiff` changes the hash, so each track's analysis files are moved to the new folder. If two
tracks hash to the same folder, their files are numbered `ANLZ0000`, `ANLZ0001`, ... and the
path stored inside each file tells them apart.
"""

import shutil
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from flac2aiff_common import ConversionError
from rekordbox_common import (
    ANLZ_SUFFIXES, FILETYPE_AIFF, AiffInfo, anlz_folder, anlz_stored_path,
    patch_anlz_extension, read_aiff_info,
)
from rekordbox_pdb import PdbFile

RB_DIR = Path("PIONEER") / "rekordbox"
PDB_NAME = "export.pdb"
DLP_NAME = "exportLibrary.db"


@dataclass
class Libraries:
    """Which track libraries a stick has.

    Attributes:
        pdb: Path of the legacy export.pdb, or None.
        dlp: Path of the Device Library Plus exportLibrary.db, or None.
    """

    pdb: Path | None
    dlp: Path | None


@dataclass
class Repoint:
    """One track to be repointed.

    Attributes:
        old_path: Track path as stored in the library, e.g. `/Contents/A/x.flac`.
        new_path: Same path with the `.aiff` extension.
        info: Properties of the existing AIFF file.
        anlz_path: Analysis file path as stored in the library (may be empty).
    """

    old_path: str
    new_path: str
    info: AiffInfo
    anlz_path: str


@dataclass
class Plan:
    """What has to change on a stick.

    Attributes:
        pdb: Repoints for the legacy library (empty if the stick has none).
        dlp: Repoints for Device Library Plus (empty if the stick has none).
        skipped: (path, reason) for FLAC tracks that cannot be repointed.
    """

    pdb: list[Repoint] = field(default_factory=list)
    dlp: list[Repoint] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of distinct tracks to repoint."""
        return len({r.old_path for r in self.pdb + self.dlp})


def find_libraries(root: Path) -> Libraries:
    """Locate the track libraries on a stick (file names are matched case-insensitively).

    Args:
        root: Mount point of the stick.
    """
    folder = root / RB_DIR
    found = {p.name.lower(): p for p in folder.iterdir()} if folder.is_dir() else {}
    return Libraries(pdb=found.get(PDB_NAME.lower()), dlp=found.get(DLP_NAME.lower()))


def stick_file(root: Path, library_path: str) -> Path:
    """Map a path stored in a library (`/Contents/x.flac`) to the file on the stick.

    Args:
        root: Mount point of the stick.
        library_path: Path as stored in the library.
    """
    return root / library_path.lstrip("/")


def open_dlp(path: Path):
    """Open a Device Library Plus database (SQLCipher) with pyrekordbox.

    Args:
        path: Path of exportLibrary.db.
    """
    from pyrekordbox import OneLibrary  # imported late: needs the optional sqlcipher3 package
    return OneLibrary(path)


def _read_libraries(libs: Libraries):
    """Return {format: [(path, analyze_path)]} for the FLAC tracks of each library."""
    tracks = {"pdb": [], "dlp": []}
    if libs.pdb:
        tracks["pdb"] = [(t.file_path, t.analyze_path) for t in PdbFile(libs.pdb).tracks()]
    if libs.dlp:
        from pyrekordbox.onelibrary import models
        with warnings.catch_warnings(), open_dlp(libs.dlp) as db:
            warnings.simplefilter("ignore")
            tracks["dlp"] = [(c.path, c.analysisDataFilePath or "")
                             for c in db.query(models.Content).all()]
    return {k: [(p, a) for p, a in v if p.lower().endswith(".flac")] for k, v in tracks.items()}


def plan_repoints(root: Path, libs: Libraries) -> Plan:
    """Work out which FLAC tracks need repointing.

    A track qualifies when its FLAC is gone and an `.aiff` of the same name exists next
    to where it was. Re-running after a partial run therefore picks up where it stopped.

    Args:
        root: Mount point of the stick.
        libs: Libraries found on the stick.
    """
    plan = Plan()
    cache, skipped = {}, set()
    for kind, tracks in _read_libraries(libs).items():
        for old, anlz in tracks:
            if stick_file(root, old).exists():
                continue  # FLAC still there: not converted (yet)
            new = old[:-len(".flac")] + ".aiff"
            aiff = stick_file(root, new)
            if not aiff.is_file():
                skipped.add((old, "neither the FLAC nor an AIFF exists on the stick"))
                continue
            if new not in cache:
                try:
                    cache[new] = read_aiff_info(aiff)
                except ConversionError as e:
                    skipped.add((old, str(e)))
                    continue
            if not cache[new].codec.startswith("pcm_"):
                skipped.add((old, f"{new} is not an uncompressed AIFF ({cache[new].codec})"))
                continue
            getattr(plan, kind).append(Repoint(old, new, cache[new], anlz))
    plan.skipped = sorted(skipped)
    return plan


def anlz_files(root: Path, anlz_path: str) -> list[Path]:
    """Return the existing analysis files (.DAT/.EXT/.2EX) belonging to one track.

    Only files sharing the pointer's name stem (e.g. `ANLZ0000`) are returned, so a track that
    shares its folder with another one does not pick up the other's files.

    Args:
        root: Mount point of the stick.
        anlz_path: The track's analysis path as stored in the library.
    """
    if not anlz_path:
        return []
    pointer = stick_file(root, anlz_path)
    if not pointer.parent.is_dir():
        return []
    return sorted(p for p in pointer.parent.iterdir()
                  if p.stem == pointer.stem and p.suffix.upper() in ANLZ_SUFFIXES
                  and not p.name.startswith("._"))


@dataclass
class AnlzMove:
    """Where one track's analysis files go.

    Attributes:
        files: {source file: (target file, patched contents)}.
        pointer: New library string for the track's `.DAT` file.
    """

    files: dict[Path, tuple[Path, bytes]]
    pointer: str


def _pick_stem(folder: Path, track_path: str, reserved: set[str]) -> str:
    """Choose the file-name stem (`ANLZ0000`, `ANLZ0001`, ...) for a track in its folder.

    A stem whose `.DAT` already names this track is reused (e.g. a partial analysis a player
    wrote earlier). Otherwise the lowest stem that is not used by another track.

    Args:
        folder: The track's hashed analysis folder (may not exist yet).
        track_path: Track path as stored in the library.
        reserved: Stems already handed out in this folder during this run.
    """
    taken = set(reserved)
    if folder.is_dir():
        for p in folder.iterdir():
            if p.suffix.upper() not in ANLZ_SUFFIXES or p.name.startswith("._"):
                continue
            if p.suffix.upper() == ".DAT" and anlz_stored_path(p.read_bytes()) == track_path:
                return p.stem
            taken.add(p.stem)
    index = 0
    while f"ANLZ{index:04d}" in taken:
        index += 1
    return f"ANLZ{index:04d}"


def plan_anlz_move(root: Path, repoints: list[Repoint],
                   reserved: dict[Path, set[str]]) -> AnlzMove | None:
    """Work out the new location and contents of one track's analysis files.

    Args:
        root: Mount point of the stick.
        repoints: The track's entries from the libraries (they share old and new path).
        reserved: Stems already used per folder in this run; updated here.

    Returns:
        The move, or None if the track has no analysis files to move.
    """
    old_path, new_path = repoints[0].old_path, repoints[0].new_path
    sources = sorted({f for r in repoints for f in anlz_files(root, r.anlz_path)})
    patched = {}
    for src in sources:
        data = src.read_bytes()
        if anlz_stored_path(data) == old_path:
            patched[src] = patch_anlz_extension(data)
    patched = {src: data for src, data in patched.items() if data is not None}
    if not patched:
        return None
    folder = stick_file(root, anlz_folder(new_path))
    stem = _pick_stem(folder, new_path, reserved.setdefault(folder, set()))
    reserved[folder].add(stem)
    suffix = Path(repoints[0].anlz_path).suffix or ".DAT"
    return AnlzMove(
        files={src: (folder / f"{stem}{src.suffix}", data) for src, data in patched.items()},
        pointer=f"{anlz_folder(new_path)}/{stem}{suffix}",
    )


def backup_files(root: Path, libs: Libraries, files: list[Path], dest: Path) -> Path:
    """Copy every file the update will modify into `dest`, keeping stick-relative paths.

    Args:
        root: Mount point of the stick.
        libs: Libraries found on the stick.
        files: Analysis files that will be moved, replaced or deleted.
        dest: Backup folder to create.

    Returns:
        The backup folder.
    """
    dest.mkdir(parents=True)
    for lib in (libs.pdb, libs.dlp):
        if lib:
            for p in lib.parent.glob(lib.name + "*"):  # includes -wal/-shm/-journal if present
                target = dest / p.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
    for p in files:
        target = dest / p.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    return dest


def default_backup_dir() -> Path:
    """Return a timestamped backup folder in the user's home (never on the stick)."""
    return Path.home() / "flac2aiff-backups" / f"stick-{datetime.now():%Y%m%d-%H%M%S}"


def apply_plan(root: Path, libs: Libraries, plan: Plan, backup_dir: Path) -> Path:
    """Write all library and analysis-file changes; restore the backup if anything fails.

    New analysis files are written first, then the libraries are updated, and only then are the
    old analysis files deleted, so an interruption never leaves a track without analysis.

    Args:
        root: Mount point of the stick.
        libs: Libraries found on the stick.
        plan: Output of `plan_repoints`.
        backup_dir: Where to put the backup of everything that is about to change.

    Returns:
        The backup folder.
    """
    by_track = defaultdict(list)
    for repoint in plan.pdb + plan.dlp:
        by_track[repoint.old_path].append(repoint)
    reserved: dict[Path, set[str]] = {}
    moves = {old: move for old, rs in by_track.items()
             if (move := plan_anlz_move(root, rs, reserved))}
    sources = [src for m in moves.values() for src in m.files]
    targets = [dst for m in moves.values() for dst, _ in m.files.values()]

    backup = backup_files(root, libs, sources + [t for t in targets if t.exists()], backup_dir)
    created = [t for t in targets if not t.exists()]
    try:
        for m in moves.values():
            for dst, data in m.files.values():
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(dst)
        pointers = {old: m.pointer for old, m in moves.items()}
        if libs.pdb and plan.pdb:
            _apply_pdb(libs.pdb, plan.pdb, pointers)
        if libs.dlp and plan.dlp:
            _apply_dlp(libs.dlp, plan.dlp, pointers)
    except BaseException:
        _delete_files(created)
        _restore(root, backup)
        raise
    kept = set(targets)
    _delete_files([src for src in sources if src not in kept])
    return backup


def _delete_files(files: list[Path]) -> None:
    """Delete analysis files and any folders left empty by that."""
    for f in files:
        f.unlink(missing_ok=True)
        for folder in (f.parent, f.parent.parent):
            try:
                folder.rmdir()
            except OSError:
                break  # not empty: another track still uses this folder


def _apply_pdb(path: Path, repoints: list[Repoint], pointers: dict[str, str]) -> None:
    """Patch the legacy library in place for the given repoints."""
    pdb = PdbFile(path)
    by_path = {r.old_path: r for r in repoints}
    for track in pdb.tracks():
        r = by_path.get(track.file_path)
        if r:
            pdb.repoint(track, r.new_path, FILETYPE_AIFF, r.info.size, r.info.bitrate,
                        analyze_path=pointers.get(r.old_path))
    pdb.save()


def _apply_dlp(path: Path, repoints: list[Repoint], pointers: dict[str, str]) -> None:
    """Update the Device Library Plus content rows for the given repoints (one transaction)."""
    from pyrekordbox.onelibrary import models
    by_path = {r.old_path: r for r in repoints}
    with warnings.catch_warnings(), open_dlp(path) as db:
        warnings.simplefilter("ignore")
        try:
            for c in db.query(models.Content).all():
                r = by_path.get(c.path)
                if r:
                    c.path = r.new_path
                    c.fileName = r.new_path.rsplit("/", 1)[-1]
                    c.fileType = FILETYPE_AIFF
                    c.fileSize = r.info.size
                    c.bitrate = r.info.bitrate
                    if r.old_path in pointers:
                        c.analysisDataFilePath = pointers[r.old_path]
            db.session.commit()
        except BaseException:
            db.session.rollback()
            raise


def _restore(root: Path, backup: Path) -> None:
    """Copy everything in `backup` back to its place on the stick."""
    for p in backup.rglob("*"):
        if p.is_file():
            shutil.copy2(p, root / p.relative_to(backup))


def check_stick(root: Path, libs: Libraries) -> list[str]:
    """Check that the libraries and analysis files of a stick agree with the files on it.

    Args:
        root: Mount point of the stick.
        libs: Libraries found on the stick.

    Returns:
        Human-readable problems; empty if everything is consistent.
    """
    problems = []
    seen = {}
    if libs.pdb:
        seen["pdb"] = {t.file_path: t for t in PdbFile(libs.pdb).tracks()}
    if libs.dlp:
        from pyrekordbox.onelibrary import models
        with warnings.catch_warnings(), open_dlp(libs.dlp) as db:
            warnings.simplefilter("ignore")
            seen["dlp"] = {c.path: c for c in db.query(models.Content).all()}
            for c in seen["dlp"].values():
                _check_track(root, "dlp", c.path, c.fileType, c.fileSize, problems)
                problems += _check_anlz(root, c.analysisDataFilePath or "", c.path)
    if libs.pdb:
        for p, t in seen["pdb"].items():
            _check_track(root, "pdb", p, t.file_type, t.file_size, problems)
            problems += _check_anlz(root, t.analyze_path, p)
    if libs.pdb and libs.dlp and set(seen["pdb"]) != set(seen["dlp"]):
        problems.append("the legacy and Device Library Plus libraries list different tracks")
    return problems


def _check_track(root: Path, kind: str, path: str, file_type: int, size: int, out: list) -> None:
    """Append problems for one library row to `out`."""
    f = stick_file(root, path)
    if not f.is_file():
        out.append(f"[{kind}] file missing: {path}")
    elif f.stat().st_size != size:
        out.append(f"[{kind}] size differs from file: {path}")
    if path.lower().endswith((".aiff", ".aif")) and file_type != FILETYPE_AIFF:
        out.append(f"[{kind}] wrong file type for AIFF: {path}")


def _check_anlz(root: Path, anlz_path: str, track_path: str) -> list[str]:
    """Return problems for one track's analysis files.

    The folder must be the one players compute from the track path, the files must exist, and
    the path stored inside them must be the track's.
    """
    if not anlz_path:
        return []
    problems = []
    expected = anlz_folder(track_path)
    if anlz_path.rsplit("/", 1)[0].upper() != expected.upper():
        problems.append(f"analysis folder {anlz_path.rsplit('/', 1)[0]} is not where players look "
                        f"for {track_path} (expected {expected})")
    files = anlz_files(root, anlz_path)
    if not files:
        problems.append(f"analysis files missing: {anlz_path}")
    for f in files:
        stored = anlz_stored_path(f.read_bytes())
        if stored is not None and stored != track_path:
            problems.append(f"analysis file path mismatch: {f.relative_to(root)} has {stored}, "
                            f"library has {track_path}")
    return problems
