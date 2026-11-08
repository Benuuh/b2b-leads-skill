"""PostgreSQL staging experiment, restricted to isolated test databases.

This module cannot enable a Skill command or create tables in the live schema.
It exercises transactional behavior before any production migration exists.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import evidence_stage_prototype as stage
from evidence_store_prototype import StoreError, _id

SCHEMA = re.compile(r'^stage_test_[a-f0-9]{12}$')
VERSION = 'evidence-store-pg-prototype-0.1'


class Store:
    def __init__(self, engine, schema: str, *, retention_days: int = 30, clock=None):
        if not SCHEMA.fullmatch(schema):
            raise StoreError('invalid isolated schema')
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 30:
            raise StoreError('retention_days must be between 1 and 30')
        self.engine, self.schema = engine, schema
        self.retention_days = retention_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with engine.begin() as conn:
            database = conn.scalar(text('SELECT current_database()'))
            if not database.startswith('sca_test_'):
                raise StoreError('only isolated sca_test_ databases are allowed')
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            conn.execute(text(f'''CREATE TABLE IF NOT EXISTS "{schema}".artifacts (
                run_id text NOT NULL, op_key text NOT NULL, input_hash text NOT NULL,
                payload bytea, created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
                PRIMARY KEY (run_id, op_key))'''))
            conn.execute(text(f'''CREATE TABLE IF NOT EXISTS "{schema}".drafts (
                run_id text NOT NULL, op_key text NOT NULL, request_hash text NOT NULL,
                response bytea, created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
                PRIMARY KEY (run_id, op_key))'''))

    def _now(self):
        now = self.clock()
        if now.utcoffset() is None:
            raise StoreError('clock must be timezone aware')
        return now.astimezone(timezone.utc)

    def put(self, run_id: str, key: str, payload: dict) -> dict:
        run_id, key = _id(run_id), _id(key)
        if stage.inspect(payload)['run_id'] != run_id:
            raise StoreError('artifact belongs to a different run')
        blob = stage.canonical(payload)
        checksum, now = stage.digest(blob), self._now()
        args = {'run': run_id, 'key': key}
        with self.engine.begin() as conn:
            conn.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))'),
                         {'lock': f'{self.schema}:artifact:{run_id}:{key}'})
            row = conn.execute(text(f'SELECT input_hash, payload, expires_at FROM "{self.schema}".artifacts '
                                    'WHERE run_id=:run AND op_key=:key FOR UPDATE'), args).first()
            if row:
                if row.input_hash != checksum:
                    raise StoreError('IDEMPOTENCY_CONFLICT')
                if row.payload is None or row.expires_at <= now:
                    raise StoreError('ARTIFACT_EXPIRED')
                return {'input_sha256': checksum, 'expires_at': row.expires_at.isoformat(), 'replayed': True}
            expires = now + timedelta(days=self.retention_days)
            conn.execute(text(f'INSERT INTO "{self.schema}".artifacts VALUES '
                              '(:run,:key,:hash,:payload,:now,:expires)'),
                         {**args, 'hash': checksum, 'payload': blob, 'now': now, 'expires': expires})
        return {'input_sha256': checksum, 'expires_at': expires.isoformat(), 'replayed': False}

    def read(self, run_id: str, key: str) -> dict:
        args = {'run': _id(run_id), 'key': _id(key)}
        now = self._now()
        with self.engine.connect() as conn:
            row = conn.execute(text(f'SELECT input_hash, payload, expires_at FROM "{self.schema}".artifacts '
                                    'WHERE run_id=:run AND op_key=:key'), args).first()
        if not row or row.payload is None or row.expires_at <= now:
            raise StoreError('ARTIFACT_UNAVAILABLE')
        data = bytes(row.payload)
        if stage.digest(data) != row.input_hash:
            raise StoreError('ARTIFACT_HASH_MISMATCH')
        return json.loads(data)

    def draft(self, run_id: str, artifact_key: str, operation_key: str,
              customer_spec: dict, candidate_ids: list[str], *, as_of: str) -> dict:
        run_id, artifact_key, operation_key = _id(run_id), _id(artifact_key), _id(operation_key)
        now = self._now()
        with self.engine.begin() as conn:
            # Lock the artifact before the draft key in every transaction.
            row = conn.execute(text(f'SELECT input_hash, payload, expires_at FROM "{self.schema}".artifacts '
                                    'WHERE run_id=:run AND op_key=:key FOR UPDATE'),
                               {'run': run_id, 'key': artifact_key}).first()
            if not row or row.payload is None or row.expires_at <= now:
                raise StoreError('ARTIFACT_UNAVAILABLE')
            blob = bytes(row.payload)
            if stage.digest(blob) != row.input_hash:
                raise StoreError('ARTIFACT_HASH_MISMATCH')
            payload = json.loads(blob)
            request_hash = stage.digest(stage.canonical({'artifact': payload, 'spec': customer_spec,
                                                         'candidate_ids': candidate_ids, 'as_of': as_of}))
            conn.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))'),
                         {'lock': f'{self.schema}:draft:{run_id}:{operation_key}'})
            old = conn.execute(text(f'SELECT request_hash, response, expires_at FROM "{self.schema}".drafts '
                                    'WHERE run_id=:run AND op_key=:key FOR UPDATE'),
                               {'run': run_id, 'key': operation_key}).first()
            if old:
                if old.request_hash != request_hash:
                    raise StoreError('IDEMPOTENCY_CONFLICT')
                if old.response is None or old.expires_at <= now:
                    raise StoreError('DRAFT_EXPIRED')
                return json.loads(bytes(old.response))
            result = stage.draft(payload, customer_spec, candidate_ids, as_of=as_of)
            expires = min(now + timedelta(days=self.retention_days), row.expires_at)
            if self._now() >= expires:
                raise StoreError('ARTIFACT_EXPIRED_DURING_DRAFT')
            conn.execute(text(f'INSERT INTO "{self.schema}".drafts VALUES '
                              '(:run,:key,:hash,:response,:now,:expires)'),
                         {'run': run_id, 'key': operation_key, 'hash': request_hash,
                          'response': stage.canonical(result), 'now': now,
                          'expires': expires})
            return result

    def purge_expired(self) -> dict:
        now = self._now()
        with self.engine.begin() as conn:
            artifacts = conn.execute(text(f'UPDATE "{self.schema}".artifacts SET payload=NULL '
                                          'WHERE expires_at<=:now AND payload IS NOT NULL'), {'now': now}).rowcount
            drafts = conn.execute(text(f'UPDATE "{self.schema}".drafts SET response=NULL '
                                       'WHERE expires_at<=:now AND response IS NOT NULL'), {'now': now}).rowcount
        return {'artifacts': artifacts, 'drafts': drafts}
