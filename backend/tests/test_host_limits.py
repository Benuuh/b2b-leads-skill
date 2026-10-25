"""Cross-process host state and additive migrations, without internet requests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text

from app import persistence, runs
from app.config import ROOT, settings
from app.errors import AppError
from app.persistence.host_limits import limit
from app.persistence.models import HostLimit, MaintenanceState, RobotsCache, Run


def test_host_claim_is_atomic_and_stale_release_cannot_remove_new_lease(clean_db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: limit('claim', {'host': 'public.example.com'}), range(2)))
    granted = [row for row in results if row.get('token')]
    assert len(granted) == 1
    assert next(row['wait_seconds'] for row in results if not row.get('token')) > 80
    old = granted[0]['token']
    with persistence.transaction() as session:
        row = session.get(HostLimit, 'public.example.com')
        row.lease_until = persistence.utcnow() - timedelta(seconds=1)
    new = limit('claim', {'host': 'public.example.com'})['token']
    assert new != old
    limit('release', {'host': 'public.example.com', 'token': old})
    assert limit('dispatch', {'host': 'public.example.com', 'token': new, 'delay': 120})['ready']
    limit('release', {'host': 'public.example.com', 'token': new})
    # Releasing a lease, including a crashed old request, never resets spacing.
    assert limit('claim', {'host': 'public.example.com'})['wait_seconds'] > 119


def test_retry_after_is_shared_by_independent_python_processes(clean_db):
    env = {**os.environ, 'SCA_DATABASE_URL': settings.database_url}
    def command(event, **payload):
        code = ('import json, sys; from app.persistence.host_limits import limit; '
                'print(json.dumps(limit(sys.argv[1], json.loads(sys.argv[2]))))')
        result = subprocess.run([sys.executable, '-c', code, event,
                                 json.dumps({'host': 'shared.example.com', **payload})],
                                env=env, cwd=ROOT, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        return json.loads(result.stdout)
    first = command('claim')
    assert 'token' in first
    assert command('claim')['wait_seconds'] > 80
    command('dispatch', token=first['token'], delay=2)
    command('release', token=first['token'], retry_after_s=120)
    # A third new interpreter has no module cooldown/cache from the first one.
    assert command('claim')['wait_seconds'] > 118


def test_newly_observed_crawl_delay_applies_to_previous_dispatch(clean_db):
    token = limit('claim', {'host': 'robots.example.com', 'delay': 2})['token']
    limit('dispatch', {'host': 'robots.example.com', 'token': token, 'delay': 2})
    limit('release', {'host': 'robots.example.com', 'token': token})
    # The page request learns the 120-second rule from the robots response.
    assert limit('claim', {'host': 'robots.example.com', 'delay': 120})['wait_seconds'] > 119


def test_robots_cache_allows_business_progress_in_a_new_process_after_due_time(clean_db):
    env = {**os.environ, 'SCA_DATABASE_URL': settings.database_url}
    code = '''
import json
from app.web import http
from app.web.errors import WebError
from app.web.observation import observation_context
from app.persistence.host_limits import limit
calls = []
http.validate_public_url = lambda url: (url, ['93.184.216.34'])
def transport(url, *args, **kwargs):
    calls.append(url)
    if url.endswith('/robots.txt'):
        return 200, {'content-type': 'text/plain'}, b'User-agent: *\\nAllow: /\\nCrawl-delay: 120'
    return 200, {'content-type': 'text/html'}, b'<h1>Public business page</h1>'
http._open_pinned = transport
with observation_context(host_limiter=limit):
    try:
        page = http.safe_http('https://delayed.example.com/contact')
        outcome = {'status': page['http_status']}
    except WebError as error:
        outcome = {'error': error.code, 'retry_after_s': error.retry_after_s}
print(json.dumps({'calls': calls, **outcome}))
'''
    def command():
        result = subprocess.run([sys.executable, '-c', code], env=env, cwd=ROOT,
                                capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        return json.loads(result.stdout)
    first = command()
    assert first['calls'] == ['https://delayed.example.com/robots.txt']
    assert first['error'] == 'RATE_LIMITED' and first['retry_after_s'] > 110
    with persistence.transaction() as session:
        row = session.get(HostLimit, 'delayed.example.com')
        now = persistence.utcnow()
        row.last_started_at = now - timedelta(seconds=121)
        row.next_allowed_at = now - timedelta(seconds=1)
    second = command()
    assert second == {'calls': ['https://delayed.example.com/contact'], 'status': 200}


@pytest.mark.parametrize('status', [200, 404, 410])
def test_robots_decisions_expire_within_one_hour(clean_db, status):
    rules = 'User-agent: *\nDisallow: /private' if status == 200 else None
    assert limit('robots_put', {'origin': 'https://cache.example.com', 'status': status,
                              'rules_text': rules})['cached']
    cached = limit('robots_get', {'origin': 'https://cache.example.com'})
    assert cached['status'] == status and cached['rules_text'] == rules
    assert 3590 <= cached['ttl_seconds'] <= 3600
    with persistence.transaction() as session:
        session.get(RobotsCache, 'https://cache.example.com').expires_at = persistence.utcnow() - timedelta(seconds=1)
    assert limit('robots_get', {'origin': 'https://cache.example.com'}) is None


@pytest.mark.parametrize('payload', [
    {'origin': 'https://user:secret@cache.example.com', 'status': 200, 'rules_text': ''},
    {'origin': 'https://cache.example.com/path?secret=value', 'status': 200, 'rules_text': ''},
    {'origin': 'https://cache.example.com', 'status': 302, 'rules_text': ''},
    {'origin': 'https://cache.example.com', 'status': 200, 'rules_text': 'x' * (256 * 1024 + 1)},
])
def test_robots_cache_rejects_nonorigin_inputs_or_unverified_responses(clean_db, payload):
    with pytest.raises(ValueError):
        limit('robots_put', payload)


def _old_baseline():
    with persistence.engine().begin() as connection:
        connection.execute(text('DROP TABLE skill_host_limits, skill_pending_artifact_deletes, skill_robots_cache'))
        connection.execute(text('ALTER TABLE skill_exports DROP COLUMN report_hash'))
        connection.execute(text("UPDATE alembic_version SET version_num='skill_v2_001'"))


@pytest.mark.parametrize('entrypoint', ['prepare', 'alembic'])
def test_additive_schema_upgrade_preserves_existing_batch(clean_db, entrypoint):
    runs.create('retained-baseline', {'name': 'Existing batch', 'industry': 'freight', 'countries': ['US']})
    _old_baseline()
    try:
        if entrypoint == 'prepare':
            assert persistence.initialize()['schema_revision'] == persistence.SCHEMA_REVISION
        else:
            env = {**os.environ, 'SCA_DATABASE_URL': settings.database_url}
            result = subprocess.run([sys.executable, '-m', 'alembic', '-c', str(ROOT / 'backend' / 'alembic.ini'),
                                     'upgrade', 'head'], env=env, cwd=ROOT, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stderr.decode(errors='replace')
        with persistence.transaction() as session:
            assert session.get(Run, 'retained-baseline').spec_json['name'] == 'Existing batch'
        with persistence.engine().connect() as connection:
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == persistence.SCHEMA_REVISION
            assert {'skill_host_limits', 'skill_pending_artifact_deletes', 'skill_robots_cache'} <= set(inspect(connection).get_table_names())
            assert 'report_hash' in {column['name'] for column in inspect(connection).get_columns('skill_exports')}
    finally:
        persistence.initialize()


def test_upgrade_refuses_incomplete_baseline(clean_db):
    _old_baseline()
    with persistence.engine().begin() as connection:
        connection.execute(text('DROP TABLE skill_maintenance'))
    try:
        with pytest.raises(AppError) as exc:
            persistence.initialize()
        assert exc.value.code == 'SCHEMA_INCOMPLETE'
        with persistence.engine().connect() as connection:
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == 'skill_v2_001'
    finally:
        with persistence.engine().begin() as connection:
            MaintenanceState.__table__.create(connection)
        persistence.initialize()
