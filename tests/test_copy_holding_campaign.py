from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from hexbytes import HexBytes
import pytest

from arcscan import ArcscanError
from copy_holding_campaign import CampaignBudget, advance, connect, prepare, status, QUEUE
from copy_holding_pilot import PilotLimit
from copy_holding_duration import stored_holding
from historical_collector import HistoricalCollector
from historical_snapshots import decoding_context
from historical_store import SCHEMA
from test_copy_holding_duration import W,tx


@pytest.fixture
def prepared(tmp_path):
    cfg=SimpleNamespace(chain_id=5042,trusted_dex_registry=str(tmp_path/'registry.json'))
    Path(cfg.trusted_dex_registry).write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    src=tmp_path/'source.sqlite3'
    with sqlite3.connect(src) as db:
        db.executescript(SCHEMA+'CREATE TABLE historical_refresh_context(wallet PRIMARY KEY,context,reuse);')
        db.execute('INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase) VALUES(?,1,1,99,10000,?,?)',
            (W,'0x'+'a'*64,'DECODING'))
        db.execute('INSERT INTO historical_refresh_context VALUES(?,?,1)',(W,decoding_context(cfg)))
        db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(W,tx(1,100)['tx_hash'],json.dumps(tx(1,100))))
        db.execute('INSERT INTO historical_transactions VALUES(?,?,NULL)',(W,tx(2,220)['tx_hash']))
    scope=tmp_path/'scope.json';scope.write_text(json.dumps(dict(schema_version=1,purpose='copy_research_shortlist',wallets=[W])))
    return src,scope,tmp_path/'campaign',cfg


def rpc():
    return SimpleNamespace(get_block=lambda *a:{'hash':HexBytes('0x'+'a'*64)},
        w3=SimpleNamespace(provider=SimpleNamespace(historical_headers=nullcontext)))


def test_prepare_readonly_exact_scope_and_initial_backup(prepared):
    src,scope,out,cfg=prepared;before=src.read_bytes();prepare(src,scope,out,cfg)
    assert src.read_bytes()==before
    assert len(list((out/'backups').glob('initial_*.sqlite3')))==1
    with connect(out/'campaign.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM campaign_queue').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM historical_transactions').fetchone()[0]==2
    assert (out/'scope.json').read_bytes()==scope.read_bytes()
    with pytest.raises(FileExistsError):prepare(src,scope,out,cfg)


@pytest.mark.parametrize('invalid', ['version','context'])
def test_invalidated_or_unknown_decoder_never_revalidated_implicitly(prepared,invalid):
    src,scope,out,cfg=prepared
    with sqlite3.connect(src) as db:
        db.execute('UPDATE historical_runs SET version=0' if invalid=='version' else "UPDATE historical_refresh_context SET context='different'")
    prepare(src,scope,out,cfg)
    with connect(out/'campaign.sqlite3') as db:
        assert db.execute('SELECT state FROM campaign_queue').fetchone()[0]=='REVIEW_REQUIRED'
        assert db.execute('SELECT count(*) FROM historical_transactions').fetchone()[0]==2


def test_budget_persists_attempts_and_deadline_after_restart(prepared):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg,rpc_cap=2)
    settings=json.loads((out/'settings.json').read_text())
    with connect(out/'campaign.sqlite3') as db:
        first=CampaignBudget(out,db,settings);first.take('rpc','eth_chainId')
        second=CampaignBudget(out,db,settings);second.take('rpc','eth_chainId')
        with pytest.raises(PilotLimit):second.take('rpc','eth_chainId')
        assert db.execute("SELECT attempts FROM campaign_budget WHERE service='rpc'").fetchone()[0]==2
        expired=CampaignBudget(out,db,dict(settings,created_at=0))
        with pytest.raises(PilotLimit):expired.take('arcscan','address_activity')


def test_missing_only_decode_no_duplicate_on_resume(prepared,monkeypatch):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg);before=src.read_bytes();calls=[]
    def decode(self,w,h,run,rpc=None):calls.append(h);return tx(2,220,'SELL')
    monkeypatch.setattr(HistoricalCollector,'transaction',decode)
    with connect(out/'campaign.sqlite3') as db:
        r=advance(db,W,rpc(),None,batch=1)
        assert r['holding_duration_eligible'] and r['median_first_sale_seconds']==120
        assert advance(db,W,rpc(),None)['holding_duration_eligible']
        assert calls==[tx(2,220)['tx_hash']]
        assert db.execute('SELECT count(*) FROM historical_transactions').fetchone()[0]==2
    assert src.read_bytes()==before


def test_failed_transaction_preserves_successful_prefix_and_never_completes(prepared,monkeypatch):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    calls=[]
    def decode(self,w,h,run,rpc=None):
        calls.append(h)
        if h==tx(3,300)['tx_hash']:raise PilotLimit()
        return tx(2,220,'SELL')
    monkeypatch.setattr(HistoricalCollector,'transaction',decode)
    with connect(out/'campaign.sqlite3') as db:
        db.execute('INSERT INTO historical_transactions VALUES(?,?,NULL)',(W,tx(3,300)['tx_hash']));db.commit()
        with pytest.raises(PilotLimit):advance(db,W,rpc(),None)
        assert db.execute('SELECT payload FROM historical_transactions WHERE tx_hash=?',(tx(2,220)['tx_hash'],)).fetchone()[0]
        assert db.execute('SELECT payload FROM historical_transactions WHERE tx_hash=?',(tx(3,300)['tx_hash'],)).fetchone()[0] is None
        assert not stored_holding(db,W)['holding_duration_eligible']


def test_bad_page_keeps_cursor_and_existing_transactions(prepared):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    with connect(out/'campaign.sqlite3') as db:
        db.execute("UPDATE historical_runs SET phase='FETCHING',cursor='saved',seen='[\"saved\"]'");db.commit()
        arc=SimpleNamespace(address_activity=lambda *a,**k:{'items':[],'page':{'has_more':False}})
        with pytest.raises(ArcscanError):advance(db,W,rpc(),arc)
        assert db.execute('SELECT cursor FROM historical_runs').fetchone()[0]=='saved'
        assert db.execute('SELECT count(*) FROM historical_transactions').fetchone()[0]==2


def test_valid_page_advances_only_after_validation(prepared):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    with connect(out/'campaign.sqlite3') as db:
        db.execute("UPDATE historical_runs SET phase='FETCHING',cursor='saved',seen='[\"saved\"]'");db.commit()
        page=dict(items=[{'tx_hash':tx(3,300)['tx_hash']}],page=dict(has_more=False,next=None,
            coverage=dict(indexed=True,from_block=0,to_block=99,last_contiguous_block=99,gaps=[],gap_blocks=0)))
        assert advance(db,W,rpc(),SimpleNamespace(address_activity=lambda *a,**k:page)) is None
        assert db.execute('SELECT phase,cursor FROM historical_runs').fetchone()[:]==('DECODING',None)
        assert db.execute('SELECT count(*) FROM historical_transactions').fetchone()[0]==3


def test_ambiguous_payload_preserved_without_refetch_or_authority(prepared,monkeypatch):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    monkeypatch.setattr(HistoricalCollector,'transaction',lambda *a,**k:pytest.fail('No repeated decoding of identical ambiguous evidence'))
    with connect(out/'campaign.sqlite3') as db:
        payload=dict(tx(2,220,'SELL'),error='unsupported_or_ambiguous_reconstruction')
        db.execute('UPDATE historical_transactions SET payload=? WHERE payload IS NULL',(json.dumps(payload),));db.commit()
        r=advance(db,W,rpc(),None)
        assert not r['holding_duration_eligible']
        assert r['holding_duration_reason']=='INCOMPLETE_OR_AMBIGUOUS_HISTORY'
        s=status(db,out)
        assert all(not x['paper_selected'] and not x['live_signal_authority'] for x in s['rows'])


def test_final_anchor_mismatch_cannot_publish_complete(prepared,monkeypatch):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    monkeypatch.setattr(HistoricalCollector,'transaction',lambda *a,**k:tx(2,220,'SELL'))
    r=rpc();hashes=iter(['a','b']);r.get_block=lambda *a:{'hash':HexBytes('0x'+next(hashes)*64)}
    with connect(out/'campaign.sqlite3') as db:
        with pytest.raises(ValueError,match='anchor_mismatch'):advance(db,W,r,None)
        assert not stored_holding(db,W)['holding_duration_eligible']


def test_infrastructure_scope_rejected_before_creating_output(prepared):
    src,scope,out,cfg=prepared
    Path(cfg.trusted_dex_registry).write_text(json.dumps(dict(chain_id=5042,emitters=[dict(address=W)])))
    with pytest.raises(ValueError,match='infrastructure'):prepare(src,scope,out,cfg)
    assert not out.exists()


def test_success_clears_consecutive_failures_without_promoting_partial_history(prepared):
    from copy_holding_campaign import record_success
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    with connect(out/'campaign.sqlite3') as db:
        db.execute("UPDATE campaign_queue SET failures=2,next_attempt=500,reason='timeout'");db.commit()
        before=db.execute('SELECT phase,cursor FROM historical_runs').fetchone()[:]
        record_success(db,W,None)
        assert db.execute('SELECT state,failures,next_attempt,reason FROM campaign_queue').fetchone()[:]==('PENDING',0,0,None)
        assert db.execute('SELECT phase,cursor FROM historical_runs').fetchone()[:]==before
        assert not stored_holding(db,W)['holding_duration_eligible']
