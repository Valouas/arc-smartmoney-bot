import errno
import json
import os
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import atomic_json
import historical_analysis as worker
from config import Settings
from historical_report import publish, ReportExportError, export_artifact
from wallet_ranking import export_csv


@pytest.mark.parametrize('fmt', ['csv', 'json'])
def test_export_permission_retries_preserve_previous_file(tmp_path, monkeypatch, fmt):
    path = tmp_path / ('report.'+fmt)
    path.write_text('previous')
    real_replace = os.replace
    calls = []
    def replace(src, dst):
        calls.append(1)
        assert path.read_text() == 'previous'
        if len(calls) < 3: raise PermissionError(errno.EACCES, 'secret URL must not appear')
        real_replace(src, dst)
    monkeypatch.setattr(atomic_json.os, 'replace', replace)
    monkeypatch.setattr(atomic_json.time, 'sleep', Mock())
    if fmt == 'csv': export_csv([{'wallet':'test'}], path)
    else: atomic_json.write_json(path, {'wallet':'test'})
    assert len(calls) == 3 and 'test' in path.read_text(encoding='utf-8-sig')
    assert not list(tmp_path.glob('*.tmp'))


def test_non_permission_failure_is_not_retried_or_truncated(tmp_path, monkeypatch):
    path = tmp_path/'report.json'; path.write_text('previous')
    replace = Mock(side_effect=OSError(errno.ENOSPC, 'disk full'))
    monkeypatch.setattr(atomic_json.os, 'replace', replace)
    with pytest.raises(OSError): atomic_json.write_json(path, {'new':True})
    assert replace.call_count == 1
    assert path.read_text() == 'previous'
    assert not list(tmp_path.glob('*.tmp'))


def test_csv_serialization_failure_does_not_publish_partial_file(tmp_path):
    path = tmp_path/'report.csv'; path.write_text('previous')
    with pytest.raises(ValueError): export_csv([{'wallet':'ok'}, {'wallet':{'bad':float('nan')}}], path)
    assert path.read_text() == 'previous'
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
@pytest.mark.parametrize('fmt', ['csv', 'json'])
def test_real_windows_reader_lock_preserves_export_then_recovers(tmp_path, fmt):
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k.CreateFileW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    path = tmp_path/('report.'+fmt); path.write_text('previous')
    handle = k.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    def write():
        if fmt == 'csv': export_csv([], path)
        else: atomic_json.write_json(path, {'complete':True})
    try:
        with pytest.raises(ReportExportError) as result: export_artifact(path, write)
        assert 'category=PermissionError' in result.value.safe_details
        assert path.read_text() == 'previous'
        assert not list(tmp_path.glob('*.tmp'))
    finally: k.CloseHandle(handle)
    write()
    assert path.read_text() != 'previous'


def setup_worker(monkeypatch):
    collector = Mock()
    db, rpc, arc = Mock(), Mock(), Mock()
    monkeypatch.setattr(worker, 'Storage', Mock(return_value=db))
    monkeypatch.setattr(worker, 'ArcRPC', Mock(return_value=rpc))
    monkeypatch.setattr(worker, 'ArcscanClient', Mock(return_value=arc))
    monkeypatch.setattr(worker, 'ProcessLock', lambda *args: nullcontext())
    monkeypatch.setattr(worker, 'HistoricalCollector', Mock(return_value=collector))
    # These tests isolate export recovery, independently of the queue strategy.
    monkeypatch.setattr(worker, 'PriorityCollector', Mock(return_value=collector))
    monkeypatch.setattr(worker, 'build_report', Mock(return_value=[]))
    return collector, db, rpc, arc


@pytest.mark.parametrize('enabled', [False, True])
def test_worker_selects_configured_queue_strategy(monkeypatch, enabled):
    from dataclasses import replace
    collector, *_ = setup_worker(monkeypatch)
    collector.cycle.return_value = {}
    monkeypatch.setattr(worker, 'SETTINGS', replace(Settings(), historical_priority_enabled=enabled))
    monkeypatch.setattr(worker, 'publish', Mock())
    assert worker.main(['--once']) == 0
    assert worker.PriorityCollector.call_count == int(enabled)
    collector.cycle.assert_called_once()


@pytest.mark.parametrize('artifact', ['historical_wallet_ranking.csv', 'historical_selection.json',
                                    'paper_review_selection.json', 'historical_wallet_ranking.json'])
def test_worker_keeps_collecting_and_retries_export_next_cycle(tmp_path, monkeypatch, caplog, artifact):
    publish([], Settings(), tmp_path, now=1)
    path = tmp_path/artifact; previous = path.read_bytes()
    collector, db, rpc, arc = setup_worker(monkeypatch)
    collector.cycle.side_effect = [{'wallet_steps':1}, {'wallet_steps':1}, KeyboardInterrupt()]
    real_replace = os.replace
    blocked = True
    attempts = []
    def replace(src, dst):
        if Path(dst).name == artifact and blocked:
            attempts.append(1)
            raise PermissionError(errno.EACCES, 'DO_NOT_LOG_SECRET', 'credential-bearing-path')
        real_replace(src, dst)
    def sleep(seconds):
        nonlocal blocked
        if seconds < 1: return  # Bounded replacement retries.
        if blocked:
            assert path.read_bytes() == previous  # Includes original expiry/generation fields.
            assert collector.cycle.call_count == 1
        blocked = False
    monkeypatch.setattr(atomic_json.os, 'replace', replace)
    monkeypatch.setattr(worker.time, 'sleep', sleep)
    assert worker.main(['--output-dir', str(tmp_path)]) == 0
    assert collector.cycle.call_count == 3
    assert len(attempts) == 3
    assert 'collection continues' in caplog.text and artifact in caplog.text
    assert 'DO_NOT_LOG_SECRET' not in caplog.text and 'credential-bearing-path' not in caplog.text
    assert json.loads((tmp_path/'historical_wallet_ranking.json').read_text())['generated_at'] > 1
    db.db.close.assert_called_once()
    rpc.w3.provider.session.close.assert_called_once()


def test_database_failure_is_still_fatal(monkeypatch):
    collector, *_ = setup_worker(monkeypatch)
    collector.cycle.return_value = {}
    worker.build_report.side_effect = sqlite3.OperationalError('database error')
    with pytest.raises(sqlite3.OperationalError): worker.main(['--once'])


def test_once_reports_unsuccessful_export(monkeypatch, tmp_path):
    collector, *_ = setup_worker(monkeypatch)
    collector.cycle.return_value = {}
    monkeypatch.setattr(atomic_json.os, 'replace', Mock(side_effect=PermissionError(errno.EACCES, 'locked')))
    monkeypatch.setattr(atomic_json.time, 'sleep', Mock())
    assert worker.main(['--once', '--output-dir', str(tmp_path)]) == 1
    assert collector.cycle.call_count == 1


def test_launcher_can_prepare_with_a_locked_historical_export(tmp_path, monkeypatch, capsys):
    import run_all
    import historical_report
    import paper_copy.selection
    import paper_copy.webhook
    import watchlist_manager
    monkeypatch.chdir(tmp_path)
    (tmp_path/'data/launcher_logs').mkdir(parents=True)
    with sqlite3.connect(tmp_path/'data/arc_bot.sqlite3'): pass
    monkeypatch.setattr(historical_report, 'build_report', Mock(return_value=[]))
    def fail(*args, **kwargs):
        def denied(): raise PermissionError(errno.EACCES, 'secret')
        export_artifact('historical_wallet_ranking.csv', denied)
    monkeypatch.setattr(historical_report, 'publish', fail)
    selected = Mock(return_value={})
    monkeypatch.setattr(paper_copy.selection, 'load_selection', selected)
    monkeypatch.setattr(paper_copy.webhook, 'watch_addresses', lambda cfg: set())
    monkeypatch.setattr(watchlist_manager, 'WatchlistManager', Mock())
    monkeypatch.setattr(watchlist_manager, 'effective_watchlist', lambda *args: [])
    run_all.prepare(Settings())
    selected.assert_called_once()
    assert 'Export historique differe' in capsys.readouterr().out
    assert json.loads((tmp_path/'data/alchemy_addresses.json').read_text()) == {'addresses':[]}
