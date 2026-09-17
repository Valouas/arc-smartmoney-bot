"""Read-only portfolio report. Unsold tokens never disappear from performance."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .policy import decimal


def report(path):
    file = Path(path).resolve()
    if not file.exists():
        print('PAPER COPY: aucune simulation initialisee.'); return []
    with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        policy = json.loads(db.execute("SELECT value FROM state WHERE key='policy'").fetchone()[0])
        initial = decimal(policy['initial_cash'])
        rows = []
        for account in db.execute('SELECT * FROM wallets ORDER BY wallet'):
            positions = [dict(p) for p in db.execute('SELECT * FROM positions WHERE wallet=?',(account['wallet'],)) if decimal(p['quantity'])>0]
            fills = list(db.execute('SELECT side,realized_pnl FROM fills WHERE wallet=?',(account['wallet'],)))
            pnl = sum((decimal(r['realized_pnl'].lstrip('-'))*(-1 if r['realized_pnl'].startswith('-') else 1) for r in fills),decimal(0))
            cost = sum((decimal(p['cost']) for p in positions),decimal(0))
            rows.append({'wallet':account['wallet'],'active':bool(account['active']),'cash':account['cash'],
                'realized_pnl':str(pnl),'open_cost':str(cost),'open_positions':positions,
                'equity_market':None,'unrealized_pnl':None,'cash_only_pnl':str(decimal(account['cash'])-initial),
                'buys':sum(r['side']=='BUY' for r in fills),'sells':sum(r['side']=='SELL' for r in fills)})
        progress = {r[0]:r[1] for r in db.execute("SELECT key,value FROM state WHERE key<>'policy'")}
        orders = {r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM observations GROUP BY status')}
        reasons = {r[0]:r[1] for r in db.execute('SELECT reason,COUNT(*) FROM observations WHERE reason IS NOT NULL GROUP BY reason')}
        gaps = db.execute('SELECT COUNT(*) FROM gaps').fetchone()[0]
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        webhook={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM webhook_triggers GROUP BY status')} if 'webhook_triggers' in tables else {}
    print('PAPER COPY | 100% simulation | prix de remplissage modelises, aucun ordre reel')
    print('Wallet | Cash USDC | PnL realise | Cout positions ouvertes | PnL prudent (solde ouvert nul) | Achats/Ventes')
    for r in rows:
        print(f"{r['wallet']} | {float(r['cash']):.2f} | {float(r['realized_pnl']):+.2f} | "
              f"{float(r['open_cost']):.2f} | {float(r['cash_only_pnl']):+.2f} | {r['buys']}/{r['sells']}")
    print('Valorisation des positions ouvertes: inconnue; elles ne sont pas declarees sans perte.')
    print(json.dumps({'wallets':len(rows),'progress':progress,'observations':orders,'reasons':reasons,'reconstruction_gaps':gaps,'webhook_triggers':webhook}))
    return rows
