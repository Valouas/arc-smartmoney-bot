"""Prospective profiles: matched observed deployment universe and fixed outcomes.

Historical winner-selected research rows never enter this scoring cohort.
"""
import time
import math
from wallet_metrics import profile_from_rows
from wallet_policy import require_smart_money_wallet


def intervals(rows):
    out = []
    for row in rows:
        block, ts = row['block'], row['timestamp']
        if out and block == out[-1][1]+1:
            out[-1] = (out[-1][0],block,out[-1][2],ts)
        elif not out or block > out[-1][1]:
            out.append((block,block,ts,ts))
    return out


def build_forward_profile(storage, wallet, cfg, now=None):
    require_smart_money_wallet(wallet,cfg)
    now = int(time.time()) if now is None else now
    db = storage.db; wallet = wallet.lower()
    epoch = db.execute("SELECT started_at FROM observation_epochs WHERE name='launch_universe'").fetchone()
    candidate = db.execute('SELECT first_discovered_at FROM discovery_candidates WHERE wallet=?',(wallet,)).fetchone()
    start = max(epoch[0] if epoch else now,candidate[0] if candidate else 0)
    coverage = intervals(db.execute('SELECT block,timestamp FROM wallet_coverage WHERE wallet=? AND timestamp>=? ORDER BY block',(wallet,start)))
    active = {r[0]//3600 for r in db.execute('SELECT timestamp FROM wallet_activity WHERE wallet=? AND timestamp>=?',(wallet,start))}
    def covered(block): return block is not None and any(lo <= block <= hi for lo,hi,_,_ in coverage)
    universe = {r['token']:dict(r) for r in db.execute('SELECT * FROM launch_candidates WHERE deployed_at>=?',(start,))
                if r['deployed_at'] is not None and covered(r['deployment_block']) and r['deployed_at']//3600 in active}
    rows = db.execute("SELECT * FROM events WHERE lower(wallet)=? AND side='BUY' AND timestamp>=? ORDER BY timestamp,block_number,id",(wallet,start)).fetchall()
    entries=[]; seen=set(); unmatched=0
    for row in rows:
        token=row['token'].lower()
        if token in seen: continue
        seen.add(token)
        if token not in universe:
            known=db.execute("SELECT deployed_at FROM launch_candidates WHERE token=?",(token,)).fetchone()
            if known and known[0] is not None and known[0]<start: continue
            unmatched += 1; continue
        launch=universe[token]
        if not row['trade_verified'] or not row['token_amount'] or row['token_amount']<=0:
            unmatched += 1; continue
        if not covered(row['block_number']) or row['block_number']<launch['deployment_block']:
            unmatched += 1; continue
        target=row['timestamp']+cfg.outcome_horizon_seconds
        db.execute("INSERT OR IGNORE INTO trade_outcomes(tx_hash,wallet,token,entry_at,target_at,deadline_at) VALUES(?,?,?,?,?,?)",
                   (row['tx_hash'].lower(),wallet,token,row['timestamp'],target,target+cfg.outcome_grace_seconds))
        outcome=db.execute('SELECT * FROM trade_outcomes WHERE tx_hash=? AND wallet=? AND token=?',
                           (row['tx_hash'].lower(),wallet,token)).fetchone()
        state=outcome['status']
        if state=='PENDING' and now>outcome['deadline_at']: state='MISSING'
        entries.append({'token':token,'usdc':row['usdc_amount'],'age_seconds':row['timestamp']-launch['deployed_at'],
                        'entry_price':row['usdc_amount']/row['token_amount'], 'outcome_price':outcome['price'] if state=='OBSERVED' else None,
                        'outcome_state':state, 'exit_price':None,'deployer':launch['deployer']})
    storage.commit()
    p=profile_from_rows(wallet,entries,len(universe),len(active),cfg)
    missing=sum(e['outcome_state']=='MISSING' for e in entries)
    gaps=db.execute('SELECT COUNT(*) FROM reconstruction_gaps WHERE wallet=? AND observed_at>=?',(wallet,start)).fetchone()[0]
    cursors=db.execute("SELECT * FROM discovery_cursors WHERE stream='contracts'").fetchall()
    healthy=len(cursors)==1 and all(r['error'] is None and r['completed_at'] is not None
        and now-r['completed_at']<=max(cfg.launch_poll_seconds*4,60) for r in cursors)
    quality=bool(epoch and coverage and universe and healthy and not unmatched and not gaps and not missing)
    # Token count is never substituted for a missing opportunity denominator.
    p.update(profile_version=2,cohort='forward_after_discovery',generated_at=now,observation_start=start,
             coverage_intervals=len(coverage),unmatched_buys=unmatched,reconstruction_gaps=gaps,
             missing_outcomes=missing,data_quality_ok=quality,
             winner_tokens=[e['token'] for e in entries if e['outcome_price'] is not None and e['outcome_price']/e['entry_price']-1>=cfg.winner_return_pct/100])
    p['confirmation_eligible'] &= quality and p['evaluable_trades']>=cfg.profile_min_evaluable_trades
    return p


def collect_outcomes(storage, market, now=None):
    fixed_now=now
    now=int(time.time()) if now is None else now
    storage.db.execute("UPDATE trade_outcomes SET status='MISSING' WHERE status='PENDING' AND deadline_at<?",(now,))
    storage.commit()
    due=storage.db.execute("SELECT * FROM trade_outcomes WHERE status='PENDING' AND target_at<=? AND deadline_at>=? ORDER BY deadline_at LIMIT 100",(now,now)).fetchall()
    cache={}; count=0
    for row in due:
        token=row['token']
        if token not in cache:
            quote=market.token_snapshot(token)
            cache[token]=(quote,int(time.time()) if fixed_now is None else fixed_now)
        quote,observed=cache[token]
        if not quote or not quote.price_usd or not math.isfinite(quote.price_usd) or quote.price_usd<=0: continue
        if observed>row['deadline_at']: continue
        storage.db.execute("UPDATE trade_outcomes SET status='OBSERVED',price=?,observed_at=? WHERE tx_hash=? AND wallet=? AND token=? AND status='PENDING'",
                           (quote.price_usd,observed,row['tx_hash'],row['wallet'],token))
        count+=1
    storage.commit()
    return count
