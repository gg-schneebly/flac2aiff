"""Tests of the page-level salvage of a damaged SQLCipher library on synthetic databases."""

import pytest

from repair_dlp import (
    PAGE_SIZE, RepairError, decrypt_page, derive_keys, finish_repair, repair_bytes, _connect,
)

PASSPHRASE = "test-passphrase"
TRACKS = 400


def build_db(path):
    """Write an encrypted library with a multi-page content table and playlist entries."""
    con = _connect(path, PASSPHRASE)
    con.execute("CREATE TABLE content(content_id integer primary key, path varchar, title varchar)")
    con.execute("CREATE TABLE playlist_content(playlist_id integer, content_id integer, "
                "sequenceNo integer)")
    for i in range(1, TRACKS + 1):
        con.execute("INSERT INTO content VALUES (?, ?, ?)",
                    (i, f"/Contents/Artist {i}/Album/{'x' * 60} {i}.flac", f"Title {i}"))
        con.execute("INSERT INTO playlist_content VALUES (1, ?, ?)", (i, i))
    con.commit()
    con.close()
    return path.read_bytes()


def content_page(raw):
    """Pick a content leaf page in the middle of the table (it has siblings on both sides)."""
    keys = derive_keys(PASSPHRASE, raw[:16])
    pages = []
    for number in range(2, len(raw) // PAGE_SIZE + 1):
        page, _ = decrypt_page(keys, raw[(number - 1) * PAGE_SIZE:number * PAGE_SIZE], number)
        if page[0] == 0x0D and b"/Contents/" in page:
            pages.append(number)
    return pages[len(pages) // 2]


def damage(raw, number, start, end, value=None):
    """Return a copy of `raw` with bytes [start, end) of a page zeroed (or set to `value`)."""
    out = bytearray(raw)
    base = (number - 1) * PAGE_SIZE
    out[base + start:base + end] = bytes(end - start) if value is None else value
    return bytes(out)


def test_intact_database_is_left_alone(tmp_path):
    raw = build_db(tmp_path / "db")
    assert repair_bytes(raw, PASSPHRASE) == (raw, [])


def test_zeroed_page_head_keeps_the_rows_in_the_intact_tail(tmp_path):
    raw = build_db(tmp_path / "db")
    number = content_page(raw)
    broken = damage(raw, number, 0, 2048)

    repaired, repairs = repair_bytes(broken, PASSPHRASE)
    assert [r.number for r in repairs] == [number]
    assert repairs[0].recovered  # the tail of the page survived
    changed = {i // PAGE_SIZE + 1 for i in range(len(raw)) if repaired[i] != raw[i]}
    assert changed == {number}

    path = tmp_path / "repaired"
    path.write_bytes(repaired)
    (loss,) = finish_repair(path, PASSPHRASE, repairs)
    con = _connect(path, PASSPHRASE)
    ids = {r[0] for r in con.execute("SELECT content_id FROM content")}
    refs = {r[0] for r in con.execute("SELECT content_id FROM playlist_content")}
    con.close()
    assert loss.table == "content" and loss.lost
    assert ids | set(loss.lost) == set(range(1, TRACKS + 1))
    assert set(repairs[0].recovered) <= ids
    assert refs == ids
    assert loss.removed_refs == {("playlist_content", "content_id"): len(loss.lost)}


def test_damage_that_cannot_be_located_is_refused(tmp_path):
    raw = build_db(tmp_path / "db")
    number = content_page(raw)
    flipped = bytes([raw[(number - 1) * PAGE_SIZE + 100] ^ 1])
    with pytest.raises(RepairError, match="cannot tell which bytes"):
        repair_bytes(damage(raw, number, 100, 101, flipped), PASSPHRASE)
