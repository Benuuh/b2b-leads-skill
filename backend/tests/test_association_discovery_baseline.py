"""Frozen observations of policy-2 limitations, not policy-3 acceptance tests.

These examples are synthetic; they do not establish the cause of any real run.
When a version-dispatched replacement exists, keep these checks against the
actual legacy implementation and add separate acceptance tests for the new one.
"""
from copy import deepcopy

import pytest

from app.capabilities import capabilities
from app.rules.policy import assess_policy, country_supported
from app.schemas import parse_segment


@pytest.mark.parametrize(('code', 'excerpt', 'supported'), [
    ('US', 'Acme Logistics in USA', False),
    ('US', 'Acme Logistics in U.S.', False),
    ('US', 'Acme Logistics in United States', True),
    ('US', 'Acme Logistics serves us worldwide', False),
    ('CA', 'Acme Logistics in CA', True),
])
def test_policy_2_literal_country_baseline(code, excerpt, supported):
    assert country_supported(code, excerpt) is supported


def test_policy_2_country_field_does_not_validate_role(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    candidate = deepcopy(packaging_candidate)
    row = next(row for row in candidate['sources'] if row['field'] == 'entity.headquarters_country')
    row['source_excerpt'] = 'Klarwerk Verpackungen GmbH has an office in Germany'
    hq = next(row for row in assess_policy(candidate, [], spec)['conditions'] if row['id'] == 'hq')
    assert hq['status'] == 'pass'  # Historical behaviour; office is not proof of headquarters.


def test_unimplemented_discovery_features_are_disclosed_offline():
    limits = capabilities()['discovery_limitations']
    assert limits == {
        'shared_directory_page_budget': 'shared-page-budget-1',
        'automatic_shared_directory_collection': 'unavailable',
        'automatic_js_rendering': 'unavailable',
        'country_rule_version_dispatch': 'unavailable',
        'country_evidence': 'legacy_literal_matching',
        'structured_discovery_southeast_asia': 'not_covered',
    }
