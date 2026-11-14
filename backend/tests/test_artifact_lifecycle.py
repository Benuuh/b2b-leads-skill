"""Deletion intents commit with revocation; failed unlink remains recoverable."""
from datetime import timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, object_session

from app import persistence, runs
from app.persistence.models import Company, Export, PendingArtifactDelete, Snapshot, Suppression


def seed_files():
    runs.create('file-lifecycle', {'spec_version': 1, 'name': 'Lifecycle fixture', 'countries': ['US'], 'industry': 'freight'})
    key, checksum = persistence.put(b'fixture workbook', 'xlsx')
    report, report_hash = persistence.put(b'{"fixture":true}', 'json')
    raw, raw_hash = persistence.put(b'fixture original page', 'html')
    with persistence.transaction() as session:
        session.add(Company(id='file-company', run_id='file-lifecycle', identity_key='fixture',
            data_json={}, expires_at=persistence.utcnow() + timedelta(days=30)))
        session.flush()
        session.add(Snapshot(id='file-snapshot', run_id='file-lifecycle', company_id='file-company',
            source_url='https://directory.example.org/synthetic', artifact_ref=raw, artifact_hash=raw_hash,
            expires_at=persistence.utcnow() + timedelta(days=30)))
        session.add(Export(id='file-export', run_id='file-lifecycle', format='xlsx', filename='fixture.xlsx',
            artifact_ref=key, artifact_hash=checksum, report_ref=report, report_hash=report_hash,
            manifest_json={'fixture': True}, expires_at=persistence.utcnow() + timedelta(days=30)))
    return key, report, raw


def pending():
    with persistence.transaction() as session:
        return list(session.scalars(select(PendingArtifactDelete)))


def test_failed_revocation_transaction_preserves_original_files(clean_db, monkeypatch):
    keys = seed_files()
    def fail_after_queued(row):
        # A real PostgreSQL error after revocation/outbox inserts forces the
        # transaction to roll back before it could perform any unlink.
        object_session(row).execute(text('SELECT 1 / 0'))
    # S10 拆包后 runs 是包：suppression_add 在 app.runs.state 里调用该辅助函数，
    # 补丁必须打到实际持有绑定的子模块。
    import app.runs.state as runs_state
    monkeypatch.setattr(runs_state, '_suppression_view', fail_after_queued)
    with pytest.raises(DBAPIError):
        runs.suppression_add('domain', 'example.org')
    assert all(persistence.artifact_path(key).is_file() for key in keys)
    assert not pending()
    with persistence.transaction() as session:
        export = session.get(Export, 'file-export')
        assert export.artifact_ref == keys[0] and export.revoked_at is None
        assert session.scalar(select(Suppression)) is None


def test_failed_company_delete_transaction_keeps_snapshots(clean_db, monkeypatch):
    keys = seed_files()
    def fail_audit(*args, **kwargs):
        raise RuntimeError('failure before commit')
    import app.runs.state as runs_state
    monkeypatch.setattr(runs_state, '_audit', fail_audit)
    with pytest.raises(RuntimeError, match='before commit'):
        runs.delete_company('file-company')
    assert all(persistence.artifact_path(key).is_file() for key in keys)
    assert not pending()
    with persistence.transaction() as session:
        assert session.get(Company, 'file-company')
        assert session.get(Snapshot, 'file-snapshot').artifact_ref == keys[2]
        assert session.get(Export, 'file-export').revoked_at is None


def test_committed_revocation_recovers_after_unlink_failure(clean_db, monkeypatch):
    keys = seed_files()
    original_unlink = persistence.unlink
    def locked_file(key):
        raise PermissionError('simulated open workbook')
    monkeypatch.setattr(persistence, 'unlink', locked_file)
    result = runs.suppression_add('domain', 'example.org')
    assert result['id']
    assert all(persistence.artifact_path(key).is_file() for key in keys)
    with persistence.transaction() as session:
        export = session.get(Export, 'file-export')
        assert export.revoked_at is not None and export.artifact_ref is None
    deferred = pending()
    assert {row.artifact_ref for row in deferred} == set(keys[:2])
    assert all(row.attempts >= 1 and row.last_error == 'PermissionError' for row in deferred)
    monkeypatch.setattr(persistence, 'unlink', original_unlink)
    # A later applied command retries the durable intents, including after an
    # intervening process exit. The unrelated raw page remains referenced.
    runs.maintenance(apply=True, periodic=True)
    assert not pending()
    assert all(not persistence.artifact_path(key).exists() for key in keys[:2])
    assert persistence.artifact_path(keys[2]).is_file()


def test_company_delete_retries_all_revoked_files(clean_db, monkeypatch):
    keys = seed_files()
    original_unlink = persistence.unlink
    monkeypatch.setattr(persistence, 'unlink', lambda key: (_ for _ in ()).throw(OSError('simulated disk error')))
    assert runs.delete_company('file-company')['exports_revoked']
    assert {row.artifact_ref for row in pending()} == set(keys)
    monkeypatch.setattr(persistence, 'unlink', original_unlink)
    assert persistence.drain_pending_deletes()['deleted'] == 3
    assert not pending()
    assert all(not persistence.artifact_path(key).exists() for key in keys)


def test_pending_delete_never_unlinks_still_referenced_file(clean_db):
    key, _, _ = seed_files()
    with persistence.transaction() as session:
        persistence.queue_artifact_delete(session, key)
    assert persistence.artifact_path(key).is_file()
    assert {row.artifact_ref for row in pending()} == {key}


def test_unlink_then_failed_outbox_commit_is_safe_to_retry(clean_db):
    keys = seed_files()
    def fail_outbox_commit(session):
        if any(isinstance(row, PendingArtifactDelete) for row in session.deleted):
            raise RuntimeError('outbox commit unavailable')
    event.listen(Session, 'before_commit', fail_outbox_commit)
    try:
        # The reference revocation commits first. Physical unlink succeeds,
        # but a failed outbox commit must leave retryable durable intents.
        assert runs.suppression_add('domain', 'example.org')['id']
    finally:
        event.remove(Session, 'before_commit', fail_outbox_commit)
    assert {row.artifact_ref for row in pending()} == set(keys[:2])
    assert all(not persistence.artifact_path(key).exists() for key in keys[:2])
    assert persistence.drain_pending_deletes()['deleted'] == 2
    assert not pending()
