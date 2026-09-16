from identity_graph import IdentityGraph
from reputation import ReputationBook
from models import TradeEvent
from cluster import ClusterBook
import tempfile, os, time

class FakeArcscan:
    def __init__(self): self.data = {}
    def address_facts(self, addr): return self.data.get(addr.lower(), {})

def test_common_funder_is_one_identity():
    f = FakeArcscan()
    a = "0x0000000000000000000000000000000000000001"
    b = "0x0000000000000000000000000000000000000002"
    funder = "0x00000000000000000000000000000000000000ff"
    f.data[a] = {"funded_by": funder}; f.data[b] = {"funded_by": funder}
    g = IdentityGraph(f); g.refresh([a,b])
    assert g.independent_count([a,b]) == 1

def test_related_wallets_do_not_double_weight():
    now = int(time.time()); b = ClusterBook(1200,600)
    token = "0x1111111111111111111111111111111111111111"
    e1 = TradeEvent("0x1",1,now,"0x0000000000000000000000000000000000000001","a",3,"BUY",token,"X",1,100,identity_id="clusterA",reputation_score=80)
    e2 = TradeEvent("0x2",1,now+1,"0x0000000000000000000000000000000000000002","b",3,"BUY",token,"X",1,100,identity_id="clusterA",reputation_score=80)
    b.add(e1); s = b.add(e2)
    assert s.distinct_wallets == 2
    assert s.independent_identities == 1
    assert s.cluster_weight == 3

def test_reputation_shrinks_one_lucky_trade():
    with tempfile.TemporaryDirectory() as d:
        r = ReputationBook(os.path.join(d,"r.json")); a = "0xabc"
        r.mark_known_winner(a, 1)
        assert r.get(a).score < 80

if __name__ == "__main__":
    test_common_funder_is_one_identity(); test_related_wallets_do_not_double_weight(); test_reputation_shrinks_one_lucky_trade(); print("ok")
