from contextlib import nullcontext
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from hexbytes import HexBytes
import pytest

import hybrid_history as h
import dune_hybrid_worker as worker
from copy_holding_campaign import decode_hashes
from historical_collector import HistoricalCollector


@pytest.fixture
def fixture():
    return json.loads((Path(__file__).parent/'fixtures/dune_raw_arc_bundle.json').read_text())


def response(rows):
    return {'state':'QUERY_STATE_COMPLETED','result':{'rows':rows,'metadata':{'total_row_count':len(rows)}}}


@pytest.fixture
def store(tmp_path):
    path=tmp_path/'history.sqlite3';db=h.connect(path);db.executescript(h.SCHEMA)
    yield path,db
    db.close()


def test_live_bundle_runs_same_decoder_and_gas(fixture):
    bundles,rejected=h.parse_rows(fixture['rows'],[fixture['expected']]);assert not rejected
    b=bundles[fixture['expected']];tx=b['transaction'];receipt=b['receipt']
    rpc=SimpleNamespace(token_meta=lambda t:{'symbol':'MURMUR','decimals':18},swap_emitter_trusted=lambda a,n:a.lower()=='0x8366a39cc670b4001a1121b8f6a443a643e40951')
    block={'hash':tx['blockHash'],'timestamp':b['timestamp'],'number':tx['blockNumber']}
    result=object.__new__(HistoricalCollector).transaction(tx['from'],fixture['expected'],{'cutoff_block':tx['blockNumber']},reads=(tx,receipt,block),rpc=rpc)
    expected=json.loads((Path(__file__).parent/'fixtures/provider_history_arc_live.json').read_text())['local_transaction']
    assert result==expected


@pytest.mark.parametrize('field,value',[('chain_id','0x1'),('success',None),('gas_price',1.5),('gas_used',None),('log_count',99),('block_hash','bad')])
def test_malformed_transaction_never_becomes_bundle(fixture,field,value):
    fixture['rows'][0][field]=value
    valid,bad=h.parse_rows(fixture['rows'],[fixture['expected']])
    assert not valid and fixture['expected'] in bad


@pytest.mark.parametrize('change',['missing_log','duplicate_log','wrong_block','topic_gap','bad_data','failed_with_logs'])
def test_bad_receipt_rejected(fixture,change):
    rows=fixture['rows']
    if change=='missing_log':rows.pop()
    elif change=='duplicate_log':rows[-1]=copy.deepcopy(rows[-2])
    elif change=='wrong_block':rows[-1]['block_number']+=1
    elif change=='topic_gap':rows[-1]['topic0']=None;rows[-1]['topic1']='0x'+'a'*64
    elif change=='bad_data':rows[-1]['data']='0x1'
    else:rows[0]['success']=False
    assert not h.parse_rows(rows,[fixture['expected']])[0]


def test_only_explicit_complete_zero_logs_are_empty(fixture):
    row=fixture['rows'][0];row['log_count']=0;row['success']=False
    bundles,bad=h.parse_rows([row],[fixture['expected']]);assert not bad
    assert bundles[fixture['expected']]['receipt']['logs']==[]
    assert not h.parse_rows([],[fixture['expected']])[0]


def test_truncated_result_imports_nothing(store,fixture):
    path,db=store;r=response(fixture['rows']);r['next_offset']=100
    with pytest.raises(ValueError):h.import_result(db,r,[fixture['expected']],'test')
    assert not db.execute('SELECT * FROM bundles').fetchall()


def test_duplicate_hash_rejected_and_conflicts_keep_old_data(store,fixture):
    path,db=store;rows=fixture['rows'];tx=fixture['expected']
    h.import_result(db,response(rows),[tx],'test');before=db.execute('SELECT payload FROM bundles').fetchone()[0]
    with pytest.raises(ValueError):h.import_result(db,response(rows+[rows[0]]),[tx],'duplicate')
    rows[0]['gas_used']+=1
    with pytest.raises(ValueError):h.import_result(db,response(rows),[tx],'conflict')
    assert db.execute('SELECT payload FROM bundles').fetchone()[0]==before


def test_cached_reads_only_need_canonical_block(store,fixture):
    path,db=store;h.import_result(db,response(fixture['rows']),[fixture['expected']],'test')
    row=fixture['rows'][0];calls=[]
    def block(n,full):
        calls.append(n);return {'hash':HexBytes(row['block_hash']),'number':n,'timestamp':h.timestamp(row['block_time'])}
    rpc=SimpleNamespace(get_block=block)
    cache=h.HybridReads(path);bundle=cache.get(fixture['expected'],{'cutoff_block':row['block_number']},rpc)
    assert bundle and calls==[row['block_number']]
    assert cache.get('0x'+'f'*64,{'cutoff_block':row['block_number']},rpc) is None


def test_reorg_or_tampered_cache_falls_back(store,fixture):
    path,db=store;h.import_result(db,response(fixture['rows']),[fixture['expected']],'test')
    row=fixture['rows'][0];rpc=SimpleNamespace(get_block=lambda *a:{'hash':HexBytes('0x'+'f'*64)})
    assert h.HybridReads(path).get(fixture['expected'],{'cutoff_block':row['block_number']},rpc) is None
    db.execute("UPDATE bundles SET digest='bad'");db.commit()
    assert h.HybridReads(path).get(fixture['expected'],{'cutoff_block':row['block_number']},None) is None


def test_canonical_rpc_error_is_not_empty_success(store,fixture):
    path,db=store;h.import_result(db,response(fixture['rows']),[fixture['expected']],'test')
    def fail(*a):raise RuntimeError('transport')
    with pytest.raises(RuntimeError):h.HybridReads(path).get(fixture['expected'],{'cutoff_block':99999999},SimpleNamespace(get_block=fail))


@pytest.mark.parametrize('mode',['valid','missing','ambiguous','transport'])
def test_consumer_fallback_and_persisted_prefix(mode,tmp_path,monkeypatch):
    db=sqlite3.connect(tmp_path/'campaign.sqlite3')
    db.execute('CREATE TABLE historical_transactions(wallet,tx_hash,payload,PRIMARY KEY(wallet,tx_hash))')
    db.execute("INSERT INTO historical_transactions VALUES('w','h',NULL)");db.commit();calls=[]
    def decode(self,w,tx,run,reads=None,rpc=None):
        calls.append('dune' if reads else 'rpc')
        if reads and mode=='ambiguous':return {'error':'ambiguous'}
        return {'trades':[]}
    monkeypatch.setattr(HistoricalCollector,'transaction',decode)
    def reads(*args):
        if mode=='transport':raise RuntimeError('transport')
        return None if mode=='missing' else ('tx','receipt','block')
    rpc=SimpleNamespace(w3=SimpleNamespace(provider=SimpleNamespace(historical_headers=nullcontext)))
    if mode=='transport':
        with pytest.raises(RuntimeError):decode_hashes(db,'w',['h'],{},rpc,hybrid=SimpleNamespace(get=reads))
        assert db.execute('SELECT payload FROM historical_transactions').fetchone()[0] is None
    else:
        decode_hashes(db,'w',['h'],{},rpc,hybrid=SimpleNamespace(get=reads))
        assert calls==(['dune'] if mode=='valid' else ['rpc'] if mode=='missing' else ['dune','rpc'])
        assert db.execute('SELECT payload FROM historical_transactions').fetchone()[0]
    db.close()


def test_sql_is_complete_raw_logs_not_winner_filter(fixture):
    sql=worker.query([fixture['expected']],'2026-09-18')
    assert 'arc.transactions' in sql and 'arc.logs' in sql and 'count(*)' in sql
    assert '2026-09-17' in sql and '2026-09-19' in sql
    assert 'topic0 =' not in sql and 'LIMIT' not in sql
    assert 'log_count<=128' in sql  # Large receipts fall back; wallets aren't filtered.
    with pytest.raises(ValueError):worker.query(['0x; DROP TABLE x'],'2026-09-18')
    with pytest.raises(ValueError):worker.query([fixture['expected']]*101,'2026-09-18')


def saved_page(folder,name,data):
    raw=json.dumps(data).encode();(folder/(name+'.body')).write_bytes(raw)
    (folder/(name+'.response.json')).write_text(json.dumps({'status':200,'error':None,'sha256':hashlib.sha256(raw).hexdigest()}))


def test_result_pagination_reuses_archived_pages_without_network(tmp_path,monkeypatch):
    e='E'*26
    first={'execution_id':e,'state':'QUERY_STATE_COMPLETED','next_offset':1,
           'result':{'rows':[{'n':1}],'metadata':{'total_row_count':2,'row_count':1}}}
    second=copy.deepcopy(first);second.pop('next_offset');second['result']['rows']=[{'n':2}]
    saved_page(tmp_path,'result',first);saved_page(tmp_path,'result_1',second)
    monkeypatch.setattr(worker,'capture',lambda *a,**kw:pytest.fail('network must not run'))
    result=worker.fetch_result(None,{},tmp_path,e)
    assert result['result']['rows']==[{'n':1},{'n':2}]
    assert result['result']['metadata']['total_row_count']==2


@pytest.mark.parametrize('change',['cycle','wrong_execution','changed_total','short_last'])
def test_bad_pagination_does_not_publish_partial_rows(tmp_path,change):
    e='E'*26
    first={'execution_id':e,'state':'QUERY_STATE_COMPLETED','next_offset':1,
           'result':{'rows':[{}],'metadata':{'total_row_count':2,'row_count':1}}}
    second=copy.deepcopy(first);second.pop('next_offset')
    if change=='cycle':first['next_offset']=0
    elif change=='wrong_execution':second['execution_id']='BAD'
    elif change=='changed_total':second['result']['metadata']['total_row_count']=3
    else:second['result']={'rows':[],'metadata':{'total_row_count':2,'row_count':0}}
    saved_page(tmp_path,'result',first);saved_page(tmp_path,'result_1',second)
    with pytest.raises(ValueError):worker.fetch_result(None,{},tmp_path,e)


def test_missing_large_logs_is_fallback_not_empty_receipt(fixture):
    r=fixture['rows'][0];r['log_count']=600
    bundles,rejected=h.parse_rows([r],[fixture['expected']])
    assert not bundles and rejected


def test_hybrid_priority_preserves_all_pending_hashes(store,fixture):
    path,db=store;h.import_result(db,response(fixture['rows']),[fixture['expected']],'test')
    hashes=['0x'+'f'*64,fixture['expected'],'0x'+'e'*64]
    result=h.HybridReads(path).prioritize(hashes)
    assert result==[fixture['expected'],hashes[0],hashes[2]]


def test_plan_only_missing_campaign_hashes_and_never_retries_paid_jobs(tmp_path):
    source=tmp_path/'campaign';source.mkdir();db=sqlite3.connect(source/'campaign.sqlite3')
    db.executescript('''CREATE TABLE historical_transactions(wallet,tx_hash,payload);
      CREATE TABLE campaign_queue(wallet,state,priority);CREATE TABLE historical_runs(wallet,version,phase);
      INSERT INTO campaign_queue VALUES('w','PENDING',0);INSERT INTO historical_runs VALUES('w',1,'DECODING');''')
    hashes=['0x'+str(i)*64 for i in range(1,5)]
    db.executemany('INSERT INTO historical_transactions VALUES(?,?,?)',[('w',x,None if i<3 else '{}') for i,x in enumerate(hashes)])
    db.commit();db.close()
    cache=h.connect(tmp_path/'cache.sqlite3');cache.executescript(h.SCHEMA+'CREATE TABLE hints(tx_hash PRIMARY KEY,day);')
    cache.executemany('INSERT INTO hints VALUES(?,?)',[(x,'2026-09-18') for x in hashes])
    cache.execute("INSERT INTO jobs(id,state,hashes) VALUES(1,'SUBMITTED',?)",(json.dumps([hashes[0]]),))
    cache.execute('INSERT INTO bundles VALUES(?,?,?,?,?)',(hashes[1],'{}','digest','fixture',0));cache.commit()
    before=(source/'campaign.sqlite3').read_bytes()
    assert worker.plan(cache,source)==([hashes[2]],'2026-09-18')
    assert (source/'campaign.sqlite3').read_bytes()==before
    cache.close()


def test_job_budget_survives_restart_and_no_network_after_cap(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();scope=b'{}';(source/'scope.json').write_bytes(scope)
    cfg={'context':'ctx','scope_sha256':hashlib.sha256(scope).hexdigest(),'campaign':str(source),
         'max_queries':1,'max_credits':50,'batch_size':100,'created_at':worker.time.time(),'max_seconds':100}
    (source/'settings.json').write_text(json.dumps(cfg));(tmp_path/'settings.json').write_text(json.dumps(cfg))
    (tmp_path/'backups').mkdir()
    with h.connect(tmp_path/'history.sqlite3') as db:
        db.executescript(h.SCHEMA);db.execute("INSERT INTO jobs(id,state,hashes,cost) VALUES(1,'COMPLETE','[]',1)")
    monkeypatch.setattr(worker,'capture',lambda *a,**kw:pytest.fail('must not request after persisted cap'))
    worker.run(tmp_path,'dummy')
    worker.run(tmp_path,'dummy')
