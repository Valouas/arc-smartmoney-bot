"""Only validated buy opportunities and newly promoted wallets reach Telegram."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alert_outbox import AlertOutbox
from auto_discovery import AutoDiscovery
from config import Settings
from storage import Storage
from test_audit_regressions import db, W, T, ROUTER, event
from test_followup import make_bot, signal


def transport():
    return SimpleNamespace(enabled=True, send=Mock(return_value={'state':'SENT','message_id':'1'}))


def events():
    return (event(timestamp=100,wallet_weight=4,reputation_score=90,identity_id=W),
            event(timestamp=101,tx_hash='second',wallet=ROUTER,wallet_weight=4,
                  reputation_score=90,identity_id=ROUTER))


@pytest.mark.parametrize('mode', ['watch','blocked','sell','unverified'])
def test_research_events_are_recorded_without_telegram(db,tmp_path,mode):
    bot = make_bot(db,tmp_path)
    e = events()[0]
    if mode == 'blocked':
        report = bot.risk.inspect(T)
        report.hard_fail_reasons = ['insufficient_liquidity']
        bot.risk.inspect = lambda _: report
    if mode == 'sell': e.side = 'SELL'
    if mode == 'unverified': e.confirmation_eligible = False; e.trade_verified = False
    bot.handle_event(e)
    assert db.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0] == 1
    assert db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    assert db.event_processed(e)
    wire = transport()
    assert AlertOutbox(db,wire).flush() == 0
    wire.send.assert_not_called()


def test_validated_buy_one_message_not_signal_plus_paper_plus_proposal(db,tmp_path):
    bot = make_bot(db,tmp_path)
    first, second = events()
    bot.handle_event(first)
    bot.handle_event(second)
    assert db.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
    assert db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0] == 1
    rows = db.db.execute('SELECT kind,text FROM alert_outbox').fetchall()
    assert len(rows) == 1 and rows[0]['kind'] == 'BUY_OPPORTUNITY'
    assert T in rows[0]['text'] and 'achat' in rows[0]['text']
    wire = transport()
    assert AlertOutbox(db,wire).flush() == 1
    bot.handle_event(second)
    # Further eligible buyers on the same pending opportunity do not spam.
    bot.handle_event(event(timestamp=102,tx_hash='third',wallet=W,wallet_weight=4,
                           reputation_score=90,identity_id=W))
    restarted = make_bot(db,tmp_path)
    for prior in db.recent_runtime_events(0): restarted.clusters.add(prior)
    restarted.handle_event(event(timestamp=103,tx_hash='after-restart',wallet=W,
                                 wallet_weight=4,reputation_score=90,identity_id=W))
    assert AlertOutbox(db,wire).flush() == 0
    wire.send.assert_called_once()
    assert db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 4


def test_paper_exits_are_simulated_without_a_telegram_message(db,tmp_path):
    bot = make_bot(db,tmp_path)
    s = signal()
    assert bot.paper.maybe_open(s)
    fill = bot.paper._sell(T,s.risk.market,200,'paper_stop_loss','exit-test')
    assert fill and db.get_position(T)['status'] == 'CLOSED'
    wire = transport()
    assert AlertOutbox(db,wire).flush() == 0
    assert db.db.execute('SELECT state FROM alert_outbox').fetchone()[0] == 'SUPPRESSED'
    wire.send.assert_not_called()


def test_buy_notification_failure_rolls_back_proposal_and_paper(db,tmp_path,monkeypatch):
    bot = make_bot(db,tmp_path)
    first,second = events()
    bot.handle_event(first)
    original = db.enqueue
    monkeypatch.setattr(db,'enqueue',Mock(side_effect=RuntimeError('crash before queue commit')))
    with pytest.raises(RuntimeError): bot.handle_event(second)
    assert not db.event_processed(second)
    assert db.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
    assert db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0] == 0
    monkeypatch.setattr(db,'enqueue',original)
    bot.handle_event(second)
    assert db.event_processed(second)
    assert db.db.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0] == 1


def discovery(db):
    d = AutoDiscovery.__new__(AutoDiscovery); d.db=db; d.cfg=Settings()
    db.upsert_discovery_candidate(W,T,1)
    db.db.execute('UPDATE discovery_candidates SET first_discovered_at=1,hydrated_at=1,history_complete=1,source_winners=3')
    db.commit()
    p = dict(wallet=W,generated_at=1,smart_score=80,profile_class='SMART',recommended_weight=3,
             confirmation_eligible=True,launches_bought=10,opportunity_capture_rate=0.1,
             evaluable_trades=8,winners=3,profile_version=2,data_quality_ok=True,independent_winners=3)
    d.profile_candidate = Mock(return_value=p)
    d._funding_collision = lambda _: False
    d.write_generated_watchlist = Mock()
    return d


def test_new_wallet_promotion_notifies_once_and_preserves_gates(db):
    d = discovery(db)
    assert d.promote_candidates() == 1
    assert d.promote_candidates() == 0
    row = db.db.execute('SELECT * FROM alert_outbox').fetchone()
    assert row['id'] == 'wallet:'+W and row['kind'] == 'WALLET_PROMOTED'
    assert W in row['text'] and '3/8' in row['text']
    wire = transport()
    assert AlertOutbox(db,wire).flush() == 1
    # A later requalification of the same wallet is not a new discovery.
    db.db.execute("UPDATE discovery_candidates SET status='QUARANTINE'"); db.commit()
    assert d.promote_candidates() == 1
    assert AlertOutbox(db,wire).flush() == 0
    wire.send.assert_called_once()


@pytest.mark.parametrize('failure', ['score','history','identity'])
def test_unqualified_wallet_has_no_notification(db,failure):
    d = discovery(db)
    if failure == 'score': d.profile_candidate.return_value['smart_score'] = 1
    if failure == 'history':
        db.db.execute('UPDATE discovery_candidates SET history_complete=0'); db.commit()
    if failure == 'identity': d._funding_collision = lambda _: True
    assert d.promote_candidates() == 0
    assert db.db.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0] == 0


def test_promotion_and_notification_commit_together(db,monkeypatch):
    d = discovery(db)
    original = db.enqueue
    monkeypatch.setattr(db,'enqueue',Mock(side_effect=RuntimeError('crash')))
    with pytest.raises(RuntimeError): d.promote_candidates()
    assert db.db.execute('SELECT status FROM discovery_candidates').fetchone()[0] == 'QUARANTINE'
    assert db.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0] == 0
    monkeypatch.setattr(db,'enqueue',original)
    assert d.promote_candidates() == 1
    assert db.db.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0] == 1


def test_old_queue_migration_preserves_history_and_suppresses_pending_only(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as old:
        old.execute('''CREATE TABLE alert_outbox (
            id TEXT PRIMARY KEY,text TEXT NOT NULL,state TEXT NOT NULL,created_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL,last_error TEXT,message_id TEXT)''')
        for state in ('PENDING','SENT','UNKNOWN','SENDING','FAILED'):
            old.execute('INSERT INTO alert_outbox(id,text,state,created_at,next_attempt) VALUES(?,?,?,1,0)',
                        (state,'old '+state,state))
    storage = Storage(path)
    try:
        rows = {r['id']:dict(r) for r in storage.db.execute('SELECT * FROM alert_outbox')}
        assert rows['PENDING']['state'] == 'SUPPRESSED'
        assert rows['PENDING']['text'] == 'old PENDING'
        for state in ('SENT','UNKNOWN','SENDING','FAILED'): assert rows[state]['state'] == state
        wire = transport()
        out = AlertOutbox(storage,wire)
        out.recover()
        assert out.flush() == 0
        wire.send.assert_not_called()
        storage.enqueue('buy:new','fresh validated buy',kind='BUY_OPPORTUNITY')
    finally:
        storage.db.close()
    storage = Storage(path)
    try:
        assert storage.db.execute("SELECT state FROM alert_outbox WHERE id='buy:new'").fetchone()[0] == 'PENDING'
        assert AlertOutbox(storage,wire).flush() == 1
    finally:
        storage.db.close()


@pytest.mark.parametrize('kind', ['WATCH','BLOCKED','PAPER','INTERNAL','OTHER'])
def test_sender_uses_typed_policy_not_message_body(db,kind):
    db.enqueue('noise','BUY VALIDATED profitable wallet',kind=kind)
    # Even old/custom writers forcing PENDING cannot bypass the sender guard.
    db.db.execute("UPDATE alert_outbox SET state='PENDING' WHERE id='noise'"); db.commit()
    wire = transport()
    assert AlertOutbox(db,wire).flush() == 0
    wire.send.assert_not_called()
    assert db.db.execute("SELECT state FROM alert_outbox WHERE id='noise'").fetchone()[0] == 'SUPPRESSED'
