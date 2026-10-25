"""PostgreSQL and artifact boundaries for the single-user Skill core."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.errors import AppError

_engine = None
_factory = None
SCHEMA_REVISION = 'skill_v2_002'


def utcnow():
    return datetime.now(timezone.utc)


def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex


def engine():
    global _engine, _factory
    if _engine is None:
        settings.validate()
        _engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5,
            max_overflow=5, connect_args={'connect_timeout': 5,
            'options': '-c lock_timeout=5000 -c statement_timeout=30000'})
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine():
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = _factory = None


@contextmanager
def transaction():
    engine()
    with _factory() as session:
        with session.begin():
            yield session
        # Filesystem effects follow the successful database commit. A process
        # exit here is safe: the outbox survives for applied maintenance.
        if session.info.get('artifact_deletes_queued'):
            drain_pending_deletes()


def initialize():
    """Prepare the isolated Skill schema; upgrade only its known v2 baseline."""
    from .models import Base
    with engine().begin() as connection:
        connection.execute(text('SELECT pg_advisory_xact_lock(7334002)'))
        names = set(inspect(connection).get_table_names())
        if names:
            if 'alembic_version' not in names:
                raise AppError('LEGACY_DATABASE', '数据库已有旧表；请配置独立的 SCA_DATABASE_URL', 409)
            revision = connection.scalar(text('SELECT version_num FROM alembic_version'))
            if revision not in {'skill_v2_001', SCHEMA_REVISION} or names - set(Base.metadata.tables) - {'alembic_version'}:
                raise AppError('LEGACY_DATABASE', '数据库不是 Skill v2 基线；不会覆盖旧数据库', 409)
            operational = {'skill_host_limits', 'skill_pending_artifact_deletes', 'skill_robots_cache'}
            required = set(Base.metadata.tables) - (operational if revision == 'skill_v2_001' else set())
            if required - names:
                raise AppError('SCHEMA_INCOMPLETE', 'Skill v2 数据库缺表，请恢复备份或检查迁移', 409)
            if revision == 'skill_v2_001':
                from .schema import add_operational_tables
                add_operational_tables(connection)
                connection.execute(text('UPDATE alembic_version SET version_num = :revision'), {'revision': SCHEMA_REVISION})
            elif 'report_hash' not in {column['name'] for column in inspect(connection).get_columns('skill_exports')}:
                raise AppError('SCHEMA_INCOMPLETE', 'Skill v2 数据库缺少报告校验列，请恢复备份或检查迁移', 409)
        else:
            Base.metadata.create_all(connection)
            connection.execute(text('CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)'))
            connection.execute(text('INSERT INTO alembic_version VALUES (:revision)'), {'revision': SCHEMA_REVISION})
    return {'schema_revision': SCHEMA_REVISION}


def put(data, suffix='bin'):
    root = Path(settings.artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = uid('object') + '.' + suffix
    path = root / key
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return key, hashlib.sha256(data).hexdigest()


def artifact_path(key):
    if not key or Path(key).name != key:
        raise ValueError('Invalid opaque artifact key')
    root = Path(settings.artifact_root).resolve()
    path = (root / key).resolve()
    if path.parent != root:
        raise ValueError('Artifact outside configured namespace')
    return path


def unlink(key):
    if key:
        artifact_path(key).unlink(missing_ok=True)


def queue_artifact_delete(session, key):
    """Record deletion intent atomically with the reference revocation."""
    if not key:
        return
    from .models import PendingArtifactDelete
    artifact_path(key)  # Reject unsafe paths before changing durable state.
    session.execute(insert(PendingArtifactDelete).values(artifact_ref=key)
                    .on_conflict_do_nothing(index_elements=['artifact_ref']))
    session.info['artifact_deletes_queued'] = True


def drain_pending_deletes(limit=100):
    """Best-effort bounded outbox drain; committed revocations stay committed."""
    from .models import PendingArtifactDelete, Snapshot, Export
    engine()
    result = {'deleted': 0, 'failed': 0, 'deferred': 0}
    try:
        # Do not use transaction(): draining must never recursively drain.
        with _factory() as session, session.begin():
            # Backup holds this lock through its consistent snapshot and copy.
            session.execute(text('SELECT pg_advisory_xact_lock(7334003)'))
            rows = list(session.scalars(select(PendingArtifactDelete)
                        .order_by(PendingArtifactDelete.requested_at,
                                  PendingArtifactDelete.artifact_ref)
                        .limit(limit).with_for_update(skip_locked=True)))
            for row in rows:
                key = row.artifact_ref
                # A stale intent must never remove a referenced file, even if
                # another writer reused a key or repaired an export meanwhile.
                referenced = session.scalar(select(Snapshot.id).where(Snapshot.artifact_ref == key).limit(1))
                referenced = referenced or session.scalar(select(Export.id).where(
                    (Export.artifact_ref == key) | (Export.report_ref == key)).limit(1))
                if referenced:
                    result['deferred'] += 1
                    continue
                try:
                    unlink(key)
                except (OSError, ValueError) as exc:
                    row.attempts += 1
                    row.last_error = type(exc).__name__
                    row.updated_at = utcnow()
                    result['failed'] += 1
                else:
                    session.delete(row)
                    result['deleted'] += 1
    except Exception:
        # The intent is durable even if the DB becomes unavailable after the
        # originating commit. Retrying unlink is safe after an uncertain commit.
        result['deferred'] += 1
    return result


def observation_sink(event, payload):
    """Independent transactions at the physical HTTP/DNS/MX send boundary."""
    from .models import Observation
    serializable = {key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in payload.items() if key != 'workspace_id'}
    with transaction() as session:
        if event.endswith('_start') or event == 'start':
            session.add(Observation(id=payload['id'], run_id=payload.get('run_id') or payload.get('batch_id'),
                job_id=payload.get('job_id'), kind=payload.get('kind', 'http'), state='started',
                started_at=payload.get('started_at', utcnow()), data_json=serializable))
        else:
            row = session.get(Observation, payload['id'], with_for_update=True)
            if row and row.state == 'started':
                row.state = payload.get('state', 'completed')
                row.finished_at = payload.get('finished_at', utcnow())
                row.data_json = {**row.data_json, **serializable}
