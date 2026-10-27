"""AC-02/04/05/11: evidence-first decisions, minimal sufficient delivery, stop rules."""
import copy
from datetime import datetime, timezone

import pytest

from app.delivery.selection import channel_status, delivery_companies
from app.pipeline import process_company
from app.rules.policy import assess_policy
from app.schemas import parse_segment


def task(spec, candidate, pages_used=0):
    return {'input': candidate, 'segment': spec, 'demo_only': False,
            'plan': {'page_limit_per_company': 10, 'page_limit_total': 1000},
            'pages_used': pages_used, 'suppressions': []}


def test_qualified_company_is_traceable_per_condition(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    assessment = assess_policy(packaging_candidate, [], spec)
    assert assessment['decision'] == 'qualified'
    for condition in assessment['conditions']:
        assert condition['status'] == 'pass'
        assert condition['evidence'], condition['id']
        assert all(item['source_url'] for item in condition['evidence']
                   if isinstance(item, dict))


def test_unknown_facts_are_not_negative_facts(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    bare = copy.deepcopy(packaging_candidate)
    bare['sources'] = [source for source in bare['sources']
                       if source['field'] == 'company_name']
    bare['source_excerpt'] = f'{packaging_candidate["company_name"]}, Düsseldorf, Germany'
    for field in ('business_models', 'offering_products', 'headquarters_country'):
        bare.pop(field, None)
    assessment = assess_policy(bare, [], spec)
    assert assessment['decision'] == 'uncertain'
    assert {item['field'] for item in assessment['missing']} >= {
        'business.models', 'offering.products', 'entity.headquarters_country'}
    assert all(item['reason_code'] == 'fact_unknown' for item in assessment['missing'])


def test_customer_news_and_assertions_do_not_prove_own_offer(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    news = copy.deepcopy(packaging_candidate)
    # The excerpt is about a customer, not about the candidate's own offering.
    news['sources'] = [source for source in news['sources']
                       if source['field'] not in {'offering.products', 'business.models'}]
    news.pop('business_models', None)
    news.pop('offering_products', None)
    news['sources'].append({
        'field': 'offering.products', 'value': 'reusable industrial packaging',
        'source_url': 'https://packaging-news.org/story', 'source_type': 'news',
        'retrieved_at': packaging_candidate['retrieved_at'],
        'source_excerpt': 'Our customer BMW uses reusable industrial packaging for engines.'})
    assessment = assess_policy(news, [], spec)
    product = next(item for item in assessment['conditions'] if item['id'] == 'product')
    assert product['status'] == 'unknown'
    assert assessment['decision'] == 'uncertain'


def test_two_evidenced_headquarters_are_a_conflict(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    conflicted = copy.deepcopy(packaging_candidate)
    conflicted['sources'].append({
        'field': 'entity.headquarters_country', 'value': 'AT',
        'source_url': 'https://austria-registry.org/klarwerk', 'source_type': 'registry',
        'retrieved_at': packaging_candidate['retrieved_at'],
        'source_excerpt': 'Klarwerk Verpackungen GmbH, headquartered in Linz, Austria'})
    assessment = assess_policy(conflicted, [], spec)
    hq = next(item for item in assessment['conditions'] if item['id'] == 'hq')
    assert hq['status'] == 'conflict'
    assert assessment['decision'] == 'uncertain'
    assert assessment['missing'][0]['reason_code'] == 'fact_conflict'


def test_missing_channel_only_produces_a_channel_gap(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    candidate = copy.deepcopy(packaging_candidate)
    candidate.pop('email')
    candidate['sources'] = [source for source in candidate['sources'] if source['field'] != 'email']
    pages = []
    result = process_company(task(spec, candidate), pages)
    assert result['qualification']['decision'] == 'qualified'
    assert result['status'] == 'insufficient'
    assert result['pipeline']['stop_reason'] in {'channel_missing', 'fact_missing'}
    assert set(result['channel_policy']['missing']) >= {'business_email', 'business_phone', 'contact_form'}
    assert pages == []


def test_all_policy_requires_every_listed_channel(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    spec['qualification_policy']['contact_policy'] = {
        'mode': 'all', 'allowed_types': ['business_email', 'business_phone'],
        'named_person_required': False, 'required_roles': []}
    result = process_company(task(spec, packaging_candidate), [])
    assert result['status'] == 'insufficient'
    assert result['channel_policy']['missing'] == ['business_phone']


def test_any_policy_accepts_one_observed_form(uae_spec, uae_candidate, monkeypatch):
    import app.web.crawler as crawler

    def forbidden(*args, **kwargs):
        raise AssertionError('a qualified form target must stop before any request')

    monkeypatch.setattr(crawler, 'safe_http', forbidden)
    spec, _ = parse_segment(uae_spec)
    pages = []
    result = process_company(task(spec, uae_candidate), pages)
    assert result['status'] == 'qualified'
    assert result['delivery_channels'] == ['contact_form']
    assert result['pipeline']['stop_reason'] == 'saved_evidence_sufficient'
    assert pages == []


def test_named_person_policy_rejects_a_role_mailbox(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    role = copy.deepcopy(packaging_candidate)
    role['email'] = 'info@klarwerk.de'
    for source in role['sources']:
        if source['field'] == 'email':
            source['value'] = 'info@klarwerk.de'
            source['source_excerpt'] = f'{role["company_name"]} — Business email: info@klarwerk.de'
    record = _record_from(role)
    strict = {'mode': 'any', 'allowed_types': ['business_email'], 'named_person_required': True,
              'required_roles': []}
    status = channel_status(record, strict)
    assert status['satisfied'] is False and 'named_person' in status['missing']

    personal = copy.deepcopy(packaging_candidate)
    personal['email'] = 'a.weber@klarwerk.de'
    for source in personal['sources']:
        if source['field'] == 'email':
            source['value'] = 'a.weber@klarwerk.de'
            source['source_excerpt'] = f'{personal["company_name"]} — Business email: a.weber@klarwerk.de'
    named = _record_from(personal, contact_name='Anna Weber', title='Sales Manager')
    assert channel_status(named, strict)['satisfied'] is True
    wrong_role = {**strict, 'required_roles': ['Purchasing']}
    assert channel_status(named, wrong_role)['satisfied'] is False


def test_exclusion_unknown_policy_controls_delivery(packaging_spec, packaging_candidate):
    spec, _ = parse_segment({**packaging_spec})
    review_spec = copy.deepcopy(spec)
    review_spec['qualification_policy']['exclude_any'][0]['unknown_policy'] = 'review'
    review = assess_policy(packaging_candidate, [], review_spec)
    assert review['decision'] == 'uncertain'
    assert any(item['id'] == 'dealer_only' for item in review['missing'])

    allowed = assess_policy(packaging_candidate, [], spec)
    assert allowed['decision'] == 'qualified'
    assert any('排除条件保留未知' in reason for reason in allowed['reasons'])
    assert next(item for item in allowed['exclusions'] if item['id'] == 'dealer_only')['status'] == 'unknown'

    conflicted = copy.deepcopy(packaging_candidate)
    conflicted['sources'].append({
        'field': 'entity.headquarters_country', 'value': 'AT',
        'source_url': 'https://austria-registry.org/klarwerk', 'source_type': 'registry',
        'retrieved_at': packaging_candidate['retrieved_at'],
        'source_excerpt': 'Klarwerk Verpackungen GmbH, headquartered in Linz, Austria'})
    still_review = assess_policy(conflicted, [], spec)
    assert still_review['decision'] == 'uncertain'


def _record_from(candidate, contact_name=None, title=None):
    """Build the extracted record for a candidate to exercise channel policy."""
    from app.rules.contacts import extract_company
    record = extract_company(candidate, [], {'countries': ['DE'], 'freshness_days': 180},
                             mx_lookup=lambda _: 'unknown')
    if contact_name:
        for contact in record['contacts']:
            contact['contact_name'] = contact_name
            contact['title'] = title
            contact['binding_status'] = 'explicit'
            contact['phone_type'] = 'direct_business'
            contact['email_type'] = 'named_business'
    return record


def test_delivery_selection_uses_the_same_policy(packaging_spec, packaging_candidate):
    spec, _ = parse_segment(packaging_spec)
    candidate = copy.deepcopy(packaging_candidate)
    candidate.pop('email')
    candidate['sources'] = [source for source in candidate['sources'] if source['field'] != 'email']
    policy = spec['qualification_policy']['contact_policy']
    gap = _record_from(candidate)
    gap.update(status='qualified', candidate_ordinal=1,
               qualification={'decision': 'qualified', 'confidence': .9})
    assert delivery_companies([gap], policy=policy) == []
    good = _record_from(packaging_candidate)
    good.update(status='qualified', candidate_ordinal=2,
                qualification={'decision': 'qualified', 'confidence': .9})
    assert len(delivery_companies([good], policy=policy)) == 1
