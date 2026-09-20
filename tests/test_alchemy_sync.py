from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from alchemy_sync import NotifyClient, SyncError
from config import Settings

W='0x'+'1'*40
W2='0x'+'2'*40
W3='0x'+'3'*40


@pytest.fixture
def cfg():
    return replace(Settings(),alchemy_notify_auth_token='test-auth-secret',
        alchemy_webhook_id='wh_test',alchemy_webhook_expected_url='https://example.test/webhook/alchemy',
        alchemy_webhook_signing_key='test-signing-secret',alchemy_sync_enabled=True)


def hook(cfg,**changes):
    result={'id':cfg.alchemy_webhook_id,'webhook_url':cfg.alchemy_webhook_expected_url,
            'network':'ARC_MAINNET','webhook_type':'ADDRESS_ACTIVITY','is_active':True,
            'signing_key':cfg.alchemy_webhook_signing_key}
    result.update(changes)
    return {'data':[result]}


def page(values,total=None,after=None):
    return {'data':values,'pagination':{'total_count':len(values) if total is None else total,
            'cursors':{} if after is None else {'after':after}}}


def client(cfg,*responses):
    session=Mock()
    session.request.side_effect=[Mock(status_code=200,json=Mock(return_value=r)) for r in responses]
    return NotifyClient(cfg,session)


def test_adds_only_missing_and_preserves_existing_manual_addresses(cfg):
    c=client(cfg,hook(cfg),page([W,W3]),{},page([W,W2,W3]))
    assert c.sync({W,W2})['added_count']==1
    calls=c.session.request.call_args_list
    assert calls[2].args==('PATCH','https://dashboard.alchemy.com/api/update-webhook-addresses')
    assert calls[2].kwargs['json']=={'webhook_id':'wh_test','addresses_to_add':[W2],'addresses_to_remove':[]}
    assert all(x.kwargs['allow_redirects'] is False and x.kwargs['timeout']==15 for x in calls)


def test_repeated_sync_and_empty_selection_do_not_remove_or_readd(cfg):
    c=client(cfg,hook(cfg),page([W,W2]),hook(cfg),page([W,W2]))
    assert c.sync({W})['added_count']==0
    assert c.sync(set())['added_count']==0
    assert all(x.args[0]=='GET' for x in c.session.request.call_args_list)


@pytest.mark.parametrize('changes',[{'network':'ETH_MAINNET'},{'webhook_type':'GRAPHQL'},
    {'is_active':False},{'webhook_url':'https://other.test'},{'signing_key':'wrong'}, {'id':'wh_other'}])
def test_wrong_webhook_refused_before_any_mutation(cfg,changes):
    c=client(cfg,hook(cfg,**changes))
    with pytest.raises(SyncError): c.sync({W})
    assert c.session.request.call_count==1


def test_all_address_pages_are_read_before_patch(cfg):
    c=client(cfg,hook(cfg),page([W],2,'second'),page([W2],2))
    assert c.sync({W,W2})['remote_count']==2
    assert c.session.request.call_args_list[2].kwargs['params']['after']=='second'


@pytest.mark.parametrize('responses',[
    [page([W],2)],
    [page([W],3,'same'),page([W2],3,'same')],
    [page([W],2,'next'),page([W],2)],
    [page([W],2,'next'),page([W2],3)],
    [{'data':['invalid'],'pagination':{'total_count':1,'cursors':{}}}],
    [{'data':[W]}],
])
def test_incomplete_ambiguous_or_cyclic_list_never_mutates(cfg,responses):
    c=client(cfg,hook(cfg),*responses)
    with pytest.raises(SyncError): c.sync({W3})
    assert all(x.args[0]=='GET' for x in c.session.request.call_args_list)


def test_patch_success_is_not_enough_without_readback(cfg):
    c=client(cfg,hook(cfg),page([]),{},page([]))
    with pytest.raises(SyncError,match='verification_incomplete'): c.sync({W})


@pytest.mark.parametrize('failure',[401,429,500,302,'timeout'])
def test_transport_failure_never_leaks_credentials(cfg,failure):
    c=NotifyClient(cfg,Mock())
    if failure=='timeout': c.session.request.side_effect=requests.Timeout('test-auth-secret body url')
    else: c.session.request.return_value=Mock(status_code=failure,text='test-auth-secret')
    with pytest.raises(SyncError) as exc:c.sync({W})
    assert 'secret' not in str(exc.value)
    assert c.session.request.call_count==1


def test_disabled_configuration_and_secrets_hidden(cfg):
    assert 'test-auth-secret' not in repr(cfg) and 'test-signing-secret' not in repr(cfg)
    c=NotifyClient(replace(cfg,alchemy_notify_auth_token=''),Mock())
    with pytest.raises(SyncError,match='missing_configuration'):c.sync({W})
    c.session.request.assert_not_called()


def test_sync_role_is_optional_and_requires_receiver(cfg):
    from run_all import roles_for
    assert 'alchemy_sync' in roles_for(replace(cfg,alchemy_webhook_enabled=True))
    assert 'alchemy_sync' not in roles_for(replace(cfg,alchemy_webhook_enabled=False))
    assert 'alchemy_sync' not in roles_for(replace(cfg,alchemy_webhook_enabled=True,alchemy_sync_enabled=False))
