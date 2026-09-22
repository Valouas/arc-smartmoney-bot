import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import automatic_backup as backup
import run_all


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('data').mkdir()
    cfg=SimpleNamespace(automatic_backup_enabled=True,automatic_backup_directory='data/automatic_backups',
                        automatic_backup_keep_all=False,
                        automatic_backup_keep_last=2,automatic_backup_keep_daily=2,
                        automatic_backup_timeout_seconds=10,automatic_backup_reserve_mb=0,
                        automatic_backup_interval_seconds=3600,automatic_backup_max_age_seconds=10800)
    main=sqlite3.connect('data/arc_bot.sqlite3')
    main.execute('PRAGMA journal_mode=WAL')
    main.executescript('CREATE TABLE events(id PRIMARY KEY,value); INSERT INTO events VALUES(1,42);')
    main.commit()
    yield cfg,main
    main.close()


def test_backup_includes_committed_wal_excludes_uncommitted_and_restores(setup,tmp_path):
    cfg,live=setup
    live.execute('INSERT INTO events VALUES(2,84)');live.commit()
    live.execute('INSERT INTO events VALUES(3,126)')
    generation=backup.create_backup(cfg)
    backup.verify_backup(generation)
    restored=tmp_path/'restored.sqlite3'
    shutil.copy2(generation/'arc_bot.sqlite3',restored)
    with sqlite3.connect(restored) as db:
        assert list(db.execute('SELECT * FROM events ORDER BY id'))==[(1,42),(2,84)]
        db.execute('INSERT INTO events VALUES(4,168)');db.commit()
    assert live.execute('SELECT COUNT(*) FROM events').fetchone()[0]==3
    assert not (generation/'arc_bot.sqlite3-wal').exists()
    live.rollback()


def test_failed_copy_does_not_publish_or_prune_previous_good_backup(setup,monkeypatch):
    cfg,_=setup
    first=backup.create_backup(cfg)
    original=backup.digest(first/'arc_bot.sqlite3')
    def fail(source,target,timeout):
        target.write_bytes(b'incomplete')
        raise TimeoutError('private transport details')
    monkeypatch.setattr(backup,'backup_database',fail)
    with pytest.raises(TimeoutError):backup.create_backup(cfg)
    assert backup.digest(first/'arc_bot.sqlite3')==original
    assert len(backup.generations(cfg.automatic_backup_directory))==1
    assert not list(Path(cfg.automatic_backup_directory).glob('.partial_*'))


def test_disk_full_preserves_existing_backups(setup,monkeypatch):
    cfg,_=setup
    first=backup.create_backup(cfg)
    monkeypatch.setattr(backup.shutil,'disk_usage',lambda _:SimpleNamespace(free=0))
    with pytest.raises(OSError):backup.create_backup(cfg)
    assert first.exists()
    backup.verify_backup(first)


def test_retention_keeps_recent_and_daily_and_never_removes_user_evidence(setup):
    cfg,_=setup
    root=Path(cfg.automatic_backup_directory)
    evidence=root/'backup_user_evidence';evidence.mkdir(parents=True);(evidence/'precious.txt').write_text('keep')
    forensic=Path('data/recovery_forensics');forensic.mkdir();(forensic/'old.sqlite3').write_bytes(b'keep')
    times=[86400,86410,172800,172810,259200,259210]
    made=[backup.create_backup(cfg,now=t) for t in times]
    kept={p for p,_ in backup.generations(root)}
    assert kept=={made[3],made[4],made[5]}
    assert (evidence/'precious.txt').read_text()=='keep'
    assert (forensic/'old.sqlite3').read_bytes()==b'keep'


def test_keep_all_never_prunes_any_verified_generation(setup):
    cfg,_=setup;cfg.automatic_backup_keep_all=True
    made=[backup.create_backup(cfg,now=t*86400) for t in range(1,8)]
    assert {p for p,_ in backup.generations(cfg.automatic_backup_directory)}==set(made)


def test_backup_copies_evidence_blobs_and_detects_missing_or_corrupt_copy(setup,tmp_path):
    from data_preservation import archive_file
    cfg,_=setup;cfg.automatic_backup_keep_all=True
    source=Path('data/provider.json');source.write_text('{"wallets":12}')
    key=archive_file(source,root=tmp_path)
    generation=backup.create_backup(cfg)
    manifest=backup.verify_backup(generation)
    assert manifest['evidence_blobs']==1 and 'catalog.sqlite3' in manifest['files']
    original=Path('data/evidence_archive/blobs')/(key+'.gz')
    copied=Path(cfg.automatic_backup_directory)/'evidence_blobs'/(key+'.gz')
    assert copied.read_bytes()==original.read_bytes()
    copied.write_bytes(b'corrupt')
    with pytest.raises((ValueError,OSError)):backup.verify_backup(generation)


def test_extra_user_file_prevents_retention_delete(setup):
    cfg,_=setup
    first=backup.create_backup(cfg,now=1)
    (first/'user_notes.txt').write_text('do not delete')
    for t in [86400,172800,259200,345600]:backup.create_backup(cfg,now=t)
    assert (first/'arc_bot.sqlite3').exists()
    assert (first/'user_notes.txt').read_text()=='do not delete'


def test_corrupt_archive_detected_before_restore(setup):
    cfg,_=setup
    first=backup.create_backup(cfg)
    path=first/'arc_bot.sqlite3'
    original=path.read_bytes();path.write_bytes(bytes(len(original)))
    with pytest.raises(ValueError,match='checksum'):backup.verify_backup(first)


def test_new_paper_database_triggers_backup_before_interval(setup):
    cfg,_=setup
    backup.create_backup(cfg,now=100)
    assert not backup.due(cfg,101)
    with sqlite3.connect('data/paper_copy.sqlite3') as db:db.execute('CREATE TABLE paper(id)')
    assert backup.due(cfg,101)
    fresh=backup.create_backup(cfg,now=102)
    assert set(backup.verify_backup(fresh)['files'])=={'arc_bot.sqlite3','paper_copy.sqlite3'}


def test_no_source_is_not_created_as_empty_database(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg=SimpleNamespace(automatic_backup_directory='backups')
    assert backup.create_backup(cfg) is None
    assert not Path('data/arc_bot.sqlite3').exists()


def test_missing_previously_protected_database_fails_closed(setup):
    cfg,live=setup
    first=backup.create_backup(cfg)
    live.close()
    Path('data/arc_bot.sqlite3').unlink()
    assert backup.due(cfg,0)
    with pytest.raises(FileNotFoundError):backup.create_backup(cfg)
    backup.verify_backup(first)


def test_supervisor_never_starts_rpc_workers_when_startup_backup_fails(setup,monkeypatch):
    cfg,_=setup
    monkeypatch.setattr(run_all,'preflight',lambda _:None)
    monkeypatch.setattr(backup,'create_backup',Mock(side_effect=OSError('failed')))
    launch=Mock();prepare=Mock()
    monkeypatch.setattr(run_all,'launch',launch);monkeypatch.setattr(run_all,'prepare',prepare)
    with pytest.raises(OSError):run_all.supervise(cfg)
    launch.assert_not_called();prepare.assert_not_called()


def test_backup_worker_is_supervised_as_core_role():
    cfg=SimpleNamespace(automatic_backup_enabled=True,historical_analysis_enabled=False,alchemy_webhook_enabled=False)
    assert 'automatic_backup' in run_all.roles_for(cfg)


def test_stale_backup_failure_exits_worker_and_logs_only_category(setup,monkeypatch,capsys):
    from functools import partial
    import data_preservation
    cfg,_=setup
    # These functions bind the repository root at import time, not cwd. Keep
    # this failure test inside its temporary fixture, never the user's archive.
    monkeypatch.setattr(data_preservation,'install_all',partial(data_preservation.install_all,root=Path.cwd()))
    monkeypatch.setattr(data_preservation,'archive_existing',partial(data_preservation.archive_existing,root=Path.cwd()))
    backup.create_backup(cfg,now=1)
    monkeypatch.setattr('config.SETTINGS',cfg)
    monkeypatch.setattr(backup.time,'time',lambda:20000)
    monkeypatch.setattr(backup,'create_backup',Mock(side_effect=OSError('SECRET')))
    monkeypatch.setattr('sys.argv',['automatic_backup.py'])
    with pytest.raises(RuntimeError,match='Protection sauvegarde'):backup.main()
    assert 'SECRET' not in capsys.readouterr().out
    assert json.loads(backup.STATUS.read_text())['status']=='FAILED'
