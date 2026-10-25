"""Admission/dispatch telemetry with fake transport and an isolated real DB."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import persistence, runs
from app.delivery.metrics import calculate_metrics
from app.persistence.models import Observation, Run
from app.web import http, observation
from app.web.errors import WebError

RUN_ID = 'dispatch-observation'
URL = 'https://public.example.com/contact'


@pytest.fixture
def transport(clean_db, monkeypatch):
    runs.create(RUN_ID, {'name': 'Dispatch fixture', 'countries': ['US'], 'industry': 'freight'})
    monkeypatch.setattr(http, '_blocked_until', {})
    monkeypatch.setattr(http, '_last_request', {})
    monkeypatch.setattr(http, 'validate_public_url', lambda url: (url, ['93.184.216.34']))
    sends = []
    def send(*args):
        sends.append(args[0])
        return 200, {}, b'fixture page'
    monkeypatch.setattr(http, '_open_pinned', send)
    return sends


def context(limiter, sink=None):
    return observation.observation_context(sink=sink or persistence.observation_sink,
        run_id=RUN_ID, purpose='contact', source_id='synthetic', query_id='dispatch',
        host_limiter=limiter)


def ready(event, payload):
    return {'token': 'fixture'} if event == 'claim' else {'ready': True}


def telemetry():
    with persistence.transaction() as session:
        current = persistence.utcnow()
        result = runs._metrics_context(session, session.get(Run, RUN_ID), current)
    # Verify the exported requests mapping forwards the new distinctions too.
    return result['observation_summary'], calculate_metrics([], context=result)['requests']


@pytest.mark.parametrize('failure', ['denied', 'storage_unavailable'])
def test_dispatch_denial_is_observed_without_counting_a_physical_send(transport, failure):
    def limiter(event, payload):
        if event == 'dispatch':
            if failure == 'storage_unavailable':
                raise RuntimeError('synthetic limiter failure')
            return {'revoked': True, 'wait_seconds': 90}
        return ready(event, payload)
    with context(limiter):
        with pytest.raises(WebError):
            http._rate_limited_request(URL, delay=2)
    assert transport == []
    with persistence.transaction() as session:
        row = session.scalar(select(Observation).where(Observation.kind == 'http'))
        assert row.state == 'error' and row.data_json['dispatched'] is False
    summary, requests = telemetry()
    assert summary['recorded_attempts'] == 1 and summary['actual_http_attempts'] == 0
    assert summary['dispatch_denied_attempts'] == requests['dispatch_denied_attempts'] == 1
    assert summary['confirmed_http_attempts'] == requests['confirmed_http_attempts'] == 0
    assert summary['dispatch_unknown_attempts'] == requests['dispatch_unknown_attempts'] == 0
    assert summary['by_purpose'] == summary['status_counts'] == {}
    assert summary['completeness'] == 'observed'


def test_successful_transport_is_marked_at_the_guarded_boundary(transport):
    events = []
    def sink(event, payload):
        events.append((event, dict(payload)))
        persistence.observation_sink(event, payload)
    with context(ready, sink):
        http._rate_limited_request(URL, delay=2)
    assert transport == [URL]
    assert events[0][1]['dispatched'] is None
    assert events[-1][1]['dispatched'] is True
    summary, requests = telemetry()
    assert summary['actual_http_attempts'] == requests['confirmed_http_attempts'] == 1
    assert summary['dispatch_denied_attempts'] == summary['dispatch_unknown_attempts'] == 0
    assert summary['by_purpose'] == {'contact': 1}


def test_transport_connection_error_still_counts_a_dispatched_attempt(transport, monkeypatch):
    def failed_send(*args):
        transport.append(args[0])
        raise WebError('CONNECTION_ERROR', 'synthetic socket failure', retryable=True)
    monkeypatch.setattr(http, '_open_pinned', failed_send)
    with context(ready):
        with pytest.raises(WebError):
            http._rate_limited_request(URL, delay=2)
    assert transport == [URL]
    summary, _ = telemetry()
    assert summary['actual_http_attempts'] == 1
    assert summary['status_counts'] == {'CONNECTION_ERROR': 1}


@pytest.mark.parametrize('dispatched', [False, True])
def test_unsettled_admission_remains_unknown_on_either_side_of_send(transport, dispatched):
    def limiter(event, payload):
        if event == 'dispatch' and not dispatched:
            return {'revoked': True, 'wait_seconds': 90}
        return ready(event, payload)
    def failed_settlement(event, payload):
        if event == 'finish':
            raise RuntimeError('synthetic crash or unavailable settlement')
        persistence.observation_sink(event, payload)
    with context(limiter, failed_settlement):
        if dispatched:
            http._rate_limited_request(URL, delay=2)
        else:
            with pytest.raises(WebError):
                http._rate_limited_request(URL, delay=2)
    assert len(transport) == int(dispatched)
    with persistence.transaction() as session:
        row = session.scalar(select(Observation).where(Observation.kind == 'http'))
        assert row.state == 'started' and row.data_json['dispatched'] is None
    summary, requests = telemetry()
    assert summary['actual_http_attempts'] is requests['actual_http_attempts'] is None
    assert summary['unfinished_attempts'] == summary['outcome_unknown'] == 1
    assert summary['dispatch_unknown_attempts'] == requests['dispatch_unknown_attempts'] == 1
    assert summary['confirmed_http_attempts'] == 0
    assert summary['completeness'] == 'incomplete'


def test_legacy_settled_rows_preserve_their_original_send_contract(transport):
    now = persistence.utcnow()
    with persistence.transaction() as session:
        session.add(Observation(id='legacy-physical-send', run_id=RUN_ID, kind='http', state='completed',
            started_at=now - timedelta(seconds=1), finished_at=now,
            data_json={'protocol_version': 'observed-1', 'purpose': 'contact', 'http_status': 200}))
    summary, _ = telemetry()
    assert summary['actual_http_attempts'] == 1
    assert summary['dispatch_unknown_attempts'] == 0


@pytest.mark.parametrize('dispatch_field', [{}, {'dispatched': None}])
def test_explicit_unknown_dispatch_is_never_inferred_as_a_send(transport, dispatch_field):
    now = persistence.utcnow()
    with persistence.transaction() as session:
        session.add(Observation(id='explicit-unknown', run_id=RUN_ID, kind='http', state='error',
            started_at=now - timedelta(seconds=1), finished_at=now,
            data_json={'protocol_version': 'observed-2', **dispatch_field}))
    summary, _ = telemetry()
    assert summary['actual_http_attempts'] is None
    assert summary['dispatch_unknown_attempts'] == 1 and summary['unfinished_attempts'] == 0
    assert summary['completeness'] == 'incomplete'
