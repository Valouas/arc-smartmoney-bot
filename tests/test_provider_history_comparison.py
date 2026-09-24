import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import requests

import provider_history_comparison as p
from nansen_holding_campaign import SCHEMA

W='0x'+'1'*40
T='0x'+'2'*40
H='0x'+'a'*64


def example():
    local={'wallet':W,'transactions':[{'tx_hash':H,'timestamp':1789552800,'trades':[
        {'side':'BUY','token':T,'token_amount':20,'usdc_amount':10,'confirmation_eligible':True}]}]}
    row={'chain':'arc','trader_address':W,'transaction_hash':H,'block_timestamp':'2026-09-16T10:00:00Z',
         'token_bought_address':T,'token_sold_address':p.USDC,'token_bought_amount':20,'token_sold_amount':10}
    data={'pagination':{'page':1,'per_page':1000,'is_last_page':True},'data':[row]}
    return local,data


def test_matched_trade_does_not_grant_authority():
    local,data=example();r=p.compare_wallet(local,data)
    assert r['amount_matches']==r['hash_direction_token_matches']==1
    assert not r['history_completeness_authority'] and not r['paper_selected']


@pytest.mark.parametrize('field,value',[('token_bought_address','0x'+'3'*40),('token_bought_amount',21),
                                      ('token_sold_address','0x'+'4'*40),('token_sold_amount',11)])
def test_direction_and_both_token_amounts_required(field,value):
    local,data=example();data['data'][0][field]=value
    assert p.compare_wallet(local,data)['amount_matches']==0


def test_duplicate_and_partial_pages_are_not_complete():
    local,data=example();data['pagination']['is_last_page']=False
    assert not p.compare_wallet(local,data)['provider_page_complete']
    data['pagination']['is_last_page']=True;data['data']*=2
    result=p.compare_wallet(local,data)
    assert result['duplicate_rows']==1 and not result['provider_page_complete']


@pytest.mark.parametrize('field,value',[('chain','ethereum'),('trader_address','0x'+'9'*40),('transaction_hash','bad')])
def test_malformed_or_wrong_scope_rejected(field,value):
    local,data=example();data['data'][0][field]=value
    with pytest.raises(ValueError):p.compare_wallet(local,data)


def test_naive_timestamp_is_marked_unverified():
    local,data=example();data['data'][0]['block_timestamp']='2026-09-16T10:00:00'
    assert not p.compare_wallet(local,data)['comparisons'][0]['timestamps'][0]['timezone_explicit']


def test_shared_reservation_is_persistent_and_blocks_other_reservations(tmp_path):
    path=tmp_path/'b.sqlite3'
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    db.execute('INSERT INTO policy VALUES(1,2000,1999,1999,NULL)');db.commit()
    assert p.reserve_research(db,{'body':{}})==2000
    db.close();db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    assert db.execute('SELECT attempts FROM policy').fetchone()[0]==2000
    with pytest.raises(ValueError):p.reserve_research(db,{})
    db.execute('UPDATE policy SET halt=NULL');db.commit()
    with pytest.raises(ValueError):p.reserve_research(db,{})
    assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
    db.close()


def test_capture_retains_uncertain_attempt_no_retry(tmp_path):
    class Fail:
        calls=0
        def request(self,*a,**kw):
            self.calls+=1;raise requests.Timeout('secret must not be stored')
    s=Fail();raw,meta=p.capture(s,'POST','https://invalid',{},tmp_path,'one',{})
    assert s.calls==1 and meta['error']=='transport_or_payload_uncertain'
    assert 'secret' not in (tmp_path/'one.response.json').read_text()
    with pytest.raises(FileExistsError):p.capture(s,'POST','https://invalid',{},tmp_path,'one',{})
    assert s.calls==1


@pytest.mark.parametrize('total,next_offset,state',[(2,None,'QUERY_STATE_COMPLETED'),(1,100,'QUERY_STATE_COMPLETED'),(1,None,'QUERY_STATE_EXECUTING')])
def test_dune_partial_rejected(total,next_offset,state):
    with pytest.raises(ValueError):p.dune_rows({'state':state,'next_offset':next_offset,'result':{'rows':[{}],'metadata':{'total_row_count':total}}})


def test_sql_rejects_hash_injection():
    with pytest.raises(ValueError):p.dune_sql({'wallets':[{'transactions':[{'tx_hash':"0x');DROP TABLE x;",'timestamp':1}]}]})


def test_report_is_offline(tmp_path,monkeypatch):
    def fail(*a,**kw):raise AssertionError('network forbidden')
    monkeypatch.setattr(requests.sessions.Session,'request',fail)
    local,data=example()
    p.write_new(tmp_path/'reference.json',{'wallets':[local],'rpc_receipts':{}})
    raw=json.dumps(data).encode();(tmp_path/('nansen_'+W+'.body')).write_bytes(raw)
    p.write_new(tmp_path/('nansen_'+W+'.response.json'),{'status':200,'error':None,'sha256':hashlib.sha256(raw).hexdigest()})
    result=p.report(tmp_path)
    assert result['wallets'][0]['amount_matches']==1
    assert not result['production_modified']


def test_receipt_transfer_match_checks_raw_value_and_log_index():
    topic=lambda w:'0x'+'0'*24+w[2:]
    ref={'wallets':[{'transactions':[{'tx_hash':H,'block':1,'index':0,'timestamp':0}]}],
         'rpc_receipts':{H:{'logs':[{'address':T,'logIndex':'0x4','topics':[p.TRANSFER,topic(W),topic(T)],'data':hex(20)}]}}}
    row={'kind':'transfer','tx_hash':H,'log_index':4,'emitter':T,'sender':W,'recipient':T,'raw_value':'20',
         'block_number':1,'tx_index':0,'block_time':'1970-01-01 00:00:00 UTC'}
    assert p.compare_dune(ref,[row])['exact_matches']==1
    assert p.compare_dune(ref,[row])['timing_mismatch_hashes']==[]
    row['raw_value']='21'
    assert p.compare_dune(ref,[row])['exact_matches']==0
    row['raw_value']='20';row['log_index']=5
    assert p.compare_dune(ref,[row])['exact_matches']==0
    row['block_number']=2
    assert p.compare_dune(ref,[row])['timing_mismatch_hashes']==[H]


def test_known_native_usdc_representation_is_compared():
    local,data=example();data['data'][0]['token_sold_address']='0xfffffffffffffffffffffffffffffffffffffffe'
    assert p.compare_wallet(local,data)['amount_matches']==1


def test_unknown_extra_trade_does_not_fill_local_ambiguity():
    local,data=example();row=dict(data['data'][0],transaction_hash='0x'+'b'*64)
    data['data'].append(row)
    local['transactions'].append({'tx_hash':row['transaction_hash'],'error':'unsupported_or_ambiguous_reconstruction','trades':[]})
    r=p.compare_wallet(local,data)
    assert r['local_confirmed_trades']==1 and r['nansen_extra_hashes']==[row['transaction_hash']]
    assert local['transactions'][-1]['error']=='unsupported_or_ambiguous_reconstruction'
    assert not r['history_completeness_authority']


def test_live_arc_native_usdc_fixture():
    f=json.loads((Path(__file__).parent/'fixtures/provider_history_arc_live.json').read_text())
    r=p.compare_wallet({'wallet':f['wallet'],'transactions':[f['local_transaction']]},
        {'pagination':{'page':1,'per_page':1000,'is_last_page':True},'data':[f['nansen_row']]})
    assert r['amount_matches']==1
    assert r['comparisons'][0]['timestamps']==[{'offset_seconds':0,'timezone_explicit':True}]
