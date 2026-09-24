import json
import sqlite3
from unittest.mock import Mock

from historical_store import initialize
from recover_historical import recover
from test_historical_analysis import cfg, store, W, W2, NOW, trade
from test_historical_snapshots import completed, saved


def backup():
    source = sqlite3.connect(':memory:');source.row_factory=sqlite3.Row
    initialize(source)
    for n,(wallet,phase) in enumerate(((W,'COMPLETE'),(W2,'DECODING')),1):
        source.execute('''INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase,completed_at)
            VALUES(?,1,?,50,?,'anchor',?,?)''',(wallet,NOW-100,NOW-50,phase,NOW-40 if phase=='COMPLETE' else None))
        row=trade(n,'BUY',n)
        source.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(wallet,row['tx_hash'],json.dumps(row)))
    source.commit()
    return source


def test_dry_run_does_not_write_or_verify_network(store):
    source=backup();verify=Mock()
    before=store.db.total_changes
    result=recover(source,store.db,'backup',verify)
    assert result['missing_from_current_work']==2
    assert store.db.total_changes==before
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name='historical_recovery_runs'").fetchone()
    verify.assert_not_called();source.close()


def test_restore_finished_evidence_archive_partial_and_preserve_work(store):
    source=backup();verify=Mock(return_value=True)
    work=trade(3,'BUY',10)
    store.db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(W,work['tx_hash'],json.dumps(work)));store.db.commit()
    before=[tuple(r) for r in store.db.execute('SELECT * FROM historical_transactions')]
    result=recover(source,store.db,'backup',verify,apply=True)
    assert result['archived_rows_added']==2
    assert result['completed_snapshots_restored']==1
    assert [tuple(r) for r in store.db.execute('SELECT * FROM historical_transactions')]==before
    assert [r[0] for r in store.db.execute('SELECT wallet FROM historical_snapshots')]==[W]
    assert store.db.execute('SELECT COUNT(*) FROM historical_recovery_transactions').fetchone()[0]==2
    assert store.db.execute('SELECT COUNT(*) FROM historical_runs').fetchone()[0]==0
    verify.assert_called_once_with(50,'anchor')
    repeat=recover(source,store.db,'backup',verify,apply=True)
    assert repeat['archived_rows_added']==repeat['completed_snapshots_restored']==0
    assert store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    assert store.db.execute('SELECT COUNT(*) FROM scanner_state').fetchone()[0]==0
    source.close()


def test_recovery_never_replaces_a_more_recent_completed_snapshot(store,cfg):
    completed(store,cfg)
    before=saved(store)
    source=backup();verify=Mock(return_value=True)
    result=recover(source,store.db,'backup',verify,apply=True)
    assert result['completed_snapshots_restored']==0
    assert saved(store)==before
    verify.assert_not_called();source.close()


def test_wrong_anchor_is_archived_but_not_used_for_ranking(store):
    source=backup()
    result=recover(source,store.db,'backup',Mock(return_value=False),apply=True)
    assert result['completed_snapshots_restored']==0
    assert store.db.execute('SELECT COUNT(*) FROM historical_recovery_transactions').fetchone()[0]==2
    assert store.db.execute('SELECT COUNT(*) FROM historical_snapshots').fetchone()[0]==0
    source.close()
