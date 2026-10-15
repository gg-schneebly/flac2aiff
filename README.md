# flac2aiff

Convert a music library's FLAC files to AIFF **in place**, losslessly and with tags and cover art intact, then fix the **rekordbox** library so your playlists, cues and beatgrids keep working.

- Each `song.flac` becomes `song.aiff` in the same folder. Every other file and the folder structure stay untouched.
- A FLAC is deleted only after the new AIFF's audio has been checked to be **bit-for-bit identical**.
- Can also convert a **rekordbox USB/SD stick** in place, so it keeps working in your players (see [Converting a rekordbox USB stick](#converting-a-rekordbox-usb-stick)).
- Built and tested on macOS with rekordbox 7; the USB stick tool was tested on Linux. The converter should work anywhere ffmpeg and Python 3.11+ run.

> ⚠️ This tool deletes the original FLAC files after converting them. **Back up your music library first**, and before running the rekordbox step, make a rekordbox backup too (File → Library → Backup Library).

## Quick start

```sh
# 1. Install ffmpeg (macOS)
brew install ffmpeg

# 2. Get the code; you need Python 3.11+
git clone <this repo> flac2aiff && cd flac2aiff
pip install pyrekordbox==0.5.0   # only needed for the rekordbox and USB stick tools

# 3. Convert (try a dry run first)
python convert_library.py ~/Music --dry-run
python convert_library.py ~/Music

# 4. Fix rekordbox (quit rekordbox first; try a dry run first)
python repoint_rekordbox.py
python repoint_rekordbox.py --apply
```

**Converting a rekordbox USB/SD stick instead?** Plug it in and run (see [below](#converting-a-rekordbox-usb-stick) for details):

```sh
python convert_usb.py /path/to/stick --dry-run     # e.g. /run/media/you/MYSTICK or /Volumes/MYSTICK
python convert_usb.py /path/to/stick
```

Every script has `--help`.

The dependencies are declared in `pyproject.toml`, so `uv sync` also works.

## The scripts

| Script | What it does | Needs |
|---|---|---|
| `convert_library.py [DIR]` | Converts every FLAC in `DIR` and all its subfolders. `DIR` defaults to the current directory. | ffmpeg |
| `convert_file.py FILE` | Converts one file. Exits with an error if `FILE` isn't a FLAC. | ffmpeg |
| `repoint_rekordbox.py` | Points rekordbox tracks at the AIFF files that replaced their FLACs. | ffmpeg, `pyrekordbox` |
| `convert_usb.py STICK` | Converts a rekordbox USB/SD stick and repoints its libraries, in one step. | ffmpeg, `pyrekordbox` |
| `flac2aiff_common.py` | Shared conversion code used by the scripts above. Not run directly. | |
| `rekordbox_common.py`, `rekordbox_pdb.py`, `rekordbox_usb.py` | Shared rekordbox code (analysis files, legacy `export.pdb` reader/patcher, stick logic). Not run directly. | |

### Options

| Option | Scripts | Meaning |
|---|---|---|
| `--dry-run` | `convert_library.py`, `convert_usb.py` | Only list what would be converted. |
| `--backup-dir PATH` | `convert_usb.py` | Where to back up the stick's library files (default `~/flac2aiff-backups/stick-<timestamp>`). |
| `--id3v2-version {3,4}` | `convert_library.py`, `convert_file.py`, `convert_usb.py` | Tag version written into the AIFF. Default `3`: the most widely supported. `4` keeps full release dates instead of just the year. |
| `--apply` | `repoint_rekordbox.py` | Actually make the changes. Without it, the script is a dry run. |
| `--db-dir PATH` | `repoint_rekordbox.py` | Use a rekordbox data folder other than the default (`~/Library/Pioneer/rekordbox`). Useful for testing on a copy. |
| `-v`, `--verbose` | `repoint_rekordbox.py` | List every track that would be repointed. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | At least one file failed (its original is kept), the input was invalid, or rekordbox was still running. |
| `130` | `convert_library.py` was interrupted with Ctrl-C. Files already converted stay converted; the file in progress is left untouched. |

## What the conversion guarantees

- **Lossless:** sample rate, channels and bit depth are kept, so 16-bit FLAC becomes 16-bit AIFF and 24-bit becomes 24-bit.
- **Verified before delete:** the decoded audio of the FLAC and the AIFF is hashed (MD5). The FLAC is deleted only if the two match.
- **Metadata:**
  - All tags are copied into an ID3v2 chunk in the AIFF.
  - Embedded cover art is kept.
  - BPM, key, ISRC and label are written to their standard ID3 frames (`TBPM`, `TKEY`, `TSRC`, `TPUB`), which DJ software reads.
  - The file's modification time is kept.
- **Checks the disk space first:** AIFF is about 1.5× the size of FLAC, so before converting anything every script estimates the growth from each file's duration and format, plus a margin for tags and cover art. If even the optimistic estimate won't fit, it stops without touching anything. If the free space could get tight, it prints a warning and continues (press Ctrl-C to cancel). It also warns about files that would reach 4 GiB, which FAT32 drives can't store. Because each FLAC is deleted only after its AIFF is verified, the check allows for the busiest moment of the run, not just the final size.
- **Stops cleanly if the disk fills up anyway:** before each file the tool checks that its AIFF still fits with a reserve (at least 256 MiB, and on a stick enough to update the libraries), and it stops instead of filling the disk. If the disk fills up during a write regardless, the half-written AIFF is removed, the FLAC stays and the run ends at once. On a stick, the tracks converted so far still get their library entries and analysis files updated, so the stick stays usable (the rest stay FLAC, unchanged). Free up space and run the same command again to continue.
- **Never destructive by accident:**
  - An existing `.aiff` is never overwritten.
  - A file with a `.flac` extension but no real FLAC header is rejected.
  - macOS `._*` metadata files are skipped.
  - A half-written AIFF is removed if conversion fails or is interrupted.
  - In a library run, a failed file is reported and the run moves on to the next one. The failures are listed at the end.

## Fixing rekordbox after converting

Rekordbox finds tracks by their exact file path, so after conversion every converted track shows as missing. Deleting those tracks and re-importing the AIFFs would lose their playlists, cues, beatgrids, ratings and play counts. `repoint_rekordbox.py` instead updates the existing entries, so all of that is kept.

- **Which tracks it changes:** only tracks whose `.flac` is gone and that have a `.aiff` with the same name in the same folder. Running it again is safe, for example after converting more files.
- **What it changes:**
  - In rekordbox's database: the track's path, file name, file type, file size and bit rate.
  - In the track's analysis files (waveforms, beatgrid): only the stored file name's `.flac` becomes `.aiff`. Every other byte stays the same.
- **Backup first:** before any change, the database and every analysis file it will change are copied to `~/Library/Pioneer/rekordbox/flac2aiff-backup-<timestamp>/`.
- **All or nothing:** if anything fails, the database changes are rolled back and the analysis files restored.
- **Refuses to run while rekordbox is open**, because rekordbox would overwrite the changes.
- **What it leaves alone:**
  - tracks that went missing for other reasons (moved or deleted); fix those with rekordbox's Relocate
  - converted files that were never imported into rekordbox

**To undo it:** quit rekordbox and copy the contents of the backup folder back into `~/Library/Pioneer/rekordbox/`.

**After running it:** re-export any playlists you keep on USB sticks for CDJs, because those copies still reference the FLAC files.

## Converting a rekordbox USB stick

```sh
python convert_usb.py /run/media/you/MYSTICK --dry-run   # macOS: /Volumes/MYSTICK
python convert_usb.py /run/media/you/MYSTICK
```

Then **eject the stick** before unplugging it.

- **Both stick formats are supported**, alone or together: the legacy library (`PIONEER/rekordbox/export.pdb`) read by older players, and Device Library Plus (`exportLibrary.db`). The script finds out which the stick has and updates every one it finds.
- **What changes per track:**
  - the FLAC becomes a bit-for-bit verified AIFF next to it (the same conversion as above) and the FLAC is deleted
  - in each library: the track's path, file name, file type, file size, bit rate and analysis path
  - the track's analysis files (`PIONEER/USBANLZ/…`) are **moved to a new folder** and their stored file name's `.flac` becomes `.aiff`; every other byte stays (see below)
- **Why the analysis files move:** players don't read the analysis location from the library. They compute the folder from a hash of the track's path (`/Contents/Artist/Album/file.aiff`), so renaming `.flac` to `.aiff` makes the player look in a new, empty folder and re-analyse the track live (no pre-analysed waveform, no memory cues). The tool computes the folder for the new path, moves the files there and points the libraries at it. If two tracks hash to the same folder, their files are numbered `ANLZ0000`, `ANLZ0001`, … like rekordbox does. The hash (a two-multiplier hash of the path's UTF-16 units, modulo 200003) is from the community notes at [morizkraemer/fourfour](https://github.com/morizkraemer/fourfour/blob/master/pioneer-usb-writer/reference-code/PIONEER.md) and was checked against 1,400+ folders written by rekordbox and by a CDJ-850.
- **What stays:** cues, hot cues, memory cues, loops, beatgrids, waveforms, playlists, ratings, play counts, tags and artwork. The existing library rows and analysis files are patched in place, never rebuilt.
- **Backup:** before the libraries are touched, every library and analysis file that will change is copied to `~/flac2aiff-backups/stick-<timestamp>/` (never onto the stick). If anything goes wrong (failed files, an early stop, or a problem found by the final check) it is kept, and you restore it by copying its contents back onto the stick. After a clean run it is deleted: the FLACs it refers to are gone by then, so restoring it would only break the stick. To keep it anyway, pass `--backup-dir DIR`.
- **Safe to re-run:** if the run is interrupted (or some files fail), run it again. It converts what is left and then repoints every track whose FLAC is gone and whose AIFF exists. Until the libraries are updated, converted tracks won't play from the stick.
- **Space:** AIFF is bigger than FLAC (roughly 1.5×). The script estimates the growth first, refuses to start if the stick is too full and warns if it would get tight (see above). The stick must be FAT32/exFAT as usual for players; a single AIFF can't exceed 4 GiB on FAT32.
- **Checks at the end:** the script verifies that every library row points to a file on the stick with the right size, that both libraries list the same tracks, that every analysis folder is the one the players compute for that track's path, and that the analysis files exist and name the right track.
- **Also converted:** any FLAC on the stick outside `PIONEER*`, even if it isn't in the library.

## Known limitations

- **Comments and lyrics** are stored as custom `TXXX` tag frames instead of `COMM`/`USLT`, because ffmpeg's AIFF tag writer can't write those. Some players won't show them.
- **Speed:** verification decodes every file twice, so a large library takes a while.
- **rekordbox's database format isn't public.** `repoint_rekordbox.py` relies on `pyrekordbox` to read and write it. It was tested on macOS with rekordbox 7 and `pyrekordbox` 0.4.4; `pyproject.toml` now pins 0.5.0 (needed for Device Library Plus), which that script has not been re-run against on a real library. After a rekordbox update, run the dry run first.
- **The legacy `export.pdb` has no public spec from Pioneer.** `rekordbox_pdb.py` follows the community reverse-engineered format (Deep Symmetry's analysis) and only touches the track table.

## For AI agents and contributors

### Layout

- `space_check.py`: estimates the disk space a run needs (optimistic and pessimistic bounds, peak during the run) and gives the warnings; used by all three conversion scripts.
- `flac2aiff_common.py`: everything about converting one file. Probes the file, checks the FLAC header, runs ffmpeg, verifies by MD5, then swaps the files.
- `convert_library.py`, `convert_file.py`: thin command-line wrappers around `flac2aiff_common.convert()`.
- `repoint_rekordbox.py`: repoints the rekordbox library on a computer. Works on its own and doesn't call the conversion code.
- `convert_usb.py`: converts a stick, then repoints its libraries and moves its analysis files via `rekordbox_usb.py` (`plan_repoints` → `apply_plan` → `check_stick`).
- `rekordbox_common.py`: ANLZ path patching, the analysis-folder path hash and file-type constants shared by both repoint scripts.
- `rekordbox_pdb.py`: reader/patcher for the track table of a legacy `export.pdb`.

### Rules that must not be broken

1. **Never delete a FLAC unless the AIFF's decoded audio MD5 matches it.** Write to a temporary `.part` file first and replace atomically.
2. **Never overwrite an existing `.aiff`.**
3. **Don't save rekordbox analysis (ANLZ) files with `pyrekordbox`.** `AnlzFile.save()` and `Rekordbox6Database.update_content_path()` silently drop sections pyrekordbox doesn't understand, such as rekordbox 7's `PVB2` waveforms. Patch the bytes directly, as `patch_anlz_extension()` does. `.flac` and `.aiff` have the same length, so no section lengths change.
4. **rekordbox 7 stores `?/<file name>` in the ANLZ `PPTH` section, not the full path.** Keep that format.
5. **Compare paths after Unicode NFC normalization.** macOS file names and rekordbox's paths can encode accented letters differently.
6. **Never edit the live rekordbox database while rekordbox is running.** For `--apply`, back up the database and the affected ANLZ files first.
7. **On a stick, patch the library files in place.** `export.pdb` rows are patched byte-for-byte (same string lengths, so no page layout changes); the Device Library Plus `content` rows are updated by column. Never rebuild either file.
8. **Analysis folders are named by the path hash, not by the library.** Whenever a track's path changes on a stick, move its analysis files to `anlz_folder(new_path)` (`rekordbox_common.py`) and update both libraries. Write the new files first and delete the old ones last, so an interruption never leaves a track without analysis.
9. **Update the libraries only after the audio is converted**, and only for tracks whose FLAC is gone and whose AIFF exists, so a re-run after an interruption is always safe.

### Testing

Run the unit tests (synthetic PDB and ANLZ files) with `pip install pytest` and then `python -m pytest tests`. They don't cover conversion or the libraries of a real stick, so check those by hand:

- **Conversion:** build a small library with ffmpeg. Include:
  - 16-bit and 24-bit FLACs, with tags and embedded cover art
  - an extension in a different case (`.FLAC`)
  - a fake `.flac` (a text file)
  - a `._` file and a non-FLAC file

  Run `convert_library.py` and check with `ffprobe`: codecs, bit depth, tags and cover art should all be right.
- **rekordbox:** copy `master.db*`, `masterPlaylists6.xml` and the `share/` folder from `~/Library/Pioneer/rekordbox` into a scratch folder. Run `repoint_rekordbox.py --db-dir <copy> --apply` against that copy. Then compare it with the backup the script made:
  - every ANLZ file should be the same size and differ by exactly 4 bytes
  - playlists and cues should be unchanged
  - a second run should find nothing left to repoint
- **USB stick:** copy a real stick's `PIONEER/` folder and hard-link (`cp -al`) its audio folder into a scratch directory, once with both libraries and once each with `export.pdb` or `exportLibrary.db` deleted. Run `convert_usb.py` on each copy, then run it again with `--dry-run` (nothing left to do) and compare the backup with the result: only the track rows and the ANLZ path bytes should differ. Finally try the real stick in your player.
