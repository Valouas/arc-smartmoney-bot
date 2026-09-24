import pytest
from copy_pool_provenance import attest,address_word,FACTORY,ProbeBudget
from copy_holding_pilot import PilotLimit

POOL='0x'+'3'*40
ENTRY=dict(pool=POOL,block=123)
def word(a):return '0x'+a[2:].zfill(64)


class Provider:
    def __init__(self,values):self.values=iter(values);self.calls=[]
    def make_request(self,m,p):
        self.calls.append((m,p))
        assert p[-1]=='0x7b'
        assert m in {'eth_getCode','eth_call'}
        return dict(result=next(self.values))


def values():return [word(FACTORY),word('0x'+'1'*40),word('0x'+'2'*40),'0x'+hex(3000)[2:].zfill(64),word(POOL),'0x6001']


def test_membership_cross_checked_at_exact_block():
    p=Provider(values());r=attest(p,ENTRY)
    assert r['status']=='FACTORY_MEMBERSHIP_OBSERVED'
    assert p.calls[4][1][0]['to']==FACTORY and len(p.calls)==6
    assert 'paper_selected' not in r and 'engine_authority' not in r


def test_pool_cannot_self_certify():
    v=values();v[4]=word('0x'+'4'*40)
    p=Provider(v)
    assert attest(p,ENTRY)['status']=='FACTORY_MEMBERSHIP_MISMATCH'
    assert len(p.calls)==5


def test_different_factory_not_approved():
    p=Provider([word('0x'+'4'*40)])
    assert attest(p,ENTRY)['status']=='DIFFERENT_FACTORY'
    assert len(p.calls)==1


@pytest.mark.parametrize('v',['0x','0x'+'0'*64,'0x'+'f'*64,'0x'+'z'*64])
def test_invalid_address_response(v):
    with pytest.raises(ValueError):address_word(v)


def test_limit_before_any_reservation():
    b=object.__new__(ProbeBudget);b.probe_attempts=700
    with pytest.raises(PilotLimit):b.take_paid('eth_call')
    b.probe_attempts=0
    with pytest.raises(PilotLimit):b.take_paid('eth_getLogs')
