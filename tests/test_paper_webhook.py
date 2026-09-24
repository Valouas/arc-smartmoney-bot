import json
import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest

from paper_copy.webhook import LiveWakeup, watch_addresses
from paper_copy.scanner import Scanner
from paper_copy.execution import Execution
from paper_copy.policy import Policy
from webhook_queue import SCHEMA,accept
from rpc_transport import RPCError
from test_paper_copy import (cfg,ledger,W,W2,T,NOW,entry,contract,checkpoint,
                             fake_rpc,attach_transactions,market,cash,order)
from test_alchemy_webhook import endpoint,request,payload,KEY


@pytest.fixture
def inbox(tmp_path):
    path=tmp_path/'inbox.sqlite3'
    db=sqlite3.connect(path); db.executescript(SCHEMA)
    yield path,db
    db.close()


def deliver(db,tx='0x'+'a'*64,wallet=W,event_id='paper',now=NOW):
    p=payload(event_id,wallet)
    p['event']['activity'][0]['hash']=tx
    p['event']['activity'][0]['log']['transactionHash']=tx
    return accept(db,json.dumps(p).encode(),{},now,paper_watch={wallet})


def trigger_status(store):
    return store.db.execute('SELECT status FROM webhook_triggers').fetchone()[0]


def test_signed_http_routes_qualified_wallet_without_v4_authority(endpoint,cfg):
    settings=replace(cfg,alchemy_webhook_enabled=True,alchemy_webhook_signing_key=KEY,
                     alchemy_webhook_host='127.0.0.1')
    paper_only='0x'+'7'*40
    server,path=endpoint(settings,paper_watch=lambda:{paper_only})
    assert request(server,payload(wallet=paper_only),signature='0'*64)[0]==401
    status,result=request(server,payload(wallet=paper_only))
    assert status==200 and result['paper_queued']==1 and result['queued']==0
    assert request(server,payload(wallet=paper_only))[1]['duplicate']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM alchemy_paper_triggers').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM alchemy_jobs').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0


def test_paper_watch_requires_qualification_and_preserves_existing_exits(tmp_path,ledger,cfg):
    path=tmp_path/'selection.json'; path.write_text(json.dumps(contract()))
    assert watch_addresses(cfg,path,ledger.path,NOW)=={W}
    data=contract(); data['wallets'][0]['historical_class']='PROMISING'
    path.write_text(json.dumps(data))
    assert watch_addresses(cfg,path,ledger.path,NOW)==set()
    Execution(ledger,Policy(),market(),clock=lambda:NOW).fill(order(ledger),NOW)
    assert watch_addresses(cfg,path,ledger.path,NOW)=={W}
    (tmp_path/'registry.json').write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    assert watch_addresses(cfg,path,ledger.path,NOW)==set()


def test_watch_export_never_creates_database_or_authority(tmp_path,cfg):
    paper=tmp_path/'absent.sqlite3'
    assert watch_addresses(cfg,tmp_path/'missing.json',paper,NOW)==set()
    assert not paper.exists()


def test_receiver_state_cannot_be_consumed_from_paper_ledger(ledger):
    with pytest.raises(ValueError): LiveWakeup(ledger,ledger.path,Policy())


def test_trigger_dedupe_restart_and_read_only_source(ledger,inbox):
    path,db=inbox; checkpoint(ledger)
    deliver(db); deliver(db,event_id='another')
    before=db.total_changes
    bridge=LiveWakeup(ledger,path,Policy())
    assert bridge.refresh({W},NOW)
    assert ledger.get('next_block')=='1' and cash(ledger)==1000
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0
    reopened=LiveWakeup(ledger,path,Policy())
    assert reopened.refresh({W},NOW)
    assert ledger.db.execute('SELECT COUNT(*) FROM webhook_triggers').fetchone()[0]==1
    assert db.total_changes==before
    assert db.execute("SELECT name FROM sqlite_master WHERE name='state'").fetchone() is None


def test_idle_rpc_reduction_immediate_wakeup_and_periodic_fallback(ledger,inbox):
    path,db=inbox; bridge=LiveWakeup(ledger,path,Policy())
    selection={W:entry()}
    assert bridge.due(selection,{W},NOW)
    bridge.scanned(selection,0,NOW)
    assert not bridge.due(selection,{W},NOW+2)
    assert not bridge.due(selection,{W},NOW+14)
    assert bridge.due(selection,{W},NOW+15)
    bridge.scanned(selection,0,NOW+15)
    deliver(db,now=NOW+16)
    assert bridge.due(selection,{W},NOW+16)


def test_selection_change_and_backlog_cannot_wait_for_webhook(ledger,inbox):
    path,_=inbox; bridge=LiveWakeup(ledger,path,Policy())
    selection={W:entry()}; bridge.due(selection,{W},NOW); bridge.scanned(selection,0,NOW)
    assert bridge.due({},set(),NOW+1)
    bridge.scanned(selection,Policy().block_window,NOW+1)
    assert bridge.due(selection,{W},NOW+1)


def test_missing_webhook_database_preserves_original_polling(ledger,tmp_path):
    path=tmp_path/'missing.sqlite3'; bridge=LiveWakeup(ledger,path,Policy())
    selection={W:entry()}; bridge.scanned(selection,0,NOW)
    assert bridge.due(selection,{W},NOW+1)
    assert not bridge.available and not path.exists()


@pytest.mark.parametrize('watched,age,expected',[(set(),0,'IGNORED_NOT_WATCHED'),({W},61,'EXPIRED')])
def test_ineligible_or_old_trigger_cannot_create_order(ledger,inbox,watched,age,expected):
    path,db=inbox; deliver(db)
    bridge=LiveWakeup(ledger,path,Policy())
    assert not bridge.refresh(watched,NOW+age)
    assert trigger_status(ledger)==expected
    assert cash(ledger)==1000 and ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0


def prepared_scan(ledger,cfg,swap=True):
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.token_meta=lambda _: {'decimals':18,'symbol':'TEST'}
    rpc.swap_emitter_trusted=lambda *a:True
    logs=[transfer(T,POOL,W,10*10**18)]
    if swap: logs.insert(0,transfer(USDC_ERC20,W,POOL,200*10**6))
    entries=attach_transactions(rpc,[receipt(logs,swap)])
    scanner=Scanner(ledger,rpc,Policy(),cfg)
    scanner.participants=Mock(return_value={(1,tx):item for tx,item in entries})
    return scanner,entries


def test_webhook_to_ordered_scan_fill_restart_and_backfill_dedupe(ledger,cfg,inbox):
    path,db=inbox; scanner,entries=prepared_scan(ledger,cfg)
    tx='0x'+entries[0][0]; deliver(db,tx)
    bridge=LiveWakeup(ledger,path,Policy()); selection={W:entry()}
    assert bridge.due(selection,{W},NOW)
    ex=Execution(ledger,Policy(),market(3),clock=lambda:NOW)
    assert scanner.cycle(selection,on_block=lambda:ex.pending(NOW))==3
    assert not bridge.refresh({W},NOW)
    assert trigger_status(ledger)=='SCANNED' and cash(ledger)==900 and cash(ledger,W2)==1000
    deliver(db,tx,event_id='redelivery')
    assert not LiveWakeup(ledger,path,Policy()).refresh({W},NOW)
    checkpoint(ledger)
    scanner.cycle(selection,on_block=lambda:ex.pending(NOW))
    assert cash(ledger)==900 and ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==1
    assert len(json.loads(ledger.db.execute('SELECT baseline FROM wallets WHERE wallet=?',(W,)).fetchone()[0]))==11


def test_polling_first_late_webhook_does_not_retrade(ledger,cfg,inbox):
    path,db=inbox; scanner,entries=prepared_scan(ledger,cfg)
    scanner.cycle({W:entry()})
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW); ex.pending(NOW)
    deliver(db,'0x'+entries[0][0])
    assert not LiveWakeup(ledger,path,Policy()).refresh({W},NOW)
    assert cash(ledger)==900 and trigger_status(ledger)=='SCANNED'


def test_transfer_trigger_is_scanned_without_copying(ledger,cfg,inbox):
    path,db=inbox; scanner,entries=prepared_scan(ledger,cfg,swap=False)
    deliver(db,'0x'+entries[0][0])
    bridge=LiveWakeup(ledger,path,Policy()); bridge.refresh({W},NOW)
    scanner.cycle({W:entry()}); bridge.refresh({W},NOW)
    assert trigger_status(ledger)=='SCANNED'
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0
    assert cash(ledger)==1000


def test_failed_rpc_keeps_trigger_and_checkpoint_until_complete_retry(ledger,cfg,inbox):
    path,db=inbox; scanner,entries=prepared_scan(ledger,cfg)
    deliver(db,'0x'+entries[0][0]); bridge=LiveWakeup(ledger,path,Policy())
    bridge.refresh({W},NOW)
    original=scanner.rpc.get_receipt.side_effect
    scanner.rpc.get_receipt.side_effect=RPCError('eth_getTransactionReceipt','429',3,'primary')
    with pytest.raises(RPCError): scanner.cycle({W:entry()})
    assert ledger.get('next_block')=='1' and trigger_status(ledger)=='PENDING'
    assert ledger.db.execute('SELECT COUNT(*) FROM scanned_transactions').fetchone()[0]==0
    scanner.rpc.get_receipt.side_effect=original
    scanner.cycle({W:entry()}); bridge.refresh({W},NOW)
    assert ledger.get('next_block')=='4' and trigger_status(ledger)=='SCANNED'


def test_hint_for_later_sale_cannot_skip_earlier_buy_or_transfer(ledger,cfg,inbox):
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    path,db=inbox; scanner,_=prepared_scan(ledger,cfg)
    scanner.rpc.w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value=0
    receipts=[receipt([transfer(USDC_ERC20,W,POOL,100*10**6),transfer(T,POOL,W,100*10**18)]),
              receipt([transfer(T,W2,W,100*10**18)],False),
              receipt([transfer(T,W,POOL,50*10**18),transfer(USDC_ERC20,POOL,W,100*10**6)])]
    entries=attach_transactions(scanner.rpc,receipts)
    scanner.participants=Mock(return_value={(1,tx):item for tx,item in reversed(entries)})
    # Only the LAST transaction is notified; the scanner must still find all three.
    deliver(db,'0x'+entries[-1][0])
    bridge=LiveWakeup(ledger,path,Policy()); assert bridge.due({W:entry()},{W},NOW)
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW)
    scanner.cycle({W:entry()},on_block=lambda:ex.pending(NOW))
    orders=list(ledger.db.execute('SELECT side,sell_fraction FROM observations ORDER BY tx_index'))
    assert [r['side'] for r in orders]==['BUY','SELL']
    assert orders[-1]['sell_fraction']=='0.25'
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==2


def test_cli_webhook_empty_selection_never_uses_rpc(tmp_path,cfg,inbox,monkeypatch):
    import paper_copy.__main__ as cli
    path,db=inbox; deliver(db)
    selection=tmp_path/'empty.json'; data=contract(); data['wallets']=[]
    selection.write_text(json.dumps(data))
    monkeypatch.setattr(cli,'SETTINGS',replace(cfg,alchemy_webhook_enabled=True))
    monkeypatch.setattr(cli.time,'time',lambda:NOW)
    rpc=Mock(side_effect=AssertionError('Empty selection must not use RPC'))
    monkeypatch.setattr(cli,'ArcRPC',rpc)
    target=tmp_path/'new-paper.sqlite3'
    assert cli.main(['--db',str(target),'--selection',str(selection),'--webhook-db',str(path),'--once'])==0
    rpc.assert_not_called()
    with sqlite3.connect(target) as paper:
        assert paper.execute('SELECT COUNT(*) FROM wallets').fetchone()[0]==0
        assert paper.execute('SELECT status FROM webhook_triggers').fetchone()[0]=='IGNORED_NOT_WATCHED'


def test_cli_webhook_runs_existing_fill_pipeline(ledger,cfg,inbox,tmp_path,monkeypatch):
    import paper_copy.__main__ as cli
    path,db=inbox; prepared,entries=prepared_scan(ledger,cfg)
    deliver(db,'0x'+entries[0][0])
    prepared.rpc.sanity_check=Mock()
    prepared.rpc.w3.provider=Mock()
    selection=tmp_path/'qualified.json'; selection.write_text(json.dumps(contract()))
    monkeypatch.setattr(cli,'SETTINGS',replace(cfg,alchemy_webhook_enabled=True))
    monkeypatch.setattr(cli.time,'time',lambda:NOW)
    monkeypatch.setattr(cli,'ArcRPC',lambda *a:prepared.rpc)
    monkeypatch.setattr(cli,'DexScreenerClient',lambda *a:market())
    def scanner(store,rpc,policy,settings):
        result=Scanner(store,rpc,policy,settings)
        result.participants=prepared.participants
        return result
    monkeypatch.setattr(cli,'Scanner',scanner)
    monkeypatch.setattr(cli,'Execution',lambda store,policy,market:Execution(store,policy,market,clock=lambda:NOW))
    assert cli.main(['--db',str(ledger.path),'--selection',str(selection),'--webhook-db',str(path),'--once'])==0
    assert cash(ledger)==900 and trigger_status(ledger)=='SCANNED'
    assert ledger.get('next_block')=='4'
    prepared.rpc.sanity_check.assert_called_once()
