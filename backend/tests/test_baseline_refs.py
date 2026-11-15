"""Baseline capture verifies existing isolated artifacts; no raw page is copied."""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_baseline_refs import capture, verify
from app import persistence, runs
from app.persistence.models import Company, Snapshot


def test_capture_and_replay_ref_integrity(clean_db):
    rid = 'baseline-ref-test'
    runs.create(rid, {'spec_version': 1, 'name': 'fixture', 'countries': ['US'], 'industry': 'freight'})
    key, checksum = persistence.put(b'synthetic source text', 'html')
    now = persistence.utcnow()
    with persistence.transaction() as session:
        session.add(Company(id='baseline-company', run_id=rid, identity_key='identity', data_json={},
                            expires_at=now + timedelta(days=30)))
        session.flush()
        session.add(Snapshot(id='baseline-snapshot', run_id=rid, company_id='baseline-company',
                             source_url='https://directory.example.com/secret-query', artifact_ref=key,
                             artifact_hash=checksum, expires_at=now + timedelta(days=2)))
    manifest = capture(rid, 5)
    assert len(manifest['manifest']['samples']) == 1
    assert 'secret-query' not in str(manifest) and 'synthetic source text' not in str(manifest)
    assert verify(manifest)['verified'] == 1
    persistence.artifact_path(key).write_bytes(b'tampered')
    assert verify(manifest)['errors'][0]['reason'] == 'CONTENT_HASH_MISMATCH'
    manifest['manifest']['samples'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='manifest hash'):
        verify(manifest)
