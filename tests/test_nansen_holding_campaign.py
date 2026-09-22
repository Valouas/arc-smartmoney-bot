import json
import hashlib
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
import requests

import nansen_holding_campaign as n
from copy_holding_campaign import prepare as prepare_local,connect as local_connect,advance
from historical_collector import HistoricalCollector
from test_copy_holding_campaign import prepared,rpc
from test_copy_holding_duration import W,tx
from test_nansen_transactions_probe import response


@pytest.fixture
def campaign(prepared):
    src,scope,local,cfg=prepared
    prepare_local(src,scope,local,cfg)
    with local_connect(local/'campaign.sqlite3') as db:
        db.execute("UPDATE historical_runs SET phase='FETCHING',cutoff_at=1790000000")
    side=local.parent/'nansen'
    n.prepare(local,side,2)
    return local,side,cfg


def row(db):return db.execute('SELECT * FROM scope').fetchone()


def page(last=True):
    return dict(pagination=dict(page=1,per_page=100,is_last_page=last),data=[dict(
        chain='arc',transaction_hash=tx(3,300)['tx_hash'],block_timestamp='2026-08-01T00:00:00',
        source_type='transfer',method='sent',tokens_sent=[],tokens_received=[])])


def test_prepare_scope_only_pending_fetching_and_no_local_mutation(campaign):
    local,side,cfg=campaign
    before=(local/'campaign.sqlite3').read_bytes()
    another=local.parent/'another';n.prepare(local,another,2000)
    assert before==(local/'campaign.sqlite3').read_bytes()
    with n.connect(another/'campaign.sqlite3') as db:
        assert db.execute('SELECT cap FROM policy').fetchone()[0]==2000
        assert len(db.execute('SELECT * FROM scope').fetchall())==1
    with pytest.raises(ValueError):n.prepare(local,local.parent/'bad',2001)
    with pytest.raises(FileExistsError):n.prepare(local,another,1)


def test_attempts_reserved_across_restart_and_no_refund(campaign):
    _,side,_=campaign
    with n.connect(side/'campaign.sqlite3') as db:
        assert n.reserve(db,row(db))==1
        with pytest.raises(ValueError,match='uncertain'):n.reserve(db,row(db))
        db.execute("UPDATE attempts SET state='RECEIVED'");db.commit()
    with n.connect(side/'campaign.sqlite3') as db:
        assert n.reserve(db,row(db))==2
        with pytest.raises(ValueError,match='budget'):n.reserve(db,row(db))


def test_fetch_archives_charged_raw_response_without_key(campaign):
    _,side,_=campaign;http=Mock();http.post.return_value=response(page())
    with n.connect(side/'campaign.sqlite3') as db:
        raw=n.fetch(db,side,row(db),'secret-key',http)
        assert json.loads(raw)==page()
        assert db.execute('SELECT attempts,used FROM policy').fetchone()[:]==(1,1)
        n.accept(db,row(db),raw)
        assert row(db)['state']=='PAGES_COMPLETE'
        assert db.execute('SELECT count(*) FROM seeds').fetchone()[0]==1
        assert 'secret-key' not in '\n'.join(p.read_text() for p in (side/'evidence').iterdir())
    assert http.post.call_args.kwargs['allow_redirects'] is False


def test_partial_stream_timeout_preserved_and_stops_billing(campaign):
    _,side,_=campaign
    def chunks():
        yield b'partial-paid-evidence'
        raise requests.Timeout('credential-must-not-be-logged')
    r=response();r.iter_content.side_effect=lambda _:chunks()
    http=Mock();http.post.return_value=r
    with n.connect(side/'campaign.sqlite3') as db:
        with pytest.raises(ValueError,match='transport'):n.fetch(db,side,row(db),'key',http)
        assert db.execute('SELECT body FROM attempts').fetchone()[0]==b'partial-paid-evidence'
        assert db.execute('SELECT attempts,used,halt FROM policy').fetchone()[:]==(1,1,'transport_uncertain')
        assert not db.execute('SELECT * FROM seeds').fetchall()
    assert (side/'evidence'/'response_0001.body').read_bytes()==b'partial-paid-evidence'


def test_explicit_transport_resume_keeps_uncertain_attempt_charged_to_cap(campaign):
    _,side,_=campaign;http=Mock();http.post.side_effect=requests.Timeout('secret')
    with n.connect(side/'campaign.sqlite3') as db:
        with pytest.raises(ValueError):n.fetch(db,side,row(db),'key',http)
        db.execute("UPDATE scope SET state='REVIEW_REQUIRED',reason='provider_or_schema_error'");db.commit()
        before=dict(db.execute('SELECT * FROM attempts').fetchone())
    n.resume_transport(side)
    with n.connect(side/'campaign.sqlite3') as db:
        assert dict(db.execute('SELECT * FROM attempts WHERE id=1').fetchone())==before
        assert db.execute('SELECT attempts,used,cap,halt FROM policy').fetchone()[:]==(1,0,2,None)
        assert row(db)['page']==1 and row(db)['state']=='PENDING'
        assert not n.unresolved_attempts(db)
        assert n.reserve(db,row(db))==2
        with pytest.raises(ValueError,match='budget'):n.reserve(db,row(db))


def test_transport_resume_refuses_pricing_halts(campaign):
    _,side,_=campaign
    with n.connect(side/'campaign.sqlite3') as db:
        db.execute("UPDATE policy SET halt='billing_unverified_or_changed'")
    with pytest.raises(ValueError,match='not_transport'):n.resume_transport(side)


@pytest.mark.parametrize('succeeds',[True,False])
def test_bounded_automatic_transport_retry_keeps_budget_and_partial_evidence(campaign,monkeypatch,succeeds):
    _,side,_=campaign
    with n.connect(side/'campaign.sqlite3') as db:db.execute('UPDATE policy SET cap=3')
    settings=json.loads((side/'settings.json').read_text());settings['cap']=3
    (side/'settings.json').write_text(json.dumps(settings))
    monkeypatch.setattr(n.time,'sleep',lambda _:None)
    calls=[]
    def post(*args,**kwargs):
        calls.append(kwargs)
        if not succeeds or len(calls)<3:raise requests.Timeout('secret')
        return response(page())
    monkeypatch.setattr(requests.Session,'post',post)
    n.run(side,'key')
    with n.connect(side/'campaign.sqlite3') as db:
        assert db.execute('SELECT attempts FROM policy').fetchone()[0]==3
        assert len(calls)==3
        assert row(db)['state']==('PAGES_COMPLETE' if succeeds else 'REVIEW_REQUIRED')
        assert db.execute('SELECT count(*) FROM seeds').fetchone()[0]==int(succeeds)
        assert db.execute("SELECT count(*) FROM attempts WHERE state='UNCERTAIN'").fetchone()[0]==(2 if succeeds else 3)
        assert bool(db.execute('SELECT halt FROM policy').fetchone()[0]) is not succeeds


@pytest.mark.parametrize('bad',['chain','pagination','duplicate'])
def test_bad_page_never_advances_or_invents_trades(campaign,bad):
    _,side,_=campaign;p=page()
    if bad=='chain':p['data'][0]['chain']='ethereum'
    elif bad=='pagination':p['pagination']['page']=2
    else:p['data']*=2
    with n.connect(side/'campaign.sqlite3') as db:
        with pytest.raises(ValueError):n.accept(db,row(db),json.dumps(p).encode())
        assert row(db)['page']==1 and row(db)['state']=='PENDING'
        assert db.execute('SELECT count(*) FROM seeds').fetchone()[0]==0


def test_changed_billing_stops_before_next_call(campaign):
    _,side,_=campaign;http=Mock();http.post.return_value=response(page(),used='2')
    with n.connect(side/'campaign.sqlite3') as db:
        with pytest.raises(ValueError,match='billing'):n.fetch(db,side,row(db),'key',http)
        with pytest.raises(ValueError,match='halt'):n.reserve(db,row(db))
        assert db.execute('SELECT used FROM policy').fetchone()[0]==2
    assert http.post.call_count==1


def test_replay_cached_page_without_network(campaign,monkeypatch):
    _,side,_=campaign;raw=json.dumps(page()).encode()
    with n.connect(side/'campaign.sqlite3') as db:
        db.execute('INSERT INTO pages VALUES(?,?,?,?,?)',(W,1,raw,hashlib.sha256(raw).hexdigest(),'previous_pilot'))
    monkeypatch.setattr(requests.Session,'post',lambda *a,**k:pytest.fail('Cached page must not spend credits'))
    n.run(side,'key')
    with n.connect(side/'campaign.sqlite3') as db:
        assert row(db)['state']=='PAGES_COMPLETE'
        assert db.execute('SELECT attempts,used FROM policy').fetchone()[:]==(0,0)


def test_crash_after_received_response_recovered_without_repayment(campaign,monkeypatch):
    _,side,_=campaign;raw=json.dumps(page()).encode()
    with n.connect(side/'campaign.sqlite3') as db:
        ident=n.reserve(db,row(db))
        db.execute('UPDATE attempts SET state=?,status=200,body=?,digest=?,billing=? WHERE id=?',
                   ('RECEIVED',raw,hashlib.sha256(raw).hexdigest(),json.dumps({'x-nansen-credits-cost':'1','x-nansen-credits-used':'1'}),ident))
        db.execute('UPDATE policy SET used=1')
    monkeypatch.setattr(requests.Session,'post',lambda *a,**k:pytest.fail('Already paid response'))
    n.run(side,'key')
    with n.connect(side/'campaign.sqlite3') as db:
        assert row(db)['state']=='PAGES_COMPLETE'
        assert db.execute('SELECT attempts,used FROM policy').fetchone()[:]==(1,1)


def test_skip_wallet_already_advanced_and_crash_does_not_reset_budget(campaign,monkeypatch):
    local,side,_=campaign
    monkeypatch.setattr(requests.Session,'post',lambda *a,**k:pytest.fail('No network'))
    with local_connect(local/'campaign.sqlite3') as db:db.execute("UPDATE historical_runs SET phase='DECODING'")
    n.run(side,'key')
    with n.connect(side/'campaign.sqlite3') as db:
        assert row(db)['state']=='SKIPPED_LOCAL_PROGRESS'
        db.execute("UPDATE scope SET state='PENDING'");db.commit()
        n.reserve(db,row(db))
    n.run(side,'key')
    with n.connect(side/'campaign.sqlite3') as db:
        assert db.execute('SELECT attempts,halt FROM policy').fetchone()[:]==(1,'uncertain_previous_attempt')


def test_early_rpc_decode_preserves_arc_cursor_authority_and_payload(campaign,monkeypatch):
    local,side,cfg=campaign
    with n.connect(side/'campaign.sqlite3') as db:
        n.accept(db,row(db),json.dumps(page()).encode());context=row(db)['context']
    calls=[]
    def decode(self,w,h,run,rpc=None):calls.append(h);return tx(3,300,'SELL')
    monkeypatch.setattr(HistoricalCollector,'transaction',decode)
    response_page=dict(items=[],page=dict(has_more=True,next='next',
        coverage=dict(indexed=True,from_block=0,to_block=99,last_contiguous_block=99,gaps=[],gap_blocks=0)))
    arc=SimpleNamespace(address_activity=lambda *a,**k:response_page)
    with local_connect(local/'campaign.sqlite3') as db:
        assert n.ready_wallet(db,side/'campaign.sqlite3',context,9999999999)['wallet']==W
        original=db.execute('SELECT payload FROM historical_transactions WHERE tx_hash=?',(tx(1,100)['tx_hash'],)).fetchone()[0]
        assert advance(db,W,rpc(),arc,seed_database=side/'campaign.sqlite3',context=context) is None
        assert db.execute('SELECT phase,cursor FROM historical_runs').fetchone()[:]==('FETCHING','next')
        assert calls==[tx(3,300)['tx_hash']]
        assert n.ready_wallet(db,side/'campaign.sqlite3',context,9999999999) is None
        assert db.execute('SELECT added,decoded_early FROM campaign_nansen_seeds').fetchone()[:]==(1,1)
        assert db.execute('SELECT payload FROM historical_transactions WHERE tx_hash=?',(tx(1,100)['tx_hash'],)).fetchone()[0]==original
        run=dict(db.execute('SELECT * FROM historical_runs').fetchone())
        assert n.read_seeds(side/'campaign.sqlite3',W,run,'wrong-context')==[]
        assert n.read_seeds(side/'campaign.sqlite3',W,dict(run,cutoff_hash='wrong'),context)==[]
