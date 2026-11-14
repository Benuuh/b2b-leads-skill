"""AC-01 cross-industry configuration on one normal entry, plus FR-01 boundaries."""
import copy

import pytest
from sqlalchemy import select

from app import runs
from app.capabilities import RULE_VERSION, capabilities
from app.errors import AppError
from app.persistence import transaction
from app.persistence.models import Company, Job, Run
from app.pipeline import process_company
from app.schemas import parse_segment


def test_capabilities_are_versioned_offline_and_bounded():
    result = capabilities()
    assert result['spec_versions'] == [1, 2] and result['default_spec_version'] == 2
    assert result['segment_brief_version'] == 'segment-brief-1'
    assert {'entity.country_code', 'entity.headquarters_country', 'entity.registered_country',
            'entity.relationship', 'business.models', 'offering.products', 'offering.services',
            'customer.industries'} <= set(result['fields'])
    assert 'route.name' not in result['fields']
    assert result['operators']['provides']['types'] == ['text_list']
    assert result['limits']['company_workers'] == {'min': 1, 'max': 4, 'default': 2}
    assert result['limits']['page_limit_per_company']['max'] == 10
    assert result['rule_version'] == RULE_VERSION


def test_v2_configuration_is_normalized_and_self_describing(packaging_spec):
    spec, version = parse_segment(packaging_spec)
    assert version == 2 and spec['spec_version'] == 2
    policy = spec['qualification_policy']
    assert [item['id'] for item in policy['required_all']] == ['hq', 'maker', 'product']
    assert policy['exclude_any'][0]['unknown_policy'] == 'allow_unconfirmed'
    assert spec['evidence_policy'] == {'freshness_days': 180, 'evidence_version': 'evidence-2'}
    # One effective freshness value, readable by every existing consumer.
    assert spec['freshness_days'] == 180
    assert spec['execution_policy']['company_workers'] == 2


def test_v1_payload_keeps_its_historical_shape():
    payload = {'spec_version': 1, 'name': 'v1', 'industry': 'freight forwarding', 'countries': ['US'], 'quantity': 50}
    spec, version = parse_segment(payload)
    assert version == 1
    assert 'spec_version' not in spec and 'qualification_policy' not in spec
    assert spec['freshness_days'] == 180


def test_unversioned_payload_normalizes_as_v2():
    payload = {'name': 'v2-default', 'industry': 'freight forwarding', 'countries': ['US'], 'quantity': 50}
    spec, version = parse_segment(payload)
    assert version == 2 and spec['spec_version'] == 2
    assert spec['qualification_policy']['derived_from'] == 'v1_fields'
    assert spec['evidence_policy']['evidence_version'] == 'evidence-2'
    assert 'qualification_policy' not in parse_segment({**payload, 'spec_version': 1})[0]


def test_unsupported_version_field_and_operator_are_rejected(packaging_spec):
    with pytest.raises(AppError) as exc:
        parse_segment({**packaging_spec, 'spec_version': 3})
    assert exc.value.code == 'UNSUPPORTED_SPEC_VERSION'

    unknown_field = copy.deepcopy(packaging_spec)
    unknown_field['qualification_policy']['required_all'][0]['field'] = 'route.name'
    with pytest.raises(AppError) as exc:
        parse_segment(unknown_field)
    assert exc.value.code == 'UNSUPPORTED_CONDITION'
    assert exc.value.details['field'] == 'route.name'

    bad_operator = copy.deepcopy(packaging_spec)
    bad_operator['qualification_policy']['required_all'][0]['operator'] = 'contains'
    with pytest.raises(AppError) as exc:
        parse_segment(bad_operator)
    assert exc.value.code == 'UNSUPPORTED_CONDITION'

    bad_enum = copy.deepcopy(packaging_spec)
    bad_enum['qualification_policy']['required_all'][1]['value'] = 'reseller_of_packaging'
    with pytest.raises(AppError) as exc:
        parse_segment(bad_enum)
    assert exc.value.code == 'UNSUPPORTED_CONDITION'


def test_duplicate_settings_have_one_semantic_value(packaging_spec):
    conflicting = {**packaging_spec, 'freshness_days': 90}
    with pytest.raises(AppError) as exc:
        parse_segment(conflicting)
    assert exc.value.code == 'CONFLICTING_CONFIG'

    consistent = {**packaging_spec, 'freshness_days': 180}
    assert parse_segment(consistent)[0]['evidence_policy']['freshness_days'] == 180

    country_conflict = {**packaging_spec, 'countries': ['AT']}
    with pytest.raises(AppError) as exc:
        parse_segment(country_conflict)
    assert exc.value.code == 'CONFLICTING_CONFIG'


def test_necessary_conditions_cannot_opt_out_of_unknown(clean_db, packaging_spec):
    payload = copy.deepcopy(packaging_spec)
    payload['qualification_policy']['required_all'][0]['unknown_policy'] = 'allow_unconfirmed'
    with pytest.raises(AppError) as exc:
        parse_segment(payload)
    assert exc.value.code == 'UNSUPPORTED_CONDITION'


def test_creates_both_non_freight_runs_without_industry_code(clean_db, packaging_spec, uae_spec):
    first = runs.create('packaging-de', packaging_spec)
    second = runs.create('retail-ae', uae_spec)
    assert first['spec_version'] == second['spec_version'] == 2
    assert first['plan']['spec_version'] == 2
    assert first['plan']['rules_version'] == RULE_VERSION
    assert first['policy']['country_scope'] == 'headquarters'
    assert second['policy']['country_scope'] == 'operating'
    # Retrying create with the same frozen configuration is idempotent.
    again = runs.create('packaging-de', packaging_spec)
    assert again['id'] == first['id'] and again['spec'] == first['spec']
    assert again['spec']['qualification_policy']['required_all'][0]['id'] == 'hq'


def test_same_core_finishes_two_industries_with_zero_network(clean_db, packaging_spec, uae_spec,
                                                             packaging_candidate, uae_candidate, monkeypatch):
    import app.web.http as web_http
    import app.web.crawler as crawler
    def forbidden(*args, **kwargs):
        raise AssertionError('saved evidence must not trigger network work')
    monkeypatch.setattr(crawler, 'safe_http', forbidden)
    monkeypatch.setattr(web_http, 'safe_http', forbidden)

    runs.create('packaging-de', packaging_spec)
    runs.append('packaging-de', [packaging_candidate])
    outcome = runs.process('packaging-de', max_jobs=1)
    assert outcome['research']['processed'] == 1

    runs.create('retail-ae', uae_spec)
    runs.append('retail-ae', [uae_candidate])
    outcome = runs.process('retail-ae', max_jobs=1)
    assert outcome['research']['processed'] == 1

    with transaction() as session:
        companies = {row.run_id: row.data_json for row in session.scalars(select(Company))}
        jobs = list(session.scalars(select(Job)))
    assert len(jobs) == 2 and jobs[0].input_json['country_code'] != jobs[1].input_json['country_code']
    de, ae = companies['packaging-de'], companies['retail-ae']
    assert de['status'] == 'qualified' and ae['status'] == 'qualified'
    assert de['pipeline']['stop_reason'] == ae['pipeline']['stop_reason'] == 'saved_evidence_sufficient'
    assert de['pipeline']['pages_fetched'] == ae['pipeline']['pages_fetched'] == 0
    assert de['delivery_channels'] == ['email']
    assert ae['delivery_channels'] == ['contact_form']
    assert de['qualification']['conditions'][0]['field'] == 'entity.headquarters_country'
    assert ae['qualification']['conditions'][0]['field'] == 'entity.country_code'
    assert de['research']['policy']['country_scope'] == 'headquarters'
    assert ae['research']['policy']['country_scope'] == 'operating'
    # No US-specific route, numbering or manufacturer rule leaks into the UAE task.
    assert 'route' not in {item['field'] for item in ae['qualification']['conditions']}
    assert 'manufacturer' not in uae_spec['qualification_policy']['required_all'][1]['value']
