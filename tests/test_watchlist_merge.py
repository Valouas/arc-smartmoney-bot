import tempfile,json
from pathlib import Path
from watchlist_manager import WatchlistManager

def test_base_overrides_auto():
    with tempfile.TemporaryDirectory() as d:
        b=Path(d)/"b.json"; g=Path(d)/"g.json"
        addr="0x"+"1"*40
        g.write_text(json.dumps({"wallets":[{"address":addr,"label":"auto","weight":4}]}))
        b.write_text(json.dumps({"wallets":[{"address":addr,"label":"seed","weight":2}]}))
        m=WatchlistManager(b,g,0);m.reload(True)
        x=m.items[addr.lower()]
        assert x.label=="seed" and x.weight==2

if __name__=="__main__":
    test_base_overrides_auto();print("ok")
