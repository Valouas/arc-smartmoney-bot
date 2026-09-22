import json
import sqlite3
import pytest
from copy_holding_campaign import QUEUE,load_priorities,next_pending,advisory_allowed

A='0x'+'a'*40
B='0x'+'b'*40
C='0x'+'c'*40

def queue():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;db.executescript(QUEUE)
    db.execute('INSERT INTO campaign_queue(wallet,priority,turns) VALUES(?,0,0)',(A,))
    db.execute('INSERT INTO campaign_queue(wallet,priority,turns) VALUES(?,1,50)',(B,))
    db.commit()
    return db

def test_priority_does_not_reset_or_spend_and_respects_backoff():
    with queue() as db:
        before=list(db.iterdump())
        assert next_pending(db,100,(B,))['wallet']==B
        assert list(db.iterdump())==before
        assert next_pending(db,100)['wallet']==A
        db.execute('UPDATE campaign_queue SET next_attempt=200 WHERE wallet=?',(B,))
        assert next_pending(db,100,(B,))['wallet']==A

@pytest.mark.parametrize('state',['VERIFIED','REVIEW_REQUIRED','BLOCKED','TOO_SHORT'])
def test_priority_never_reopens_completed_or_blocked_wallet(state):
    with queue() as db:
        db.execute('UPDATE campaign_queue SET state=? WHERE wallet=?',(state,B))
        assert next_pending(db,100,(B,))['wallet']==A

@pytest.mark.parametrize('wallets',[[C],[A,A],[],['bad'],[A]*26])
def test_invalid_or_out_of_scope_priorities_fail(tmp_path,wallets):
    p=tmp_path/'priorities.json';p.write_text(json.dumps({'wallets':wallets}))
    with queue() as db,pytest.raises(ValueError):load_priorities(p,db)

def test_loading_valid_priorities_and_advisory_selection(tmp_path):
    p=tmp_path/'priorities.json';p.write_text(json.dumps({'wallets':[B]}))
    with queue() as db:
        assert load_priorities(p,db)==(B,)
        current=next_pending(db,100,(B,))
        assert not advisory_allowed({'wallet':A},current,(B,))
        assert advisory_allowed({'wallet':B},current,(B,))
        assert not advisory_allowed(None,current,(B,))
        assert advisory_allowed({'wallet':A},None,(B,))
