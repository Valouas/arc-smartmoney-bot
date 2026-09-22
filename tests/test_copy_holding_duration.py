import copy
import json
import sqlite3

import pytest

from copy_holding_duration import measure, stored_holding

W = '0x'+'1'*40
T = '0x'+'2'*40


def tx(i, timestamp, side='BUY', quantity=10, token=T):
    return dict(tx_hash='0x'+f'{i:064x}', block=i, index=0, timestamp=timestamp,
                inventory_gap=False, trades=[dict(token=token, side=side,
                    token_amount=quantity, confirmation_eligible=True)])


def save_history(db, duration=120, wallet=W):
    db.execute('CREATE TABLE IF NOT EXISTS historical_runs(wallet,version,phase,error,cursor,cutoff_at,cutoff_block)')
    db.execute('CREATE TABLE IF NOT EXISTS historical_transactions(wallet,tx_hash,payload)')
    db.execute('INSERT INTO historical_runs VALUES(?,1,\'COMPLETE\',NULL,NULL,10000,99)', (wallet,))
    for t in [tx(1, 100), tx(2, 100+duration, 'SELL')]:
        db.execute('INSERT INTO historical_transactions VALUES(?,?,?)', (wallet, t['tx_hash'], json.dumps(t)))


@pytest.mark.parametrize('duration,expected', [(0,False),(119,False),(120,True),(121,True)])
def test_inclusive_two_minute_boundary(duration, expected):
    r = measure([tx(1,100), tx(2,100+duration,'SELL')], 1000)
    assert r['holding_duration_eligible'] is expected
    assert r['median_first_sale_seconds'] == duration


def test_partial_sale_is_first_exit_not_last_dust_exit():
    r = measure([tx(1,100),tx(2,130,'SELL',1),tx(3,10000,'SELL',9)], 10000)
    assert r['median_first_sale_seconds']==30
    assert r['holding_sample_size']==1
    assert not r['holding_duration_eligible']


def test_fifo_first_exit_per_acquisition_and_duplicates():
    transactions=[tx(1,100),tx(2,200),tx(3,250,'SELL',15),tx(4,900,'SELL',5)]
    r=measure(list(reversed(transactions))+[transactions[2]],1000)
    assert r['holding_sample_size']==2
    assert r['median_first_sale_seconds']==100  # 150 and 50; not 700


def test_median_not_average_or_capital_weighted_and_unsold_separate():
    rows=[]
    for i,(delay,qty) in enumerate([(10,1),(20,1),(900,1000)]):
        rows.extend([tx(2*i+1,i*1000,quantity=qty),tx(2*i+2,i*1000+delay,'SELL',qty)])
    rows.append(tx(7,3000))
    r=measure(rows,4000)
    assert r['median_first_sale_seconds']==20
    assert r['holding_buy_count']==4 and r['holding_sample_size']==3
    assert r['holding_unsold_buy_count']==1


def test_no_exit_unknown_even_old_open_position():
    r=measure([tx(1,1)],100000)
    assert r['median_first_sale_seconds'] is None and not r['holding_duration_eligible']
    assert r['holding_unsold_buy_count']==1


@pytest.mark.parametrize('change', [
    lambda rows: rows[0].pop('timestamp'),
    lambda rows: rows[0].update(inventory_gap=True),
    lambda rows: rows[1].update(error='failed'),
    lambda rows: rows[1].update(timestamp=1),
    lambda rows: rows[1]['trades'][0].update(token_amount=100),
    lambda rows: rows[0]['trades'][0].update(confirmation_eligible=False),
    lambda rows: rows[0]['trades'][0].update(token_amount='NaN'),
    lambda rows: rows[1]['trades'].append(copy.deepcopy(rows[1]['trades'][0])),
    lambda rows: rows.append(dict(rows[0],timestamp=99)),
    lambda rows: rows[1].update(block=1),
])
def test_ambiguous_history_never_passes(change):
    rows=[tx(1,100),tx(2,300,'SELL')];change(rows)
    r=measure(rows,1000)
    assert r['holding_duration_status']=='UNKNOWN_HOLDING_DURATION'
    assert r['median_first_sale_seconds'] is None and not r['holding_duration_eligible']


def test_simple_transfer_never_creates_first_sale():
    t=tx(2,300);t['trades']=[]
    assert measure([tx(1,100),t],1000)['holding_sample_size']==0


def test_snapshot_selection_retained_invalidation_and_no_writes(tmp_path):
    p=tmp_path/'history.db'
    with sqlite3.connect(p) as db:
        save_history(db)
        run=dict(wallet=W,version=1,phase='COMPLETE',error=None,cursor=None,cutoff_at=10000,cutoff_block=99)
        db.execute('CREATE TABLE historical_snapshots(wallet,run,invalidated)')
        db.execute('INSERT INTO historical_snapshots VALUES(?,?,0)',(W,json.dumps(run)))
        db.execute('CREATE TABLE historical_snapshot_transactions AS SELECT * FROM historical_transactions')
    before=p.read_bytes()
    with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
        assert stored_holding(db,W)['holding_duration_eligible']
    assert before==p.read_bytes()
    with sqlite3.connect(p) as db:
        db.execute("UPDATE historical_runs SET phase='FETCHING'")
        db.execute('UPDATE historical_transactions SET payload=NULL')
        r=stored_holding(db,W)
        assert r['holding_retained_snapshot'] and r['holding_duration_eligible']
        db.execute('UPDATE historical_snapshots SET invalidated=1')
        assert not stored_holding(db,W)['holding_duration_eligible']
        db.execute('UPDATE historical_runs SET version=0')
        db.execute('UPDATE historical_snapshots SET invalidated=0')
        assert stored_holding(db,W)['holding_duration_reason']=='INVALIDATED_HISTORY'


@pytest.mark.parametrize('field,value', [('phase','DECODING'),('error','timeout'),('cursor','next'),('version',0)])
def test_incomplete_run_not_evidence(field,value):
    with sqlite3.connect(':memory:') as db:
        save_history(db)
        db.execute(f'UPDATE historical_runs SET {field}=?',(value,))
        assert not stored_holding(db,W)['holding_duration_eligible']


def test_missing_payload_and_block_outside_completed_coverage():
    with sqlite3.connect(':memory:') as db:
        save_history(db)
        db.execute('UPDATE historical_runs SET cutoff_block=1')
        assert stored_holding(db,W)['holding_duration_reason']=='TRANSACTION_OUTSIDE_BLOCK_COVERAGE'
        db.execute('UPDATE historical_runs SET cutoff_block=99')
        db.execute('UPDATE historical_transactions SET payload=NULL')
        assert stored_holding(db,W)['holding_duration_reason']=='MISSING_TRANSACTION_PAYLOAD'


@pytest.mark.parametrize('minimum',[True,-1,float('inf'),float('nan'),None])
def test_bad_minimum(minimum):
    with pytest.raises(ValueError,match='invalid_min_holding_seconds'):
        measure([],1000,minimum)
