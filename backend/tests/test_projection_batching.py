"""M1/M2/M3: one projection per operation, batch completions, single export selection."""
import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import runs
from app.persistence import transaction, utcnow
from app.persistence.models import Company, Job, Run


SPEC = {'spec_version': 2, 'name': 'batch', 'industry': 'freight forwarding', 'countries': ['US'], 'quantity': 50,
        'execution_policy': {'company_workers': 2, 'snapshot_every_new_qualified': 0,
                             'snapshot_interval_seconds': 60}}


def candidate(name):
    return dict(company_name=name, website_url=f'https://{name}.example.com', country_code='US',
                source_url=f'https://{name}.example.com/about', source_type='official_site',
                source_excerpt=f'{name} is a US freight forwarder',
                retrieved_at=utcnow().isoformat())


def record(task, pages):
    company_id = task['input']['company_id']
    return dict(company_id=company_id, company_name=task['input']['company_name'],
                domain='example.com', country_code='US', status='qualified',
                qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                contacts=[], sources=[], demo_only=False,
                pipeline={'stop_reason': 'fixture'})


def _counting(monkeypatch):
    from app.delivery import selection
    counts = {'delivery': 0, 'select_export': 0}
    original_delivery = selection.delivery_companies
    original_select = selection.select_export_companies

    def delivery(*args, **kwargs):
        counts['delivery'] += 1
        return original_delivery(*args, **kwargs)

    def select_export(*args, **kwargs):
        counts['select_export'] += 1
        return original_select(*args, **kwargs)

    monkeypatch.setattr(selection, 'delivery_companies', delivery)
    monkeypatch.setattr(selection, 'select_export_companies', select_export)
    return counts


def _seed_queued(monkeypatch, run_id, count=8):
    import app.pipeline
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.create(run_id, SPEC)
    with transaction() as session:
        run = session.get(Run, run_id)
        run.demo_only = False
        run.plan_json = {**run.plan_json, 'research': {**run.plan_json['research'],
            'admitted_count': count}}
        for index in range(count):
            company_id = f'batch-{index}'
            session.add(Job(id=f'batch-job-{index}', run_id=run_id, entity_key=company_id,
                input_json={'company_id': company_id, 'company_name': f'Batch {index}',
                            'country_code': 'US'},
                output_json={}, state='queued', attempt=0, available_at=utcnow()))


def test_claim_path_never_runs_full_delivery_selection(clean_db, monkeypatch):
    _seed_queued(monkeypatch, 'batch-light', 8)
    counts = _counting(monkeypatch)
    result = runs.process('batch-light', max_jobs=8, workers=2)
    assert result['research']['processed'] == 8
    # Startup and the authoritative final projection are bounded; per-job
    # completion commits do not run the cohort selection.
    assert counts['delivery'] <= 3


def test_rollback_switch_restores_per_completion_refresh(clean_db, monkeypatch):
    monkeypatch.setenv('B2B_BATCH_PROJECTION', '0')
    _seed_queued(monkeypatch, 'batch-legacy', 8)
    counts = _counting(monkeypatch)
    result = runs.process('batch-legacy', max_jobs=8, workers=1)
    assert result['research']['processed'] == 8
    assert counts['delivery'] >= 8


def _real_company(run_id, identifier):
    now = utcnow().isoformat()
    contact_id = 'contact-' + identifier
    domain = f'{identifier}.example.com'
    name = f'Example Co {identifier}'
    phone = '+1213555' + identifier.rsplit('-', 1)[-1].zfill(4)
    source = {'entity_id': contact_id, 'field': 'phone', 'value': phone,
        'source_url': f'https://{domain}/contact', 'source_type': 'official_site',
        'retrieved_at': now, 'method': 'extracted', 'confidence': .9,
        'source_excerpt': f'{name} contact {phone}'}
    data = {'company_id': identifier, 'company_name': name,
        'website_url': f'https://{domain}', 'domain': domain, 'country_code': 'US',
        'industry_text': 'freight forwarding',
        'qualification': {'decision': 'qualified', 'confidence': .9}, 'status': 'qualified',
        'contacts': [{'contact_id': contact_id, 'company_id': identifier, 'phone': phone,
            'phone_status': 'valid', 'phone_type': 'company_main', 'binding_status': 'company',
            'retrieved_at': now, 'source_urls': [f'https://{domain}/contact'], 'inferred': False,
            'tags': ['public_business'], 'confidence': .9}], 'sources': [source,
                {'entity_id': identifier, 'field': 'website_url', 'value': f'https://{domain}',
                 'source_url': f'https://{domain}/', 'source_type': 'official_site',
                 'retrieved_at': now, 'method': 'agent_page_observation',
                 'source_excerpt': f'{name} official website https://{domain}'}],
        'conflicts': [], 'missing': []}
    with transaction() as session:
        session.get(Run, run_id).demo_only = False
        session.add(Company(id=identifier, run_id=run_id, identity_key=identifier, data_json=data,
            expires_at=utcnow() + timedelta(days=365)))


def test_export_selects_once_and_metrics_do_not_redeliver(clean_db, monkeypatch):
    runs.create('batch-export', SPEC)
    for index in range(6):
        _real_company('batch-export', f'export-{index}')
    counts = _counting(monkeypatch)
    result = runs.export('batch-export', export_key='one')
    assert result['manifest']['row_counts']['companies'] == 6
    assert counts['select_export'] == 1
    assert counts['delivery'] == 0
