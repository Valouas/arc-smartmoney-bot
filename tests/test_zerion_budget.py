from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from external_http import ExternalError, ExternalHTTP
from zerion_budget import ZerionBudget
from test_external_providers import Response


def test_seed_persists_never_decreases_and_utc_reset(tmp_path):
    now=[86399]
    b=ZerionBudget(tmp_path/'budget','SECRET',3,clock=lambda:now[0])
    b.seed_at_least(2);b.seed_at_least(1);b.reserve()
    other=ZerionBudget(tmp_path/'budget','SECRET',3,clock=lambda:now[0])
    assert other.used()==3
    with pytest.raises(ExternalError) as exc:other.reserve()
    assert exc.value.category=='daily_request_budget' and exc.value.retry_after==1
    now[0]=86400
    b.reserve();assert b.used()==1
    assert b'SECRET' not in (tmp_path/'budget').read_bytes()


def test_concurrent_budget_never_exceeds_limit(tmp_path):
    path=tmp_path/'budget'
    b=ZerionBudget(path,'SECRET',7)
    def attempt(_):
        try:b.reserve();return 1
        except ExternalError:return 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt,range(20)))==7
    assert b.used()==7


def test_retry_counts_and_exhaustion_never_calls_transport(tmp_path):
    b=ZerionBudget(tmp_path/'budget','SECRET',2)
    session=Mock();session.get.side_effect=[Response(status=502),Response({'ok':1})]
    http=ExternalHTTP('zerion','SECRET',session,sleep=Mock(),before_request=b.reserve)
    assert http.get('chains/')=={'ok':1} and b.used()==2
    with pytest.raises(ExternalError,match='daily_request_budget'):http.get('chains/')
    assert session.get.call_count==2


def test_transport_failure_still_consumes_reservation(tmp_path):
    import requests
    b=ZerionBudget(tmp_path/'budget','SECRET',2)
    session=Mock();session.get.side_effect=requests.Timeout()
    http=ExternalHTTP('zerion','SECRET',session,sleep=Mock(),before_request=b.reserve)
    with pytest.raises(ExternalError,match='daily_request_budget'):http.get('chains/')
    assert session.get.call_count==2 and b.used()==2
