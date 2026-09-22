import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import copy_holding_campaign as c
from test_copy_holding_campaign import prepared
from test_copy_holding_duration import W

OTHER='0x'+'9'*40


def test_paid_wallet_scope_checked_before_reserving_any_attempt(prepared):
    src,scope,out,cfg=prepared;c.prepare(src,scope,out,cfg)
    settings=json.loads((out/'settings.json').read_text())
    settings['paid_rpc']={'reserved_cu_cap':1000,'wallets':[W]}
    with c.connect(out/'campaign.sqlite3') as db:
        budget=c.CampaignBudget(out,db,settings)
        for w in (None,OTHER):
            budget.wallet=w
            assert not budget.paid_available()
            with pytest.raises(c.PaidBudgetLimit):budget.take_paid('eth_call')
        assert budget.counts['rpc']==0
        budget.wallet=W;assert budget.paid_available();budget.take_paid('eth_call')
        assert budget.counts['rpc']==1
        again=c.CampaignBudget(out,db,settings);again.wallet=OTHER
        with pytest.raises(c.PaidBudgetLimit):again.take_paid('eth_call')
        assert again.counts['rpc']==1


@pytest.mark.parametrize('scope',[[],W,[123],['invalid']])
def test_malformed_scope_fails_closed(prepared,scope):
    src,source_scope,out,cfg=prepared;c.prepare(src,source_scope,out,cfg)
    settings=json.loads((out/'settings.json').read_text());settings['paid_rpc']={'wallets':scope}
    with c.connect(out/'campaign.sqlite3') as db:
        with pytest.raises(ValueError,match='invalid_paid_wallet_scope'):c.CampaignBudget(out,db,settings)


def test_scope_survives_budget_extension_without_reset(prepared):
    src,scope,out,cfg=prepared;c.prepare(src,scope,out,cfg)
    cfg.rpc_url='https://arc-mainnet.g.alchemy.com/v2/fixture'
    c.enable_paid(out,cfg,.1,scope)
    settings=json.loads((out/'settings.json').read_text())
    with c.connect(out/'campaign.sqlite3') as db:
        budget=c.CampaignBudget(out,db,settings);budget.wallet=W;budget.take_paid('eth_call')
    c.enable_paid(out,cfg,5)
    settings=json.loads((out/'settings.json').read_text())
    assert settings['paid_rpc']['wallets']==[W]
    with c.connect(out/'campaign.sqlite3') as db:
        assert db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget').fetchone()[:]==(1,100)


def test_priority_only_never_schedules_an_outside_wallet(prepared):
    src,scope,out,cfg=prepared;c.prepare(src,scope,out,cfg)
    with c.connect(out/'campaign.sqlite3') as db:
        db.execute('INSERT INTO campaign_queue(wallet,priority) VALUES(?,9)',(OTHER,))
        db.execute("UPDATE campaign_queue SET state='REVIEW_REQUIRED' WHERE wallet=?",(W,))
        assert c.next_pending(db,100,[W],True) is None
        assert c.next_pending(db,100,[W],False)['wallet']==OTHER


def test_paid_only_stops_without_public_fallback_when_budget_unavailable(prepared,monkeypatch):
    import web3
    src,scope,out,cfg=prepared;c.prepare(src,scope,out,cfg)
    cfg.rpc_cache_file=str(out/'cache.sqlite3');cfg.rpc_rate_limit_file=str(out/'rate.sqlite3')
    monkeypatch.setattr(c,'PilotCache',Mock());monkeypatch.setattr(c,'SharedRateLimiter',Mock())
    monkeypatch.setattr(c,'CampaignRPCProvider',lambda *a,**kw:SimpleNamespace(session=kw['session'],chain_cache_hits=0))
    monkeypatch.setattr(web3,'Web3',lambda provider:SimpleNamespace(provider=provider))
    monkeypatch.setattr(c,'ArcscanClient',Mock())
    advance=Mock(side_effect=AssertionError('Must not fall back'));monkeypatch.setattr(c,'advance',advance)
    c.run(out,cfg,scope,priority_only=True,paid_only=True,max_transactions=100)
    advance.assert_not_called()
    report=json.loads(next(out.glob('run_*.json')).read_text())
    assert report['stop']=='PAID_UNAVAILABLE' and report['alchemy_attempts_this_run']==0
    assert report['http_attempts_this_run']=={'rpc':0,'arcscan':0}
