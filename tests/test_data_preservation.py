import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from data_preservation import (install_database,archive_file,before_replace,verify_archive,
                               should_archive,archive_api_response)


def test_update_delete_and_replace_archive_old_rows_without_changing_active_state(tmp_path):
    p=tmp_path/'evidence.sqlite3'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE evidence(wallet TEXT PRIMARY KEY,payload TEXT,context TEXT)')
        db.execute("INSERT INTO evidence VALUES('wallet','original','v1')")
    install_database(p,{'evidence'})
    with sqlite3.connect(p) as db:
        db.execute("UPDATE evidence SET payload='second'")
        db.execute("INSERT OR REPLACE INTO evidence VALUES('wallet','third','v2')")
        db.execute("DELETE FROM evidence")
        saved=[json.loads(r[0]) for r in db.execute('SELECT row_json FROM retained_evidence_versions')]
        assert {r['payload'] for r in saved}=={'original','second','third'}
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==0


def test_unique_constraint_replace_and_blob_preserved(tmp_path):
    p=tmp_path/'evidence.sqlite3'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,tx TEXT UNIQUE,payload BLOB)')
        db.execute('INSERT INTO evidence(tx,payload) VALUES(?,?)',('hash',b'original'))
    install_database(p,{'evidence'})
    with sqlite3.connect(p) as db:
        db.execute('INSERT OR REPLACE INTO evidence(tx,payload) VALUES(?,?)',('hash',b'new'))
        saved=json.loads(db.execute('SELECT row_json FROM retained_evidence_versions').fetchone()[0])
        assert bytes.fromhex(saved['payload']['__blob_hex__'])==b'original'
        assert db.execute('SELECT payload FROM evidence').fetchone()[0]==b'new'


@pytest.mark.parametrize('clause', ['', 'OR ABORT', 'OR REPLACE'])
def test_upsert_retains_versions_without_archive_conflict(tmp_path,clause):
    p=tmp_path/'evidence.sqlite3'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute("INSERT INTO evidence VALUES(1,'a')")
    install_database(p,{'evidence'})
    with sqlite3.connect(p) as db:
        for value in ('b','a','b'):
            db.execute('INSERT '+clause+' INTO evidence VALUES(1,?) ON CONFLICT(id) '
                       'DO UPDATE SET payload=excluded.payload',(value,))
        assert db.execute('SELECT payload FROM evidence').fetchone()[0]=='b'
        saved=[json.loads(r[0]) for r in db.execute('SELECT row_json FROM retained_evidence_versions')]
        assert sorted(r['payload'] for r in saved)==['a','b']
    # Reinstallation upgrades live triggers without replacing archived evidence.
    install_database(p,{'evidence'})
    with sqlite3.connect(p) as db:
        db.execute("UPDATE evidence SET payload='c'")
        assert db.execute('SELECT count(*) FROM retained_evidence_versions').fetchone()[0]==2


def test_rollback_also_rolls_back_archive_and_idempotent_install(tmp_path):
    p=tmp_path/'evidence.sqlite3'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute("INSERT INTO evidence VALUES(1,'first')")
    install_database(p,{'evidence'});install_database(p,{'evidence'})
    with sqlite3.connect(p) as db:
        db.execute('DELETE FROM evidence');db.rollback()
        assert db.execute('SELECT count(*) FROM retained_evidence_versions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==1


def test_atomic_replace_retains_both_versions_and_deduplicates(tmp_path):
    directory=tmp_path/'data';directory.mkdir()
    old=directory/'results.json';new=directory/'next.tmp'
    old.write_text('{"pnl":100}');new.write_text('{"pnl":200}')
    before_replace(old,new,tmp_path);before_replace(old,new,tmp_path)
    assert verify_archive(tmp_path)==2 and old.read_text()=='{"pnl":100}'
    with sqlite3.connect(directory/'evidence_archive/catalog.sqlite3') as db:
        rows=list(db.execute('SELECT path,sha256 FROM versions'))
    assert {r[0] for r in rows}=={'data/results.json'}
    decoded={gzip.decompress((directory/'evidence_archive/blobs'/(r[1]+'.gz')).read_bytes()) for r in rows}
    assert decoded=={b'{"pnl":100}',b'{"pnl":200}'}


def test_failed_archive_blocks_atomic_publication(tmp_path,monkeypatch):
    import atomic_json
    from data_preservation import before_replace as real
    directory=tmp_path/'data';directory.mkdir();path=directory/'result.json';path.write_text('old')
    monkeypatch.setattr('data_preservation.before_replace',lambda path,temp:real(path,temp,tmp_path))
    monkeypatch.setattr('data_preservation.archive_file',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):atomic_json.write_json(path,{'new':True})
    assert path.read_text()=='old'


def test_no_archive_recursion_or_secrets_or_heartbeat_versions(tmp_path):
    assert not should_archive(tmp_path/'.env',tmp_path)
    assert not should_archive(tmp_path/'data/evidence_archive/nested.json',tmp_path)
    assert not should_archive(tmp_path/'data/run_all_state.json',tmp_path)
    assert should_archive(tmp_path/'data/nansen_audit.json',tmp_path)


def test_raw_provider_responses_are_opt_in_and_versioned(tmp_path,monkeypatch):
    monkeypatch.delenv('PRESERVE_RAW_API_RESPONSES',raising=False)
    archive_api_response('zerion','wallets/one/pnl',{}, {'profit':10},tmp_path)
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv('PRESERVE_RAW_API_RESPONSES','true')
    for gain in (10,20,10):archive_api_response('zerion','wallets/one/pnl',{}, {'profit':gain},tmp_path)
    assert verify_archive(tmp_path)==2
