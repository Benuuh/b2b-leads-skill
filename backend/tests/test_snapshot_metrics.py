"""AC-09/AC-10: incremental snapshots share the delivery projection; metrics stay versioned."""
import copy

from sqlalchemy import select

from app import runs
from app.capabilities import METRICS_VERSION
from app.persistence import transaction, utcnow
from app.persistence.models import Export, Run


def _qualified_variants(packaging_candidate):
    records = []
    for index in range(4):
        value = copy.deepcopy(packaging_candidate)
        name = f'Klarwerk Verpackungen {index} GmbH'
        email = f'sales@klarwerk-{index}.de'
        value['company_name'] = name
        value['email'] = email
        value['source_url'] = f'https://packaging-association.org/members/klarwerk-{index}'
        value['source_excerpt'] = f'{name}, Düsseldorf, Germany, manufactures reusable industrial packaging.'
        for source in value['sources']:
            source['source_url'] = value['source_url']
            source['source_excerpt'] = source['source_excerpt'].replace(
                'Klarwerk Verpackungen GmbH', name)
            if source['field'] == 'email':
                source['value'] = email
                source['source_excerpt'] = f'{name} — Business email: {email}'
        records.append(value)
    return records


def test_incremental_snapshot_matches_the_delivery_projection(
        clean_db, packaging_spec, packaging_candidate):
    spec = copy.deepcopy(packaging_spec)
    spec['execution_policy'] = {**spec['execution_policy'], 'snapshot_every_new_qualified': 2,
                                'snapshot_interval_seconds': 900}
    runs.create('snapshots', spec)
    with transaction() as session:
        session.get(Run, 'snapshots').demo_only = False
    runs.append('snapshots', _qualified_variants(packaging_candidate))
    result = runs.process('snapshots', max_jobs=4, workers=1)
    assert result['research']['contactable_count'] == 4

    with transaction() as session:
        run = session.get(Run, 'snapshots')
        snapshots = list(run.stats_json['snapshots'])
        exports = list(session.scalars(select(Export).order_by(Export.created_at, Export.id)))
    assert [item['rows'] for item in snapshots] == [2, 4]
    assert [item['qualified'] for item in snapshots] == [2, 4]
    assert len(exports) == 2
    sealed = {row.id: row.manifest_json for row in exports}
    for item in snapshots:
        manifest = sealed[item['export_id']]
        assert manifest['row_counts']['companies'] == item['rows'] == item['qualified']
        assert manifest['metrics_version'] == METRICS_VERSION
        # Legacy delivery channel vocabulary stays readable for existing consumers.
        assert manifest['delivery']['channels']['email'] == item['rows']
        assert manifest['spec_version'] == 2
        assert manifest['policy']['country_scope'] == 'headquarters'

    before = runs._maybe_snapshot('snapshots')
    assert before is None
    with transaction() as session:
        assert len(session.get(Run, 'snapshots').stats_json['snapshots']) == 2


def test_metrics_are_versioned_and_keep_the_existing_denominator(
        clean_db, packaging_spec, packaging_candidate):
    runs.create('metrics-v2', packaging_spec)
    runs.append('metrics-v2', _qualified_variants(packaging_candidate))
    runs.process('metrics-v2', max_jobs=4)
    with transaction() as session:
        run = session.get(Run, 'metrics-v2')
        context = runs._metrics_context(session, run, utcnow())
        view = runs._view(session, run)
    research = context['research']
    assert context['metrics_version'] == METRICS_VERSION == research['metrics_version']
    assert research['discovery_seen'] == 4 and research['append_ready'] == 4
    assert research['admitted'] == view['research']['discovered'] == 4
    assert research['physical_http_attempts'] == 0
    assert research['tool_invocations'] is None
    assert research['new_qualified'] is None
    assert research['offline_evidence_qualified'] == 4
    assert research['offline_reassess_qualified'] is None
    assert research['units'] == {'counts': 'enterprises', 'timing': 'seconds',
                                 'requests': 'physical_attempts'}


def test_v1_run_does_not_emit_v2_snapshots(clean_db, packaging_candidate, monkeypatch):
    import app.pipeline

    def research(task, pages):
        return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
                    domain='example.org', country_code='US', status='qualified',
                    qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    contacts=[], sources=[], demo_only=False,
                    pipeline={'stop_reason': 'contact_target_reached'})

    monkeypatch.setattr(app.pipeline, 'process_company', research)
    runs.create('metrics-v1', {'spec_version': 1, 'name': 'v1', 'industry': 'freight forwarding',
                               'countries': ['US'], 'quantity': 50})
    with transaction() as session:
        session.get(Run, 'metrics-v1').demo_only = False
    legacy = {'company_name': 'Harbor Logistics LLC', 'country_code': 'US',
              'industry_text': 'freight forwarding',
              'source_url': 'https://directory.example.org/harbor', 'source_type': 'directory',
              'source_excerpt': 'Harbor Logistics LLC provides freight forwarding services in the US',
              'retrieved_at': packaging_candidate['retrieved_at'],
              'sources': [{'field': 'company_name', 'value': 'Harbor Logistics LLC',
                           'source_url': 'https://directory.example.org/harbor',
                           'source_type': 'directory',
                           'retrieved_at': packaging_candidate['retrieved_at'],
                           'source_excerpt': 'Harbor Logistics LLC, United States'}]}
    runs.append('metrics-v1', [legacy])
    runs.process('metrics-v1', max_jobs=1)
    assert runs._maybe_snapshot('metrics-v1') is None
    with transaction() as session:
        run = session.get(Run, 'metrics-v1')
        context = runs._metrics_context(session, run, utcnow())
        assert 'snapshots' not in run.stats_json
    assert context['research']['spec_version'] == 1
