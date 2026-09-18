import io
import json
import os
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


def events():
    return [json.loads(line) for line in (run_all.LOGS/'supervisor.jsonl').read_text().splitlines()]


def test_state_write_retries_transient_lock_without_changing_atomic_writer(local,monkeypatch):
    write=Mock(side_effect=[PermissionError('secret URL'),PermissionError('secret URL'),None])
    sleep=Mock()
    monkeypatch.setattr(run_all,'save_state',write)
    monkeypatch.setattr(run_all.time,'sleep',sleep)
    publisher=run_all.StatePublisher()
    assert publisher.publish({},None,'RUNNING')
    assert write.call_count==3 and sleep.call_count==2
    assert not publisher.degraded


def test_long_state_lock_is_nonfatal_throttled_and_recovers(local,monkeypatch):
    original=run_all.save_state
    monkeypatch.setattr(run_all,'save_state',Mock(side_effect=PermissionError('alch_SECRET https://secret.example/key')))
    monkeypatch.setattr(run_all.time,'sleep',lambda _:None)
    monkeypatch.setattr(run_all.time,'monotonic',lambda:100)
    publisher=run_all.StatePublisher()
    assert not publisher.publish({},None,'RUNNING')
    assert not publisher.publish({},None,'RUNNING')
    failures=[e for e in events() if e['event']=='state_write_failed']
    assert len(failures)==1 and failures[0]['category']=='PermissionError'
    assert 'SECRET' not in json.dumps(events()) and 'secret.example' not in json.dumps(events())
    monkeypatch.setattr(run_all,'save_state',original)
    assert publisher.publish({},None,'RUNNING')
    assert not publisher.degraded
    assert events()[-1]['event']=='state_write_recovered'


def test_heartbeat_failure_does_not_shutdown_workers_before_explicit_stop(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    monkeypatch.setattr(run_all,'tunnel',lambda _: 'https://example.ngrok.app/webhook/alchemy')
    def launch(*_):
        p=Mock(pid=123); p.poll.return_value=None; return p
    monkeypatch.setattr(run_all,'launch',launch)
    monkeypatch.setattr(run_all,'save_state',Mock(side_effect=PermissionError('locked')))
    stop=Mock(); monkeypatch.setattr(run_all,'shutdown',stop)
    cycles=[]
    def sleep(delay):
        if delay==1:
            stop.assert_not_called()
            cycles.append(1)
            if len(cycles)==3: run_all.STOP.write_text('stop')
    monkeypatch.setattr(run_all.time,'sleep',sleep)
    run_all.supervise(cfg)
    assert len(cycles)==3
    stop.assert_called_once()
    assert events()[-1]['event']=='supervisor_stopped'
    assert events()[-1]['reason']=='stop_file'
    assert not run_all.STOP.exists()


def test_core_exit_reason_is_preserved_when_final_state_cannot_be_written(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    monkeypatch.setattr(run_all,'tunnel',lambda _: 'https://example.ngrok.app/webhook/alchemy')
    def launch(command,path):
        p=Mock(pid=123); p.poll.return_value=2 if command[-1]=='paper_copy' else None; return p
    monkeypatch.setattr(run_all,'launch',launch)
    monkeypatch.setattr(run_all,'shutdown',Mock())
    monkeypatch.setattr(run_all,'save_state',Mock(side_effect=PermissionError('locked')))
    monkeypatch.setattr(run_all.time,'sleep',lambda _:None)
    with pytest.raises(RuntimeError,match='paper_copy est arrete'):
        run_all.supervise(cfg)
    exited=[e for e in events() if e['event']=='child_exited']
    assert exited[0]['role']=='paper_copy' and exited[0]['exit_code']==2
    assert events()[-1]['reason']=='core_worker_exit'


def test_keyboard_interrupt_is_journaled_separately(local,cfg,monkeypatch):
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(run_all,'prepare',lambda _:None)
    monkeypatch.setattr(run_all,'launch',Mock(side_effect=KeyboardInterrupt()))
    monkeypatch.setattr(run_all,'shutdown',Mock())
    run_all.supervise(cfg)
    assert events()[-1]['reason']=='keyboard_interrupt'


def test_failed_journal_is_never_fatal(local,monkeypatch,capsys):
    monkeypatch.setattr(run_all.Path,'open',Mock(side_effect=PermissionError('sensitive')))
    run_all.record_event('supervisor_start')
    assert 'Journal superviseur indisponible' in capsys.readouterr().out


@pytest.mark.skipif(os.name!='nt',reason='Windows file sharing semantics')
def test_actual_windows_reader_lock_preserves_json_and_recovers(local,monkeypatch):
    run_all.save_state({},None,'RUNNING')
    before=run_all.STATE.read_bytes()
    monkeypatch.setattr(run_all.time,'sleep',lambda _:None)
    publisher=run_all.StatePublisher()
    with run_all.STATE.open('r'):
        assert not publisher.publish({},None,'STOPPED')
        assert run_all.STATE.read_bytes()==before
        assert not list(run_all.STATE.parent.glob('run_all_state.json.*.tmp'))
    assert publisher.publish({},None,'STOPPED')
    assert json.loads(run_all.STATE.read_text())['status']=='STOPPED'
