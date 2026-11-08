"""Synthetic SQLite staging tests; production lifecycle is not implemented."""
import copy
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence_stage_prototype as stage
from evidence_store_prototype import Store, StoreError
from test_evidence_stage_prototype import sample


def fixture_payload():
    data = sample()
    data['sources'][0]['url'] = 'https://directory.example.com/members'
    data['sources'][0]['collected_at'] = '2025-01-01T00:00:00Z'
    identity = copy.deepcopy(data['observations'][0])
    identity.update(field='company_name', value='Atlas Example LLC')
    data['observations'].append(identity)
    return data


def test_persist_restart_replay_and_run_isolation(tmp_path):
    path = tmp_path / 'store.sqlite'
    store = Store(path)
    data = fixture_payload()
    first = store.put(data['run_id'], 'capture', data)
    again = Store(path).put(data['run_id'], 'capture', copy.deepcopy(data))
    assert first['input_sha256'] == again['input_sha256'] and again['replayed']
    assert Store(path).read(data['run_id'], 'capture') == data
    with pytest.raises(StoreError, match='UNAVAILABLE'):
        store.read('another-run', 'capture')
    changed = copy.deepcopy(data)
    changed['observations'][0]['value'] = 'other'
    with pytest.raises(StoreError, match='IDEMPOTENCY_CONFLICT'):
        store.put(data['run_id'], 'capture', changed)


def test_concurrent_identical_and_conflicting_writes(tmp_path):
    path = tmp_path / 'store.sqlite'
    data = fixture_payload()
    Store(path)
    def put(_):
        return Store(path).put(data['run_id'], 'capture', data)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(put, range(4)))
    assert sum(not r['replayed'] for r in results) == 1
    assert len({r['input_sha256'] for r in results}) == 1


def test_draft_is_atomic_replayable_and_keeps_spec_private(tmp_path, packaging_spec, monkeypatch):
    store = Store(tmp_path / 'store.sqlite')
    data = fixture_payload()
    store.put(data['run_id'], 'capture', data)
    spec = copy.deepcopy(packaging_spec)
    spec['exclusions'] = ['private-exclusion-marker']
    original = stage.draft
    monkeypatch.setattr(stage, 'draft', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('interruption')))
    with pytest.raises(RuntimeError, match='interruption'):
        store.draft(data['run_id'], 'capture', 'chunk', spec, ['a'], as_of='2025-01-02T00:00:00Z')
    monkeypatch.setattr(stage, 'draft', original)
    first = store.draft(data['run_id'], 'capture', 'chunk', spec, ['a'], as_of='2025-01-02T00:00:00Z')
    assert len(first['submittable']['candidates']) == 1
    assert Store(store.path).draft(data['run_id'], 'capture', 'chunk', spec, ['a'], as_of='2025-01-02T00:00:00Z') == first
    with pytest.raises(StoreError, match='IDEMPOTENCY_CONFLICT'):
        store.draft(data['run_id'], 'capture', 'chunk', spec, ['b'], as_of='2025-01-02T00:00:00Z')
    assert b'private-exclusion-marker' not in store.path.read_bytes()


def test_expiry_rejects_access_and_purges_content_not_tombstones(tmp_path, packaging_spec):
    current = [datetime(2025, 1, 2, tzinfo=timezone.utc)]
    store = Store(tmp_path / 'store.sqlite', retention_days=1, clock=lambda: current[0])
    data = fixture_payload()
    store.put(data['run_id'], 'capture', data)
    store.draft(data['run_id'], 'capture', 'chunk', packaging_spec, ['a'], as_of='2025-01-02T00:00:00Z')
    current[0] += timedelta(days=1)
    with pytest.raises(StoreError, match='UNAVAILABLE'):
        store.read(data['run_id'], 'capture')
    assert store.purge_expired() == {'artifacts': 1, 'drafts': 1}
    assert store.purge_expired() == {'artifacts': 0, 'drafts': 0}
    with pytest.raises(StoreError, match='EXPIRED'):
        store.put(data['run_id'], 'capture', data)
    assert b'Atlas Example LLC' not in store.path.read_bytes()


def test_tampering_is_detected(tmp_path):
    import sqlite3
    store = Store(tmp_path / 'store.sqlite')
    data = fixture_payload()
    store.put(data['run_id'], 'capture', data)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE artifacts SET payload=?', (b'{}',))
    with pytest.raises(StoreError, match='HASH_MISMATCH'):
        store.read(data['run_id'], 'capture')


def test_expiration_during_draft_rolls_back(tmp_path, packaging_spec, monkeypatch):
    import sqlite3
    current = [datetime(2025, 1, 2, tzinfo=timezone.utc)]
    store = Store(tmp_path / 'store.sqlite', retention_days=1, clock=lambda: current[0])
    data = fixture_payload()
    store.put(data['run_id'], 'capture', data)
    original = stage.draft
    def crosses_expiry(*args, **kwargs):
        result = original(*args, **kwargs)
        current[0] += timedelta(days=1)
        return result
    monkeypatch.setattr(stage, 'draft', crosses_expiry)
    with pytest.raises(StoreError, match='EXPIRED_DURING_DRAFT'):
        store.draft(data['run_id'], 'capture', 'chunk', packaging_spec, ['a'], as_of='2025-01-02T00:00:00Z')
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM drafts').fetchone()[0] == 0
