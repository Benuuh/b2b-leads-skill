"""AC-03 per-record precheck: invalid drafts never block valid records."""
import copy

import pytest
from sqlalchemy import select

from app import runs, persistence
from app.errors import AppError
from app.persistence import transaction
from app.persistence.models import Job


def four_valid(packaging_candidate):
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


def test_validate_input_returns_the_submittable_subset(packaging_spec, packaging_candidate):
    records = four_valid(packaging_candidate)
    draft = copy.deepcopy(records[0])
    draft['company_name'] = 'Klarwerk Verpackungen Draft GmbH'
    draft.pop('source_excerpt')
    for source in draft['sources']:
        source.pop('source_excerpt', None)

    result = runs.validate_input(packaging_spec, {'candidates': [*records, draft]})
    assert result['spec_version'] == 2
    assert result['counts'] == {'input': 5, 'append_ready': 4, 'rejected': 1}
    assert result['submittable_indexes'] == [0, 1, 2, 3]
    rejected = result['records'][4]
    assert rejected['admissible'] is False
    assert rejected['reason_code'] == 'MISSING_EVIDENCE'
    assert rejected['path'] == 'candidates[4]'
    assert result['records'][0]['admissible'] is True and len(result['records'][0]['normalized_hash']) == 64


def test_validate_input_never_touches_storage_or_network(packaging_spec, packaging_candidate, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError('precheck must stay offline')

    monkeypatch.setattr(persistence, 'transaction', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    result = runs.validate_input(packaging_spec, {'candidates': [packaging_candidate]})
    assert result['counts']['append_ready'] == 1
    assert result['database_writes'] == 0 and result['network_requests'] == 0


def test_valid_subset_is_committed_atomically_and_replays(
        clean_db, packaging_spec, packaging_candidate):
    runs.create('precheck', packaging_spec)
    records = four_valid(packaging_candidate)
    draft = copy.deepcopy(records[0])
    draft['company_name'] = 'Klarwerk Verpackungen Draft GmbH'
    draft.pop('source_excerpt')
    draft.pop('retrieved_at')
    for source in draft['sources']:
        source.pop('source_excerpt', None)

    # An atomic chunk that contains a draft is rejected without partial writes.
    with pytest.raises(AppError) as exc:
        runs.append('precheck', [*records, draft], chunk_id='mixed')
    assert exc.value.code == 'MISSING_EVIDENCE'
    assert exc.value.details['index'] == 4
    with transaction() as session:
        assert not list(session.scalars(select(Job)))

    admitted = runs.append('precheck', records, chunk_id='clean')
    assert admitted['admission']['accepted'] == 4
    assert admitted['admission']['append_ready'] == 4
    replay = runs.append('precheck', records, chunk_id='clean')
    assert replay == admitted
    with transaction() as session:
        assert len(list(session.scalars(select(Job)))) == 4


def test_per_record_errors_do_not_abort_other_records(packaging_spec, packaging_candidate):
    records = four_valid(packaging_candidate)
    bad = copy.deepcopy(records[1])
    bad['unknown_formal_field'] = 'silently dropped before'
    result = runs.validate_input(packaging_spec, {'candidates': records + [bad]})
    assert result['submittable_indexes'] == [0, 1, 2, 3]
    assert result['records'][4]['reason_code'] == 'UNKNOWN_FIELD'
    assert 'unknown_formal_field' in str(result['records'][4]['message'])


def test_search_snippets_are_discovery_only(packaging_spec, packaging_candidate):
    snippet = copy.deepcopy(packaging_candidate)
    snippet['source_type'] = 'search_snippet'
    result = runs.validate_input(packaging_spec, {'candidates': [snippet]})
    assert result['records'][0]['reason_code'] == 'MISSING_EVIDENCE'
