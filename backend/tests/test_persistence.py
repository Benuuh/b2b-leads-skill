from datetime import timedelta
from sqlalchemy import inspect, text
import pytest
from app import persistence
from app.errors import AppError
from app.persistence.models import Observation


def test_initialize_reuses_v2_and_refuses_foreign_table(clean_db):
    assert persistence.initialize()['schema_revision'] == persistence.SCHEMA_REVISION
    with persistence.transaction() as session:
        session.execute(text('CREATE TABLE legacy_workspace(id integer)'))
    try:
        with pytest.raises(AppError) as exc:
            persistence.initialize()
        assert exc.value.code == 'LEGACY_DATABASE'
    finally:
        with persistence.transaction() as session:
            session.execute(text('DROP TABLE legacy_workspace'))
    assert not {'actors', 'workspaces', 'b2b_segments', 'b2b_sources'} & set(inspect(persistence.engine()).get_table_names())


def test_artifact_atomic_bytes_and_path_fence(clean_db):
    key, digest = persistence.put(b'bytes', 'json')
    assert persistence.artifact_path(key).read_bytes() == b'bytes'
    assert len(digest) == 64
    with pytest.raises(ValueError):
        persistence.artifact_path('../escape')
    persistence.unlink(key)
    assert not persistence.artifact_path(key).exists()


def test_observations_independent_transactions(clean_db):
    from app import runs
    runs.create('observed', {'spec_version': 1, 'name': 'Observed', 'countries': ['US'], 'industry': 'freight'})
    now = persistence.utcnow()
    persistence.observation_sink('start', {'id': 'http1', 'run_id': 'observed', 'kind': 'http', 'started_at': now})
    persistence.observation_sink('finish', {'id': 'http1', 'state': 'completed', 'finished_at': now, 'elapsed_seconds': 0.1})
    with persistence.transaction() as session:
        row = session.get(Observation, 'http1')
        assert row.state == 'completed'
        assert row.data_json['elapsed_seconds'] == 0.1
