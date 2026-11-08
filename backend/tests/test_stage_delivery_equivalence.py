"""Delivery equivalence: every accepted pair is admitted and delivered identically."""
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import compare_stage_inputs as comparison
from app import persistence, runs
from app.persistence.models import Company

FACT_FIELDS = {'company_name', 'entity.headquarters_country', 'business.models',
               'offering.products', 'email'}


def projection(item: dict) -> dict:
    return {'status': item['status'],
            'delivery_channels': item.get('delivery_channels'),
            'conditions': [(row['id'], row['status'], row.get('reason'))
                           for row in item.get('research', {}).get('conditions', [])],
            'contacts': [(row.get('email'), row.get('phone'), row.get('inferred'))
                         for row in item.get('contacts', [])],
            'source_facts': sorted((row.get('field') or '', str(row.get('value') or ''),
                                    row.get('source_url') or '', row.get('source_excerpt') or '')
                                   for row in item.get('sources', [])
                                   if row.get('field') in FACT_FIELDS)}


def test_each_accepted_pair_lands_the_same_record(clean_db, monkeypatch):
    import app.web.dns as dns

    monkeypatch.setattr(dns, 'lookup_mx', lambda _: 'unknown')
    report = comparison.compare()
    assert len(report['pairs']) == 4
    for pair in report['pairs']:
        records = []
        for side in ('manual', 'staged'):
            run_id = f"cmp-{side}-{pair['case']}-{pair['candidate_id']}"
            runs.create(run_id, comparison.SPEC)
            first = runs.advance(run_id, {'candidates': [pair[side]]},
                                 chunk_id=f'cmp-{side}', max_jobs=1, max_seconds=10)
            assert first['admission']['accepted'] == 1
            replay = runs.advance(run_id, {'candidates': [pair[side]]},
                                  chunk_id=f'cmp-{side}', max_jobs=1, max_seconds=10)
            assert replay['admission']['accepted'] == 1
            with persistence.transaction() as session:
                rows = session.scalars(select(Company).where(Company.run_id == run_id)).all()
            assert len(rows) == 1, 'replay must not duplicate the candidate'
            records.append(rows[0].data_json)
        assert projection(records[0]) == projection(records[1]), pair['case']
        assert records[0]['status'] == 'qualified'
        assert records[0]['delivery_channels'] == ['email']
