"""Transactional experiments use a fresh real PostgreSQL test database only."""
import copy
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence_stage_prototype as stage
from evidence_store_pg_prototype import Store, StoreError
from test_evidence_store_prototype import fixture_payload
from app import persistence


@pytest.fixture
def pg_store(clean_db):
    schema = 'stage_test_' + uuid.uuid4().hex[:12]
    return Store(persistence.engine(), schema)


def test_concurrent_replay_and_idempotency_conflict(pg_store):
    data = fixture_payload()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: pg_store.put(data['run_id'], 'capture', data), range(4)))
    assert sum(not item['replayed'] for item in results) == 1
    assert pg_store.read(data['run_id'], 'capture') == data
    changed = copy.deepcopy(data)
    changed['observations'][0]['value'] = 'different'
    with pytest.raises(StoreError, match='IDEMPOTENCY_CONFLICT'):
        pg_store.put(data['run_id'], 'capture', changed)
    with pytest.raises(StoreError, match='UNAVAILABLE'):
        pg_store.read('different-run', 'capture')


def test_concurrent_draft_rollback_and_private_spec(pg_store, packaging_spec, monkeypatch):
    data = fixture_payload()
    pg_store.put(data['run_id'], 'capture', data)
    spec = copy.deepcopy(packaging_spec)
    spec['exclusions'] = ['private-exclusion-marker']
    original = stage.draft
    monkeypatch.setattr(stage, 'draft', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('interrupt')))
    with pytest.raises(RuntimeError, match='interrupt'):
        pg_store.draft(data['run_id'], 'capture', 'chunk', spec, ['a'], as_of='2025-01-02T00:00:00Z')
    monkeypatch.setattr(stage, 'draft', original)
    def do_draft(_):
        return pg_store.draft(data['run_id'], 'capture', 'chunk', spec, ['a'], as_of='2025-01-02T00:00:00Z')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do_draft, range(4)))
    assert all(value == results[0] for value in results)
    assert len(results[0]['submittable']['candidates']) == 1
    with pytest.raises(StoreError, match='IDEMPOTENCY_CONFLICT'):
        pg_store.draft(data['run_id'], 'capture', 'chunk', spec, ['b'], as_of='2025-01-02T00:00:00Z')
    with pg_store.engine.connect() as conn:
        response = conn.scalar(text(f'SELECT response FROM "{pg_store.schema}".drafts'))
    assert b'private-exclusion-marker' not in bytes(response)


def test_expiry_purges_payload_and_keeps_key_tombstone(clean_db, packaging_spec):
    current = [datetime(2025, 1, 2, tzinfo=timezone.utc)]
    store = Store(persistence.engine(), 'stage_test_' + uuid.uuid4().hex[:12],
                  retention_days=1, clock=lambda: current[0])
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


def test_store_rejects_non_isolated_schema(clean_db):
    with pytest.raises(StoreError, match='invalid isolated schema'):
        Store(persistence.engine(), 'public')


def test_expiration_during_draft_rolls_back(clean_db, packaging_spec, monkeypatch):
    current = [datetime(2025, 1, 2, tzinfo=timezone.utc)]
    store = Store(persistence.engine(), 'stage_test_' + uuid.uuid4().hex[:12],
                  retention_days=1, clock=lambda: current[0])
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
    with store.engine.connect() as conn:
        assert conn.scalar(text(f'SELECT count(*) FROM "{store.schema}".drafts')) == 0


def test_database_session_death_rolls_back_uncommitted_capture(pg_store):
    data = fixture_payload()
    blob = stage.canonical(data)
    conn = pg_store.engine.connect()
    transaction = conn.begin()
    try:
        pid = conn.scalar(text('SELECT pg_backend_pid()'))
        now = datetime.now(timezone.utc)
        conn.execute(text(f'INSERT INTO "{pg_store.schema}".artifacts VALUES '
                          '(:run,:key,:hash,:payload,:now,:expires)'),
                     {'run': data['run_id'], 'key': 'capture', 'hash': stage.digest(blob),
                      'payload': blob, 'now': now, 'expires': now + timedelta(days=1)})
        with pg_store.engine.begin() as killer:
            assert killer.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': pid}) is True
    finally:
        try:
            transaction.rollback()
        except Exception:
            pass
        conn.close()
    result = pg_store.put(data['run_id'], 'capture', data)
    assert result['replayed'] is False
    assert pg_store.read(data['run_id'], 'capture') == data
