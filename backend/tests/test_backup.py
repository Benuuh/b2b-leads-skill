"""An isolated restore must reproduce data and all referenced file bytes."""
import secrets
import io
import json

import pytest
from psycopg import sql

from app import runs
from app.config import settings
from app.persistence import artifact_path, put, transaction, utcnow
from app.persistence.models import Company, Export, Snapshot
from app.persistence.backup import backup, restore, connection, digest
from datetime import timedelta
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def seed_export():
    runs.create("backup-run", {"name": "Backup fixture", "countries": ["US"], "industry": "freight"})
    book = Workbook()
    book.active.title = 'Companies'
    book.active.append(['企业名称'])
    book.active.append(['Synthetic backup fixture'])
    buffer = io.BytesIO()
    book.save(buffer)
    key, checksum = put(buffer.getvalue(), 'xlsx')
    manifest = {'fixture': True}
    report, report_hash = put(json.dumps(manifest).encode(), 'json')
    with transaction() as session:
        session.add(Export(id='backup-export', run_id='backup-run', format='xlsx', filename='fixture.xlsx',
            artifact_ref=key, artifact_hash=checksum, report_ref=report, report_hash=report_hash,
            manifest_json=manifest, expires_at=utcnow() + timedelta(days=1)))
    return key, report


def drop_database(target):
    with connection('postgres') as conn:
        conn.autocommit = True
        conn.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(target)))


def test_backup_restore_and_tamper_rejection(clean_db, tmp_path):
    key, report = seed_export()
    output = tmp_path / "backup"
    assert backup(output)["artifact_count"] == 2
    target = "sca_restore_test_" + secrets.token_hex(6)
    try:
        result = restore(output, target, tmp_path / "restored")
        assert result['original_artifact_hashes_match'] and result["table_hashes_match"] and result["artifact_count"] == 2
        restored_engine = create_engine(make_url(settings.database_url).set(database=target))
        original_artifacts = settings.artifact_root
        try:
            settings.artifact_root = tmp_path / 'restored'
            with Session(restored_engine) as session:
                # Exercise the same artifact/report validation used to replay
                # a saved export, then open its actual workbook bytes.
                view = runs._export_view(session.get(Export, 'backup-export'))
                book = load_workbook(view['file'])
                assert book['Companies']['A2'].value == 'Synthetic backup fixture'
                book.close()
                assert json.loads((tmp_path / 'restored' / report).read_text()) == {'fixture': True}
        finally:
            settings.artifact_root = original_artifacts
            restored_engine.dispose()
        with pytest.raises(ValueError, match="already exists"):
            restore(output, target, tmp_path / "another-restore")
        (output / "artifacts" / key).write_bytes(b"tampered")
        with pytest.raises(ValueError, match="integrity"):
            restore(output, "sca_restore_tamper_" + secrets.token_hex(4), tmp_path / "tampered")
    finally:
        drop_database(target)


@pytest.mark.parametrize('kind', ['workbook', 'snapshot', 'report', 'legacy_report'])
def test_backup_rejects_corruption_that_predates_copy(clean_db, tmp_path, kind):
    key, report = seed_export()
    if kind == 'snapshot':
        key, checksum = put(b'original source page', 'html')
        with transaction() as session:
            session.add(Company(id='backup-company', run_id='backup-run', identity_key='fixture',
                data_json={}, expires_at=utcnow() + timedelta(days=1)))
            session.flush()
            session.add(Snapshot(id='backup-snapshot', run_id='backup-run', company_id='backup-company',
                source_url='https://directory.example.org/synthetic', artifact_ref=key, artifact_hash=checksum,
                expires_at=utcnow() + timedelta(days=1)))
    if kind == 'legacy_report':
        with transaction() as session:
            session.get(Export, 'backup-export').report_hash = None
    artifact_path(key if kind in {'workbook', 'snapshot'} else report).write_bytes(b'{"tampered":true}')
    output = tmp_path / 'bad-backup'
    with pytest.raises(ValueError, match='original|manifest'):
        backup(output)
    assert not (output / 'manifest.json').exists()


def test_restore_compares_copy_hash_with_original_database_hash(clean_db, tmp_path):
    key, _ = seed_export()
    output = tmp_path / 'backup'
    backup(output)
    # Reproduce a legacy backup that blessed already-corrupt file bytes by
    # hashing only the copy. Its dump still contains the original checksum.
    copied = output / 'artifacts' / key
    copied.write_bytes(b'corrupt before backup')
    manifest_path = output / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    next(item for item in manifest['artifacts'] if item['key'] == key)['sha256'] = digest(copied)
    manifest_path.write_text(json.dumps(manifest))
    target = 'sca_restore_original_' + secrets.token_hex(6)
    try:
        with pytest.raises(ValueError, match='original hash'):
            restore(output, target, tmp_path / 'restored')
    finally:
        drop_database(target)


def test_restore_verifies_bytes_after_final_artifact_copy(clean_db, tmp_path, monkeypatch):
    import app.persistence.backup as backup_module
    key, _ = seed_export()
    output = tmp_path / 'backup'
    backup(output)
    restored = tmp_path / 'restored'
    original_copy = backup_module.shutil.copy2
    def damaged_copy(source, destination):
        result = original_copy(source, destination)
        if destination == restored / key:
            destination.write_bytes(b'damaged on restore copy')
        return result
    monkeypatch.setattr(backup_module.shutil, 'copy2', damaged_copy)
    target = 'sca_restore_copy_' + secrets.token_hex(6)
    try:
        with pytest.raises(ValueError, match='original hash'):
            restore(output, target, restored)
    finally:
        drop_database(target)


def test_backup_preserves_pre_upgrade_database_without_report_hash_column(clean_db, tmp_path):
    seed_export()
    target = 'sca_restore_legacy_' + secrets.token_hex(6)
    try:
        with transaction() as session:
            session.execute(text('ALTER TABLE skill_exports DROP COLUMN report_hash'))
        # Backup is deliberately usable before initialize/prepare performs the
        # additive migration, so operators can preserve the old business DB.
        output = tmp_path / 'pre-upgrade'
        assert backup(output)['artifact_count'] == 2
        assert restore(output, target, tmp_path / 'legacy-restored')['original_artifact_hashes_match']
    finally:
        with transaction() as session:
            session.execute(text('ALTER TABLE skill_exports ADD COLUMN IF NOT EXISTS report_hash varchar(64)'))
        drop_database(target)
