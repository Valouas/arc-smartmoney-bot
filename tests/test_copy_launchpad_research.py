from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from copy_launchpad_research import prove_buy, reviewed_code, LAUNCHPAD, BLOCK, TRADE
from copy_research_replay import receipt_info, replay_wallet


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/copy_launchpad_arc.json').read_text(encoding='utf-8'))


def setup(index=0):
    f=fixture();row=f['rows'][index];p=f['proof']
    codes={(LAUNCHPAD,BLOCK):{bytes.fromhex(p['code'][2:])}}
    info=receipt_info(row['wallet'],row['payload'],row['tx'],row['receipt'])
    return row,info,codes


@pytest.mark.parametrize('index',range(5))
def test_real_buys_reconcile_offline_and_preserve_sources(index,monkeypatch):
    import socket,requests
    def deny(*a,**kw):pytest.fail('network during research')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(requests.Session,'request',deny)
    r,info,codes=setup(index);original=deepcopy(r)
    proof=prove_buy(r['wallet'],r['payload'],r['tx'],r['receipt'],info,codes)
    assert proof and proof['engine_authority'] is False and proof['paper_selected'] is False
    assert int(proof['raw_usdc_amount'])==100000000
    h=r['payload']['tx_hash'];run=dict(version=1,phase='COMPLETE',error=None,cursor=None,cutoff_block=BLOCK,cutoff_at=r['payload']['timestamp']+500)
    # The classifier need not guess a swap from transfers; the reviewed proof
    # supplies the research BUY. With no sale it is not a validated wallet.
    result=replay_wallet(r['wallet'],run,[r['payload']],{h:(r['tx'],r['receipt'])},SimpleNamespace(),codes)
    assert not result['holding']['holding_duration_eligible']
    trade=result['records'][0]['trades'][0]
    assert trade['side']=='BUY' and trade['token_amount']==proof['raw_token_amount']
    assert result['changes'][0]['reason']=='EXACT_RECONCILED_SYNTHRA_LAUNCHPAD_BUY'
    assert r==original


@pytest.mark.parametrize('case',['block','missing_code','changed_code','conflicting_code','wrong_sender',
    'wrong_target','native_payment','different_method','trailing_input','expired','extra_log','missing_log',
    'empty_topics','changed_event','wrong_trader','wrong_amount','fee_mismatch','removed','failed','native_mismatch',
    'foreign_log','duplicate_index'])
def test_unsupported_or_inconsistent_evidence_stays_unknown(case):
    r,info,codes=setup();t,receipt,p=r['tx'],r['receipt'],r['payload']
    event=next(l for l in receipt['logs'] if l['topics'][0]==TRADE)
    if case=='block':p['block']+=1
    elif case=='missing_code':codes={}
    elif case=='changed_code':codes={(LAUNCHPAD,BLOCK):{b'wrong'}}
    elif case=='conflicting_code':codes[(LAUNCHPAD,BLOCK)].add(b'wrong')
    elif case=='wrong_sender':t['from']='0x'+'1'*40
    elif case=='wrong_target':t['to']='0x'+'1'*40
    elif case=='native_payment':t['value']='0x1'
    elif case=='different_method':t['input']='0xdeadbeef'+t['input'][10:]
    elif case=='trailing_input':t['input']+='00'
    elif case=='expired':t['input']=t['input'][:-64]+f'{1:064x}'
    elif case=='extra_log':receipt['logs'].append(deepcopy(receipt['logs'][0]))
    elif case=='missing_log':receipt['logs'].pop()
    elif case=='empty_topics':receipt['logs'][0]['topics']=[]
    elif case=='changed_event':event['topics'][0]='0x'+'0'*64
    elif case=='wrong_trader':event['topics'][2]='0x'+'0'*24+'1'*40
    elif case=='wrong_amount':event['data']=event['data'][:130]+f'{1:064x}'+event['data'][194:]
    elif case=='fee_mismatch':event['data']=event['data'][:194]+f'{1:064x}'+event['data'][258:]
    elif case=='removed':event['removed']=True
    elif case=='failed':info['success']=False
    elif case=='native_mismatch':receipt['logs'][0]['data']='0x'+f'{1:064x}'
    elif case=='foreign_log':event['transactionHash']='0x'+'0'*64
    elif case=='duplicate_index':event['logIndex']=receipt['logs'][0]['logIndex']
    assert prove_buy(r['wallet'],p,t,receipt,info,codes) is None


@pytest.mark.parametrize('case',[None,'block','code','scale','wrapper','address','status'])
def test_pinned_historical_deployment(case):
    p=fixture()['proof']
    if case=='block':p['block']+=1
    elif case=='code':p['code']='0x00'
    elif case=='scale':p['nativeScale()']='0x1'
    elif case=='wrapper':p['wrappedNative()']='0x1'
    elif case=='address':p['address']='0x'+'1'*40
    elif case=='status':p['status']='UNREVIEWED'
    assert reviewed_code(p) is (case is None)


def test_replay_still_rejects_snapshot_mismatch_and_unrelated_inventory():
    r,info,codes=setup();h=r['payload']['tx_hash']
    run=dict(version=1,phase='COMPLETE',error=None,cursor=None,cutoff_block=BLOCK,cutoff_at=r['payload']['timestamp']+500)
    r['tx']['blockHash']='0x'+'1'*64
    result=replay_wallet(r['wallet'],run,[r['payload']],{h:(r['tx'],r['receipt'])},SimpleNamespace(),codes)
    assert not result['holding']['holding_duration_eligible'] and not result['changes']
    assert result['records'][0].get('error')
