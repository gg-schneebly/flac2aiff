# flac2aiff

Convert a music library's FLAC files to AIFF **in place**, losslessly and with tags and cover art intact, then fix the **rekordbox** library so your playlists, cues and beatgrids keep working.

- Each `song.flac` becomes `song.aiff` in the same folder. Every other file and the folder structure stay untouched.
- A FLAC is deleted only after the new AIFF's audio has been checked to be **bit-for-bit identical**.
- Built and tested on macOS with rekordbox 7. The converter should work anywhere ffmpeg and Python 3 run.

> ⚠️ This tool deletes the original FLAC files after converting them. **Back up your music library first**, and before running the rekordbox step, make a rekordbox backup too (File → Library → Backup Library).

## Quick start

```sh
# 1. Install ffmpeg (macOS)
brew install ffmpeg

# 2. Set up Python (3.10+)
git clone <this repo> flac2aiff && cd flac2aiff
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # only needed for the rekordbox step

# 3. Convert (try a dry run first)
.venv/bin/python convert_library.py ~/Music --dry-run
.venv/bin/python convert_library.py ~/Music

# 4. Fix rekordbox (quit rekordbox first; try a dry run first)
.venv/bin/python repoint_rekordbox.py
.venv/bin/python repoint_rekordbox.py --apply
```

## The scripts

| Script | What it does | Needs |
|---|---|---|
| `convert_library.py [DIR]` | Converts every FLAC in `DIR` and all its subfolders. `DIR` defaults to the current directory. | ffmpeg |
| `convert_file.py FILE` | Converts one file. Exits with an error if `FILE` isn't a FLAC. | ffmpeg |
| `repoint_rekordbox.py` | Points rekordbox tracks at the AIFF files that replaced their FLACs. | ffmpeg, `pyrekordbox` |
| `flac2aiff_common.py` | Shared conversion code used by the scripts above. Not run directly. | |

### Options

| Option | Scripts | Meaning |
|---|---|---|
| `--dry-run` | `convert_library.py` | Only list what would be converted. |
| `--id3v2-version {3,4}` | `convert_library.py`, `convert_file.py` | Tag version written into the AIFF. Default `3`: the most widely supported. `4` keeps full release dates instead of just the year. |
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

## Known limitations

- **Comments and lyrics** are stored as custom `TXXX` tag frames instead of `COMM`/`USLT`, because ffmpeg's AIFF tag writer can't write those. Some players won't show them.
- **Speed:** verification decodes every file twice, so a large library takes a while.
- **rekordbox's database format isn't public.** `repoint_rekordbox.py` relies on `pyrekordbox` to read and write it, and was tested with rekordbox 7 and `pyrekordbox` 0.4.4 (pinned in `requirements.txt`). After a rekordbox update, run the dry run first.

## For AI agents and contributors

### Layout

- `flac2aiff_common.py`: everything about converting one file. Probes the file, checks the FLAC header, runs ffmpeg, verifies by MD5, then swaps the files.
- `convert_library.py`, `convert_file.py`: thin command-line wrappers around `flac2aiff_common.convert()`.
- `repoint_rekordbox.py`: works on its own and doesn't call the conversion code. It only needs `probe()` and `audio_stream()` from `flac2aiff_common.py`.

### Rules that must not be broken

1. **Never delete a FLAC unless the AIFF's decoded audio MD5 matches it.** Write to a temporary `.part` file first and replace atomically.
2. **Never overwrite an existing `.aiff`.**
3. **Don't save rekordbox analysis (ANLZ) files with `pyrekordbox`.** `AnlzFile.save()` and `Rekordbox6Database.update_content_path()` silently drop sections pyrekordbox doesn't understand, such as rekordbox 7's `PVB2` waveforms. Patch the bytes directly, as `patch_anlz_extension()` does. `.flac` and `.aiff` have the same length, so no section lengths change.
4. **rekordbox 7 stores `?/<file name>` in the ANLZ `PPTH` section, not the full path.** Keep that format.
5. **Compare paths after Unicode NFC normalization.** macOS file names and rekordbox's paths can encode accented letters differently.
6. **Never edit the live rekordbox database while rekordbox is running.** For `--apply`, back up the database and the affected ANLZ files first.

### Testing

There's no automated test suite yet. To check a change by hand:

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
