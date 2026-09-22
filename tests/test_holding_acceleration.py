"""Concurrency must save requests without weakening reconstruction or budgets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import copy_holding_campaign as campaign
from copy_holding_pilot import PilotLimit
from copy_holding_transport import CampaignArcRPC
from rpc_transport import RPCError
from test_copy_holding_campaign import prepared
from test_copy_holding_duration import W, tx


@pytest.mark.parametrize('paid', [False, True])
def test_parallel_reservations_respect_persistent_caps(prepared, paid):
    src, scope, out, cfg = prepared
    campaign.prepare(src, scope, out, cfg, rpc_cap=7)
    settings = json.loads((out/'settings.json').read_text())
    settings['paid_rpc'] = {'reserved_cu_cap': 700}
    with campaign.connect(out/'campaign.sqlite3') as db:
        budget = campaign.CampaignBudget(out, db, settings)
        def attempt(_):
            try:
                sequence = budget.take_paid('eth_call') if paid else budget.take('rpc', 'eth_call')
                budget.record({'method': 'eth_call', 'sequence': sequence})
                return True
            except PilotLimit: return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert sum(pool.map(attempt, range(30))) == 7
        assert budget.counts['rpc'] == 7
        assert campaign.CampaignBudget(out, db, settings).counts['rpc'] == 7
        assert db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget').fetchone()[:] == ((7,700) if paid else (0,0))
    assert sorted(json.loads(line)['sequence'] for line in (out/'requests.jsonl').read_text().splitlines()) == list(range(1,8))


@pytest.mark.parametrize('fail_second', [False, True])
def test_parallel_reads_ordered_owner_writes_and_failure_prefix(prepared, monkeypatch, fail_second):
    src, scope, out, cfg = prepared
    campaign.prepare(src, scope, out, cfg)
    hashes = [tx(i, i*100)['tx_hash'] for i in (2,3)]
    owner = threading.get_ident(); barrier = threading.Barrier(2)
    clients = []; decoded = []
    def factory():
        client = SimpleNamespace(w3=SimpleNamespace(provider=SimpleNamespace(
            historical_headers=nullcontext, session=SimpleNamespace(close=Mock()))))
        clients.append(client)
        return client
    rpc = factory(); rpc.campaign_read_workers=2; rpc.campaign_read_factory=factory
    def read(client, h, cutoff):
        assert threading.get_ident() != owner
        barrier.wait(timeout=5)
        if fail_second and h == hashes[1]: raise RPCError('eth_getTransactionReceipt','502',3,'primary')
        return (h, {}, {})
    def decode(self, wallet, h, run, *, reads=None, rpc=None):
        assert threading.get_ident() == owner and reads[0] == h
        decoded.append(h)
        # An ambiguous RPC response is evidence, never a reason to read it twice.
        return dict(tx(int(h,16),200), error='unsupported_or_ambiguous_reconstruction')
    monkeypatch.setattr(campaign,'read_transaction',read)
    monkeypatch.setattr(campaign.HistoricalCollector,'transaction',decode)
    with campaign.connect(out/'campaign.sqlite3') as db:
        db.execute('INSERT INTO historical_transactions VALUES(?,?,NULL)',(W,hashes[1]));db.commit()
        run = dict(db.execute('SELECT * FROM historical_runs').fetchone())
        if fail_second:
            with pytest.raises(RPCError):campaign.decode_hashes(db,W,hashes,run,rpc)
        else:campaign.decode_hashes(db,W,hashes,run,rpc)
        assert decoded == hashes[:1 if fail_second else 2]
        assert (db.execute('SELECT payload FROM historical_transactions WHERE tx_hash=?',(hashes[1],)).fetchone()[0] is None) == fail_second
        assert db.execute('SELECT phase FROM historical_runs').fetchone()[0] != 'COMPLETE'
    assert all(c.w3.provider.session.close.call_count == 1 for c in clients[1:])


def test_minimal_metadata_cached_without_name_or_supply_reads():
    rpc=object.__new__(CampaignArcRPC)
    functions=Mock()
    functions.symbol.return_value.call.return_value='TEST'
    functions.decimals.return_value.call.return_value=18
    rpc.w3=SimpleNamespace(eth=SimpleNamespace(contract=Mock(return_value=SimpleNamespace(functions=functions))))
    assert rpc.token_meta(W)['decimals']==18
    assert rpc.token_meta(W)['symbol']=='TEST'
    assert functions.symbol.call_count==functions.decimals.call_count==1
    functions.name.assert_not_called();functions.totalSupply.assert_not_called()


@pytest.mark.parametrize('method', ['symbol','decimals'])
def test_minimal_metadata_propagates_transport_failures(method):
    rpc=object.__new__(CampaignArcRPC);functions=Mock()
    functions.symbol.return_value.call.return_value='TEST'
    functions.decimals.return_value.call.return_value=18
    getattr(functions,method).return_value.call.side_effect=RPCError('eth_call','timeout',3,'primary')
    rpc.w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **kw:SimpleNamespace(functions=functions)))
    with pytest.raises(RPCError):rpc.token_meta(W)


@pytest.mark.parametrize('options',[{'workers':0},{'workers':5},{'max_transactions':0},{'max_run_seconds':0}])
def test_invalid_benchmark_limits_rejected_before_io(options):
    with pytest.raises(ValueError):campaign.run('nonexistent',None,**options)


def test_benchmark_stops_at_exact_transaction_limit_and_preserves_existing(prepared,monkeypatch):
    import web3
    src,scope,out,cfg=prepared
    campaign.prepare(src,scope,out,cfg)
    cfg.rpc_cache_file=str(out/'cache.sqlite3');cfg.rpc_rate_limit_file=str(out/'rate.sqlite3')
    with campaign.connect(out/'campaign.sqlite3') as db:
        original=db.execute('SELECT payload FROM historical_transactions WHERE payload IS NOT NULL').fetchone()[0]
        db.executemany('INSERT INTO historical_transactions VALUES(?,?,NULL)',[(W,tx(i,100)['tx_hash']) for i in range(3,12)])
    monkeypatch.setattr(campaign,'PilotCache',Mock())
    monkeypatch.setattr(campaign,'SharedRateLimiter',Mock())
    monkeypatch.setattr(campaign,'CampaignRPCProvider',lambda *a,**kw:SimpleNamespace(session=kw['session'],chain_cache_hits=0))
    monkeypatch.setattr(web3,'Web3',lambda provider:SimpleNamespace(provider=provider))
    monkeypatch.setattr(campaign,'ArcscanClient',Mock())
    batches=[]
    def advance(db,wallet,rpc,arc,*,batch,**kw):
        batches.append(batch)
        pending=db.execute('SELECT tx_hash FROM historical_transactions WHERE payload IS NULL ORDER BY tx_hash LIMIT ?',(batch,)).fetchall()
        with db:
            for row in pending:
                db.execute('UPDATE historical_transactions SET payload=? WHERE tx_hash=?',(json.dumps(tx(int(row[0],16),100)),row[0]))
    monkeypatch.setattr(campaign,'advance',advance)
    campaign.run(out,cfg,max_transactions=3,max_run_seconds=60)
    report=json.loads(next(out.glob('run_*.json')).read_text())
    assert batches==[3] and report['stop']=='TRANSACTION_LIMIT'
    assert report['decoded_this_run']==3 and report['http_attempts_this_run']=={'rpc':0,'arcscan':0}
    with campaign.connect(out/'campaign.sqlite3') as db:
        assert db.execute('SELECT count(payload) FROM historical_transactions').fetchone()[0]==4
        assert db.execute('SELECT payload FROM historical_transactions WHERE tx_hash=?',(tx(1,100)['tx_hash'],)).fetchone()[0]==original
