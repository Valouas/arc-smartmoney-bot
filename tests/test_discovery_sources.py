from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from auto_discovery import AutoDiscovery
from arcscan import ArcscanError
from discovery_sources import observed_sources
from historical_priority import PriorityCollector
from historical_metrics import analyze
from test_historical_analysis import cfg, store, fake_collector, trade, W, W2, T, H, NOW


def quote(store, token=T, peak=10, base=1, base_liq=10000, peak_liq=10000,
          pair=W, observed_at=200):
    with store.db:
        store.db.execute('''INSERT OR REPLACE INTO launches(token,discovered_at,pair_created_at,
            pair_address,first_price_usd,first_liquidity_usd) VALUES(?,100,90,?,?,?)''',
            (token,pair,base,base_liq))
        store.db.execute('''INSERT OR REPLACE INTO launch_snapshots(token,horizon_minutes,target_at,
            observed_at,delay_seconds,price_usd,liquidity_usd) VALUES(?,5,200,?,0,?,?)''',
            (token,observed_at,peak,peak_liq))


def discovery(store, cfg):
    d=AutoDiscovery.__new__(AutoDiscovery)
    d.cfg=cfg; d.db=store
    d.early_buyers=Mock(return_value=[(W,1,H)])
    return d


@pytest.mark.parametrize('multiple,tier',[(1,None),(4.999999,None),(5,1),(9.999999,1),
                                         (10,2),(12,2),(50,2),(100,2)])
def test_multiples_are_inclusive_floors_not_exact_matches(store,cfg,multiple,tier):
    quote(store,peak=multiple)
    sources=observed_sources(store.db,cfg)
    if tier is None: assert T not in sources
    else:
        assert sources[T]['tier']==tier
        assert sources[T]['multiple']==Decimal(str(multiple))


def test_ratio_is_measured_against_initial_quote_not_absolute_price(store,cfg):
    quote(store,base=2.5,peak=12.5)
    assert observed_sources(store.db,cfg)[T]['multiple']==5
    d=discovery(store,cfg); d.detect_winners()
    assert store.db.execute('SELECT max_return_pct FROM discovery_winners').fetchone()[0]==400


@pytest.mark.parametrize('changes',[
    {'base':None},{'base':0},{'base':-1},{'base':float('inf')},
    {'peak':None},{'peak':0},{'peak':float('inf')},
    {'base_liq':None},{'base_liq':999},{'peak_liq':999},{'peak_liq':float('nan')},
    {'pair':None},{'observed_at':99},{'base':1e-300,'peak':1e300},
])
def test_missing_malformed_or_illiquid_observations_never_qualify(store,cfg,changes):
    quote(store,**changes)
    assert observed_sources(store.db,cfg)=={}


def test_illiquid_peak_does_not_override_valid_lower_multiple(store,cfg):
    quote(store,peak=5)
    with store.db:
        store.db.execute('''INSERT INTO launch_snapshots(token,horizon_minutes,target_at,observed_at,
            delay_seconds,price_usd,liquidity_usd) VALUES(?,15,300,300,0,100,1)''',(T,))
    assert observed_sources(store.db,cfg)[T]['multiple']==5


def test_growth_to_x10_updates_priority_without_reprocessing_completed_source(store,cfg):
    d=discovery(store,cfg); quote(store,peak=5)
    assert d.detect_winners()==1
    d.discover_from_winners()
    before=dict(store.db.execute('SELECT * FROM discovery_winners').fetchone())
    quote(store,peak=12)
    d.detect_winners();d.discover_from_winners()
    after=dict(store.db.execute('SELECT * FROM discovery_winners').fetchone())
    assert after['max_return_pct']==1100
    assert after['processed_at']==before['processed_at'] and after['detected_at']==before['detected_at']
    d.early_buyers.assert_called_once_with(T)


def test_legacy_under_x5_or_unproven_source_never_triggers_history_reads(store,cfg):
    quote(store,peak=4)
    with store.db:
        store.db.execute('INSERT INTO discovery_winners VALUES(?,1,9900,NULL)',(T,))
    d=discovery(store,cfg)
    assert d.discover_from_winners()==0
    d.early_buyers.assert_not_called()
    # Existing wallets and historical sources are preserved.
    assert store.db.execute('SELECT count(*) FROM discovery_candidates').fetchone()[0]==1
    assert store.db.execute('SELECT count(*) FROM candidate_sources').fetchone()[0]==1
    assert store.db.execute('SELECT processed_at FROM discovery_winners').fetchone()[0] is None


def test_x10_plus_discovery_first_and_failed_source_cooldown_does_not_block_x5(store,cfg,monkeypatch):
    token2='0x'+'4'*40
    quote(store,T,peak=5);quote(store,token2,peak=50)
    d=discovery(store,replace(cfg,discovery_source_tokens_per_cycle=1))
    monkeypatch.setattr('auto_discovery.time.time',lambda:NOW)
    d.detect_winners()
    d.early_buyers=Mock(side_effect=ArcscanError('outage'))
    d.discover_from_winners()
    assert d.early_buyers.call_args.args[0]==token2
    d.discover_from_winners()
    assert d.early_buyers.call_args.args[0]==T
    d.discover_from_winners()
    assert d.early_buyers.call_count==2
    assert store.db.execute('SELECT count(*) FROM discovery_winners WHERE processed_at IS NULL').fetchone()[0]==2


def test_x10_wallet_priority_never_excludes_old_candidates_or_adds_authority(store,cfg,monkeypatch):
    token2='0x'+'4'*40
    quote(store,T,peak=5);quote(store,token2,peak=10)
    store.upsert_discovery_candidate(W2,token2,10)
    with store.db:store.db.execute('UPDATE discovery_candidates SET source_winners=99 WHERE wallet=?',(W,))
    c=fake_collector(store,replace(cfg,discovery_max_wallets_per_cycle=4))
    c.step=Mock()
    monkeypatch.setattr('historical_priority.time.time',lambda:NOW)
    PriorityCollector(c).cycle()
    assert c.step.call_args_list[0].args[0]==W2
    assert W in {call.args[0] for call in c.step.call_args_list}
    assert store.db.execute('SELECT count(*) FROM promoted_wallets').fetchone()[0]==0
    assert store.db.execute("SELECT count(*) FROM discovery_candidates WHERE status='QUARANTINE'").fetchone()[0]==2


def test_losing_tokens_still_count_in_whole_wallet_performance(store,cfg):
    loser='0x'+'4'*40
    quote(store,peak=10)
    rows=[trade(1,'BUY',100),trade(2,'SELL',1000),
          trade(3,'BUY',2000,token=loser),trade(4,'SELL',100,token=loser)]
    before=analyze(rows,cfg)
    d=discovery(store,cfg);d.detect_winners();d.discover_from_winners()
    after=analyze(rows,cfg)
    assert before==after and after['conservative_pnl_usdc']==-1000
    assert len(after['buy_details'])==2


@pytest.mark.parametrize('change',[{'discovery_min_multiple':0},{'discovery_min_multiple':float('nan')},
    {'discovery_priority_multiple':4},{'discovery_priority_multiple':float('inf')},
    {'discovery_source_tokens_per_cycle':0},{'discovery_source_retry_seconds':0}])
def test_invalid_source_settings(cfg,change):
    with pytest.raises(ValueError,match='discovery source'):replace(cfg,**change)


def test_discovery_query_has_bounded_work_instead_of_cartesian_price_scan(store,cfg):
    with store.db:
        for i in range(300):
            token='0x'+f'{i+100:040x}'
            store.db.execute('''INSERT INTO launches(token,discovered_at,pair_created_at,pair_address,
                first_price_usd,first_liquidity_usd) VALUES(?,100,90,?,1,10000)''',(token,W))
            for h in (5,15,60,360):
                store.db.execute('''INSERT INTO launch_snapshots(token,horizon_minutes,target_at,observed_at,
                    delay_seconds,price_usd,liquidity_usd) VALUES(?,?,200,200,0,10,10000)''',(token,h))
    ticks=[0]
    def progress():
        ticks[0]+=1
        return int(ticks[0]>500)  # SQLite VM work, independent of machine speed.
    store.db.set_progress_handler(progress,1000)
    try: assert len(observed_sources(store.db,cfg))==300
    finally:store.db.set_progress_handler(None,0)
