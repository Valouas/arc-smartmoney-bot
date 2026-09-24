import json
import sqlite3
import pytest
from copy_raw_proofs import validate_plan,RawProofBudget
from copy_holding_pilot import PilotLimit

W='0x'+'1'*40
H='0x'+'2'*64

def fixture():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE historical_transactions(wallet,tx_hash,payload)')
    db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(W,H,json.dumps(dict(block=100,tx_hash=H))))
    return [dict(wallets=[W],method='eth_getTransactionReceipt',tx_hash=H,block=100)],db

def test_stored_authorized_proof_only():
    plan,db=fixture()
    assert validate_plan(plan,db,[W],[])==plan

@pytest.mark.parametrize('change',[
    lambda p:p[0].update(wallets=['0x'+'3'*40]),
    lambda p:p[0].update(block=101),
    lambda p:p[0].update(method='eth_getLogs'),
    lambda p:p[0].update(method='eth_sendRawTransaction'),
    lambda p:p[0].update(tx_hash='0x'+'4'*64),
    lambda p:p.append(dict(p[0])),
    lambda p:p[0].update(wallets=[]),
])
def test_outside_scope_rejected_before_network(change):
    plan,db=fixture();change(plan)
    with pytest.raises(ValueError):validate_plan(plan,db,[W],[])

def test_no_refetch_when_cached(monkeypatch):
    plan,db=fixture()
    monkeypatch.setattr('copy_raw_proofs.cached',lambda *a:{'hash':H})
    with pytest.raises(ValueError,match='already_available'):validate_plan(plan,db,[W],[])

def test_bounded_methods_and_attempts():
    b=object.__new__(RawProofBudget);b.raw_attempts=450
    with pytest.raises(PilotLimit):b.take_paid('eth_getTransactionReceipt')
    b.raw_attempts=0
    with pytest.raises(PilotLimit):b.take_paid('eth_getLogs')
