import hashlib
import json
import sqlite3
import struct

import pytest

from recover_sqlite_evidence import (
    body_records, carve, historical_record, leaf_records, orphan_records,
    pages, record, varint,
)

WALLET = '0x' + 'a' * 40
TX = '0x' + 'b' * 64
PAYLOAD = json.dumps({'tx_hash': TX, 'block': 123, 'trades': [], 'gas_usdc': '0.01'})


def fixture_db(path):
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA secure_delete=OFF')
        db.execute('CREATE TABLE historical_transactions(wallet TEXT,tx_hash TEXT,payload TEXT)')
        db.execute('INSERT INTO historical_transactions VALUES(?,?,?)', (WALLET, TX, PAYLOAD))
    return path


def test_leaf_record_and_orphan_have_exact_fields(tmp_path):
    path = fixture_db(tmp_path / 'source.db')
    page = path.read_bytes()[4096:8192]
    assert [r for _, r in leaf_records(page, 2)] == [(WALLET, TX, PAYLOAD)]
    assert [r for _, r in orphan_records(page)] == [(WALLET, TX, PAYLOAD)]
    # Losing the btree cell pointers must not lose a still-intact record.
    damaged = bytearray(page)
    damaged[:12] = bytes(12)
    assert list(leaf_records(damaged, 2)) == []
    assert [r for _, r in orphan_records(damaged)] == [(WALLET, TX, PAYLOAD)]


def test_deleted_record_body_can_be_saved_without_inventing_authority():
    body = (WALLET + TX + PAYLOAD).encode()
    assert [r for _, r in body_records(body)] == [(WALLET, TX, PAYLOAD)]
    assert list(orphan_records(body)) == []  # No intact SQLite record header.
    assert list(body_records(body[:-1])) == []


def test_invalid_json_hash_or_wallet_not_recovered():
    assert historical_record([WALLET, TX, '{}']) is None
    assert historical_record([WALLET, TX, PAYLOAD.replace(TX, '0x' + 'c' * 64)]) is None
    assert historical_record(['pool', TX, PAYLOAD]) is None
    assert historical_record([WALLET, TX, PAYLOAD[:-1]]) is None
    assert historical_record([WALLET, TX, None]) is None


def test_archive_deduplicates_and_never_mutates_input(tmp_path):
    source = fixture_db(tmp_path / 'source.db')
    before = hashlib.sha256(source.read_bytes()).digest()
    output = tmp_path / 'evidence.db'
    result = carve([source, source], output)
    assert result['distinct_wallet_transactions'] == 1
    assert result['complete_record_occurrences'] == 2
    assert hashlib.sha256(source.read_bytes()).digest() == before
    with sqlite3.connect(output) as archive:
        assert archive.execute('SELECT authority FROM metadata').fetchone()[0] == 'QUARANTINED_NOT_TRADING_INPUT'
        assert archive.execute('SELECT occurrences FROM evidence').fetchone()[0] == 2
        assert archive.execute("SELECT name FROM sqlite_master WHERE name='historical_transactions'").fetchone() is None
    with pytest.raises(ValueError, match='new evidence archive'):
        carve([source], source)
    assert hashlib.sha256(source.read_bytes()).digest() == before


def test_stale_wal_frame_is_evidence_not_committed_state(tmp_path):
    source = fixture_db(tmp_path / 'source.db')
    page = source.read_bytes()[4096:8192]
    header = struct.pack('>8I', 0x377f0682, 3007000, 4096, 0, 3, 4, 0, 0)
    # Deliberately stale salt, no committed database size/checksum assertion.
    frame = struct.pack('>6I', 2, 0, 1, 2, 0, 0) + page
    path = tmp_path / 'source.db-wal'
    path.write_bytes(header + frame + b'incomplete tail')
    output = tmp_path / 'wal_evidence.db'
    assert carve([path], output)['distinct_wallet_transactions'] == 1
    with sqlite3.connect(output) as db:
        assert db.execute('SELECT salt FROM evidence').fetchone()[0] == '0000000100000002'
        assert db.execute('SELECT authority FROM metadata').fetchone()[0] == 'QUARANTINED_NOT_TRADING_INPUT'


def test_bad_varint_and_record_fail_closed():
    with pytest.raises(ValueError):
        varint(b'\x80', 0, 1)
    with pytest.raises(ValueError):
        record(b'\x05\x61')
    assert list(leaf_records(b'\x0d' + bytes(4095), 2)) == []


def test_utf8_corruption_in_fragment_is_rejected():
    raw = (WALLET + TX + PAYLOAD).encode().replace(b'gas_usdc', b'gas_\xffsdc')
    assert list(body_records(raw)) == []


def test_unsupported_source_is_not_silently_accepted(tmp_path):
    path = tmp_path / 'source'
    path.write_bytes(b'not a sqlite file')
    with pytest.raises(ValueError, match='Unsupported'):
        list(pages(path, 4096))
