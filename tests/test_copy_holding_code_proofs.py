import json
import sqlite3
from types import SimpleNamespace
import pytest

from copy_holding_code_proofs import validate_plan, ProofBudget
from copy_holding_pilot import PilotLimit

W='0x'+'1'*40
E='0x'+'2'*40
H='0x'+'3'*64


def fixture():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE historical_transactions(wallet,tx_hash,payload)')
    db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(W,H,json.dumps(dict(block=123,error='ambiguous'))))
    plan=[dict(wallet=W,tx_hash=H,method='eth_getCode',params=[E,hex(123)])]
    return plan,db,dict(paid_rpc=dict(wallets=[W])),SimpleNamespace(entries={E:'hash'})


def test_only_exact_stored_failed_transaction_proof():
    plan,db,settings,registry=fixture()
    assert validate_plan(plan,db,settings,registry)==plan


@pytest.mark.parametrize('change',[
    lambda p:p[0].update(wallet='0x'+'4'*40),
    lambda p:p[0].update(method='eth_sendRawTransaction'),
    lambda p:p[0].update(params=[E,'latest']),
    lambda p:p[0].update(params=[E,hex(124)]),
    lambda p:p[0].update(params=['0x'+'4'*40,hex(123)]),
    lambda p:p.append(dict(p[0])),
])
def test_reject_out_of_scope_and_duplicate(change):
    plan,db,settings,registry=fixture();change(plan)
    with pytest.raises(ValueError):validate_plan(plan,db,settings,registry)


def test_attempt_ceiling_and_read_method_before_reservation():
    budget=object.__new__(ProofBudget);budget.proof_attempts=20
    with pytest.raises(PilotLimit):budget.take_paid('eth_getCode')
    budget.proof_attempts=0
    with pytest.raises(PilotLimit):budget.take_paid('eth_getLogs')
