from datetime import timedelta
import threading

import pytest
from sqlalchemy import select
from app import runs
from app.errors import AppError
from app.persistence import transaction, utcnow
from app.persistence.models import Run, Job, Company, Operation, Export, MaintenanceState, Observation


SPEC = {'name': '美国货代', 'industry': 'freight forwarding', 'countries': ['US'], 'quantity': 50}


def candidate(name='Acme', **extra):
    return dict(company_name=name, website_url='https://example.com', country_code='US',
        source_url='https://example.com/about', source_type='official_site',
        source_excerpt='A US freight forwarding company', retrieved_at=utcnow().isoformat(), **extra)


def record(task, pages):
    return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
        domain='example.com', country_code='US', status='qualified',
        qualification={'decision': 'qualified', 'confidence': 1, 'reasons': []},
        contacts=[], sources=[], demo_only=True)


def test_process_preserves_retry_after_and_injects_shared_limiter(clean_db, monkeypatch):
    import app.pipeline
    from app.web.observation import current_host_limiter
    def limited(task, pages):
        assert current_host_limiter() is runs.host_limit
        raise AppError('RATE_LIMITED', 'synthetic source cooldown', retryable=True,
                       retry_after_s=120, upstream_status=429)
    monkeypatch.setattr(app.pipeline, 'process_company', limited)
    runs.create('persistent-cooldown', SPEC)
    runs.append('persistent-cooldown', [candidate()])
    before = utcnow()
    result = runs.process('persistent-cooldown', max_jobs=1)
    assert result['processed_jobs'] == 1
    assert result['research']['next_retry_at'] is not None
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'persistent-cooldown'))
        assert job.state == 'queued'
        assert job.error_json['retry_after_s'] == 120
        assert job.error_json['upstream_status'] == 429
        assert job.available_at >= before + timedelta(seconds=120)


def test_run_records_execution_release(clean_db):
    from app.persistence import SCHEMA_REVISION
    run = runs.create('versioned', SPEC)
    assert run['plan']['release']['schema_revision'] == SCHEMA_REVISION
    assert run['plan']['release']['core_version'] == '2.0.1'


def test_chunk_replay_hash_and_version(clean_db):
    runs.create('run1', SPEC)
    values = [candidate()]
    first = runs.append('run1', values, chunk_id='chunk1', checkpoint={'page': 2}, external_tool_usage={'search': 3})
    assert runs.append('run1', values, chunk_id='chunk1', checkpoint={'page': 2}, external_tool_usage={'search': 3}) == first
    current = runs.status('run1')
    assert current['research']['admitted_count'] == 1
    assert current['research']['external_tool_usage'] == {'search': 3}
    with pytest.raises(AppError, match='操作键'):
        runs.append('run1', [candidate('Other')], chunk_id='chunk1')
    with pytest.raises(AppError) as exc:
        runs.append('run1', [], chunk_id='old-version', expected_version=1)
    assert exc.value.code == 'VERSION_CONFLICT'


def test_page_evidence_and_shared_domain_entities(clean_db):
    runs.create('run2', SPEC)
    bad = candidate()
    del bad['source_excerpt']
    with pytest.raises(AppError) as exc:
        runs.append('run2', [bad])
    assert exc.value.code == 'MISSING_EVIDENCE'
    values = [candidate('Company A'), candidate('Company B')]
    result = runs.append('run2', values)
    assert result['admission']['accepted'] == 2
    assert runs.append('run2', [candidate('Company A')])['admission']['duplicates'] == 1


def test_domain_hint_upgrade_keeps_one_identity(clean_db):
    runs.create('hint', SPEC)
    value = candidate()
    del value['company_name']
    runs.append('hint', [value])
    named = candidate('Named Legal Entity')
    assert runs.append('hint', [named])['admission']['duplicates'] == 1
    assert runs.append('hint', [named], chunk_id='third') ['admission']['duplicates'] == 1
    with transaction() as session:
        jobs = list(session.scalars(select(Job).where(Job.run_id == 'hint')))
        assert len(jobs) == 1
        assert jobs[0].input_json['company_name'] == 'Named Legal Entity'


def test_pause_clock_and_resume(clean_db):
    runs.create('clock', SPEC)
    with transaction() as session:
        row = session.get(Run, 'clock')
        row.active_started_at = utcnow() - timedelta(seconds=100)
    paused = runs.status('clock', action='pause')
    assert 100 <= paused['research']['active_wall_seconds'] < 105
    with transaction() as session:
        row = session.get(Run, 'clock')
        row.active_started_at = utcnow() - timedelta(days=5)
    assert runs.status('clock')['research']['active_wall_seconds'] < 105
    resumed = runs.status('clock', action='resume')
    assert resumed['status'] == 'running'
    assert resumed['research']['active_wall_seconds'] < 105


def test_process_commit_replay_and_finish(clean_db, monkeypatch):
    import app.pipeline
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.create('processing', SPEC)
    runs.append('processing', [candidate('A'), candidate('B')])
    first = runs.process('processing', max_jobs=1, operation_key='process1')
    assert first['processed_jobs'] == 1
    assert runs.process('processing', max_jobs=1, operation_key='process1') == first
    assert runs.status('processing')['research']['processed'] == 1
    ended = runs.finish('processing', checkpoint={'done': True})
    assert ended['status'] == 'partial'
    assert ended['research']['processed'] == 2
    assert ended['checkpoint'] == {'done': True}


def test_expired_lease_reclaim_and_fence(clean_db, monkeypatch):
    import app.pipeline
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.create('fencing', SPEC)
    runs.append('fencing', [candidate()])
    with transaction() as session:
        run = session.get(Run, 'fencing')
        run.process_token = 'old'
        run.process_until = utcnow() - timedelta(seconds=5)
        job = session.scalar(select(Job).where(Job.run_id == run.id))
        job.state, job.attempt, job.lease_token = 'running', 1, 'old-lease'
        job.lease_until = utcnow() - timedelta(seconds=5)
        old_task = dict(run_id=run.id, id=job.id, process_token='old', lease_token='old-lease')
        session.add(Observation(id='expired-send', run_id=run.id, job_id=job.id, kind='http', state='started',
            started_at=utcnow(), data_json={'attempt': 1, 'purpose': 'qualification'}))
    result = runs.process('fencing')
    assert result['research']['processed'] == 1
    assert runs._complete(old_task, {}, [], None, 0, 'old-op') is False
    with transaction() as session:
        assert len(list(session.scalars(select(Company)))) == 1


def test_process_recovers_committed_progress_after_process_death(clean_db, monkeypatch):
    import app.pipeline
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.create('crashed', SPEC)
    runs.append('crashed', [candidate('A'), candidate('B')])
    runs.process('crashed', max_jobs=1)
    with transaction() as session:
        run = session.get(Run, 'crashed')
        operation, _ = runs._operation(session, run, 'process', 'crash-key',
            dict(max_jobs=2, max_seconds=60, resume=False))
        operation.state = 'running'
        operation.result_json = {'processed_jobs': 1, '_owner': 'dead-process'}
        run.process_token = 'dead-process'
        run.process_until = utcnow() - timedelta(seconds=5)
    result = runs.process('crashed', max_jobs=2, operation_key='crash-key')
    assert result['processed_jobs'] == 2
    assert result['research']['processed'] == 2
    with transaction() as session:
        assert len(list(session.scalars(select(Company)))) == 2
    assert runs.process('crashed', max_jobs=2, operation_key='crash-key') == result


def test_retry_stops_after_three_attempts_and_manual_retry_resets(clean_db, monkeypatch):
    import app.pipeline
    def fail(task, pages):
        raise AppError('TEMPORARY', '临时故障', retryable=True)
    monkeypatch.setattr(app.pipeline, 'process_company', fail)
    runs.create('retry', SPEC)
    runs.append('retry', [candidate()])
    for attempt in range(3):
        with transaction() as session:
            job = session.scalar(select(Job).where(Job.run_id == 'retry'))
            job.available_at = utcnow()
        runs.process('retry')
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'retry'))
        assert job.attempt == 3 and job.state == 'failed'
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.status('retry', action='retry')
    assert runs.process('retry')['research']['processed'] == 1


def test_company_page_budget_counts_failed_attempts(clean_db, monkeypatch):
    import app.pipeline
    limits = []
    def fail_after_pages(task, pages):
        limits.append(task['plan']['page_limit_per_company'])
        pages.extend({'final_url': 'https://example.com/page'} for _ in range(min(6, task['plan']['page_limit_per_company'])))
        raise AppError('TEMPORARY', '临时故障', retryable=True)
    monkeypatch.setattr(app.pipeline, 'process_company', fail_after_pages)
    runs.create('page-retry', SPEC)
    runs.append('page-retry', [candidate()])
    runs.process('page-retry')
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'page-retry'))
        job.available_at = utcnow()
    result = runs.process('page-retry')
    assert limits == [10, 4]
    assert result['stats']['pages'] == 10
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'page-retry'))
        assert job.state == 'failed' and job.output_json['pages_used'] == 10


def test_killed_attempt_reserves_physical_starts_before_retry(clean_db, monkeypatch):
    import app.pipeline
    limits = []
    def resume(task, pages):
        limits.append(task['plan']['page_limit_per_company'])
        pages.extend({'final_url': 'https://example.com/page'} for _ in range(task['plan']['page_limit_per_company']))
        return record(task, pages)
    monkeypatch.setattr(app.pipeline, 'process_company', resume)
    runs.create('kill-budget', SPEC)
    runs.append('kill-budget', [candidate()])
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'kill-budget'))
        job.state, job.attempt, job.lease_token = 'running', 2, 'dead'
        job.lease_until = utcnow() - timedelta(seconds=1)
        # Two pages from an earlier settled attempt have already been charged.
        job.output_json = {'pages_used': 2}
        session.get(Run, 'kill-budget').stats_json = {'pages': 2}
        for index in range(3):
            session.add(Observation(id='kill-' + str(index), run_id='kill-budget', job_id=job.id,
                kind='http', state='started', started_at=utcnow(),
                data_json={'attempt': 2, 'purpose': 'contact'}))
        # A previous settled attempt must not be charged twice.
        session.add(Observation(id='already-settled', run_id='kill-budget', job_id=job.id,
            kind='http', state='completed', started_at=utcnow(), finished_at=utcnow(),
            data_json={'attempt': 1, 'purpose': 'qualification'}))
    result = runs.process('kill-budget')
    assert limits == [5]
    assert result['stats']['pages'] == 10
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'kill-budget'))
        assert job.output_json['pages_used'] == 10


def test_killed_attempt_without_observation_closes_budget_conservatively(clean_db, monkeypatch):
    import app.pipeline
    def should_not_fetch(task, pages):
        raise AssertionError('Unknown prior consumption must never receive a fresh page allowance')
    monkeypatch.setattr(app.pipeline, 'process_company', should_not_fetch)
    runs.create('unknown-kill', SPEC)
    runs.append('unknown-kill', [candidate()])
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'unknown-kill'))
        job.state, job.attempt, job.lease_token = 'running', 1, 'dead'
        job.lease_until = utcnow() - timedelta(seconds=1)
    result = runs.process('unknown-kill')
    assert result['stats']['pages'] == 10
    assert result['research']['failed'] == 1
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'unknown-kill'))
        assert job.output_json['recovery_policy'] == 'remaining_budget_reserved'
        assert job.error_json['code'] == 'INTERRUPTED_PAGE_BUDGET'


def test_live_process_exclusion_and_pause_fence(clean_db, monkeypatch):
    import app.pipeline
    entered, release = threading.Event(), threading.Event()
    def slow(task, pages):
        entered.set()
        assert release.wait(15)
        return record(task, pages)
    monkeypatch.setattr(app.pipeline, 'process_company', slow)
    runs.create('concurrent', SPEC)
    runs.append('concurrent', [candidate()])
    errors = []
    def worker():
        try:
            runs.process('concurrent')
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    try:
        with pytest.raises(AppError) as exc:
            runs.process('concurrent')
        assert exc.value.code == 'PROCESS_BUSY'
        assert runs.status('concurrent', action='pause')['status'] == 'paused'
    finally:
        release.set()
        thread.join(15)
    assert not errors
    assert runs.status('concurrent')['research']['processed'] == 0
    with transaction() as session:
        job = session.scalar(select(Job).where(Job.run_id == 'concurrent'))
        assert job.output_json['pages_used'] == 10
        assert job.error_json['code'] == 'INTERRUPTED_PAGE_BUDGET'


def test_pause_blocks_subsequent_physical_sends(clean_db, monkeypatch):
    import app.pipeline
    from app.web.observation import observe_http
    entered, release = threading.Event(), threading.Event()
    sends, errors = [], []
    def research(task, pages):
        with observe_http('https://example.com/first') as outcome:
            sends.append('first')
            outcome['http_status'] = 200
        entered.set()
        assert release.wait(15)
        with observe_http('https://example.com/second'):
            sends.append('second')
        return record(task, pages)
    monkeypatch.setattr(app.pipeline, 'process_company', research)
    runs.create('pause-sends', SPEC)
    runs.append('pause-sends', [candidate()])
    def worker():
        try:
            runs.process('pause-sends')
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    try:
        runs.status('pause-sends', action='pause')
    finally:
        release.set()
        thread.join(15)
    assert not errors
    assert sends == ['first']


def test_candidate_and_time_caps(clean_db):
    runs.create('caps', SPEC)
    for number in range(8):
        result = runs.append('caps', [candidate(str(number * 20 + i)) for i in range(20)])
    assert result['research']['admitted_count'] == 150
    assert result['research']['stop_reason'] == 'candidate_limit'
    assert result['admission']['skipped'] == 10
    runs.create('timecap', SPEC)
    with transaction() as session:
        session.get(Run, 'timecap').active_started_at = utcnow() - timedelta(hours=9)
    result = runs.status('timecap')
    assert result['status'] == 'partial'
    assert result['research']['stop_reason'] == 'time_limit'


def test_export_revoke_on_delete_and_suppression(clean_db, monkeypatch):
    import app.pipeline
    import app.delivery.exporting
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    def build(records, manifest, **kwargs):
        return b'export bytes', 'leads.xlsx', 'xlsx', {**manifest, 'quality_report': {'count': len(records)}}
    monkeypatch.setattr(app.delivery.exporting, 'build_export', build)
    runs.create('exporting', SPEC)
    runs.append('exporting', [candidate()])
    runs.process('exporting')
    with transaction() as session:
        session.get(Run, 'exporting').demo_only = False
    first = runs.export('exporting', export_key='one')
    assert runs.export('exporting', export_key='one')['file'] == first['file']
    from pathlib import Path
    assert Path(first['file']).exists()
    sup = runs.suppression_add('domain', 'example.com')
    assert not Path(first['file']).exists()
    with pytest.raises(AppError) as exc:
        runs.export('exporting', export_key='one')
    assert exc.value.code == 'EXPORT_REVOKED'
    runs.suppression_remove(sup['id'])
    second = runs.export('exporting', export_key='two')
    with transaction() as session:
        company_id = session.scalar(select(Company.id))
    runs.delete_company(company_id)
    assert not Path(second['file']).exists()
    with transaction() as session:
        job = session.scalar(select(Job))
        assert job.input_json == {'company_id': company_id, 'deleted': True}
        assert job.state == 'deleted'


def test_finish_closed_updates_metadata_once_and_detects_conflict(clean_db):
    runs.create('finished', SPEC)
    runs.append('finished', [], finalize=True)
    result = runs.finish('finished', checkpoint={'page': 'final'}, external_tool_usage={'search': 4}, operation_key='final')
    assert result['checkpoint'] == {'page': 'final'}
    runs.finish('finished', checkpoint={'page': 'final'}, external_tool_usage={'search': 4}, operation_key='final')
    assert runs.status('finished')['research']['external_tool_usage'] == {'search': 4}
    with pytest.raises(AppError) as exc:
        runs.finish('finished', checkpoint={'page': 'other'}, external_tool_usage={'search': 4}, operation_key='final')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'


def test_periodic_maintenance_persists_throttle_across_processes(clean_db):
    first = runs.maintenance(apply=True, periodic=True)
    assert not first.get('skipped')
    # Reloading the service simulates a fresh CLI process losing module state.
    import importlib
    importlib.reload(runs)
    assert runs.maintenance(apply=True, periodic=True) == {'skipped': True}
    with transaction() as session:
        session.get(MaintenanceState, 'periodic').last_run_at = utcnow() - timedelta(seconds=61)
    assert not runs.maintenance(apply=True, periodic=True).get('skipped')


def _real_company(run_id, identifier='one'):
    now = utcnow().isoformat()
    contact_id = 'contact-' + identifier
    source = {'entity_id': contact_id, 'field': 'phone', 'value': '+12135550123',
        'source_url': 'https://example.com/contact', 'source_type': 'official_site',
        'retrieved_at': now, 'method': 'extracted', 'confidence': .9,
        'source_excerpt': 'Example Co contact +1 213 555 0123'}
    data = {'company_id': identifier, 'company_name': 'Example Co', 'website_url': 'https://example.com',
        'domain': 'example.com', 'country_code': 'US', 'industry_text': 'freight forwarding',
        'qualification': {'decision': 'qualified', 'confidence': .9}, 'status': 'qualified',
        'contacts': [{'contact_id': contact_id, 'company_id': identifier, 'phone': '+12135550123',
            'phone_status': 'valid', 'phone_type': 'company_main', 'binding_status': 'company',
            'retrieved_at': now, 'source_urls': ['https://example.com/contact'], 'inferred': False,
            'tags': ['public_business'], 'confidence': .9}], 'sources': [source,
                {'entity_id': identifier, 'field': 'website_url', 'value': 'https://example.com',
                 'source_url': 'https://directory.example.org/member/example', 'source_type': 'directory',
                 'retrieved_at': now, 'method': 'agent_page_observation',
                 'source_excerpt': 'Example Co official website https://example.com'}], 'conflicts': [], 'missing': []}
    with transaction() as session:
        session.get(Run, run_id).demo_only = False
        session.add(Company(id=identifier, run_id=run_id, identity_key=identifier, data_json=data,
            expires_at=utcnow() + timedelta(days=365)))


def test_default_export_tracks_suppression_and_freshness(clean_db, monkeypatch):
    runs.create('snapshot', SPEC)
    _real_company('snapshot')
    first = runs.export('snapshot')
    assert first['manifest']['row_counts']['companies'] == 1
    sup = runs.suppression_add('domain', 'example.com')
    empty = runs.export('snapshot')
    assert empty['manifest']['row_counts']['companies'] == 0
    runs.suppression_remove(sup['id'])
    restored = runs.export('snapshot')
    assert restored['manifest']['row_counts']['companies'] == 1
    assert restored['export_id'] != first['export_id']
    assert runs.export('snapshot')['export_id'] == restored['export_id']
    future = utcnow() + timedelta(days=181)
    monkeypatch.setattr(runs, 'utcnow', lambda: future)
    expired = runs.export('snapshot')
    assert expired['manifest']['row_counts']['companies'] == 0


def test_suppression_serializes_with_inflight_export(clean_db, monkeypatch):
    import app.delivery.exporting
    from pathlib import Path
    runs.create('export-race', SPEC)
    _real_company('export-race')
    entered, release, suppressed = threading.Event(), threading.Event(), threading.Event()
    original = app.delivery.exporting.build_export
    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(app.delivery.exporting, 'build_export', gated)
    results, errors = [], []
    def exporter():
        try:
            results.append(runs.export('export-race'))
        except Exception as error:
            errors.append(error)
    def suppressor():
        try:
            runs.suppression_add('domain', 'example.com')
            suppressed.set()
        except Exception as error:
            errors.append(error)
    first = threading.Thread(target=exporter)
    second = threading.Thread(target=suppressor)
    first.start()
    assert entered.wait(10)
    second.start()
    assert not suppressed.wait(.2)
    release.set()
    first.join(15)
    second.join(15)
    assert not errors and suppressed.is_set()
    assert not Path(results[0]['file']).exists()
    assert runs.status('export-race')['research']['contactable_count'] == 0
