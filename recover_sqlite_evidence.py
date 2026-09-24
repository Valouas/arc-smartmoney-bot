"""Offline SQLite record carving. Output is quarantined evidence, NOT trading input.

Reads database pages and all WAL frames (including stale generations). Never
opens the source through SQLite, changes the live database, or calls a network.
Only complete, self-contained records are accepted; overflow is not guessed.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import struct

ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')
HASH = re.compile(r'0x[0-9a-fA-F]{64}\Z')
BODY = re.compile(rb'(0x[0-9a-fA-F]{40})(0x[0-9a-fA-F]{64})(\{)')


def varint(data, offset, end):
    value = 0
    for i in range(9):
        if offset >= end:
            raise ValueError('short varint')
        byte = data[offset]
        offset += 1
        value = (value << (8 if i == 8 else 7)) | (byte if i == 8 else byte & 127)
        if i == 8 or byte < 128:
            return value, offset
    raise ValueError('varint')


def record(payload):
    end = len(payload)
    header, pos = varint(payload, 0, end)
    if not pos <= header <= end:
        raise ValueError('header')
    types = []
    while pos < header:
        serial, pos = varint(payload, pos, header)
        types.append(serial)
    pos = header
    values = []
    widths = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8}
    for serial in types:
        if serial == 0:
            values.append(None)
        elif serial in (8, 9):
            values.append(serial - 8)
        else:
            if serial in (10, 11):
                raise ValueError('reserved type')
            size = widths[serial] if serial < 8 else (serial - 12) // 2
            raw = payload[pos:pos + size]
            if len(raw) != size:
                raise ValueError('short field')
            pos += size
            if serial == 7:
                values.append(struct.unpack('>d', raw)[0])
            elif serial < 7:
                values.append(int.from_bytes(raw, 'big', signed=True))
            elif serial % 2:
                values.append(raw.decode('utf-8'))
            else:
                values.append(raw)
    if pos != end:
        raise ValueError('trailing bytes')
    return values


def historical_record(values):
    # The fourth field is the source id in the existing recovery archive.
    if len(values) == 4:
        if not isinstance(values[0], str) or not re.fullmatch(r'[0-9a-f]{64}', values[0]):
            return None
        values = values[1:]
    if len(values) != 3:
        return None
    wallet, tx_hash, payload = values
    if not all(isinstance(x, str) for x in values):
        return None
    if not ADDRESS.fullmatch(wallet) or not HASH.fullmatch(tx_hash):
        return None
    try:
        decoded = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict) or decoded.get('tx_hash') != tx_hash:
        return None
    # All collector payload variants have one of these explicit markers.
    if not any(k in decoded for k in ('trades', 'outside_snapshot', 'error')):
        return None
    return wallet, tx_hash, payload


def leaf_records(page, pgno, reserved=0):
    offset = 100 if pgno == 1 else 0
    usable = len(page) - reserved
    if len(page) < offset + 8 or page[offset] != 13:
        return
    count = int.from_bytes(page[offset + 3:offset + 5], 'big')
    pointers_end = offset + 8 + count * 2
    if pointers_end > usable:
        return
    for i in range(count):
        pointer = offset + 8 + i * 2
        start = int.from_bytes(page[pointer:pointer + 2], 'big')
        try:
            if start < pointers_end or start >= usable:
                continue
            size, pos = varint(page, start, usable)
            _, pos = varint(page, pos, usable)
            # SQLite table leaf max local payload. Larger records use overflow.
            if size > usable - 35 or pos + size > usable:
                continue
            item = historical_record(record(page[pos:pos + size]))
            if item:
                yield start, item
        except (ValueError, UnicodeError, KeyError, struct.error):
            continue


def orphan_records(page):
    """Find intact SQLite records whose btree pointer was removed/overwritten.

    Require a valid SQLite serial-type header and exact record boundary in
    addition to the adjacent wallet/hash/JSON. Never infer missing fields.
    """
    for match in BODY.finditer(page):
        start = match.start()
        for header_size in range(4, 24):
            header_start = start - header_size
            if header_start < 0 or page[header_start] != header_size:
                continue
            try:
                pos = header_start + 1
                types = []
                while pos < start:
                    serial, pos = varint(page, pos, start)
                    types.append(serial)
                if len(types) != 3 or types[:2] != [97, 145] or types[2] < 13 or types[2] % 2 != 1:
                    continue
                length = header_size + 42 + 66 + (types[2] - 13) // 2
                end = header_start + length
                if end > len(page):
                    continue
                item = historical_record(record(page[header_start:end]))
                if item:
                    yield header_start, item
                    break
            except (ValueError, UnicodeError, KeyError, struct.error):
                continue


def body_records(page):
    """Quarantine intact adjacent fields when the record header is destroyed.

    These fragments have weaker provenance than serial-header-checked records.
    They must never be loaded as a completed or authoritative history.
    """
    decoder = json.JSONDecoder()
    for match in BODY.finditer(page):
        raw = page[match.start(3):].decode('utf-8', errors='surrogateescape')
        try:
            _, end = decoder.raw_decode(raw)
            payload = raw[:end]
            # Reject invalid UTF-8 within the JSON, even if json.loads accepts it.
            payload.encode('utf-8')
            item = historical_record([match[1].decode(), match[2].decode(), payload])
            if item:
                yield match.start(), item
        except (ValueError, UnicodeError, RecursionError):
            continue


def pages(path, page_size):
    with path.open('rb') as stream:
        header = stream.read(32)
        stream.seek(0)
        if header.startswith(b'SQLite format 3\x00'):
            stored_size = int.from_bytes(header[16:18], 'big')
            if (65536 if stored_size == 1 else stored_size) != page_size or header[20] != 0:
                raise ValueError('Unsupported database page size or reserved bytes')
            for number in range(1, path.stat().st_size // page_size + 1):
                yield number, number, '', stream.read(page_size)
        elif header[:4] in (b'7\x7f\x06\x82', b'7\x7f\x06\x83'):
            if int.from_bytes(header[8:12], 'big') != page_size:
                raise ValueError('WAL page size mismatch')
            stream.seek(32)
            frame = 0
            while True:
                raw = stream.read(24 + page_size)
                if not raw:
                    break
                if len(raw) != 24 + page_size:
                    break
                frame += 1
                yield frame, int.from_bytes(raw[:4], 'big'), raw[8:16].hex(), raw[24:]
        else:
            raise ValueError('Unsupported evidence file')


def carve(sources, output, page_size=4096, reserved=0, orphans=False, bodies=False):
    if output.exists():
        raise ValueError('Output must be a new evidence archive')
    output.parent.mkdir(parents=True, exist_ok=True)
    archive = sqlite3.connect(output)
    archive.executescript('''
      CREATE TABLE sources(id INTEGER PRIMARY KEY,path TEXT,bytes INTEGER,sha256 TEXT);
      CREATE TABLE evidence(wallet TEXT,tx_hash TEXT,payload_hash TEXT,payload TEXT,
        source_id INTEGER,frame_or_page INTEGER,page_number INTEGER,salt TEXT,
        cell_offset INTEGER,occurrences INTEGER DEFAULT 1,
        PRIMARY KEY(wallet,tx_hash,payload_hash));
      CREATE TABLE summary(payload TEXT);
      CREATE TABLE metadata(mode TEXT,authority TEXT);
    ''')
    archive.execute('INSERT INTO metadata VALUES(?,?)',
                    ('body_fragments' if bodies else 'orphan_records' if orphans else 'leaf_records',
                     'QUARANTINED_NOT_TRADING_INPUT'))
    counts = Counter()
    try:
        for source_id, path in enumerate(sources, 1):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    digest.update(chunk)
            archive.execute('INSERT INTO sources VALUES(?,?,?,?)',
                            (source_id, str(path.resolve()), path.stat().st_size, digest.hexdigest()))
            for frame, pgno, salt, page in pages(path, page_size):
                counts['pages_examined'] += 1
                if b'"tx_hash"' not in page:
                    continue
                records = body_records(page) if bodies else orphan_records(page) if orphans else leaf_records(page, pgno, reserved)
                for offset, (wallet, tx_hash, payload) in records:
                    counts['complete_record_occurrences'] += 1
                    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
                    archive.execute('''INSERT INTO evidence
                      VALUES(?,?,?,?,?,?,?,?,?,1)
                      ON CONFLICT(wallet,tx_hash,payload_hash)
                      DO UPDATE SET occurrences=occurrences+1''',
                      (wallet, tx_hash, payload_hash, payload, source_id, frame, pgno, salt, offset))
                if frame % 10000 == 0:
                    archive.commit()
            archive.commit()
            print(json.dumps({'source': path.name, **counts}), flush=True)
        counts['distinct_payload_versions'] = archive.execute('SELECT count(*) FROM evidence').fetchone()[0]
        counts['distinct_wallet_transactions'] = archive.execute('SELECT count(*) FROM (SELECT DISTINCT wallet,tx_hash FROM evidence)').fetchone()[0]
        archive.execute('INSERT INTO summary VALUES(?)', (json.dumps(dict(counts)),))
        archive.commit()
        return dict(counts)
    finally:
        archive.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--orphans', action='store_true', help='Search intact records even outside active leaf cell pointers')
    parser.add_argument('--bodies', action='store_true', help='Quarantine adjacent fields with destroyed SQLite headers; weaker provenance')
    args = parser.parse_args()
    print(json.dumps(carve(args.source, args.output, orphans=args.orphans, bodies=args.bodies)))


if __name__ == '__main__':
    main()
