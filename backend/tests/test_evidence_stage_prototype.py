"""Offline synthetic fixture checks; this does not certify G1."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('evidence_stage_prototype', ROOT / 'scripts' / 'evidence_stage_prototype.py')
stage = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = stage
spec.loader.exec_module(stage)


def sample():
    # Hash fixes the exact synthetic captured text. Entry offsets are character offsets.
    raw = (ROOT / 'backend/tests/fixtures/speed_token_directory.txt').read_bytes()
    assert stage.digest(raw) == '8d554b5d41b9a32c36ed06a8409256da6c33197b153ffb3b922e97c1e7cbd2fe'
    text = raw.decode('utf-8')
    a = 'Atlas Example LLC manufactures air cargo containers. Email: sales@atlas.example'
    b = 'Boreal Example LLC distributes packaging. Email: sales@boreal.example'
    return {'version': stage.VERSION, 'run_id': 'synthetic-fixture',
            'sources': [{'source_id': 'directory', 'url': 'https://directory.example/members',
                         'source_type': 'directory', 'collected_at': '2026-11-03T00:00:00Z',
                         'text': text, 'sha256': stage.digest(raw)}],
            'entries': [{'candidate_id': key, 'source_id': 'directory', 'subject': name,
                         'start': text.index(line), 'end': text.index(line) + len(line)}
                        for key, name, line in [('a', 'Atlas Example LLC', a), ('b', 'Boreal Example LLC', b)]],
            'observations': [{'candidate_id': key, 'source_id': 'directory', 'subject': name,
                              'field': 'email', 'value': email, 'method': 'synthetic_fixture',
                              'excerpt': line, 'kind': 'evidence'}
                             for key, name, email, line in [('a', 'Atlas Example LLC', 'sales@atlas.example', a),
                                                            ('b', 'Boreal Example LLC', 'sales@boreal.example', b)]]}


def test_fixture_is_replayable_and_keeps_subject_boundaries():
    data = sample()
    first = stage.inspect(data)
    assert first == stage.inspect(copy.deepcopy(data))
    assert [item['status'] for item in first['records']] == ['observed', 'observed']
    data['observations'][0]['excerpt'] = data['observations'][1]['excerpt']
    assert stage.inspect(data)['records'][0]['reason_code'] == 'EXCERPT_UNVERIFIED'


def test_rejects_corrupt_source_and_overlapping_entries():
    data = sample()
    data['sources'][0]['text'] += ' altered'
    with pytest.raises(ValidationError):
        stage.inspect(data)
    data = sample()
    data['entries'][0]['end'] = data['entries'][1]['end']
    with pytest.raises(ValueError, match='overlapping'):
        stage.inspect(data)


def test_missing_entry_snippet_and_nonliteral_are_visible():
    data = sample()
    data['observations'][0]['value'] = 'nonexistent@example.com'
    assert stage.inspect(data)['records'][0]['reason_code'] == 'VALUE_NOT_LITERAL'
    data['sources'][0]['source_type'] = 'search_snippet'
    assert stage.inspect(data)['records'][0]['reason_code'] == 'SEARCH_SNIPPET_ONLY'
    data['entries'].pop(0)
    assert stage.inspect(data)['records'][0]['reason_code'] == 'ENTRY_UNVERIFIED'


def test_single_value_conflict_never_silently_chooses_one():
    data = sample()
    first = data['observations'][0]
    first.update(field='company_name', value='Atlas Example LLC')
    other = copy.deepcopy(first)
    other['value'] = 'air cargo'
    data['observations'].append(other)
    rows = stage.inspect(data)['records']
    assert rows[0]['reason_code'] == rows[2]['reason_code'] == 'CONFLICTING_VALUES'


def test_unknown_fields_rejected():
    data = sample()
    data['sources'][0]['secret'] = 'not accepted'
    with pytest.raises(ValidationError):
        stage.inspect(data)


def test_literal_matching_does_not_accept_substrings_of_other_identifiers():
    assert not stage.literal_value_in_excerpt('DE', 'Model details')
    assert not stage.literal_value_in_excerpt('sales@atlas.example', 'sales@atlas.example.evil')
    assert stage.literal_value_in_excerpt('DE', 'Headquarters: DE; Germany')


def test_known_second_subject_in_excerpt_requires_review():
    data = sample()
    line_a = 'Atlas Example LLC works with Boreal Example LLC on air cargo.'
    line_b = 'Boreal Example LLC provides air cargo.'
    text = line_a + '\n' + line_b
    data['sources'][0].update(text=text, sha256=stage.digest(text.encode()))
    data['entries'][0].update(start=0, end=len(line_a))
    data['entries'][1].update(start=len(line_a) + 1, end=len(text))
    data['observations'][0].update(field='offering.services', value='air cargo', excerpt=line_a)
    data['observations'][1].update(field='offering.services', value='air cargo', excerpt=line_b)
    rows = stage.inspect(data)['records']
    assert rows[0]['reason_code'] == 'MULTIPLE_SUBJECTS'
    assert rows[1]['status'] == 'observed'


def test_pages_preserve_errors_and_detail_returns_original_source():
    data = sample()
    data['observations'][1]['value'] = 'missing@example.com'
    first = stage.page(data, limit=1, max_bytes=1024)
    second = stage.page(data, cursor=first['next_cursor'], expected_input_sha256=first['input_sha256'],
                        limit=1, max_bytes=1024)
    assert first['input_sha256'] == second['input_sha256']
    assert second['records'][0]['reason_code'] == 'VALUE_NOT_LITERAL'
    assert second['next_cursor'] is None
    full = stage.detail(data, candidate_id='b', source_id='directory')
    assert full['source']['text'] == data['sources'][0]['text']
    assert full['records'][0]['reason_code'] == 'VALUE_NOT_LITERAL'
    with pytest.raises(ValueError):
        stage.detail(data, candidate_id='other', source_id='directory')
    data['observations'][0]['value'] = 'modified'
    with pytest.raises(ValueError, match='different artifact'):
        stage.page(data, cursor=1, expected_input_sha256=first['input_sha256'])


def test_draft_reuses_precheck_and_withholds_incomplete_candidate(packaging_spec, monkeypatch):
    import socket
    from app import persistence

    def forbidden(*args, **kwargs):
        raise AssertionError('draft must remain offline')

    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(persistence, 'transaction', forbidden)
    data = sample()
    data['sources'][0]['url'] = 'https://directory.example.com/members'
    data['sources'][0]['collected_at'] = '2025-01-01T00:00:00Z'
    identity = copy.deepcopy(data['observations'][0])
    identity.update(field='company_name', value='Atlas Example LLC')
    data['observations'].append(identity)
    result = stage.draft(data, packaging_spec, ['a', 'b'], as_of='2025-01-02T00:00:00Z')
    assert result == stage.draft(data, packaging_spec, ['a', 'b'], as_of='2025-01-02T00:00:00Z')
    assert len(result['submittable']['candidates']) == 1
    assert result['submittable']['candidates'][0]['email'] == ['sales@atlas.example']
    assert result['errors'][0]['index'] == 1
    assert result['errors'][0]['reason_code'] == 'MISSING_IDENTITY'
    assert len(result['output_sha256']) == 64
    # Any failed evidence blocks the complete candidate, not just one field.
    data['observations'][0]['value'] = 'injected@example.com'
    assert stage.draft(data, packaging_spec, ['a'], as_of='2025-01-02T00:00:00Z')['submittable']['candidates'] == []
    data['observations'][0]['value'] = 'sales@atlas.example'
    expired = stage.draft(data, packaging_spec, ['a'], as_of='2026-01-02T00:00:00Z')
    assert expired['errors'][0]['reason_code'] == 'EVIDENCE_OUT_OF_DATE'


def test_duplicate_subjects_are_held_for_review(packaging_spec):
    data = sample()
    duplicate = copy.deepcopy(data['entries'][0])
    duplicate.update(candidate_id='another-id', source_id='second-directory')
    second = copy.deepcopy(data['sources'][0])
    second.update(source_id='second-directory', url='https://another.example.com/members')
    data['sources'].append(second)
    data['entries'].append(duplicate)
    result = stage.draft(data, packaging_spec, ['a', 'another-id'], as_of='2026-11-04T00:00:00Z')
    assert [error['reason_code'] for error in result['errors']] == ['DUPLICATE_SUBJECT_REVIEW'] * 2
    assert result['submittable']['candidates'] == []


def test_transformed_observations_match_manual_precheck_and_policy(packaging_spec):
    from app import runs
    from app.schemas import parse_segment
    from app.rules.policy import assess_policy

    name, source = 'Atlas Example LLC', 'https://directory.example.com/atlas'
    facts = [('company_name', name, f'{name}'),
             ('entity.headquarters_country', 'DE', f'{name} headquarters: DE'),
             ('business.models', 'manufacturer', f'{name} is a manufacturer'),
             ('offering.products', 'reusable industrial packaging', f'{name} provides reusable industrial packaging'),
             ('email', 'sales@atlas.example', f'{name} email sales@atlas.example')]
    text = '\n'.join(row[2] for row in facts)
    at = '2025-01-01T00:00:00Z'
    payload = {'version': stage.VERSION, 'run_id': 'fixed-policy-test',
               'sources': [{'source_id': 'source', 'url': source, 'source_type': 'directory',
                            'collected_at': at, 'text': text, 'sha256': stage.digest(text.encode())}],
               'entries': [{'candidate_id': 'a', 'source_id': 'source', 'subject': name,
                            'start': 0, 'end': len(text)}],
               'observations': [{'candidate_id': 'a', 'source_id': 'source', 'subject': name,
                                 'field': field, 'value': value, 'method': 'synthetic_fixture',
                                 'excerpt': excerpt, 'kind': 'evidence'} for field, value, excerpt in facts]}
    generated = stage.draft(payload, packaging_spec, ['a'], as_of='2025-01-02T00:00:00Z')['submittable']
    references = [{'field': field, 'value': value, 'source_url': source, 'source_type': 'directory',
                   'retrieved_at': at, 'method': 'synthetic_fixture', 'source_excerpt': excerpt}
                  for field, value, excerpt in facts]
    manual = {'candidates': [{'company_name': name, 'source_url': source, 'source_type': 'directory',
                              'retrieved_at': at, 'source_excerpt': name, 'sources': references,
                              'headquarters_country': 'DE', 'business_models': ['manufacturer'],
                              'offering_products': ['reusable industrial packaging'],
                              'email': ['sales@atlas.example']}]}
    assert generated == manual
    assert runs.validate_input(packaging_spec, generated) == runs.validate_input(packaging_spec, manual)
    frozen, _ = parse_segment(packaging_spec)
    assert assess_policy(generated['candidates'][0], [], frozen) == assess_policy(manual['candidates'][0], [], frozen)


def test_subset_file_atomic_replay_and_hash_check(tmp_path, packaging_spec, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import json

    data = sample()
    data['sources'][0]['url'] = 'https://directory.example.com/members'
    data['sources'][0]['collected_at'] = '2025-01-01T00:00:00Z'
    identity = copy.deepcopy(data['observations'][0])
    identity.update(field='company_name', value='Atlas Example LLC')
    data['observations'].append(identity)
    result = stage.draft(data, packaging_spec, ['a', 'b'], as_of='2025-01-02T00:00:00Z')
    with ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(lambda _: stage.write_subset(result, tmp_path), range(4)))
    assert len({item['path'] for item in files}) == 1
    target = Path(files[0]['path'])
    assert json.loads(target.read_text(encoding='utf-8')) == result['submittable']
    assert len(result['errors']) == 1 and result['errors'][0]['index'] == 1
    target.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='existing subset'):
        stage.write_subset(result, tmp_path)
    assert not list(tmp_path.glob('.stage-subset-*.tmp'))


def test_unconfirmed_website_is_not_submitted(packaging_spec):
    name = 'Atlas Example LLC'
    line = f'About us: {name} official website https://atlas.example.com/'
    data = {'version': stage.VERSION, 'run_id': 'website-fixture',
            'sources': [{'source_id': 'source', 'url': 'https://directory.example.com/atlas',
                         'source_type': 'directory', 'collected_at': '2025-01-01T00:00:00Z',
                         'text': line, 'sha256': stage.digest(line.encode())}],
            'entries': [{'candidate_id': 'a', 'source_id': 'source', 'subject': name,
                         'start': 0, 'end': len(line)}],
            'observations': [{'candidate_id': 'a', 'source_id': 'source', 'subject': name,
                              'field': field, 'value': value, 'method': method,
                              'excerpt': line, 'kind': 'evidence'}
                             for field, value, method in [('company_name', name, 'observed'),
                                                          ('website_url', 'https://atlas.example.com/', 'observed')]]}
    result = stage.draft(data, packaging_spec, ['a'], as_of='2025-01-02T00:00:00Z')
    assert result['submittable']['candidates'] == []
    assert result['errors'][0]['reason_code'] == 'WEBSITE_UNCONFIRMED'


def test_inferred_method_never_becomes_evidence():
    data = sample()
    data['observations'][0]['method'] = 'model_inferred'
    assert stage.inspect(data)['records'][0]['reason_code'] == 'UNOBSERVED_METHOD'


def test_long_page_has_bounded_summary_and_intact_raw_source():
    data = sample()
    prefix = 'unrelated page content ' * 5000 + '\n'
    source = data['sources'][0]
    source['text'] = prefix + source['text']
    source['sha256'] = stage.digest(source['text'].encode())
    for entry in data['entries']:
        entry['start'] += len(prefix)
        entry['end'] += len(prefix)
    compact = stage.page(data, limit=1, max_bytes=1024)
    assert len(stage.canonical(compact)) <= 1024
    assert len(stage.canonical(compact)) < len(stage.canonical(data)) // 10
    full = stage.detail(data, candidate_id='a', source_id='directory')
    assert full['source']['text'] == source['text']


def test_source_size_and_missing_original_are_rejected():
    data = sample()
    data['sources'][0]['text'] = ''
    with pytest.raises(ValidationError):
        stage.inspect(data)
    data = sample()
    data['sources'][0]['text'] = 'x' * (stage.MAX_PAGE_BYTES + 1)
    data['sources'][0]['sha256'] = stage.digest(data['sources'][0]['text'].encode())
    with pytest.raises(ValidationError):
        stage.inspect(data)
