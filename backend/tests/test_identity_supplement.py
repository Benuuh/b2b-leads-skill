"""AC-06: one identity, evidence-backed aliases and explicit incremental supplement."""
import copy

import pytest
from sqlalchemy import select

from app import runs
from app.errors import AppError
from app.persistence import transaction
from app.persistence.models import Company, Job


def _variant(packaging_candidate, name, website='https://klarwerk.de/'):
    value = copy.deepcopy(packaging_candidate)
    value['company_name'] = name
    value['website_url'] = website
    value['source_url'] = f'https://directory.example.org/{name.split()[1].lower()}'
    value['source_excerpt'] = f'{name}, Düsseldorf, Germany, manufactures reusable industrial packaging.'
    for source in value['sources']:
        source['source_url'] = value['source_url']
        source['source_excerpt'] = source['source_excerpt'].replace(
            'Klarwerk Verpackungen GmbH', name)
    if value.get('email'):
        value['email'] = 'sales@' + website.split('//')[1].strip('/')
        for source in value['sources']:
            if source['field'] == 'email':
                source['value'] = value['email']
                source['source_excerpt'] = f'{name} — Business email: {value["email"]}'
    return value


def test_same_domain_different_legal_entities_stay_separate(clean_db, packaging_spec, packaging_candidate):
    runs.create('identity', packaging_spec)
    result = runs.append('identity', [
        _variant(packaging_candidate, 'Klarwerk Verpackungen GmbH'),
        _variant(packaging_candidate, 'Klarwerk Logistik GmbH'),
    ])
    assert result['admission']['accepted'] == 2
    with transaction() as session:
        jobs = list(session.scalars(select(Job)))
    assert len({job.entity_key for job in jobs}) == 2


def test_legal_suffix_difference_is_the_same_entity(clean_db, packaging_spec, packaging_candidate):
    runs.create('suffix', packaging_spec)
    runs.append('suffix', [_variant(packaging_candidate, 'Klarwerk Verpackungen GmbH')])
    result = runs.append('suffix', [_variant(packaging_candidate, 'Klarwerk Verpackungen AG')])
    assert result['admission']['duplicates'] == 1 and result['admission']['accepted'] == 0
    with transaction() as session:
        assert len(list(session.scalars(select(Job)))) == 1


def test_alias_merges_only_when_an_observation_names_both(clean_db, packaging_spec, packaging_candidate):
    runs.create('alias', packaging_spec)
    runs.append('alias', [_variant(packaging_candidate, 'Klarwerk Verpackungen GmbH')])
    named = _variant(packaging_candidate, 'Klarwerk Nord GmbH')
    named['aliases'] = [{
        'name': 'Klarwerk Verpackungen GmbH',
        'source_url': 'https://registry.example.org/klarwerk-nord',
        'source_type': 'registry', 'retrieved_at': named['retrieved_at'],
        'source_excerpt': 'Klarwerk Verpackungen GmbH, trading as Klarwerk Nord GmbH'}]
    assert runs.append('alias', [named])['admission']['duplicates'] == 1

    unverified = _variant(packaging_candidate, 'Klarwerk Süd GmbH')
    unverified['aliases'] = ['Klarwerk Verpackungen GmbH']
    assert runs.append('alias', [unverified])['admission']['accepted'] == 1
    with transaction() as session:
        assert len(list(session.scalars(select(Job)))) == 2


def _uncertain_candidate(packaging_candidate):
    value = copy.deepcopy(packaging_candidate)
    value['sources'] = [source for source in value['sources']
                        if source['field'] != 'offering.products']
    value.pop('offering_products', None)
    return value


def _supplement_evidence(packaging_candidate):
    return {'retrieved_at': packaging_candidate['retrieved_at'], 'sources': [{
        'field': 'offering.products', 'value': 'reusable industrial packaging',
        'source_url': 'https://klarwerk.de/products',
        'source_type': 'official_site', 'retrieved_at': packaging_candidate['retrieved_at'],
        'source_excerpt': 'Klarwerk Verpackungen GmbH manufactures reusable industrial packaging'}]}


def test_supplement_revises_unknown_without_creating_a_candidate(
        clean_db, packaging_spec, packaging_candidate):
    runs.create('supplement', packaging_spec)
    runs.append('supplement', [_uncertain_candidate(packaging_candidate)])
    runs.process('supplement', max_jobs=1)
    with transaction() as session:
        company = session.scalar(select(Company))
        company_id, revision = company.id, company.data_json['revision']
        original_times = {source['retrieved_at'] for source in company.data_json['sources']}
    assert company_id and revision == 1

    answer = runs.supplement('supplement', company_id,
                             {'expected_revision': 1, 'candidates': [_supplement_evidence(packaging_candidate)]},
                             operation_key='supplement-1')
    assert answer['decision'] == 'qualified' and answer['revision'] == 2
    assert answer['research']['qualified_by_origin'].get('supplemented_evidence') == 1
    assert answer['network_requests'] == 0 and answer['dns_requests'] == 0
    assert answer['dispatch_resumed'] is False
    again = runs.supplement('supplement', company_id,
                            {'expected_revision': 1, 'candidates': [_supplement_evidence(packaging_candidate)]},
                            operation_key='supplement-1')
    assert again == answer
    with transaction() as session:
        companies = list(session.scalars(select(Company)))
        assert len(companies) == 1
        data = companies[0].data_json
        assert data['revision'] == 2 and data['evidence_revision'] == 1
        assert data['supplement']['network_requests'] == 0
        # Retained observations keep their original collection time.
        assert original_times <= {source['retrieved_at'] for source in data['sources']}
        assert any(source['source_url'] == 'https://klarwerk.de/products' for source in data['sources'])


def test_supplement_rejects_stale_revisions_and_identity_edits(
        clean_db, packaging_spec, packaging_candidate):
    runs.create('revisions', packaging_spec)
    runs.append('revisions', [_uncertain_candidate(packaging_candidate)])
    runs.process('revisions', max_jobs=1)
    with transaction() as session:
        company_id = session.scalar(select(Company.id))
    runs.supplement('revisions', company_id,
                    {'expected_revision': 1, 'candidates': [_supplement_evidence(packaging_candidate)]},
                    operation_key='first')

    with pytest.raises(AppError) as exc:
        runs.supplement('revisions', company_id,
                        {'expected_revision': 1, 'candidates': [_supplement_evidence(packaging_candidate)]},
                        operation_key='stale')
    assert exc.value.code == 'VERSION_CONFLICT'

    renamed = _supplement_evidence(packaging_candidate)
    renamed['company_name'] = 'Other Company GmbH'
    with pytest.raises(AppError) as exc:
        runs.supplement('revisions', company_id,
                        {'expected_revision': 2, 'candidates': [renamed]}, operation_key='rename')
    assert exc.value.code == 'IMMUTABLE_IDENTITY'

    with pytest.raises(AppError) as exc:
        runs.supplement('revisions', company_id,
                        {'expected_revision': 2, 'candidates': [_supplement_evidence(packaging_candidate)]},
                        operation_key='duplicate-evidence')
    assert exc.value.code == 'NO_NEW_EVIDENCE'


def test_supplement_respects_closed_input_and_version(clean_db, packaging_spec, packaging_candidate):
    runs.create('closed', packaging_spec)
    runs.append('closed', [_uncertain_candidate(packaging_candidate)], finalize=True)
    runs.process('closed', max_jobs=1)
    with transaction() as session:
        company_id = session.scalar(select(Company.id))
    with pytest.raises(AppError) as exc:
        runs.supplement('closed', company_id,
                        {'expected_revision': 1, 'candidates': [_supplement_evidence(packaging_candidate)]},
                        operation_key='late')
    assert exc.value.code == 'INPUT_CLOSED'

    runs.create('v1-run', {'name': 'v1', 'industry': 'freight forwarding',
                           'countries': ['US'], 'quantity': 50})
    with pytest.raises(AppError) as exc:
        runs.supplement('v1-run', 'any',
                        {'expected_revision': 1, 'candidates': [_supplement_evidence(packaging_candidate)]},
                        operation_key='v1')
    assert exc.value.code == 'UNSUPPORTED_OPTION'
