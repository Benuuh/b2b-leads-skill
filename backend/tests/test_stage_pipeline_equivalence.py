"""Compare manual and staged evidence through isolated PostgreSQL runs."""
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence_stage_prototype as stage
from app import persistence, runs
from app.persistence.models import Company


def test_manual_and_staged_candidate_produce_same_result(clean_db, packaging_spec, monkeypatch):
    import socket
    import app.web.dns as dns

    def forbidden(*args, **kwargs):
        raise AssertionError('fixed evidence comparison must not use the network')

    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(dns, 'lookup_mx', lambda _: 'unknown')
    now = datetime.now(timezone.utc).isoformat()
    name, url = 'Atlas Example LLC', 'https://directory.example.com/atlas'
    facts = [('company_name', name, name),
             ('entity.headquarters_country', 'DE', f'{name} headquarters DE'),
             ('business.models', 'manufacturer', f'{name} is a manufacturer'),
             ('offering.products', 'reusable industrial packaging', f'{name} manufactures reusable industrial packaging'),
             ('email', 'sales@atlas.example.com', f'{name} email sales@atlas.example.com')]
    text = '\n'.join(item[2] for item in facts)
    payload = {'version': stage.VERSION, 'run_id': 'equivalence-fixture',
               'sources': [{'source_id': 'source', 'url': url, 'source_type': 'directory',
                            'collected_at': now, 'text': text, 'sha256': stage.digest(text.encode())}],
               'entries': [{'candidate_id': 'a', 'source_id': 'source', 'subject': name,
                            'start': 0, 'end': len(text)}],
               'observations': [{'candidate_id': 'a', 'source_id': 'source', 'subject': name,
                                 'field': field, 'value': value, 'method': 'agent_page_observation',
                                 'excerpt': excerpt, 'kind': 'evidence'} for field, value, excerpt in facts]}
    staged = stage.draft(payload, packaging_spec, ['a'], as_of=now)['submittable']['candidates'][0]
    refs = [{'field': field, 'value': value, 'source_url': url, 'source_type': 'directory',
             'retrieved_at': now, 'method': 'agent_page_observation', 'source_excerpt': excerpt}
            for field, value, excerpt in facts]
    manual = {'company_name': name, 'headquarters_country': 'DE', 'business_models': ['manufacturer'],
              'offering_products': ['reusable industrial packaging'], 'email': ['sales@atlas.example.com'],
              'source_url': url, 'source_type': 'directory', 'retrieved_at': now,
              'source_excerpt': name, 'sources': refs}
    assert staged == manual
    for rid, candidate in [('manual-eq', manual), ('stage-eq', staged)]:
        runs.create(rid, packaging_spec)
        outcome = runs.advance(rid, {'candidates': [candidate]}, chunk_id='same-input', max_jobs=1, max_seconds=10)
        assert outcome['admission']['accepted'] == 1
    with persistence.transaction() as session:
        outputs = [session.scalar(select(Company).where(Company.run_id == rid)).data_json
                   for rid in ('manual-eq', 'stage-eq')]
    def projection(item):
        return {'status': item['status'], 'delivery_channels': item.get('delivery_channels'),
                'conditions': [(x['id'], x['status'], x.get('reason'))
                               for x in item.get('research', {}).get('conditions', [])],
                'contacts': [(x.get('email'), x.get('phone'), x.get('inferred'))
                             for x in item.get('contacts', [])],
                'source_facts': sorted((x.get('field') or '', str(x.get('value') or ''),
                                        x.get('source_url') or '', x.get('source_excerpt') or '')
                                       for x in item.get('sources', [])
                                       if x.get('field') in {'company_name', 'entity.headquarters_country',
                                                             'business.models', 'offering.products', 'email'})}
    assert projection(outputs[0]) == projection(outputs[1])
    assert outputs[0]['status'] == 'qualified'
    from app.delivery.selection import delivery_companies
    delivered = [delivery_companies([item], freshness_days=180, now=now) for item in outputs]
    assert [len(rows) for rows in delivered] == [1, 1]
    assert delivered[0][0]['delivery_channels'] == delivered[1][0]['delivery_channels']
    blocked = [delivery_companies([item], freshness_days=180, now=now,
                                  suppressions=[{'type': 'company', 'value': name}]) for item in outputs]
    assert blocked == [[], []]
