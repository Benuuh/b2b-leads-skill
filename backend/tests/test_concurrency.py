"""AC-07: bounded workers, atomic page reservation, pause/deadline fencing."""
import copy
import threading
import time

import pytest
from sqlalchemy import select

from app import runs
from app.errors import AppError
from app.persistence import transaction
from app.persistence.models import Company, Job, Run


def _record(task, pages, stop_reason='saved_evidence_sufficient'):
    return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
                domain='klarwerk.de', country_code='DE', status='qualified',
                qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                contacts=[], sources=[], demo_only=False,
                pipeline={'stop_reason': stop_reason, 'pages_fetched': len(pages)})


def _variants(packaging_candidate):
    records = []
    for index in range(4):
        value = copy.deepcopy(packaging_candidate)
        name = f'Klarwerk Verpackungen {index} GmbH'
        value['company_name'] = name
        value['source_url'] = f'https://packaging-association.org/members/klarwerk-{index}'
        value['source_excerpt'] = f'{name}, Düsseldorf, Germany, manufactures reusable industrial packaging.'
        for source in value['sources']:
            source['source_url'] = value['source_url']
            source['source_excerpt'] = source['source_excerpt'].replace(
                'Klarwerk Verpackungen GmbH', name)
        records.append(value)
    return records


def test_two_workers_process_bounded_jobs(clean_db, packaging_spec, packaging_candidate, monkeypatch):
    import app.pipeline

    active, peak, lock = [0], [0], threading.Lock()

    def research(task, pages):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return _record(task, pages)

    monkeypatch.setattr(app.pipeline, 'process_company', research)
    runs.create('parallel', packaging_spec)
    runs.append('parallel', _variants(packaging_candidate))
    result = runs.process('parallel', max_jobs=4)
    assert result['processed_jobs'] == 4
    assert result['workers'] == 2
    assert peak[0] == 2
    with transaction() as session:
        assert len(list(session.scalars(select(Company)))) == 4
        assert session.get(Run, 'parallel').stats_json['reserved_pages'] == 0


def test_v1_runs_keep_the_serial_contract(clean_db):
    runs.create('serial', {'name': 'v1', 'industry': 'freight forwarding',
                           'countries': ['US'], 'quantity': 50})
    with pytest.raises(AppError) as exc:
        runs.process('serial', workers=2)
    assert exc.value.code == 'UNSUPPORTED_OPTION'
    with transaction() as session:
        assert not session.scalar(select(Job))


def test_page_budget_is_reserved_before_dispatch(clean_db, packaging_spec, packaging_candidate, monkeypatch):
    import app.pipeline

    def consume(task, pages):
        count = task['plan']['page_limit_per_company']
        pages.extend({'final_url': f'https://klarwerk.de/{index}'} for index in range(count))
        return _record(task, pages, stop_reason='page_budget_reached')

    monkeypatch.setattr(app.pipeline, 'process_company', consume)
    runs.create('budget', packaging_spec)
    runs.append('budget', _variants(packaging_candidate))
    with transaction() as session:
        run = session.get(Run, 'budget')
        run.plan_json = {**run.plan_json, 'page_limit_total': 3}
    result = runs.process('budget', max_jobs=4)
    assert result['stats']['pages'] == 3
    assert result['processed_jobs'] == 1
    assert result['status'] == 'partial'
    with transaction() as session:
        states = sorted(job.state for job in session.scalars(select(Job)))
        assert states == ['completed', 'skipped', 'skipped', 'skipped']
        assert session.get(Run, 'budget').stats_json['reserved_pages'] == 0


def test_pause_revokes_every_inflight_lease(clean_db, packaging_spec, packaging_candidate, monkeypatch):
    import app.pipeline

    entered = threading.Barrier(3, timeout=15)
    release = threading.Event()

    def research(task, pages):
        entered.wait()
        assert release.wait(15)
        return _record(task, pages)

    monkeypatch.setattr(app.pipeline, 'process_company', research)
    runs.create('pause-parallel', packaging_spec)
    runs.append('pause-parallel', _variants(packaging_candidate)[:2])
    errors = []

    def worker():
        try:
            runs.process('pause-parallel', max_jobs=2)
        except Exception as error:  # pragma: no cover - surfaced below
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    entered.wait()
    try:
        assert runs.status('pause-parallel', action='pause')['status'] == 'paused'
    finally:
        release.set()
        thread.join(20)
    assert not errors
    with transaction() as session:
        assert not list(session.scalars(select(Company)))
        jobs = list(session.scalars(select(Job)))
        assert all(job.state == 'failed' for job in jobs)
        assert all(job.error_json['code'] == 'INTERRUPTED_PAGE_BUDGET' for job in jobs)


def test_deadline_stops_new_claims_but_allows_the_inflight_job(
        clean_db, packaging_spec, packaging_candidate, monkeypatch):
    import app.pipeline

    def slow(task, pages):
        time.sleep(1.2)
        return _record(task, pages)

    monkeypatch.setattr(app.pipeline, 'process_company', slow)
    runs.create('deadline', packaging_spec)
    runs.append('deadline', _variants(packaging_candidate))
    result = runs.process('deadline', max_jobs=4, max_seconds=1, workers=1)
    assert result['processed_jobs'] == 1
    assert result['research']['queued'] == 3
    assert result['research']['processed'] == 1
