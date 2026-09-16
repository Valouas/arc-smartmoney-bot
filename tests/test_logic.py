import time
from cluster import ClusterBook
from models import TradeEvent

def e(wallet, weight, ts, side="BUY"):
    return TradeEvent(tx_hash="0x"+wallet[-4:]*16,block_number=1,timestamp=ts,wallet=wallet,wallet_label=wallet,wallet_weight=weight,side=side,token="0x1111111111111111111111111111111111111111",token_symbol="TEST",token_amount=100,usdc_amount=100)

def test_cluster_deduplicates_wallets():
    now = int(time.time()); b = ClusterBook(1200, 600)
    b.add(e("0x0000000000000000000000000000000000000001", 3, now))
    s = b.add(e("0x0000000000000000000000000000000000000002", 2, now+1))
    assert s.distinct_wallets == 2
    assert s.cluster_weight == 5

if __name__ == "__main__":
    test_cluster_deduplicates_wallets(); print("ok")
