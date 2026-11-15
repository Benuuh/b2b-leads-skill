"""Shared-page-budget-1 ledger, company contention, and schema upgrade.

G1: a company-only workload keeps the existing page count and writes no
directory ledger row. G2: reservation, settlement, cache, retry, redirect
accounting, crash recovery, unknown sends, pause, idempotency, and races
stay inside the frozen total. Automatic collection stays refused.
"""
from datetime import timedelta
import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import inspect, select, text

from app import runs
from app.config import ROOT, settings
from app.errors import AppError
from app.page_budget import (
    CONTRACT, recover, refuse_automatic_collection, reserve, settle, summarize,
)
from app.persistence import transaction, utcnow
from app.persistence.models import Job, Observation, PageReservation, RobotsCache, Run
from app.segment_brief import inspect_segment


def _spec():
    return {'name': '目录预算', 'industry': 'trade association', 'countries': ['DE'], 'quantity': 50}


def _open(name, total=10, *, contract=True):
    prepared = inspect_segment(_spec())
    created = runs.create(name, _spec(), confirm_hash=prepared['confirm_hash'])
    with transaction() as session:
        run = session.get(Run, name)
        plan = dict(run.plan_json)
        plan['page_limit_total'] = total
        research = dict(plan.get('research') or {})
        research['checkpoint'] = {'page': 1, 'kept': True}
        plan['research'] = research
        if not contract:
            plan['page_budget_contract'] = None
        run.plan_json = plan
        run.demo_only = False
    return created


def _counts(run_id):
    with transaction() as session:
        run = session.get(Run, run_id)
        stats = run.stats_json or {}
        return int(stats.get('pages') or 0), int(stats.get('reserved_pages') or 0), run


def test_company_path_page_count_matches_with_and_without_the_contract(clean_db, monkeypatch):
    import app.pipeline

    def record(task, pages):
        pages.extend([{'final_url': 'https://example.com/a'}, {'final_url': 'https://example.com/b'}])
        return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
                    domain='example.com', country_code='DE', status='qualified',
                    qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    contacts=[], sources=[], demo_only=False,
                    pipeline={'stop_reason': 'fixture'})

    monkeypatch.setattr(app.pipeline, 'process_company', record)
    _open('g1-contract')
    _open('g1-legacy', contract=False)
    candidate = {'company_name': 'Directory Member GmbH', 'country_code': 'DE',
                 'industry_text': 'trade association', 'website_url': 'https://member.example.org',
                 'source_url': 'https://association.example.org/member', 'source_type': 'directory',
                 'source_excerpt': 'Directory Member GmbH is a trade association in Germany',
                 'retrieved_at': utcnow().isoformat()}
    for run_id in ('g1-contract', 'g1-legacy'):
        runs.append(run_id, [candidate])
        assert runs.process(run_id, max_jobs=1, workers=1)['processed_jobs'] == 1
    contracted, legacy = _counts('g1-contract'), _counts('g1-legacy')
    assert contracted[0] == legacy[0] == 2
    assert contracted[1] == legacy[1] == 0
    with transaction() as session:
        assert session.scalar(select(PageReservation.id)) is None
        assert session.get(Run, 'g1-legacy').plan_json.get('page_budget_contract') is None


def test_list_page_is_one_unit_for_many_companies_and_redirects_are_not_pages(clean_db):
    _open('list-page', total=5)
    reserved = reserve('list-page', operation_key='members', url='https://assoc.example/members',
                       page_type='list', companies=['Alpha GmbH', 'Beta GmbH', 'Gamma GmbH'])
    assert reserved['pages_reserved'] == 1 and reserved['subjects'] == ['Alpha GmbH', 'Beta GmbH', 'Gamma GmbH']
    settled = settle('list-page', operation_key='members', attempt=1, outcome='ok',
                     send_state='started', physical_attempts=4)
    assert settled['pages_consumed'] == 1 and settled['physical_attempts'] == 4
    pages, reserved_pages, _ = _counts('list-page')
    assert pages == 1 and reserved_pages == 0
    again = settle('list-page', operation_key='members', attempt=1, outcome='ok', send_state='started')
    assert again['replay'] is True
    assert _counts('list-page')[0] == 1


def test_detail_pages_share_the_company_cap_and_the_run_total(clean_db):
    _open('details', total=30)
    for attempt in range(1, 11):
        reserve('details', operation_key='detail', attempt=attempt, url=f'https://assoc.example/m/{attempt}',
                page_type='detail', company_key='company-a')
        settle('details', operation_key='detail', attempt=attempt, outcome='ok', send_state='started',
               physical_attempts=2)
    with pytest.raises(AppError) as exc:
        reserve('details', operation_key='detail', attempt=11, url='https://assoc.example/m/11',
                page_type='detail', company_key='company-a')
    assert exc.value.code == 'PAGE_BUDGET_EXCEEDED' and exc.value.details['scope'] == 'company'
    listed = reserve('details', operation_key='more-list', url='https://assoc.example/members?page=2',
                     page_type='list', companies=['company-a', 'company-b'])
    assert listed['pages_reserved'] == 1
    with transaction() as session:
        run = session.get(Run, 'details')
        run.process_token = 'tok'
        run.process_until = utcnow() + timedelta(seconds=30)
        session.add(Job(id='job-company-a', run_id='details', entity_key='company-a',
                        input_json={'company_id': 'company-a', 'company_name': 'A'},
                        output_json={'pages_used': 0}, state='queued', attempt=0))
    claimed = runs._claim('details', 'tok')
    assert claimed is not None
    assert claimed['reserved_pages'] == 0
    pages, reserved_pages, run = _counts('details')
    assert pages + reserved_pages <= run.plan_json['page_limit_total']


def test_directory_and_company_claim_compete_for_the_last_page(clean_db):
    _open('last-page', total=1)
    reserve('last-page', operation_key='only', url='https://assoc.example/only', page_type='list')
    with transaction() as session:
        run = session.get(Run, 'last-page')
        run.process_token = 'tok'
        run.process_until = utcnow() + timedelta(seconds=30)
        session.add(Job(id='job-last', run_id='last-page', entity_key='company-z',
                        input_json={'company_id': 'company-z', 'company_name': 'Z'},
                        output_json={}, state='queued', attempt=0))
    assert runs._claim('last-page', 'tok') is None
    pages, reserved_pages, run = _counts('last-page')
    assert pages + reserved_pages == 1
    assert run.plan_json['research']['checkpoint'] == {'page': 1, 'kept': True}

    _open('company-first', total=1)
    with transaction() as session:
        run = session.get(Run, 'company-first')
        run.process_token = 'tok'
        run.process_until = utcnow() + timedelta(seconds=30)
        session.add(Job(id='job-first', run_id='company-first', entity_key='company-z',
                        input_json={'company_id': 'company-z', 'company_name': 'Z'},
                        output_json={}, state='queued', attempt=0))
    assert runs._claim('company-first', 'tok')['reserved_pages'] == 1
    with pytest.raises(AppError) as exc:
        reserve('company-first', operation_key='late', url='https://assoc.example/late', page_type='list')
    assert exc.value.code == 'PAGE_BUDGET_EXCEEDED' and exc.value.details['scope'] == 'run'
    pages, reserved_pages, run = _counts('company-first')
    assert pages + reserved_pages <= 1
    assert run.plan_json['research']['input_open'] is False
    assert run.plan_json['research']['checkpoint']['kept'] is True


def test_parallel_reserves_cannot_overspend_the_last_page(clean_db):
    _open('race', total=1)
    barrier = threading.Barrier(2)
    found, errors = [], []

    def attempt(key):
        try:
            barrier.wait(timeout=5)
            found.append(reserve('race', operation_key=key, url=f'https://assoc.example/{key}', page_type='list'))
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(key,)) for key in ('left', 'right')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(found) == 1
    assert len(errors) == 1 and errors[0].code == 'PAGE_BUDGET_EXCEEDED'
    pages, reserved_pages, _ = _counts('race')
    assert pages + reserved_pages == 1


def test_cache_is_run_local_and_ignores_robots_cache(clean_db):
    _open('cached', total=5)
    _open('other', total=5)
    reserve('cached', operation_key='origin', url='https://assoc.example/members', page_type='list')
    settle('cached', operation_key='origin', attempt=1, outcome='ok', send_state='started', physical_attempts=1)
    hit = reserve('cached', operation_key='reuse', url='https://assoc.example/members',
                  page_type='list', use_cache=True)
    assert hit['cache_hit'] is True and hit['pages_reserved'] == 0
    assert _counts('cached')[0] == 1
    with transaction() as session:
        session.add(RobotsCache(origin='https://robots.example', status=200, rules_text='User-agent: *\nAllow: /',
                                fetched_at=utcnow(), expires_at=utcnow() + timedelta(hours=1)))
    fresh = reserve('cached', operation_key='robots-not-page', url='https://robots.example/members',
                    page_type='list', use_cache=True)
    assert fresh['cache_hit'] is False and fresh['pages_reserved'] == 1
    other = reserve('other', operation_key='cross', url='https://assoc.example/members',
                    page_type='list', use_cache=True)
    assert other['cache_hit'] is False and other['pages_reserved'] == 1


def test_replay_conflict_retry_unknown_and_cancel(clean_db):
    _open('idem', total=5)
    first = reserve('idem', operation_key='same', url='https://assoc.example/a', page_type='list')
    replay = reserve('idem', operation_key='same', url='https://assoc.example/a', page_type='list')
    assert replay['replay'] is True and replay['id'] == first['id']
    assert _counts('idem')[1] == 1
    with pytest.raises(AppError) as exc:
        reserve('idem', operation_key='same', url='https://assoc.example/b', page_type='list')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    assert _counts('idem')[1] == 1
    settle('idem', operation_key='same', attempt=1, outcome='failed', send_state='started', physical_attempts=1)
    reserve('idem', operation_key='same', attempt=2, url='https://assoc.example/a', page_type='list')
    settle('idem', operation_key='same', attempt=2, outcome='timeout', send_state='unknown', physical_attempts=3)
    pages, reserved_pages, _ = _counts('idem')
    assert pages == 2 and reserved_pages == 0
    with transaction() as session:
        view = summarize(session, session.get(Run, 'idem'))
    assert view['unknown'] == 1 and view['within_budget'] is True
    reserve('idem', operation_key='drop', url='https://assoc.example/c', page_type='list')
    cancelled = settle('idem', operation_key='drop', attempt=1, outcome='cancelled', send_state='not_started')
    assert cancelled['state'] == 'released' and cancelled['pages_consumed'] == 0
    assert _counts('idem')[0] == 2


def test_crash_recovery_uses_observations_and_keeps_unknown_sends(clean_db, monkeypatch):
    _open('crash', total=5)
    reserve('crash', operation_key='died', url='https://assoc.example/died', page_type='list')
    with transaction() as session:
        session.add(Observation(id='obs-died', run_id='crash', kind='http', state='completed',
                                started_at=utcnow(),
                                data_json={'operation_key': 'died', 'attempt': 1, 'dispatched': True}))
    recovered = recover('crash', operation_key='died', attempt=1)
    assert recovered['state'] == 'settled' and recovered['pages_consumed'] == 1
    assert recover('crash', operation_key='died', attempt=1)['replay'] is True
    assert _counts('crash')[0] == 1

    reserve('crash', operation_key='silent', url='https://assoc.example/silent', page_type='list')
    unknown = recover('crash', operation_key='silent', attempt=1)
    assert unknown['state'] == 'unknown' and unknown['pages_consumed'] == 1
    assert _counts('crash')[0] == 2

    reserve('crash', operation_key='blocked', url='https://assoc.example/blocked', page_type='list')
    with transaction() as session:
        session.add(Observation(id='obs-blocked', run_id='crash', kind='http', state='completed',
                                started_at=utcnow(),
                                data_json={'operation_key': 'blocked', 'attempt': 1, 'dispatched': False}))
    released = recover('crash', operation_key='blocked', attempt=1)
    assert released['state'] == 'released'
    assert _counts('crash')[0] == 2

    reserve('crash', operation_key='fault', url='https://assoc.example/fault', page_type='list')
    def boom(*args, **kwargs):
        raise RuntimeError('observation store unavailable')
    monkeypatch.setattr('app.page_budget._observation', boom)
    faulted = recover('crash', operation_key='fault', attempt=1)
    assert faulted['state'] == 'unknown'
    pages, reserved_pages, run = _counts('crash')
    assert reserved_pages == 0 and pages == 3
    assert pages + reserved_pages <= run.plan_json['page_limit_total']


def test_pause_blocks_new_reserves_and_old_runs_refuse_collection(clean_db):
    _open('paused', total=3)
    reserve('paused', operation_key='before', url='https://assoc.example/before', page_type='list')
    settle('paused', operation_key='before', attempt=1, outcome='ok', send_state='started')
    assert runs.status('paused', action='pause')['status'] == 'paused'
    with pytest.raises(AppError) as exc:
        reserve('paused', operation_key='after', url='https://assoc.example/after', page_type='list')
    assert exc.value.code == 'RUN_PAUSED'
    assert reserve('paused', operation_key='before', url='https://assoc.example/before', page_type='list')['replay'] is True

    _open('legacy', contract=False)
    with pytest.raises(AppError) as exc:
        reserve('legacy', operation_key='nope', url='https://assoc.example/nope', page_type='list')
    assert exc.value.code == 'PAGE_BUDGET_UNAVAILABLE'
    with pytest.raises(AppError) as exc:
        refuse_automatic_collection('legacy')
    assert exc.value.code == 'SHARED_COLLECTION_REFUSED'
    with pytest.raises(AppError) as exc:
        refuse_automatic_collection('paused')
    assert exc.value.code == 'SHARED_COLLECTION_UNAVAILABLE'
    assert exc.value.details['automatic_js_rendering'] == 'unavailable'
    status = runs.status('paused')
    assert status['page_budget']['automatic_shared_directory_collection'] == 'unavailable'
    assert status['page_budget']['within_budget'] is True
    assert 'page_budget' not in runs.status('legacy')


def test_upgrade_from_003_is_additive_and_downgrade_is_refused(clean_db):
    _open('kept', contract=False)
    with transaction() as session:
        from app.persistence import engine
        connection = session.connection()
        connection.execute(text('DROP TABLE skill_page_reservations'))
        connection.execute(text("UPDATE alembic_version SET version_num='skill_v2_003'"))
    from app import persistence
    assert persistence.initialize()['schema_revision'] == 'skill_v2_004'
    with persistence.engine().connect() as connection:
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == 'skill_v2_004'
        assert 'skill_page_reservations' in inspect(connection).get_table_names()
        assert 'prospect_source_candidates' in inspect(connection).get_table_names()
    assert runs.status('kept')['spec_version'] == 2
    with pytest.raises(AppError) as exc:
        refuse_automatic_collection('kept')
    assert exc.value.code == 'SHARED_COLLECTION_REFUSED'

    import importlib.util
    path = ROOT / 'backend' / 'migrations' / 'versions' / 'skill_v2_004.py'
    spec = importlib.util.spec_from_file_location('skill_v2_004', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match='isolated'):
        module.downgrade()


def test_alembic_upgrade_from_003_creates_only_the_budget_table(clean_db):
    from app import persistence
    with persistence.engine().begin() as connection:
        connection.execute(text('DROP TABLE skill_page_reservations'))
        connection.execute(text("UPDATE alembic_version SET version_num='skill_v2_003'"))
    env = {**os.environ, 'SCA_DATABASE_URL': settings.database_url}
    result = subprocess.run([sys.executable, '-m', 'alembic', '-c', str(ROOT / 'backend' / 'alembic.ini'),
                             'upgrade', 'head'], env=env, cwd=ROOT, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    with persistence.engine().connect() as connection:
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == 'skill_v2_004'
        assert 'skill_page_reservations' in inspect(connection).get_table_names()
