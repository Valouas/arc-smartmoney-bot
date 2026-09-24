"""Fixed-horizon forward evaluation, separate from simulated realized PnL."""
import json
import math
import statistics
import time
from config import SETTINGS
from storage import Storage
from atomic_json import write_json


def f(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError): return None


class BacktestV3:
    def __init__(self, db_path='data/arc_bot.sqlite3'):
        self.storage=Storage(db_path)
        self.db=self.storage.db

    def validated_signals(self):
        return self.db.execute("SELECT id,created_at,token,score,payload FROM signals WHERE side='BUY' AND status='VALIDATED' ORDER BY created_at,id").fetchall()

    def signal_entry_price(self,row):
        try: return f(((json.loads(row['payload']).get('risk') or {}).get('market') or {}).get('price_usd'))
        except (TypeError,ValueError): return None

    def run(self):
        now=int(time.time())
        epoch=self.db.execute("SELECT started_at FROM observation_epochs WHERE name='launch_universe'").fetchone()
        trades=[]; seen=set()
        for row in self.validated_signals():
            token=row['token'].lower()
            if token in seen: continue  # Repeated alerts on a token are not independent trials.
            if not epoch or row['created_at']<epoch[0]: continue
            seen.add(token)
            entry=self.signal_entry_price(row)
            target=row['created_at']+SETTINGS.outcome_horizon_seconds
            key='signal:'+str(row['id'])
            self.db.execute('INSERT OR IGNORE INTO trade_outcomes(tx_hash,wallet,token,entry_at,target_at,deadline_at) VALUES(?,?,?,?,?,?)',
                            (key,'signal',token,row['created_at'],target,target+SETTINGS.outcome_grace_seconds))
            outcome=self.db.execute('SELECT * FROM trade_outcomes WHERE tx_hash=? AND wallet=? AND token=?',(key,'signal',token)).fetchone()
            state=outcome['status']
            if state=='PENDING' and now>outcome['deadline_at']: state='MISSING'
            ret=(outcome['price']/entry-1)*100 if state=='OBSERVED' and entry and entry>0 else None
            trades.append({'signal_id':row['id'],'token':token,'state':state,'target_at':outcome['target_at'],
                           'observed_at':outcome['observed_at'],'return_pct':ret,'winner':ret is not None and ret>=SETTINGS.winner_return_pct})
        self.storage.commit()
        observed=[r for r in trades if r['return_pct'] is not None]
        missing=sum(r['state']=='MISSING' for r in trades); wins=sum(r['winner'] for r in observed)
        mature=len(observed)+missing
        summary={'cohort':'forward_first_signal_per_token','horizon_seconds':SETTINGS.outcome_horizon_seconds,
                 'signals':len(trades),'observed':len(observed),'missing':missing,'pending':sum(r['state']=='PENDING' for r in trades),
                 'win_rate_observed_only':wins/len(observed) if observed else None,
                 'win_rate_lower_bound':wins/mature if mature else None,
                 'win_rate_upper_bound':(wins+missing)/mature if mature else None,
                 'median_fixed_horizon_return_pct':statistics.median(r['return_pct'] for r in observed) if observed else None,
                 'simulated_realized_pnl_usd':self.db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM paper_fills WHERE side='SELL'").fetchone()[0],
                 'insufficient_data':len(observed)<10 or missing>0}
        out={'summary':summary,'trades':trades}
        write_json('data/backtest_v3.json',out)
        print(json.dumps(summary,indent=2))
        return out


if __name__=='__main__': BacktestV3().run()
