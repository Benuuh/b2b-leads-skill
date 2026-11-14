"""FR-11: old inputs, frozen hashes and v1 decision semantics stay readable."""
import copy
import json

import pytest
from sqlalchemy import select

from app import cli, runs
from app.errors import AppError
from app.persistence import transaction
from app.persistence.models import Job, Run

SPEC = {'spec_version': 1, 'name': '美国货代', 'industry': 'freight forwarding', 'countries': ['US'], 'quantity': 50}


def candidate(**extra):
    from datetime import datetime, timezone
    value = {'company_name': 'Harbor Logistics LLC', 'country_code': 'US',
             'industry_text': 'freight forwarding', 'website_url': 'https://harbor.example.org',
             'source_url': 'https://directory.example.org/harbor', 'source_type': 'directory',
             'source_excerpt': 'Harbor Logistics LLC provides freight forwarding services in the US',
             'retrieved_at': datetime.now(timezone.utc).isoformat()}
    value.update(extra)
    return value


def test_v1_spec_hash_and_shape_are_unchanged(clean_db):
    first = runs.create('legacy', SPEC)
    assert 'spec_version' not in first['spec']
    assert first['spec_version'] == 1 and first['plan']['spec_version'] == 1
    assert 'qualification_policy' not in first['spec']
    with transaction() as session:
        stored = session.get(Run, 'legacy')
        assert stored.spec_json == {'name': '美国货代', 'industry': 'freight forwarding',
            'industry_codes': [], 'countries': ['US'], 'regions': [], 'employee_range': '',
            'revenue_range': '', 'company_types': [], 'roles': [], 'keywords': [],
            'exclude_keywords': [], 'example_domains': [], 'exclusions': [], 'languages': [],
            'quantity': 50, 'freshness_days': 180, 'notes': '', 'paid_sources': False, 'budget': 0}
    again = runs.create('legacy', SPEC)
    assert again['id'] == first['id'] and again['spec'] == first['spec']


def test_v1_run_rejects_versioned_fact_fields(clean_db):
    runs.create('legacy-fields', SPEC)
    value = candidate(headquarters_country='US')
    result = runs.validate_input(SPEC, {'candidates': [value]})
    assert result['spec_version'] == 1
    assert result['records'][0]['reason_code'] == 'UNSUPPORTED_FIELD'
    with pytest.raises(AppError) as exc:
        runs.append('legacy-fields', [value])
    assert exc.value.code == 'UNSUPPORTED_FIELD'


def test_v1_processing_and_export_keep_their_contract(clean_db, monkeypatch):
    import app.pipeline
    import app.delivery.exporting

    def record(task, pages):
        return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
                    domain='harbor.example.org', country_code='US', status='qualified',
                    qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    contacts=[], sources=[], demo_only=False,
                    pipeline={'stop_reason': 'contact_target_reached'})

    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.create('legacy-export', SPEC)
    with transaction() as session:
        session.get(Run, 'legacy-export').demo_only = False
    runs.append('legacy-export', [candidate()])
    outcome = runs.process('legacy-export', max_jobs=1)
    assert outcome['processed_jobs'] == 1
    assert outcome['research']['spec_version'] == 1
    assert 'policy' not in outcome
    runs.finish('legacy-export')
    exported = runs.export('legacy-export')
    assert exported['manifest']['spec_version'] == 1
    assert 'policy' not in exported['manifest']
    assert exported['manifest']['metrics_version'].startswith('b2b-research-metrics-')


def test_v1_append_replay_is_idempotent(clean_db):
    runs.create('legacy-chunk', SPEC)
    values = [candidate()]
    first = runs.append('legacy-chunk', values, chunk_id='r1')
    assert runs.append('legacy-chunk', values, chunk_id='r1') == first
    with transaction() as session:
        assert len(list(session.scalars(select(Job)))) == 1


def test_capabilities_and_validate_input_are_available_from_the_cli(tmp_path, capsys):
    assert cli.main(['capabilities']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['spec_versions'] == [1, 2] and payload['limits']['max_jobs_per_process']['max'] == 20

    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps(SPEC), encoding='utf-8')
    body = tmp_path / 'input.json'
    body.write_text(json.dumps({'candidates': [candidate()]}), encoding='utf-8')
    assert cli.main(['validate-input', '--spec-json', str(spec), '--input-json', str(body)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts'] == {'input': 1, 'append_ready': 1, 'rejected': 0}
