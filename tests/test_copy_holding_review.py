import json
import sqlite3
from copy_holding_review import review
from test_copy_holding_duration import W,save_history

def test_review_readonly_complete_and_incomplete(tmp_path):
    p=tmp_path/'c.db'
    with sqlite3.connect(p) as db:
        save_history(db,30)
        db.execute('CREATE TABLE campaign_queue(wallet,state,reason)')
        db.execute("INSERT INTO campaign_queue VALUES(?,'PENDING',NULL)",(W,))
    before=p.read_bytes()
    r=review(p,[W]);assert r['too_short']==1 and r['network_calls']==0
    assert r['rows'][0]['holding_fast_exit_pct']==100
    assert p.read_bytes()==before
    with sqlite3.connect(p) as db:db.execute("UPDATE historical_runs SET phase='DECODING'")
    r=review(p,[W]);assert r['unknown']==1 and r['rows'][0]['holding_fast_exit_pct'] is None

def test_review_reports_ambiguity_without_inventing_duration(tmp_path):
    p=tmp_path/'c.db'
    with sqlite3.connect(p) as db:
        save_history(db)
        db.execute('CREATE TABLE campaign_queue(wallet,state,reason)')
        h,payload=db.execute('SELECT tx_hash,payload FROM historical_transactions LIMIT 1').fetchone()
        t=json.loads(payload);t['inventory_gap']=True
        db.execute('UPDATE historical_transactions SET payload=? WHERE tx_hash=?',(json.dumps(t),h))
    r=review(p,[W]);assert r['wallets_with_reconstruction_issues']==1
    assert r['unknown']==1 and r['rows'][0]['holding_fast_exit_pct'] is None
