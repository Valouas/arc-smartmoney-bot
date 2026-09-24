from types import SimpleNamespace
from unittest.mock import Mock
from hexbytes import HexBytes
import pytest
from web3 import Web3

from rpc import TRANSFER_TOPIC
from rpc_transport import RPCError
from scanner_ranges import prepare_window
from storage import Storage
from test_followup import make_bot
from test_audit_regressions import W,T,ROUTER


def hashed(n):return HexBytes(n.to_bytes(32,'big'))


def network():
    blocks={n:{'number':n,'hash':hashed(n),'parentHash':hashed(n-1),'timestamp':100+n,
               'transactions':([{'hash':hashed(1000+n),'from':T,'to':T,'value':0}] if n in (2,7) else [])} for n in range(1,11)}
    logs=[{'blockNumber':n,'blockHash':hashed(n),'transactionHash':hashed(1000+n),'removed':False,
           'topics':[HexBytes(TRANSFER_TOPIC),HexBytes('0x'+'0'*24+W[2:]),HexBytes('0x'+'0'*24+T[2:])]} for n in (2,7)]
    def get_logs(query):
        if query['topics'][1] is None:return []
        return [l for l in logs if query['fromBlock']<=l['blockNumber']<=query['toBlock']]
    rpc=SimpleNamespace(get_block=Mock(side_effect=lambda n,*a,**k:blocks[n]),
        w3=SimpleNamespace(eth=SimpleNamespace(get_logs=Mock(side_effect=get_logs))),
        get_receipt=Mock(side_effect=lambda h:{'blockHash':hashed(int.from_bytes(h,'big')-1000),'status':1}),
        sanity_check=Mock(),block_number=lambda:10)
    return rpc,blocks,logs


def bot(tmp_path,rpc):
    db=Storage(str(tmp_path/'bot.db'))
    b=make_bot(db,tmp_path);b.watch={W:b.watch[W]};b.rpc=rpc
    b.process_transaction=Mock()
    db.checkpoint(1)
    return b


def test_ten_blocks_two_log_requests_ordered_and_no_missing_relayed_wallets(tmp_path):
    rpc,_,_=network();b=bot(tmp_path,rpc)
    prepared=prepare_window(rpc,1,10,[W])
    assert rpc.w3.eth.get_logs.call_count==2
    assert [n for n,_,_ in prepared]==list(range(1,11))
    for n,block,participants in prepared:b.process_block(n,prepared=(block,participants))
    assert b.db.next_block()==11
    assert [c.args[3] for c in b.process_transaction.call_args_list]==[2,7]
    assert all(c.args[2]=={W} for c in b.process_transaction.call_args_list)
    # Same staged window replay never processes the transactions a second time.
    for n,block,participants in prepared:b.process_block(n,prepared=(block,participants))
    assert b.process_transaction.call_count==2
    b.db.db.close()


def test_second_log_failure_does_not_checkpoint_any_block(tmp_path):
    rpc,_,logs=network();b=bot(tmp_path,rpc)
    rpc.w3.eth.get_logs.side_effect=[logs,RPCError('eth_getLogs','429',3,'primary')]
    with pytest.raises(RPCError):prepare_window(rpc,1,10,[W])
    assert b.db.next_block()==1
    b.process_transaction.assert_not_called();b.db.db.close()


def test_failed_receipt_resumes_same_block_without_loss_or_duplicates(tmp_path):
    rpc,_,_=network();b=bot(tmp_path,rpc)
    original=rpc.get_receipt.side_effect
    rpc.get_receipt.side_effect=lambda h: (_ for _ in ()).throw(RPCError('eth_getTransactionReceipt','502',3,'primary')) if h==hashed(1007) else original(h)
    with pytest.raises(RPCError):
        for n,block,participants in prepare_window(rpc,1,10,[W]):b.process_block(n,prepared=(block,participants))
    assert b.db.next_block()==7
    rpc.get_receipt.side_effect=original
    for n,block,participants in prepare_window(rpc,b.db.next_block(),10,[W]):b.process_block(n,prepared=(block,participants))
    assert b.db.next_block()==11
    assert [c.args[3] for c in b.process_transaction.call_args_list]==[2,7]
    b.db.db.close()


@pytest.mark.parametrize('defect',['removed','wrong_block','wrong_tx','wrong_wallet','out_of_range','parent'])
def test_inconsistent_range_rejected(defect):
    rpc,blocks,logs=network()
    if defect=='removed':logs[0]['removed']=True
    if defect=='wrong_block':logs[0]['blockHash']=hashed(999)
    if defect=='wrong_tx':logs[0]['transactionHash']=hashed(999)
    if defect=='wrong_wallet':logs[0]['topics'][1]=HexBytes('0x'+'0'*24+T[2:])
    if defect=='out_of_range':logs[0]['blockNumber']=11;rpc.w3.eth.get_logs.side_effect=None;rpc.w3.eth.get_logs.return_value=logs
    if defect=='parent':blocks[4]['parentHash']=hashed(999)
    with pytest.raises(RPCError):prepare_window(rpc,1,10,[W])


def test_empty_range_still_checks_anchor_and_per_block_canonical_hash(tmp_path):
    rpc,blocks,_=network();b=bot(tmp_path,rpc)
    rpc.w3.eth.get_logs.side_effect=None;rpc.w3.eth.get_logs.return_value=[]
    prepared=prepare_window(rpc,1,10,[W])
    rpc.get_block.side_effect=lambda n,*a,**k:{**blocks[n],'hash':hashed(999)}
    with pytest.raises(RPCError):b.process_block(1,prepared=prepared[0][1:])
    assert b.db.next_block()==1
    with pytest.raises(RPCError):prepare_window(rpc,1,10,[W])
    b.db.db.close()


def test_watchlist_change_restages_remaining_window(tmp_path,monkeypatch):
    rpc,_,_=network();b=bot(tmp_path,rpc)
    b.refresh_watch=Mock();b.webhooks=None;b.outbox=SimpleNamespace(flush=Mock())
    seen=[]
    def prepare(rpc,first,last,wallets):
        seen.append((first,set(wallets)))
        return prepare_window(rpc,first,last,wallets)
    monkeypatch.setattr('bot.prepare_window',prepare)
    process=b.process_block
    def block(n,**kw):
        if n==3:raise KeyboardInterrupt()
        process(n,**kw)
        if n==1:b.watch[ROUTER]=b.watch[W]
    b.process_block=block
    b._run()
    assert seen==[(1,{W}),(2,{W,ROUTER})]
    assert b.db.next_block()==3
    b.db.db.close()


def test_wallet_batches_over_one_hundred_preserve_all_participants():
    rpc,_,logs=network()
    def filtered(query):
        return logs if query['topics'][1] and '0x'+'0'*24+W[2:] in query['topics'][1] else []
    rpc.w3.eth.get_logs.side_effect=filtered
    wallets=[W]+['0x'+f'{n:040x}' for n in range(1,101)]
    staged=prepare_window(rpc,1,10,wallets)
    assert rpc.w3.eth.get_logs.call_count==4
    assert staged[1][2]=={Web3.to_hex(hashed(1002)):{W}}
