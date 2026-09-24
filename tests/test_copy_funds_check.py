import pytest
from decimal import Decimal
from copy_funds_check import check_funds, uint256, priced_token, token_valuator, USDC

W='0x'+'1'*40
W2='0x'+'2'*40


class RPC:
    def __init__(self, balances, chain='0x13b2', changed=False):
        self.balances=iter(balances);self.chain=chain;self.changed=changed;self.calls=[]
    def make_request(self,method,params):
        self.calls.append((method,params))
        if method=='eth_chainId':result=self.chain
        elif method=='eth_getBlockByNumber':
            result={'number':'0x100','hash':'0x'+'a'*64,'timestamp':hex(1000)}
            if params[0]!='latest' and self.changed:result['hash']='0x'+'b'*64
        elif params[0]['data']=='0x313ce567':result='0x'+f'{6:064x}'
        else:result=next(self.balances)
        return {'jsonrpc':'2.0','result':result}


def candidates():
    return [{'wallet':w,'copy_research_eligible':True} for w in (W,W2)]


def test_positive_balance_exact_units_and_bounded_target():
    rpc=RPC(['0x'+f'{1234567:064x}'])
    evidence=[]
    r=check_funds(candidates(),rpc,evidence.append,target=1,maximum=2,clock=lambda:1010,minimum_usd=1)
    assert r['checked']==r['qualifying_wallets']==1
    assert r['observations'][0]['liquid_usdc']=='1.234567'
    assert r['observations'][0]['paper_selected'] is False
    assert any(e['kind']=='rpc_evidence' for e in evidence)
    assert rpc.calls[-2][1][1]=='0x100'


def test_zero_and_malformed_never_invent_funds_or_empty_portfolio():
    r=check_funds(candidates(),RPC(['0x'+'0'*64,'0x']),lambda _:None,target=2,maximum=2,clock=lambda:1010)
    assert r['qualifying_wallets']==0
    assert r['observations'][0]['funds_status']=='BELOW_MINIMUM_OBSERVED_PORTFOLIO_INCOMPLETE'
    assert r['observations'][1]['liquid_usdc'] is None


def test_wrong_chain_refused_before_balances():
    rpc=RPC([],chain='0x1')
    with pytest.raises(ValueError,match='wrong_chain'):
        check_funds(candidates(),rpc,lambda _:None,clock=lambda:1010)
    assert len(rpc.calls)==1


def test_changed_block_not_confirmed():
    with pytest.raises(ValueError,match='block_changed'):
        check_funds(candidates(),RPC(['0x'+f'{10000000:064x}'],changed=True),lambda _:None,target=1,maximum=2,clock=lambda:1010)


@pytest.mark.parametrize('value',[None,'0x','0x1','0x'+'g'*64,-1])
def test_bad_uint256(value):
    with pytest.raises(ValueError):uint256(value)


def test_stale_balance_check_not_allowed():
    with pytest.raises(ValueError,match='stale_or_future_block'):
        check_funds(candidates(),RPC([]),lambda _:None,clock=lambda:1500)


def test_crypto_counts_toward_ten_dollars_and_no_authority():
    r=check_funds(candidates(),RPC(['0x'+f'{2000000:064x}']),lambda _:None,target=1,maximum=2,
                  clock=lambda:1010,token_value=lambda *args:(Decimal(8),[{'token':W2,'value_usd':'8'}]))
    assert r['qualifying_wallets']==1
    assert r['observations'][0]['observed_value_usd']=='10'
    assert not r['observations'][0]['paper_selected']


def test_dust_usdc_not_enough():
    r=check_funds(candidates(),RPC(['0x'+f'{1:064x}']*2),lambda _:None,target=1,maximum=2,clock=lambda:1010)
    assert r['qualifying_wallets']==0 and r['checked']==2


def test_price_must_match_chain_base_and_have_liquidity():
    valid={'chainId':'arc','baseToken':{'address':W},'priceUsd':'2','liquidity':{'usd':100}}
    assert priced_token([valid],W)[1]==2
    assert priced_token([dict(valid,chainId='ethereum')],W) is None
    assert priced_token([valid],W2) is None
    assert priced_token([dict(valid,liquidity={'usd':0})],W) is None
    assert priced_token([dict(valid,priceUsd='NaN')],W) is None


def test_usdc_and_mirror_never_double_counted():
    def forbidden(*args):raise AssertionError('Duplicate USDC lookup')
    value=token_valuator(forbidden,None)
    total,details=value({'wallet':W,'known_tokens':[USDC,'0xfffffffffffffffffffffffffffffffffffe']},forbidden,'0x100',Decimal(10))
    assert total==0 and not details


def test_live_token_amount_times_price_is_used_not_historical_holding(monkeypatch):
    monkeypatch.setattr('copy_funds_check.time.sleep',lambda _:None)
    from unittest.mock import Mock, MagicMock
    session=Mock()
    response=MagicMock()
    response.__enter__.return_value=response
    response.json.return_value=[{'chainId':'arc','baseToken':{'address':W2},
        'priceUsd':'3','liquidity':{'usd':10000},'pairAddress':W}]
    session.get.return_value=response
    requests=[];evidence=[]
    def request(method,params):
        requests.append((method,params))
        return '0x'+f'{(4000000 if params[0]["data"].startswith("0x70a08231") else 6):064x}'
    value=token_valuator(evidence.append,session)
    total,details=value({'wallet':W,'known_tokens':[W2,W2]},request,'0x100',Decimal(10))
    assert total==12 and len(details)==1 and details[0]['amount']=='4'
    assert all(p[1]=='0x100' for _,p in requests)
    assert evidence[0]['kind']=='price_evidence'
    session.get.assert_called_once()


def test_missing_quote_never_counts_as_funds(monkeypatch):
    monkeypatch.setattr('copy_funds_check.time.sleep',lambda _:None)
    from unittest.mock import Mock, MagicMock
    session=Mock();response=MagicMock();response.__enter__.return_value=response
    response.json.return_value=[];session.get.return_value=response
    def request(method,params):return '0x'+f'{6:064x}'
    total,details=token_valuator(lambda _:None,session)({'wallet':W,'known_tokens':[W2]},request,'0x100',Decimal(10))
    assert total==0 and details[0]['status']=='UNKNOWN'
