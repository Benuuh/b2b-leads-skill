"""Isolated staging-store experiment. Not connected to a production run.

The caller owns and protects the SQLite file. Until this store is integrated
with the Skill's artifact, backup, access and deletion lifecycle, use only
synthetic fixtures; it is not an approved store for customer evidence.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import evidence_stage_prototype as stage

STORE_VERSION = 'evidence-store-prototype-0.1'
IDENTIFIER = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')


class StoreError(ValueError):
    pass


def _id(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StoreError('invalid identifier')
    return value


def _utc(value: datetime) -> str:
    if value.utcoffset() is None:
        raise StoreError('clock must provide timezone-aware UTC')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


class Store:
    def __init__(self, path: Path, *, retention_days: int = 30, clock=None):
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 30:
            raise StoreError('retention_days must be between 1 and 30')
        self.path = Path(path)
        self.retention_days = retention_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Do not create or choose the containing directory on the caller's behalf.
        if not self.path.parent.is_dir():
            raise StoreError('store parent must already exist')
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS artifacts (
                    run_id TEXT NOT NULL, operation_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL, payload BLOB, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, PRIMARY KEY (run_id, operation_key));
                CREATE TABLE IF NOT EXISTS drafts (
                    run_id TEXT NOT NULL, operation_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, response BLOB, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, PRIMARY KEY (run_id, operation_key));
            ''')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA foreign_keys = ON')
            db.execute('PRAGMA secure_delete = ON')
            db.execute('PRAGMA journal_mode = DELETE')
            db.execute('PRAGMA synchronous = FULL')
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def _times(self):
        now = self.clock()
        return _utc(now), _utc(now + timedelta(days=self.retention_days))

    def put(self, run_id: str, operation_key: str, payload: dict) -> dict:
        run_id, operation_key = _id(run_id), _id(operation_key)
        validated = stage.inspect(payload)
        if validated['run_id'] != run_id:
            raise StoreError('artifact belongs to a different run')
        blob = stage.canonical(payload)
        checksum = stage.digest(blob)
        now, expires = self._times()
        with self._write() as db:
            row = db.execute('SELECT input_hash, payload, expires_at FROM artifacts WHERE run_id=? AND operation_key=?',
                             (run_id, operation_key)).fetchone()
            if row:
                if row[0] != checksum:
                    raise StoreError('IDEMPOTENCY_CONFLICT')
                if row[1] is None or row[2] <= now:
                    raise StoreError('ARTIFACT_EXPIRED')
                return {'version': STORE_VERSION, 'input_sha256': checksum, 'expires_at': row[2], 'replayed': True}
            db.execute('INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)',
                       (run_id, operation_key, checksum, blob, now, expires))
        return {'version': STORE_VERSION, 'input_sha256': checksum, 'expires_at': expires, 'replayed': False}

    def read(self, run_id: str, operation_key: str) -> dict:
        run_id, operation_key = _id(run_id), _id(operation_key)
        now, _ = self._times()
        with self._connection() as db:
            row = db.execute('SELECT input_hash, payload, expires_at FROM artifacts WHERE run_id=? AND operation_key=?',
                             (run_id, operation_key)).fetchone()
        if not row or row[1] is None or row[2] <= now:
            raise StoreError('ARTIFACT_UNAVAILABLE')
        if stage.digest(row[1]) != row[0]:
            raise StoreError('ARTIFACT_HASH_MISMATCH')
        return json.loads(row[1])

    def draft(self, run_id: str, artifact_key: str, operation_key: str, customer_spec: dict,
              candidate_ids: list[str], *, as_of: str) -> dict:
        run_id, artifact_key, operation_key = _id(run_id), _id(artifact_key), _id(operation_key)
        # Store only the request hash, never the customer specification or exclusion list.
        payload = self.read(run_id, artifact_key)
        request_hash = stage.digest(stage.canonical({'artifact': payload, 'spec': customer_spec,
                                                     'candidate_ids': candidate_ids, 'as_of': as_of}))
        now, expires = self._times()
        with self._write() as db:
            artifact = db.execute('SELECT input_hash, payload, expires_at FROM artifacts WHERE run_id=? AND operation_key=?',
                                  (run_id, artifact_key)).fetchone()
            if not artifact or artifact[1] is None or artifact[2] <= now or stage.digest(artifact[1]) != artifact[0]:
                raise StoreError('ARTIFACT_UNAVAILABLE')
            if artifact[0] != stage.digest(stage.canonical(payload)):
                raise StoreError('ARTIFACT_CHANGED')
            old = db.execute('SELECT request_hash, response, expires_at FROM drafts WHERE run_id=? AND operation_key=?',
                             (run_id, operation_key)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise StoreError('IDEMPOTENCY_CONFLICT')
                if old[1] is None or old[2] <= now:
                    raise StoreError('DRAFT_EXPIRED')
                return json.loads(old[1])
            result = stage.draft(payload, customer_spec, candidate_ids, as_of=as_of)
            # The output may not outlive its source evidence.
            expires = min(expires, artifact[2])
            if self._times()[0] >= expires:
                raise StoreError('ARTIFACT_EXPIRED_DURING_DRAFT')
            db.execute('INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)',
                       (run_id, operation_key, request_hash, stage.canonical(result), now, expires))
            return result

    def purge_expired(self) -> dict:
        """Revoke retained blobs, preserving hash-only idempotency tombstones."""
        now, _ = self._times()
        with self._write() as db:
            artifacts = db.execute('UPDATE artifacts SET payload=NULL WHERE expires_at<=? AND payload IS NOT NULL',
                                   (now,)).rowcount
            drafts = db.execute('UPDATE drafts SET response=NULL WHERE expires_at<=? AND response IS NOT NULL',
                                (now,)).rowcount
        return {'artifacts': artifacts, 'drafts': drafts}
