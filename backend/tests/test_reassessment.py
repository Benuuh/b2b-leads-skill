"""Saved-evidence recovery must be auditable, offline and atomic."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import runs
from app.config import settings
from app.errors import AppError
from app.persistence import artifact_path, transaction, utcnow
from app.persistence.models import Audit, Company, Job, Operation, Run, Snapshot


SPEC = {'name': 'Saved forwarders', 'industry': 'freight forwarding',
        'keywords': ['freight forwarding'], 'countries': ['US'], 'quantity': 50}
SITE = 'https://saved-forwarder.example.com/'


def completed_review(monkeypatch, run_id='review', *, name='Saved Forwarder', conflict=None):
    import app.pipeline
    from app.rules.contacts import extract_company

    when = (utcnow() - timedelta(days=2)).isoformat()
    value = {'company_name': name, 'website_url': SITE, 'country_code': 'US',
        'city': 'Houston', 'source_url': SITE, 'source_type': 'official_site', 'retrieved_at': when,
        'source_excerpt': name + ' provides freight forwarding services.',
        'sources': [{'field': 'website_url', 'value': SITE, 'source_url': SITE,
            'source_type': 'official_site', 'retrieved_at': when,
            'source_excerpt': name + ' official website ' + SITE}]}
    if conflict:
        value['conflicts'] = [conflict]
    page = {'final_url': SITE, 'fetched_at': when, 'fetch_status': 'ok',
        'html': '<meta property="og:site_name" content="' + name + '">'
                '<meta name="description" content="First service description">'
                '<meta property="og:description" content="Second service description">'
                '<p>We provide freight forwarding services.</p>'
                '<a href="mailto:info@saved-forwarder.example.com">Email</a>'
                '<a href="tel:+12135550123">+1 213 555 0123</a>'}

    def historical(task, pages):
        pages.append(deepcopy(page))
        result = extract_company(task['input'], pages, task['segment'],
                                 mx_lookup=lambda _: 'invalid', now=when)
        result.update(status='needs_review', qualification={'decision': 'uncertain', 'confidence': .45,
            'reasons': ['historical semantic conflict']},
            pipeline={'pages_fetched': 1, 'qualification_pages': 1})
        return result

    monkeypatch.setattr(settings, 'fixture_mode', False)
    monkeypatch.setattr(app.pipeline, 'process_company', historical)
    runs.create(run_id, SPEC)
    runs.append(run_id, [value])
    runs.process(run_id, max_jobs=1)
    with transaction() as session:
        company = session.scalar(select(Company).where(Company.run_id == run_id))
        return company.id, deepcopy(company.data_json), when


def test_reassessment_reuses_snapshots_offline_without_freshening_or_page_charges(clean_db, monkeypatch):
    import app.web.crawler
    import app.web.dns
    company_id, previous, when = completed_review(monkeypatch)

    def unexpected_network(*args, **kwargs):
        raise AssertionError('Saved evidence must never invoke HTTP or DNS')

    monkeypatch.setattr(app.web.crawler, 'fetch_company', unexpected_network)
    monkeypatch.setattr(app.web.dns, 'lookup_mx', unexpected_network)
    before = runs.status('review')
    result = runs.reassess('review', [company_id], operation_key='fix-1')
    assert result['reassessed_companies'] == 1 and result['network_requests'] == 0
    assert result['transitions'][0]['before'] == 'uncertain'
    assert result['transitions'][0]['after'] == 'qualified'
    assert result['research']['contactable_count'] == 1
    assert result['stats']['pages'] == before['stats']['pages'] == 1
    assert result['research']['processed'] == 1 and result['research']['admitted_count'] == 1
    assert runs.reassess('review', [company_id], operation_key='fix-1') == result
    with transaction() as session:
        row = session.get(Company, company_id)
        assert row.data_json['last_verified_at'] == previous['last_verified_at']
        assert {c['last_verified_at'] for c in row.data_json['contacts']} == {when.replace('+00:00', 'Z')}
        email = next(c for c in row.data_json['contacts'] if c.get('email'))
        assert email['mx_status'] == 'invalid' and email['email_status'] == 'invalid'
        assert row.expires_at > utcnow()
        assert row.data_json['reassessment']['snapshots']
        job = session.scalar(select(Job).where(Job.run_id == 'review'))
        assert job.state == 'completed' and job.attempt == 1 and job.output_json['pages_used'] == 1
        audit = session.scalar(select(Audit).where(Audit.action == 'run.reassess'))
        assert audit.details_json['transitions'] == result['transitions']
        assert len(list(session.scalars(select(Snapshot)))) == 1
    with pytest.raises(AppError) as exc:
        runs.reassess('review', [company_id, 'other'], operation_key='fix-1')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'


def test_reassessment_does_not_override_country_or_website_conflicts(clean_db, monkeypatch):
    company_id, _, _ = completed_review(monkeypatch, conflict={
        'field': 'country_code', 'values': ['US', 'CN']})
    result = runs.reassess('review', [company_id], operation_key='country')
    assert result['transitions'][0]['after'] == 'uncertain'
    assert result['research']['contactable_count'] == 0


def test_reassessment_respects_current_suppressions(clean_db, monkeypatch):
    company_id, _, _ = completed_review(monkeypatch)
    runs.suppression_add('domain', 'saved-forwarder.example.com')
    result = runs.reassess('review', [company_id], operation_key='suppressed')
    assert result['transitions'][0]['after'] == 'excluded'
    assert result['research']['contactable_count'] == 0


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'expired'])
def test_invalid_snapshot_rolls_back_result_and_operation(clean_db, monkeypatch, damage):
    company_id, previous, _ = completed_review(monkeypatch)
    with transaction() as session:
        snapshot = session.scalar(select(Snapshot))
        path = artifact_path(snapshot.artifact_ref)
        if damage == 'expired':
            snapshot.expires_at = utcnow() - timedelta(seconds=1)
    if damage == 'corrupt':
        path.write_bytes(b'{}')
    elif damage == 'missing':
        path.unlink()
    with pytest.raises(AppError) as exc:
        runs.reassess('review', [company_id], operation_key='damaged')
    assert exc.value.code == 'SNAPSHOT_UNAVAILABLE'
    with transaction() as session:
        assert session.get(Company, company_id).data_json == previous
        assert not session.scalar(select(Operation).where(Operation.command == 'reassess'))
        assert not session.scalar(select(Audit).where(Audit.action == 'run.reassess'))


def test_reassessment_refuses_inflight_processing_and_preserves_pause(clean_db, monkeypatch):
    company_id, _, _ = completed_review(monkeypatch)
    with transaction() as session:
        session.get(Run, 'review').process_token = 'inflight'
    with pytest.raises(AppError) as exc:
        runs.reassess('review', [company_id], operation_key='busy')
    assert exc.value.code == 'PROCESS_BUSY'
    with transaction() as session:
        session.get(Run, 'review').process_token = None
    runs.status('review', action='pause')
    assert runs.reassess('review', [company_id], operation_key='paused')['status'] == 'paused'


def test_reassessment_is_bounded_and_validates_ids(clean_db):
    for ids in ([], ['x'] * 2, ['x'] * 21, [None], 'x'):
        with pytest.raises(AppError) as exc:
            runs.reassess('unused', ids, operation_key='bounds')
        assert exc.value.code == 'VALIDATION_ERROR'
