"""Sanitized request observations through an injected ``sink(event, payload)``.

The crawler has no database dependency. A run binds its durable sink to this
context; a standalone caller may omit it. Start failures prevent the request;
settlement failures preserve the response and leave the observation incomplete.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4
from .errors import WebError

PROTOCOL_VERSION = 'observed-2'
PURPOSES = frozenset({'robots', 'website_confirmation', 'qualification', 'contact', 'unknown'})
_context = ContextVar('request_observation', default={})
_log = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def uid(prefix):
    return prefix + '_' + uuid4().hex


@contextmanager
def observation_context(**metadata):
    """Bind task attribution without changing crawler arguments or page contents.

    The caller supplies ``sink(event, payload)`` to persist start and finish
    events independently of company result transactions.
    """
    token = _context.set({**_context.get(), **metadata})
    try:
        yield
    finally:
        _context.reset(token)


def current_lineage():
    context = _context.get()
    return {'source_id': _identifier(context.get('source_id')),
            'query_id': _identifier(context.get('query_id'))}


def current_host_limiter():
    """Return the optional durable transport callback, without a storage import."""
    return _context.get().get('host_limiter')


def query_identifier(query):
    """Stable opaque ID for a frozen query; never store the query text in logs."""
    if not query:
        return 'unknown'
    import json
    raw = json.dumps(query, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return 'q_' + hashlib.sha256(raw.encode()).hexdigest()


def _identifier(value):
    value = str(value or 'unknown')
    return value if re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value) else 'unknown'


def sanitize_url(url):
    """Keep only public transport origin; even URL paths may contain contacts."""
    try:
        parsed = urlsplit(str(url))
        host = (parsed.hostname or '').lower()
        authority = ('[' + host + ']') if ':' in host else host
        if parsed.port:
            authority += ':' + str(parsed.port)
        return f'{parsed.scheme}://{authority}/[redacted]' if authority else '[redacted]'
    except ValueError:
        return '[redacted]'


@contextmanager
def observe_lookup(kind):
    """Measure an actual DNS/MX API call without persisting its host or value."""
    if kind not in {'dns', 'mx'}:
        raise ValueError('unknown lookup kind')
    context = _context.get()
    sink = context.get('sink')
    if sink is None:
        yield
        return
    now = utcnow()
    payload = dict(id=uid('blkp'), run_id=context.get('run_id') or context.get('batch_id'),
        job_id=context.get('job_id'), protocol_version=PROTOCOL_VERSION, kind=kind, state='started',
        started_at=now, finished_at=None, elapsed_seconds=None, error_code=None,
        expires_at=now+timedelta(days=int(os.getenv('B2B_LOG_RETENTION_DAYS', '30'))))
    try:
        sink('lookup_start', dict(payload))
    except Exception as error:
        raise WebError('LOOKUP_OBSERVATION_UNAVAILABLE', '解析观测暂不可写，未发送解析请求', retryable=True) from error
    started, state, error_code = time.monotonic(), 'completed', None
    try:
        yield
    except BaseException as error:
        state, error_code = 'error', _identifier(type(error).__name__)
        raise
    finally:
        result = dict(state=state, finished_at=utcnow(), elapsed_seconds=max(0,time.monotonic()-started), error_code=error_code)
        try:
            sink('lookup_finish', {'id':payload['id'], **result})
        except Exception:
            _log.error('B2B lookup settlement failed; persisted lookup remains incomplete')


@contextmanager
def observe_http(url, *, method='GET', body=None, throttle_seconds=0):
    """Persist before the physical send boundary; never infer a missing result.

    Failure to write the initial observation prevents an unobservable send.
    A failed settlement leaves the started row incomplete and preserves the
    crawler's already received response. This is observation, not reservation.
    """
    context = _context.get()
    enabled = context.get('sink') is not None
    # Admission is not a physical send. The adapter changes this flag only at
    # its final guarded transport boundary; a settled denial remains False.
    outcome = {'dispatched': False}
    if not enabled:
        yield outcome
        return
    purpose = context.get('purpose', 'unknown')
    if purpose not in PURPOSES:
        purpose = 'unknown'
    now = utcnow()
    request_hash = hashlib.sha256((str(url) + '\n' + str(method)).encode() + (body or b'')).hexdigest()
    operation = str(context.get('job_id') or 'standalone') + ':' + purpose + ':' + request_hash
    payload = dict(id=uid('breq'), run_id=context.get('run_id') or context.get('batch_id'),
        job_id=context.get('job_id'), protocol_version=PROTOCOL_VERSION,
        operation_id='op_' + hashlib.sha256(operation.encode()).hexdigest(),
        attempt=max(1, int(context.get('attempt') or 1)), purpose=purpose,
        **current_lineage(), method=str(method)[:12],
        host=(urlsplit(str(url)).hostname or '')[:253], url_redacted=sanitize_url(url),
        state='started', started_at=now, finished_at=None, fetched_at=None,
        # A process may die on either side of the physical boundary before its
        # settlement commits. Never infer a missing settlement as zero sends.
        dispatched=None,
        http_status=None, error_code=None, elapsed_seconds=None,
        throttle_seconds=max(0, float(throttle_seconds)), response_bytes=None,
        cache_status='network', expires_at=now + timedelta(days=int(os.getenv('B2B_LOG_RETENTION_DAYS', '30'))))
    sink = context.get('sink')
    # R0 pilot may install a single-runner stop guard. This is deliberately
    # outside storage error wrapping and is not an atomic budget reservation.
    before_send = context.get('before_send')
    if before_send:
        before_send(dict(context), dict(payload))
    try:
        sink('start', dict(payload))
    except Exception as error:
        raise WebError('OBSERVATION_UNAVAILABLE', '请求观测暂不可写，未发送网络请求', retryable=True) from error
    started = time.monotonic()
    try:
        yield outcome
    except BaseException as error:
        outcome.update(state='error', error_code=_identifier(getattr(error, 'code', type(error).__name__)))
        raise
    finally:
        finished = utcnow()
        result = dict(state=outcome.get('state', 'completed'), finished_at=finished,
            dispatched=bool(outcome.get('dispatched')),
            fetched_at=finished if outcome.get('http_status') is not None else None,
            http_status=outcome.get('http_status'), error_code=outcome.get('error_code'),
            response_bytes=outcome.get('response_bytes'), elapsed_seconds=max(0, time.monotonic() - started))
        try:
            sink('finish', {'id': payload['id'], **result})
        except Exception:
            _log.error('B2B observation settlement failed; persisted attempt remains incomplete')
