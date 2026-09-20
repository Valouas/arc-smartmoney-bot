import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from paper_copy.live import LiveScanner
from paper_copy.policy import Policy
from paper_copy.scanner import ChainConsistencyError
from paper_copy.execution import Execution
from rpc_transport import RPCError
from test_paper_copy import (cfg, ledger, fake_rpc, checkpoint, entry, order,
                            W, W2, T, NOW, cash, market)
from test_paper_webhook import prepared_scan


def ready(ledger, cfg):
    scanner = LiveScanner(ledger, fake_rpc(), Policy(), cfg)
    with ledger.db:
        scanner.store.set('next_block',1)
        scanner.store.set('last_hash',bytes(32).hex())
    return scanner


def test_live_activation_preserves_accounts_history_and_old_checkpoint(ledger,cfg):
    checkpoint(ledger)
    old = [tuple(r) for r in ledger.db.execute('SELECT * FROM wallets')]
    scanner = LiveScanner(ledger,fake_rpc(),Policy(),cfg)
    scanner.initialize(500)
    assert ledger.get('next_block') == '1'
    assert ledger.get('last_hash') == bytes(32).hex()
    assert ledger.get('live_next_block') == '501'
    assert ledger.get('live_uncovered_from') == '1'
    assert ledger.get('live_uncovered_to') == '500'
    assert old == [tuple(r) for r in ledger.db.execute('SELECT * FROM wallets')]
    scanner.initialize(900)
    assert ledger.get('live_next_block') == '501'  # Restart never skips ahead.


@pytest.mark.parametrize('exposure',['position','pending'])
def test_activation_refuses_existing_exposure(ledger,cfg,exposure):
    if exposure == 'pending': order(ledger)
    else:
        with ledger.db: ledger.db.execute('INSERT INTO positions VALUES(?,?,?,?)',(W,T,'10','50'))
    with pytest.raises(ValueError,match='no open positions'):
        LiveScanner(ledger,fake_rpc(),Policy(),cfg).initialize(500)
    assert ledger.get('live_next_block') is None
    assert cash(ledger) == 1000


def test_empty_window_covered_economically_and_resume_is_idempotent(ledger,cfg):
    checkpoint(ledger)
    scanner = ready(ledger,cfg)
    scanner.rpc.block_number.return_value = 42
    assert scanner.cycle({W:entry()}) == 40
    assert ledger.get('next_block') == '1'
    assert ledger.get('live_next_block') == '41'
    assert scanner.rpc.w3.eth.get_logs.call_count == 8
    assert scanner.rpc.get_block.call_count <= 7  # Formerly 81 for forty empty blocks.
    assert tuple(ledger.db.execute('SELECT first_block,last_block FROM live_empty_ranges').fetchone()) == (1,40)
    assert scanner.cycle({W:entry()}) == 0
    assert ledger.db.execute('SELECT COUNT(*) FROM live_empty_ranges').fetchone()[0] == 1


def test_failed_later_query_never_covers_partial_range(ledger,cfg):
    scanner = ready(ledger,cfg)
    scanner.rpc.block_number.return_value = 42
    scanner.rpc.w3.eth.get_logs.side_effect = [[],[],RPCError('eth_getLogs','429',3,'primary')]
    with pytest.raises(RPCError): scanner.cycle({W:entry()})
    assert ledger.get('live_next_block') == '1'
    assert ledger.db.execute('SELECT COUNT(*) FROM live_empty_ranges').fetchone()[0] == 0
    scanner.rpc.w3.eth.get_logs.side_effect = None
    assert scanner.cycle({W:entry()}) == 40


def test_reorg_between_logs_and_commit_refused(ledger,cfg):
    scanner = ready(ledger,cfg)
    original = scanner.rpc.get_block.side_effect
    calls = 0
    def changing(n,*args):
        nonlocal calls
        header = original(n,*args)
        if n == 3:
            calls += 1
            if calls > 1: header['hash'] = b'x'*32
        return header
    scanner.rpc.get_block.side_effect = changing
    with pytest.raises(ChainConsistencyError): scanner.cycle({W:entry()})
    assert ledger.get('live_next_block') == '1'


@pytest.mark.parametrize('swap',[False,True])
def test_live_uses_existing_decoder_and_paper_deduplication(ledger,cfg,swap):
    old,entries = prepared_scan(ledger,cfg,swap=swap)
    scanner = ready(ledger,cfg)
    scanner.rpc = old.rpc
    scanner.participants = old.participants
    ex = Execution(ledger,Policy(),market(3),clock=lambda:NOW)
    assert scanner.cycle({W:entry()},on_block=lambda:ex.pending(NOW)) == 3
    assert ledger.get('next_block') == '1'  # Old lane is untouched.
    assert cash(ledger) == (900 if swap else 1000)
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == int(swap)
    # Delayed/backfill receipt uses the same observation IDs and cannot refill.
    old.cycle({W:entry()},on_block=lambda:ex.pending(NOW))
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == int(swap)


def test_receipt_failure_keeps_active_block_pending(ledger,cfg):
    old,_ = prepared_scan(ledger,cfg)
    scanner = ready(ledger,cfg)
    scanner.rpc,scanner.participants = old.rpc,old.participants
    scanner.rpc.get_receipt.side_effect = RPCError('eth_getTransactionReceipt','502',3,'primary')
    with pytest.raises(RPCError): scanner.cycle({W:entry()})
    assert ledger.get('live_next_block') == '1'
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0


def test_dashboard_reports_live_cursor_separately(ledger,cfg):
    from dashboard import paper_snapshot
    checkpoint(ledger)
    scanner = ready(ledger,cfg)
    scanner.cycle({W:entry()})
    progress = paper_snapshot(ledger.path)['progress']
    assert progress['next_block'] == '4'
    assert progress['historical_next_block'] == '1'
    assert progress['live_lag_blocks'] == 2


def test_wide_empty_catchup_preserves_old_cursor_and_frozen_policy(ledger,cfg):
    checkpoint(ledger)
    cfg=replace(cfg,paper_log_query_blocks=100,paper_catchup_window_blocks=1000)
    scanner=ready(ledger,cfg)
    scanner.rpc.block_number.return_value=1002
    assert scanner.cycle({W:entry()})==1000
    assert ledger.get('next_block')=='1'
    assert ledger.get('live_next_block')=='1001'
    assert scanner.rpc.w3.eth.get_logs.call_count==20
    assert scanner.policy==Policy()
    assert cash(ledger)==1000
    assert scanner.cycle({W:entry()})==0


def test_range_limit_splits_without_gaps_and_remembers_provider_limit(ledger,cfg):
    scanner=ready(ledger,replace(cfg,paper_log_query_blocks=100))
    queries=[]
    def logs(query):
        if query['toBlock']-query['fromBlock']+1>10:
            raise RPCError('eth_getLogs','log_range',1,'primary')
        queries.append(query)
        return []
    scanner.rpc.w3.eth.get_logs.side_effect=logs
    assert scanner.participants(1,140,[W])=={}
    for position in (1,2):
        covered=[n for q in queries if len(q['topics'])==position+1
                 for n in range(q['fromBlock'],q['toBlock']+1)]
        assert covered==list(range(1,141))
    assert scanner.rpc.w3.provider.paper_range_limits=={0:10}
    scanner.rpc.w3.eth.get_logs.reset_mock()
    restarted=LiveScanner(ledger,scanner.rpc,Policy(),scanner.cfg)
    assert restarted.participants(141,160,[W])=={}
    assert scanner.rpc.w3.eth.get_logs.call_count==4


@pytest.mark.parametrize('category',['429','timeout','invalid_result','400'])
def test_wide_failed_window_never_checkpoints_or_splits_transport_failure(ledger,cfg,category):
    scanner=ready(ledger,replace(cfg,paper_log_query_blocks=100,paper_catchup_window_blocks=1000))
    scanner.rpc.block_number.return_value=1002
    scanner.rpc.w3.eth.get_logs.side_effect=[[],[],RPCError('eth_getLogs',category,3,'primary')]
    with pytest.raises(RPCError): scanner.cycle({W:entry()})
    assert scanner.rpc.w3.eth.get_logs.call_count==3
    assert ledger.get('live_next_block')=='1'
    assert ledger.db.execute('SELECT COUNT(*) FROM live_empty_ranges').fetchone()[0]==0
    scanner.rpc.w3.eth.get_logs.side_effect=None
    assert scanner.cycle({W:entry()})==1000


def test_wide_queries_keep_boundary_logs_and_wallet_deduplication(ledger,cfg):
    from rpc import TRANSFER_TOPIC
    scanner=ready(ledger,replace(cfg,paper_log_query_blocks=100))
    def logs(query):
        return [{'blockNumber':n,'blockHash':n.to_bytes(32,'big'),
                 'transactionHash':n.to_bytes(32,'big'),
                 'topics':[TRANSFER_TOPIC,'0x'+'0'*24+W[2:],'0x'+'0'*24+W2[2:]]}
                for n in (100,101) if query['fromBlock']<=n<=query['toBlock']]
    scanner.rpc.w3.eth.get_logs.side_effect=logs
    result=scanner.participants(1,200,[W,W2])
    assert len(result)==2
    for n in (100,101):
        assert result[(n,n.to_bytes(32,'big').hex())]['wallets']=={W,W2}


@pytest.mark.parametrize('age',[0,120])
def test_wide_window_preserves_real_decoder_fills_dedup_and_stale_rejection(ledger,cfg,age):
    from test_paper_copy import attach_transactions
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    scanner=ready(ledger,replace(cfg,paper_log_query_blocks=100,paper_catchup_window_blocks=1000))
    rpc=scanner.rpc
    rpc.block_number.return_value=1002
    header=rpc.get_block.side_effect
    rpc.get_block.side_effect=lambda n,*a:{**header(n,*a),'timestamp':NOW-age}
    rpc.token_meta=lambda _: {'decimals':18,'symbol':'TEST'}
    rpc.swap_emitter_trusted=lambda *a:True
    buy=receipt([transfer(USDC_ERC20,W,POOL,200*10**6),transfer(T,POOL,W,10*10**18)])
    attach_transactions(rpc,[buy])
    transfer_log={**buy['logs'][0],'blockNumber':1,'blockHash':buy['blockHash'],
                  'transactionHash':buy['transactionHash']}
    rpc.w3.eth.get_logs.side_effect=lambda q:[transfer_log] if q['fromBlock']<=1<=q['toBlock'] and len(q['topics'])==2 else []
    execution=Execution(ledger,Policy(),market(3),clock=lambda:NOW)
    assert scanner.cycle({W:entry()},on_block=lambda:execution.pending(NOW))==1000
    assert ledger.get('live_next_block')=='1001'
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==int(age==0)
    assert cash(ledger)==(900 if age==0 else 1000)
    assert scanner.cycle({W:entry()},on_block=lambda:execution.pending(NOW))==0
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==1


@pytest.mark.parametrize('field,value',[('paper_log_query_blocks',9),('paper_log_query_blocks',1001),
    ('paper_catchup_window_blocks',39),('paper_catchup_window_blocks',2001)])
def test_transport_windows_are_bounded(cfg,field,value):
    with pytest.raises(ValueError,match='transport windows'): replace(cfg,**{field:value})


@pytest.mark.parametrize('history,live',[(False,True),(True,False),(False,False)])
def test_research_mode_requires_history_and_live_paper(cfg,history,live):
    with pytest.raises(ValueError,match='requires historical analysis'):
        replace(cfg,research_paper_only=True,historical_analysis_enabled=history,paper_live_enabled=live)
