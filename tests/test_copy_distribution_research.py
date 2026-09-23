import json
from copy import deepcopy
from pathlib import Path
import pytest
from copy_distribution_research import reviewed_distribution,TOKEN,PAYER,DISTRIBUTOR
from copy_research_replay import receipt_info


def fixture():
    f=json.loads((Path(__file__).parent/'fixtures/copy_distribution_arc.json').read_text(encoding='utf-8'))
    tx,r=f['tx'],f['receipt']
    p=dict(tx_hash=tx['hash'],block=int(r['blockNumber'],16),index=int(r['transactionIndex'],16))
    f['info']=receipt_info(f['wallet'],p,tx,r)
    return f


def test_reviewed_recipient_is_not_the_swap_payer(monkeypatch):
    import socket,requests
    def denied(*a,**k):pytest.fail('offline distribution review attempted network')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(requests.Session,'request',denied)
    f=fixture();before=deepcopy(f)
    evidence=reviewed_distribution(f['wallet'],f['tx'],f['receipt'],f['info'],set(),set())
    assert evidence and evidence['recipient_count']==500
    assert evidence['engine_authority'] is False
    assert evidence['identity_independence_inferred'] is False
    assert f==before


def test_preserved_input_recipients_match_every_distribution_transfer():
    from eth_abi import decode
    from decoder import TRANSFER_TOPIC,hex_value,topic_to_address
    f=fixture();tx,r=f['tx'],f['receipt']
    cost,_,recipients=decode(['uint256','uint256','address[]'],bytes.fromhex(tx['input'][10:]))
    transfers=[l for l in r['logs'] if len(l.get('topics',[]))==3
               and hex_value(l['topics'][0])==hex_value(TRANSFER_TOPIC)]
    distributed=[l for l in transfers if l['address'].lower()==TOKEN
                 and topic_to_address(l['topics'][1]).lower()==DISTRIBUTOR]
    assert tuple(topic_to_address(l['topics'][2]).lower() for l in distributed)==recipients
    assert len(distributed)==500 and all(int(l['data'],16)==10**18 for l in distributed)
    payer_legs=[l for l in transfers if topic_to_address(l['topics'][1]).lower()==PAYER]
    assert len(payer_legs)==1 and int(payer_legs[0]['data'],16)==cost*10**12==int(tx['value'],16)
    assert topic_to_address(payer_legs[0]['topics'][2]).lower()==DISTRIBUTOR


@pytest.mark.parametrize('complete',[False,True])
def test_exact_review_can_explain_decoder_error_only_with_complete_history(monkeypatch,complete):
    import copy_research_replay as replay
    f=fixture();tx,r=f['tx'],f['receipt'];h=tx['hash']
    record=dict(tx_hash=h,block=int(r['blockNumber'],16),index=int(r['transactionIndex'],16),
                timestamp=1789576519,trades=[],error='unsupported_or_ambiguous_reconstruction')
    records=[record];before=deepcopy(records)
    def unsupported(*a,**kw):raise ValueError('unsupported swap')
    monkeypatch.setattr(replay,'classify_trade',unsupported)
    result=replay.replay_wallet(f['wallet'],dict(version=1,phase='COMPLETE' if complete else 'FETCHING',
        cutoff_block=record['block'],cutoff_at=record['timestamp']+60),records,{h:(tx,r)},None,{})
    assert records==before and not result['holding']['holding_duration_eligible']
    assert len(result['changes'])==int(complete)
    if complete:
        assert 'error' not in result['records'][0]
        assert result['records'][0]['trades']==[]
        assert result['changes'][0]['before']['error']==record['error']
    else:assert result['records'][0]['error']==record['error']


@pytest.mark.parametrize('case',['other_hash','other_block','other_payer','input','missing_log',
    'amount','cash','outflow','traded','disposed','wallet_is_payer','unknown_recipient','value','failed'])
def test_unknown_or_economically_used_credit_stays_ambiguous(case):
    f=fixture();tx,r,info=f['tx'],f['receipt'],f['info'];wallet=f['wallet'];disposed=set();traded=set()
    if case=='other_hash':tx['hash']='0x'+'1'*64
    elif case=='other_block':r['blockNumber']='0x1'
    elif case=='other_payer':tx['from']='0x'+'1'*40
    elif case=='input':tx['input']=tx['input'][:-2]+'ff'
    elif case=='missing_log':r['logs'].pop()
    elif case=='amount':info['deltas'][TOKEN]=10**18+1
    elif case=='cash':info['cash_touch']=True
    elif case=='outflow':info['deltas'][TOKEN]=-10**18
    elif case=='traded':traded.add(TOKEN)
    elif case=='disposed':disposed.add(TOKEN)
    elif case=='wallet_is_payer':wallet=PAYER
    elif case=='unknown_recipient':wallet='0x'+'1'*40
    elif case=='value':tx['value']='0x0'
    elif case=='failed':info['success']=False
    assert reviewed_distribution(wallet,tx,r,info,disposed,traded) is None
