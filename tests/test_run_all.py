import io
import json
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import run_all
from run_v4 import commands_for


@pytest.fixture
def local(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_all,'ROOT',tmp_path)
    return tmp_path


@pytest.fixture
def cfg():
    return SimpleNamespace(historical_analysis_enabled=True,alchemy_webhook_enabled=True,
                           alchemy_webhook_host='127.0.0.1',alchemy_webhook_port=8000)


@pytest.mark.parametrize('history,webhook',[(False,False),(True,False),(False,True),(True,True)])
def test_roles_match_v4_plus_separate_paper(cfg,history,webhook):
    cfg.historical_analysis_enabled=history; cfg.alchemy_webhook_enabled=webhook
    assert run_all.roles_for(cfg)==[c[-1].removesuffix('.py') for c in commands_for(cfg)]+['paper_copy','dashboard']


def test_existing_manual_worker_prevents_any_new_start(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'busy',lambda _:True)
    with pytest.raises(RuntimeError,match='ancienne instance'): run_all.preflight(cfg)


def test_duplicate_launcher_does_not_spawn(local,monkeypatch):
    monkeypatch.setattr(run_all,'busy',lambda _:True)
    launch=Mock(); monkeypatch.setattr(run_all,'launch',launch)
    assert run_all.main([])==0
    launch.assert_not_called()


def test_existing_tunnel_reused_and_not_stopped(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    monkeypatch.setattr(run_all,'tunnel',lambda _: 'https://example.ngrok.app/webhook/alchemy')
    children=[]
    def launch(command,path):
        p=Mock(pid=100+len(children)); p.poll.return_value=None
        children.append((command,p)); return p
    stop=Mock()
    monkeypatch.setattr(run_all,'launch',launch)
    monkeypatch.setattr(run_all,'shutdown',stop)
    monkeypatch.setattr(run_all.time,'sleep',lambda _:run_all.STOP.write_text('stop'))
    run_all.supervise(cfg)
    assert len(children)==8 and all('--worker' in c for c,_ in children)
    assert set(stop.call_args.args[0])==set(run_all.roles_for(cfg))
    state=json.loads(run_all.STATE.read_text())
    assert state['status']=='STOPPED' and state['webhook_url'].startswith('https://example.')
    assert not run_all.STOP.exists()


def test_start_failure_shuts_down_only_already_created_children(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    p=Mock(pid=1); p.poll.return_value=None
    monkeypatch.setattr(run_all,'launch',Mock(side_effect=[p,RuntimeError('start failed')]))
    stop=Mock(); monkeypatch.setattr(run_all,'shutdown',stop)
    with pytest.raises(RuntimeError): run_all.supervise(cfg)
    assert stop.call_args.args[0]=={'launch_indexer':p}


@pytest.mark.parametrize('failed_role',['alchemy_webhook','dashboard'])
def test_tunnel_or_receiver_failure_keeps_scanners(local,cfg,monkeypatch,capsys,failed_role):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    monkeypatch.setattr(run_all,'tunnel',lambda _:None)
    monkeypatch.setattr(run_all.shutil,'which',lambda _:None)
    def launch(command,path):
        p=Mock(pid=1); p.poll.return_value=1 if command[-1]==failed_role else None
        return p
    monkeypatch.setattr(run_all,'launch',launch)
    monkeypatch.setattr(run_all,'shutdown',Mock())
    monkeypatch.setattr(run_all.time,'sleep',lambda _:run_all.STOP.write_text('stop'))
    run_all.supervise(cfg)
    assert 'scanners RPC maintenus' in capsys.readouterr().out


def test_core_worker_failure_is_visible_and_stops_owned_children(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    monkeypatch.setattr(run_all,'tunnel',lambda _: 'https://example.ngrok.app/webhook/alchemy')
    def launch(command,path):
        p=Mock(pid=1); p.poll.return_value=1 if command[-1]=='paper_copy' else None
        return p
    stop=Mock()
    monkeypatch.setattr(run_all,'launch',launch); monkeypatch.setattr(run_all,'shutdown',stop)
    with pytest.raises(RuntimeError,match='paper_copy'): run_all.supervise(cfg)
    assert 'paper_copy' in stop.call_args.args[0]


def test_worker_restores_module_arguments_and_handles_interrupt(local,monkeypatch):
    monkeypatch.setattr(run_all.signal,'signal',Mock())
    monkeypatch.setattr(run_all.sys,'argv',['run_all.py','--worker','paper_copy'])
    runner=Mock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(run_all.runpy,'run_module',runner)
    run_all.worker('paper_copy')
    assert run_all.sys.argv==['paper_copy']
    runner.assert_called_once_with('paper_copy',run_name='__main__')
    assert not run_all.busy('data/paper_copy.worker.lock')


def test_stop_without_active_launcher_cannot_kill_other_processes(local,monkeypatch):
    monkeypatch.setattr(run_all,'busy',lambda _:False)
    stop=Mock(); monkeypatch.setattr(run_all,'shutdown',stop)
    assert run_all.main(['--stop'])==0
    stop.assert_not_called()
    assert not run_all.STOP.exists()


@pytest.mark.parametrize('upstream,expected',[
    ('http://localhost:8000',True),('http://127.0.0.1:8000',True),
    ('http://localhost:9000',False),('http://other-host:8000',False)])
def test_reuse_only_tunnel_pointing_at_our_local_port(monkeypatch,upstream,expected):
    body=json.dumps({'tunnels':[{'public_url':'https://test.ngrok.app','config':{'addr':upstream}}]}).encode()
    opener=Mock(); opener.open.return_value=io.BytesIO(body)
    monkeypatch.setattr(run_all.urllib.request,'build_opener',lambda *_:opener)
    assert bool(run_all.tunnel(8000)) is expected


def test_graceful_shutdown_does_not_terminate_exited_or_unowned_processes(monkeypatch):
    p=Mock(); p.poll.side_effect=[None,0,0]
    ended=Mock(); ended.poll.return_value=0
    run_all.shutdown({'bot':p,'finished':ended},grace=0)
    p.send_signal.assert_called_once()
    ended.send_signal.assert_not_called()
    p.terminate.assert_not_called(); p.kill.assert_not_called()
