#!/usr/bin/env python3
"""Salvage a damaged Device Library Plus database (exportLibrary.db) on a rekordbox stick.

Usage:
    repair_dlp.py STICK [--dry-run] [--backup-dir DIR]

exportLibrary.db is a SQLCipher 4 database: every 4096-byte page is AES-256-CBC encrypted and
carries an HMAC. When part of a page was never written (it reads back as zeros), SQLCipher
refuses the whole table, and `convert_usb.py` cannot read the library.

This tool decrypts the damaged page itself, keeps every row that lies entirely in the
undamaged part, rebuilds the page from those rows and encrypts it again. All other pages stay
byte-identical. Rows that cannot be recovered are reported, and rows in other tables that
point at them (e.g. playlist entries) are removed so the library stays consistent. The
original file is backed up to ~/flac2aiff-backups/ first and never deleted.

Only damage the tool can locate (zeroed blocks) on table leaf pages is repaired; anything else
stops with an error and leaves the stick unchanged.
"""

import argparse
import hashlib
import hmac
import os
import re
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rekordbox_pdb import PdbFile  # noqa: E402
from rekordbox_usb import find_libraries  # noqa: E402

# SQLCipher 4 defaults, which rekordbox uses
PAGE_SIZE = 4096
IV_LEN = 16
MAC_LEN = 64
RESERVE = IV_LEN + MAC_LEN
USABLE = PAGE_SIZE - RESERVE
SALT_LEN = 16
KDF_ITER = 256000
MAC_SALT_MASK = 0x3A
SQLITE_MAGIC = b"SQLite format 3\0"

LEAF_TABLE = 0x0D
INTERIOR_TABLE = 0x05
BLOCK = 16  # AES block size


class RepairError(Exception):
    """The database is damaged in a way this tool cannot repair safely."""


def dlp_passphrase() -> str:
    """Return the passphrase rekordbox uses for exportLibrary.db (derived by pyrekordbox)."""
    from pyrekordbox.onelibrary.database import BLOB
    from pyrekordbox.utils import deobfuscate
    return deobfuscate(BLOB)


@dataclass
class Keys:
    """Page keys derived from the passphrase.

    Attributes:
        cipher: AES-256 key.
        mac: HMAC-SHA512 key.
    """

    cipher: bytes
    mac: bytes


def derive_keys(passphrase: str, salt: bytes) -> Keys:
    """Derive the page encryption and HMAC keys the way SQLCipher 4 does.

    Args:
        passphrase: Database passphrase.
        salt: First 16 bytes of the database file.
    """
    cipher = hashlib.pbkdf2_hmac("sha512", passphrase.encode(), salt, KDF_ITER, 32)
    mac_salt = bytes(b ^ MAC_SALT_MASK for b in salt)
    return Keys(cipher, hashlib.pbkdf2_hmac("sha512", cipher, mac_salt, 2, 32))


def _aes(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    """AES-256-CBC without padding."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


def _page_mac(keys: Keys, body: bytes, number: int) -> bytes:
    """HMAC of an encrypted page: ciphertext + IV + little-endian page number."""
    return hmac.new(keys.mac, body + struct.pack("<I", number), hashlib.sha512).digest()


def decrypt_page(keys: Keys, raw: bytes, number: int) -> tuple[bytes, bool]:
    """Decrypt one page even if its HMAC fails.

    Args:
        keys: Page keys.
        raw: The encrypted page (PAGE_SIZE bytes).
        number: 1-based page number.

    Returns:
        (plain page as SQLite would see it, True if the HMAC matched).
    """
    start = SALT_LEN if number == 1 else 0
    data, iv, mac = raw[start:USABLE], raw[USABLE:USABLE + IV_LEN], raw[USABLE + IV_LEN:]
    ok = hmac.compare_digest(_page_mac(keys, data + iv, number), mac)
    prefix = SQLITE_MAGIC if number == 1 else b""
    return prefix + _aes(keys.cipher, iv, data, encrypt=False) + bytes(RESERVE), ok


def encrypt_page(keys: Keys, plain: bytes, number: int) -> bytes:
    """Encrypt one (non-first) page with a fresh IV and a valid HMAC.

    Args:
        keys: Page keys.
        plain: Plain page (PAGE_SIZE bytes, reserve area ignored).
        number: 1-based page number, must not be 1.
    """
    iv = os.urandom(IV_LEN)
    data = _aes(keys.cipher, iv, plain[:USABLE], encrypt=True)
    return data + iv + _page_mac(keys, data + iv, number)


def intact_mask(raw: bytes) -> list[bool] | None:
    """Mark which bytes of an encrypted page decrypt correctly.

    Damage is only locatable when it shows as all-zero ciphertext blocks: such a block and the
    one after it (CBC chains each block to the previous one) decrypt to garbage.

    Args:
        raw: The encrypted page.

    Returns:
        One flag per byte of the usable area, or None if no zeroed block was found (the damage
        is somewhere unknown).
    """
    bad = [raw[i:i + BLOCK] == bytes(BLOCK) for i in range(0, USABLE, BLOCK)]
    if not any(bad):
        return None
    hit = [b or (i > 0 and bad[i - 1]) for i, b in enumerate(bad)]
    return [not hit[i // BLOCK] for i in range(USABLE)]


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a SQLite varint; return (value, next position)."""
    value = 0
    for i in range(9):
        byte = buf[pos + i]
        if i == 8:
            return (value << 8) | byte, pos + 9
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos + i + 1
    raise AssertionError("unreachable")


def _serial_size(serial_type: int) -> int:
    """Byte length of a SQLite record value of the given serial type."""
    if serial_type in (0, 8, 9):
        return 0
    if serial_type <= 7:
        return (0, 1, 2, 3, 4, 6, 8, 8)[serial_type]
    if serial_type in (10, 11):
        raise ValueError("reserved serial type")
    return (serial_type - 12) // 2 if serial_type % 2 == 0 else (serial_type - 13) // 2


@dataclass
class Cell:
    """One row cell of a table leaf page.

    Attributes:
        offset: Start of the cell in the page.
        end: First byte after the cell.
        rowid: The row's id.
        columns: Number of columns in the record.
    """

    offset: int
    end: int
    rowid: int
    columns: int


def parse_leaf_cell(page: bytes, offset: int) -> Cell | None:
    """Parse a table-leaf cell without overflow; None if the bytes are not a valid cell.

    Args:
        page: Plain page.
        offset: Where the cell would start.
    """
    try:
        length, pos = _varint(page, offset)
        rowid, pos = _varint(page, pos)
        if length > USABLE - 35:  # would spill to overflow pages, which are not handled
            return None
        header_len, cur = _varint(page, pos)
        header_end, types = pos + header_len, []
        while cur < header_end:
            serial_type, cur = _varint(page, cur)
            types.append(serial_type)
        if cur != header_end or header_len + sum(map(_serial_size, types)) != length:
            return None
        data = header_end
        for serial_type in types:  # text must be valid UTF-8 (rekordbox uses UTF-8)
            size = _serial_size(serial_type)
            if serial_type >= 13 and serial_type % 2 == 1:
                page[data:data + size].decode("utf-8")
            data += size
    except (IndexError, ValueError):
        return None
    if pos + length > USABLE or not types:
        return None
    return Cell(offset, pos + length, rowid, len(types))


def _header_offset(number: int) -> int:
    """Offset of the b-tree page header (page 1 starts with the 100-byte file header)."""
    return 100 if number == 1 else 0


def interior_children(page: bytes, number: int) -> list[tuple[int, int | None]]:
    """Return [(child page, key)] of a table interior page; the rightmost child has key None.

    Args:
        page: Plain page.
        number: Its page number.
    """
    head = _header_offset(number)
    count, rightmost = struct.unpack_from(">H", page, head + 3)[0], struct.unpack_from(
        ">I", page, head + 8)[0]
    children = []
    for i in range(count):
        ptr = struct.unpack_from(">H", page, head + 12 + 2 * i)[0]
        child = struct.unpack_from(">I", page, ptr)[0]
        children.append((child, _varint(page, ptr + 4)[0]))
    return children + [(rightmost, None)]


@dataclass
class Tree:
    """Parent links of all table b-trees in a database.

    Attributes:
        parent: {child page: (parent page, index among the parent's children)}.
        children: {interior page: [(child page, key)]}.
    """

    parent: dict[int, tuple[int, int]] = field(default_factory=dict)
    children: dict[int, list[tuple[int, int | None]]] = field(default_factory=dict)

    def bounds(self, number: int) -> tuple[int | None, int | None]:
        """Rowid range (low exclusive, high inclusive) a page may hold; None = unbounded."""
        if number not in self.parent:
            return None, None
        parent, index = self.parent[number]
        siblings = self.children[parent]
        low_parent, high_parent = self.bounds(parent)
        low = siblings[index - 1][1] if index > 0 else low_parent
        high = siblings[index][1] if siblings[index][1] is not None else high_parent
        return low, high

    def root(self, number: int) -> int:
        """Root page of the b-tree containing `number`."""
        while number in self.parent:
            number = self.parent[number][0]
        return number


def build_tree(pages: dict[int, bytes]) -> Tree:
    """Collect the parent links of every table interior page.

    Args:
        pages: {page number: plain page} of all pages that decrypted correctly.
    """
    tree = Tree()
    last = max(pages)
    for number, page in pages.items():
        if page[_header_offset(number)] != INTERIOR_TABLE:
            continue
        try:
            children = interior_children(page, number)
        except (IndexError, struct.error):
            continue
        keys = [k for _, k in children[:-1]]
        # free pages hold stale bytes: only trust pages that look like a real interior page
        if keys == sorted(set(keys)) and all(0 < c <= last + 1 for c, _ in children):
            tree.children[number] = children
            for index, (child, _) in enumerate(tree.children[number]):
                tree.parent[child] = (number, index)
    return tree


def _sibling_columns(tree: Tree, pages: dict[int, bytes], number: int) -> int | None:
    """Column count of the rows on a sibling leaf page, used to validate salvaged cells."""
    parent = tree.parent[number][0]
    for child, _ in tree.children[parent]:
        page = pages.get(child)
        if child != number and page is not None and page[0] == LEAF_TABLE:
            count = struct.unpack_from(">H", page, 3)[0]
            if count:
                cell = parse_leaf_cell(page, struct.unpack_from(">H", page, 8)[0])
                if cell:
                    return cell.columns
    return None


def salvage_cells(page: bytes, intact: list[bool], columns: int,
                  low: int | None, high: int | None) -> list[Cell]:
    """Find the cells of a damaged leaf page that lie entirely in its intact bytes.

    Cells are taken from the page's own pointer array when its header survived, and from the
    run of back-to-back cells that ends at the end of the page (where SQLite packs them).

    Args:
        page: Plain (decrypted) damaged page.
        intact: Output of `intact_mask`.
        columns: Expected number of columns per row.
        low: Rowids must be greater than this (None = no bound).
        high: Rowids must be at most this (None = no bound).

    Returns:
        The salvaged cells, sorted by rowid.
    """
    def valid(cell: Cell | None) -> bool:
        return (cell is not None and cell.columns == columns and all(intact[cell.offset:cell.end])
                and (low is None or cell.rowid > low) and (high is None or cell.rowid <= high))

    found = {}
    count = struct.unpack_from(">H", page, 3)[0]
    if page[0] == LEAF_TABLE and all(intact[:8 + 2 * count]):
        for i in range(count):
            cell = parse_leaf_cell(page, struct.unpack_from(">H", page, 8 + 2 * i)[0])
            if valid(cell):
                found[cell.offset] = cell
    candidates = {o: c for o in range(8, USABLE) if intact[o] and valid(c := parse_leaf_cell(page, o))}
    for start in sorted(candidates):
        chain, pos = [], start
        while pos in candidates:
            chain.append(candidates[pos])
            pos = candidates[pos].end
        if pos == USABLE:
            found.update((c.offset, c) for c in chain)
            break
    by_rowid = {}
    for cell in found.values():
        by_rowid.setdefault(cell.rowid, cell)
    return sorted(by_rowid.values(), key=lambda c: c.rowid)


def build_leaf_page(page: bytes, cells: list[Cell]) -> bytes:
    """Lay out a fresh table leaf page holding the given cells.

    Args:
        page: Plain page the cells' bytes are taken from.
        cells: Cells in rowid order.
    """
    out = bytearray(PAGE_SIZE)
    pos = USABLE
    pointers = []
    for cell in cells:
        size = cell.end - cell.offset
        pos -= size
        out[pos:pos + size] = page[cell.offset:cell.end]
        pointers.append(pos)
    if 8 + 2 * len(cells) > pos:
        raise RepairError("salvaged rows do not fit in one page")
    struct.pack_into(">BHHHB", out, 0, LEAF_TABLE, 0, len(cells), pos, 0)
    struct.pack_into(f">{len(pointers)}H", out, 8, *pointers)
    return bytes(out)


@dataclass
class PageRepair:
    """What was done to one damaged page.

    Attributes:
        number: Page number.
        root: Root page of the table it belongs to.
        recovered: Rowids kept.
        low: Lowest rowid the page could hold is low + 1 (None = unbounded).
        high: Highest rowid the page could hold (None = unbounded).
    """

    number: int
    root: int
    recovered: list[int]
    low: int | None
    high: int | None


def repair_bytes(raw: bytes, passphrase: str) -> tuple[bytes, list[PageRepair]]:
    """Rebuild every page of an encrypted database whose HMAC fails.

    Args:
        raw: The whole encrypted database file.
        passphrase: Its passphrase.

    Returns:
        (repaired file contents, one PageRepair per damaged page; empty if nothing was wrong).

    Raises:
        RepairError: A damaged page cannot be repaired safely.
    """
    if len(raw) % PAGE_SIZE:
        raise RepairError("file size is not a multiple of the page size")
    keys = derive_keys(passphrase, raw[:SALT_LEN])
    plain, damaged = {}, []
    for number in range(1, len(raw) // PAGE_SIZE + 1):
        page, ok = decrypt_page(keys, raw[(number - 1) * PAGE_SIZE:number * PAGE_SIZE], number)
        if ok:
            plain[number] = page
        else:
            damaged.append((number, page))
    if not damaged:
        return raw, []
    if not plain or plain.get(1, b"")[:16] != SQLITE_MAGIC:
        raise RepairError("the passphrase is wrong or the first page is damaged")
    tree = build_tree(plain)
    out, repairs = bytearray(raw), []
    for number, page in damaged:
        start = (number - 1) * PAGE_SIZE
        intact = intact_mask(raw[start:start + PAGE_SIZE])
        if number == 1 or intact is None:
            raise RepairError(f"page {number}: cannot tell which bytes are damaged")
        if number not in tree.parent:
            raise RepairError(f"page {number} is not a page of a table (index or free page)")
        columns = _sibling_columns(tree, plain, number)
        if columns is None:
            raise RepairError(f"page {number}: no undamaged sibling page to compare rows with")
        low, high = tree.bounds(number)
        cells = salvage_cells(page, intact, columns, low, high)
        out[start:start + PAGE_SIZE] = encrypt_page(keys, build_leaf_page(page, cells), number)
        repairs.append(PageRepair(number, tree.root(number), [c.rowid for c in cells], low, high))
    return bytes(out), repairs


def _connect(path: Path, passphrase: str):
    """Open an encrypted database with sqlcipher3."""
    from sqlcipher3 import dbapi2
    con = dbapi2.connect(str(path))
    con.execute(f"PRAGMA key = '{passphrase}'")
    return con


@dataclass
class TableLoss:
    """Rows of one table lost in the repair.

    Attributes:
        table: Table name.
        lost: Rowids that were on the damaged pages and are gone.
        removed_refs: {(table, column): rows deleted because they pointed at a lost row}.
    """

    table: str
    lost: list[int]
    removed_refs: dict[tuple[str, str], int]


def finish_repair(path: Path, passphrase: str, repairs: list[PageRepair]) -> list[TableLoss]:
    """Drop references to lost rows and verify the repaired database.

    A row is counted as lost when its id lies in a damaged page's range and the table no longer
    has it. References are columns named `<table>_id` or `<table>_id_<n>` in other tables.

    Args:
        path: Repaired database (modified in place).
        passphrase: Its passphrase.
        repairs: Output of `repair_bytes`.

    Raises:
        RepairError: The repaired database still fails an integrity check.
    """
    con = _connect(path, passphrase)
    try:
        roots = {root: name for name, root in con.execute(
            "SELECT name, rootpage FROM sqlite_master WHERE type = 'table'")}
        losses = []
        for root in sorted({r.root for r in repairs}):
            table = roots.get(root)
            if table is None:
                raise RepairError(f"no table has root page {root}")
            ids = {row[0] for row in con.execute(f'SELECT rowid FROM "{table}"')}
            lost = set()
            for r in (r for r in repairs if r.root == root):
                low = r.low if r.low is not None else min(ids | {1}) - 1
                high = r.high if r.high is not None else max(ids | {low})
                lost |= {i for i in range(low + 1, high + 1) if i not in ids}
            losses.append(TableLoss(table, sorted(lost), _drop_refs(con, table, sorted(lost))))
        con.commit()
        problems = [row[0] for row in con.execute("PRAGMA cipher_integrity_check")]
        problems += [row[0] for row in con.execute("PRAGMA integrity_check") if row[0] != "ok"]
    finally:
        con.close()
    if problems:
        raise RepairError("the repaired database is still damaged: " + "; ".join(problems[:5]))
    return losses


def _drop_refs(con, table: str, lost: list[int]) -> dict[tuple[str, str], int]:
    """Delete rows of other tables that point at lost rows of `table`."""
    removed = {}
    if not lost:
        return removed
    pattern = re.compile(rf"{re.escape(table)}_id(_\d+)?")
    marks = ",".join("?" * len(lost))
    for (other,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        if other == table:
            continue
        for column in [row[1] for row in con.execute(f'PRAGMA table_info("{other}")')]:
            if pattern.fullmatch(column):
                cur = con.execute(f'DELETE FROM "{other}" WHERE "{column}" IN ({marks})', lost)
                if cur.rowcount:
                    removed[(other, column)] = cur.rowcount
    return removed


def tracks_missing_from_dlp(pdb: Path | None, dlp: Path, passphrase: str) -> list[str]:
    """Return paths the legacy library lists but Device Library Plus does not.

    Args:
        pdb: export.pdb, or None.
        dlp: A readable exportLibrary.db.
        passphrase: Its passphrase.
    """
    if pdb is None:
        return []
    con = _connect(dlp, passphrase)
    try:
        paths = {row[0] for row in con.execute("SELECT path FROM content")}
    finally:
        con.close()
    return sorted({t.file_path for t in PdbFile(pdb).tracks()} - paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stick", type=Path, help="mount point of the rekordbox USB/SD stick")
    parser.add_argument("--dry-run", action="store_true", help="only report what would be repaired")
    parser.add_argument("--backup-dir", type=Path,
                        help="where to back up the original (default: ~/flac2aiff-backups/"
                             "repair-<timestamp>)")
    args = parser.parse_args()

    root = args.stick.expanduser().resolve()
    libs = find_libraries(root)
    if libs.dlp is None:
        sys.exit(f"error: no Device Library Plus database (exportLibrary.db) on {root}")
    leftovers = [p for p in libs.dlp.parent.glob(libs.dlp.name + "-*")]
    if leftovers:
        sys.exit(f"error: unfinished database journal next to the library ({leftovers[0].name}); "
                 f"open the stick in rekordbox once or remove it by hand first")
    passphrase = dlp_passphrase()
    try:
        repaired, repairs = repair_bytes(libs.dlp.read_bytes(), passphrase)
    except RepairError as e:
        sys.exit(f"error: cannot repair {libs.dlp.name}: {e}. Nothing was changed.")
    if not repairs:
        print(f"{libs.dlp.name} is intact, nothing to repair.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / libs.dlp.name
        work.write_bytes(repaired)
        try:
            losses = finish_repair(work, passphrase, repairs)
        except RepairError as e:
            sys.exit(f"error: {e}. Nothing was changed.")
        missing = tracks_missing_from_dlp(libs.pdb, work, passphrase)
        for r in repairs:
            print(f"Damaged page {r.number}: rebuilt from {len(r.recovered)} intact row(s)")
        for loss in losses:
            print(f"Table {loss.table}: {len(loss.lost)} row(s) could not be recovered "
                  f"(ids {', '.join(map(str, loss.lost)) or '-'})")
            for (other, column), n in loss.removed_refs.items():
                print(f"  removed {n} row(s) from {other} that pointed at them ({column})")
        if missing:
            print(f"Tracks of the legacy library no longer in Device Library Plus ({len(missing)}); "
                  f"players that read exportLibrary.db will not list them:")
            for path in missing:
                print(f"  {path}")
        if args.dry_run:
            print("Dry run: the stick was not changed.")
            return
        backup = args.backup_dir or (Path.home() / "flac2aiff-backups"
                                     / f"repair-{datetime.now():%Y%m%d-%H%M%S}")
        target = backup / libs.dlp.relative_to(root)
        target.parent.mkdir(parents=True)
        shutil.copy2(libs.dlp, target)
        staged = libs.dlp.with_name(libs.dlp.name + ".tmp")
        shutil.copyfile(work, staged)
        staged.replace(libs.dlp)
    if hasattr(os, "sync"):  # not available on Windows
        os.sync()
    print(f"Repaired {libs.dlp.relative_to(root)}. Original kept at {target}")


if __name__ == "__main__":
    main()
