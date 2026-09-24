import json
import hashlib
from unittest.mock import Mock

import pytest
import requests

from nansen_transactions_probe import Probe,validate_page,timestamp,run

W='0x'+'1'*40
DATE={'from':'2026-07-15T00:00:00Z','to':'2026-09-20T00:00:00Z'}


def payload():
    return dict(pagination=dict(page=1,per_page=100,is_last_page=True),data=[dict(
        chain='arc',transaction_hash='0x'+'a'*64,block_timestamp='2026-08-01T00:00:00Z',
        method='sent',source_type='external',tokens_sent=[],tokens_received=[])])


def response(data=None,used='1'):
    r=Mock(status_code=200,headers={'X-Nansen-Credits-Cost':'1','X-Nansen-Credits-Used':used})
    r.iter_content.return_value=[json.dumps(data or payload()).encode()]
    r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
    return r


def test_valid_page_has_timing_but_never_trade_authority():
    rows,last=validate_page(payload(),1,timestamp(DATE['from']),timestamp(DATE['to']))
    assert last and rows[0]['timestamp']==timestamp('2026-08-01T00:00:00Z')
    assert 'confirmation_eligible' not in rows[0] and 'side' not in rows[0]


@pytest.mark.parametrize('bad',['chain','timestamp','pagination','outside'])
def test_bad_provider_evidence_refused(bad):
    p=payload()
    if bad=='chain':p['data'][0]['chain']='ethereum'
    elif bad=='timestamp':p['data'][0]['block_timestamp']='2026-08-01 00:00:00'
    elif bad=='pagination':p['pagination']['page']=2
    else:p['data'][0]['block_timestamp']='2025-01-01T00:00:00Z'
    with pytest.raises(ValueError):validate_page(p,1,timestamp(DATE['from']),timestamp(DATE['to']))


def test_raw_response_archived_even_when_validation_fails(tmp_path,monkeypatch):
    monkeypatch.setattr('nansen_transactions_probe.time.sleep',lambda _:None)
    h=Mock();p=payload();p['data'][0]['chain']='ethereum';h.post.return_value=response(p)
    probe=Probe('private-key',tmp_path,session=h)
    with pytest.raises(ValueError,match='chain'):probe.page(W,DATE,1)
    assert probe.attempts==1 and probe.credits==1
    assert (tmp_path/'response_01.body').exists()
    assert not any('private-key' in p.read_text() for p in tmp_path.iterdir())
    kwargs=h.post.call_args.kwargs
    assert not kwargs['allow_redirects'] and kwargs['json']['hide_spam_token'] is False


def test_request_cap_and_billing_change_stop(tmp_path,monkeypatch):
    monkeypatch.setattr('nansen_transactions_probe.time.sleep',lambda _:None)
    h=Mock();h.post.return_value=response(used='2')
    p=Probe('key',tmp_path,session=h)
    with pytest.raises(ValueError,match='billing'):p.page(W,DATE,1)
    assert p.credits==2 and not p.billing_verified
    p.attempts=10
    with pytest.raises(ValueError,match='limit'):p.page(W,DATE,1)
    assert h.post.call_count==1


def test_timeout_is_uncertain_billing_and_attempt_not_refunded(tmp_path,monkeypatch):
    monkeypatch.setattr('nansen_transactions_probe.time.sleep',lambda _:None)
    h=Mock();h.post.side_effect=requests.Timeout('secret')
    p=Probe('key',tmp_path,session=h)
    with pytest.raises(ValueError,match='uncertain'):p.page(W,DATE,1)
    assert p.attempts==1 and not p.billing_verified
    assert (tmp_path/'attempt_01.json').exists()


def test_no_mass_scope_or_reuse_of_output_directory(tmp_path):
    scope=tmp_path/'scope.json';scope.write_text(json.dumps([dict(wallet='0x'+str(i)*40,date=DATE) for i in range(4)]))
    with pytest.raises(ValueError,match='three'):run(scope,tmp_path/'missing.sqlite3',tmp_path/'out','key')
    assert not (tmp_path/'out').exists()
    scope.write_text(json.dumps([dict(wallet=W,date=DATE)]));(tmp_path/'out').mkdir()
    with pytest.raises(FileExistsError):run(scope,tmp_path/'missing.sqlite3',tmp_path/'out','key')


def test_naive_dates_retained_only_as_unverified_observations():
    p=payload();p['data'][0]['block_timestamp']='2026-08-01T00:00:00'
    rows,_=validate_page(p,1,timestamp(DATE['from']),timestamp(DATE['to']),True)
    assert rows[0]['timestamp'] is None
    assert rows[0]['timestamp_timezone_verified'] is False
    p['data'][0]['block_timestamp']='not-a-date'
    with pytest.raises(ValueError):validate_page(p,1,0,9999999999,True)


def test_prior_response_reused_without_spending_and_budget_carried(tmp_path,monkeypatch):
    monkeypatch.setattr('nansen_transactions_probe.time.sleep',lambda _:None)
    prior=tmp_path/'prior';prior.mkdir()
    h=Mock();h.post.return_value=response()
    first=Probe('key',prior,session=h);first.page(W,DATE,1)
    (prior/'summary.json').write_text(json.dumps(dict(attempts=1,credits_used_observed=1,billing_verified=True)))
    (prior/'manifest.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in prior.iterdir()}))
    out=tmp_path/'out';out.mkdir()
    network=Mock();network.post.side_effect=AssertionError('Network forbidden for cached response')
    resumed=Probe('key',out,session=network,prior=prior)
    rows,last=resumed.page(W,DATE,1)
    assert len(rows)==1 and last and resumed.attempts==1 and resumed.credits==1
    network.post.assert_not_called()
    resumed.attempts=10
    with pytest.raises(ValueError,match='limit'):resumed.page(W,DATE,2)
    (prior/'response_01.body').write_text('{}')
    another=tmp_path/'another';another.mkdir()
    with pytest.raises(ValueError,match='integrity'):Probe('key',another,prior=prior)
