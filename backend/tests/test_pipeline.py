"""Pipeline behavior: reject early, bound uncertain work, reuse pages, then rank."""
from copy import deepcopy

import pytest

from app.rules import contacts as b2b_contacts
from app.web import crawler as b2b_sources, dns as web_dns
from app.rules.qualification import assess_candidate
from app.pipeline import contact_target_reached, process_company

EXTRACT_COMPANY = b2b_contacts.extract_company

NOW = '2026-10-23T00:00:00Z'
HOME = 'https://forwarding.example.com/'
SPEC = {'industry': 'freight forwarding', 'keywords': ['freight forwarding'],
        'countries': ['US'], 'regions': [], 'company_types': [], 'roles': [],
        'exclude_keywords': ['moving services'], 'employee_range': '', 'revenue_range': '',
        'freshness_days': 180}


def candidate(**changes):
    value = {'company_id': 'bc_pipeline', 'company_name': 'Harbor Company',
            'website_url': HOME, 'country_code': 'US', 'source_url': 'https://directory.org/harbor',
            'source_type': 'directory', 'retrieved_at': NOW, **changes}
    value.setdefault('sources', [])
    if changes.get('industry_text') and 'source_excerpt' not in changes:
        value['source_excerpt'] = value['company_name'] + ' provides ' + changes['industry_text'] + ' services.'
    if value.get('website_url') and 'sources' not in changes:
        value['sources'].append({'field': 'website_url', 'value': value['website_url'],
            'source_url': value['source_url'], 'source_type': 'directory', 'retrieved_at': NOW,
            'source_excerpt': value['company_name'] + ': ' + value['website_url'], 'method': 'agent_page_observation'})
    if value.get('phone'):
        value['sources'].append({'field': 'phone', 'value': value['phone'],
            'source_url': value['source_url'], 'source_type': 'directory', 'retrieved_at': NOW,
            'source_excerpt': value['company_name'] + ' business phone ' + value['phone'], 'method': 'agent_page_observation'})
    return value


def task(value=None, **changes):
    return {'input': value or candidate(), 'segment': deepcopy(SPEC), 'demo_only': False,
            'plan': {'page_limit_per_company': 10, 'page_limit_total': 100}, 'pages_used': 0, **changes}


@pytest.fixture
def network(monkeypatch):
    content, calls, extractions = {}, [], []
    def request(url, **kwargs):
        calls.append(url)
        return {'final_url': url, 'html': content.get(url, ''), 'fetched_at': NOW,
                'source_type': 'official_site', 'fetch_status': 'ok'}
    extract = b2b_contacts.extract_company
    def tracked(value, pages, spec, **kwargs):
        extractions.append(len(pages))
        return extract(value, pages, spec, mx_lookup=lambda _: 'valid', now=NOW)
    monkeypatch.setattr(b2b_sources, 'safe_http', request)
    monkeypatch.setattr(b2b_contacts, 'extract_company', tracked)
    return content, calls, extractions


@pytest.mark.parametrize('changes', [
    {'country_code': 'CA'}, {'industry_text': 'moving services'},
])
def test_explicit_mismatch_never_fetches_or_extracts_contacts(network, changes):
    content, calls, extractions = network
    pages = []
    result = process_company(task(candidate(**changes)), pages)
    assert calls == [] and extractions == [] and pages == []
    assert result['status'] == 'excluded' and result['contacts'] == []
    assert result['lead_score'] <= 20 and result['pipeline']['deep_crawl_skipped']


def test_uncertain_candidate_stops_after_two_pages_without_contact_work(network):
    content, calls, extractions = network
    content[HOME] = '<p>General business</p><a href="/services">Services</a><a href="/team">Team</a>'
    content[HOME + 'services'] = '<p>Public general services</p><a href="/contact">Contact</a>'
    result = process_company(task(), [])
    assert calls == [HOME, HOME + 'services'] and extractions == []
    assert result['status'] == 'needs_review'
    assert result['pipeline']['stop_reason'] == 'qualification_uncertain'
    assert result['pipeline']['pages_fetched'] == 2


def test_qualified_home_reused_then_named_team_channel_stops_before_other_pages(network):
    content, calls, extractions = network
    content[HOME] = '<p>Freight forwarding</p><a href="/services">Services</a><a href="/contact">Contact</a><a href="/team">Team</a>'
    content[HOME + 'team'] = '<div class="team-member"><h3>Anna Smith</h3><span class="position">Sales Director</span><a href="mailto:anna@forwarding.example.com">Email</a></div>'
    pages = []
    result = process_company(task(), pages)
    assert calls == [HOME, HOME + 'team'] and extractions == [1, 2]
    assert result['status'] == 'qualified' and result['lead_score'] > 40
    assert result['main_email'] == 'anna@forwarding.example.com'
    assert result['contacts'][0]['contact_priority'] == 'direct_business'
    assert result['contacts'][0]['email_status'] != 'verified'
    assert result['pipeline']['stop_reason'] == 'contact_target_reached'
    assert result['pipeline']['qualification_pages'] == 1
    assert result['qualification']['independent_sources'] == 1


def test_second_qualification_page_is_reused_and_structured_country_can_confirm_seed(network):
    content, calls, _ = network
    content[HOME] = '<a href="/services">Services</a>'
    content[HOME + 'services'] = '<p>Freight forwarding</p><script type="application/ld+json">{"@type":"Organization","name":"Harbor Company","address":{"addressCountry":"United States"}}</script><a href="/team">Team</a>'
    result = process_company(task(candidate(country_code='')), [])
    assert calls.count(HOME) == calls.count(HOME + 'services') == 1
    assert result['qualification']['decision'] == 'qualified'
    assert result['country_code'] == 'US'
    assert result['pipeline']['qualification_pages'] == 2


def test_role_email_on_home_does_not_stop_search_for_named_person(network):
    content, calls, _ = network
    content[HOME] = '<p>Freight forwarding</p><a href="mailto:info@forwarding.example.com">Email</a><a href="/team">Team</a><a href="/contact">Contact</a>'
    content[HOME + 'team'] = '<p>Our team</p>'
    content[HOME + 'contact'] = '<a href="mailto:info@forwarding.example.com">Email</a><a href="tel:+12135550123">Phone</a><a href="/more">More</a>'
    result = process_company(task(), [])
    assert calls == [HOME, HOME + 'team', HOME + 'contact']
    assert result['pipeline']['stop_reason'] == 'contact_target_reached'


def test_new_negative_evidence_during_enrichment_stops_remaining_work(network):
    content, calls, extractions = network
    content[HOME] = '<p>Freight forwarding</p><a href="/team">Team</a><a href="/contact">Contact</a>'
    content[HOME + 'team'] = '<p>Moving services</p>'
    result = process_company(task(), [])
    assert calls == [HOME, HOME + 'team'] and extractions == [1]
    assert result['status'] == 'excluded' and result['contacts'] == []


def test_remaining_budget_counts_shallow_pages_and_never_regrants_allowance(network):
    content, calls, _ = network
    content[HOME] = '<p>Freight forwarding</p><a href="/team">Team</a><a href="/contact">Contact</a>'
    result = process_company(task(pages_used=99), [])
    assert calls == [HOME]
    assert result['pipeline']['stop_reason'] == 'page_budget_reached'
    assert result['pipeline']['pages_fetched'] == 1


def test_no_website_directory_can_qualify_and_use_existing_contacts_without_page_budget(network):
    content, calls, extractions = network
    result = process_company(task(candidate(website_url='', industry_text='freight forwarding', phone='+12135550123'), pages_used=100), [])
    assert calls == [] and extractions == [0]
    assert result['status'] == 'qualified' and result['main_phone'] == '+12135550123'
    assert result['pipeline']['stop_reason'] == 'no_website'


def test_cross_source_business_evidence_changes_qualification_and_strength_not_just_contacts():
    value = candidate(industry_text='')
    single = assess_candidate(value, [], SPEC)
    value['sources'] = [{'source_url': 'https://association.net/members/harbor', 'source_type': 'association',
        'industry_text': 'Freight forwarding', 'country_code': 'US', 'retrieved_at': NOW,
        'source_excerpt': 'Harbor Company provides freight forwarding services.'}]
    confirmed = assess_candidate(value, [], SPEC)
    assert single['decision'] == 'uncertain' and confirmed['decision'] == 'qualified'
    value['sources'].append({'source_url': 'https://registry.org/harbor', 'source_type': 'registry',
        'industry_text': 'Freight forwarding', 'retrieved_at': NOW,
        'source_excerpt': 'Harbor Company provides freight forwarding services.'})
    crossed = assess_candidate(value, [], SPEC)
    assert crossed['independent_sources'] == 2 and crossed['confidence'] > confirmed['confidence']
    assert {e['source_url'] for e in crossed['evidence']} == {s['source_url'] for s in value['sources']}


def test_same_source_paths_or_subdomains_do_not_multiply_independent_evidence():
    value = candidate(industry_text='freight forwarding', source_url='https://registry.org/a')
    first = assess_candidate(value, [], SPEC)
    value['sources'] = [{'source_url': url, 'industry_text': 'freight forwarding',
        'source_excerpt': 'Harbor Company provides freight forwarding services.'} for url in
        ('https://registry.org/b', 'https://members.registry.org/c')]
    other = assess_candidate(value, [], SPEC)
    assert other['independent_sources'] == first['independent_sources'] == 1
    assert other['confidence'] == first['confidence']


def test_bare_source_links_or_search_query_do_not_create_industry_proof():
    value = candidate(company_name='Harbor')
    value['sources'] = [{'source_url': 'https://association.net/search?q=freight+forwarding'},
                        {'source_url': 'https://registry.org/search?q=freight+forwarding'}]
    result = assess_candidate(value, [], SPEC)
    assert result['decision'] == 'uncertain' and result['independent_sources'] == 0


def test_disagreeing_country_sources_are_uncertain_instead_of_silently_choosing_one():
    value = candidate(country_code='CA', industry_text='freight forwarding', sources=[{
        'source_url': 'https://association.net/harbor', 'country_code': 'US', 'industry_text': 'freight forwarding'}])
    assert assess_candidate(value, [], SPEC)['decision'] == 'uncertain'


def test_unvalidated_direct_phone_does_not_satisfy_contact_target():
    record = {'contacts': [{'contact_name': 'Anna Smith', 'binding_status': 'explicit', 'phone': '+12135550123',
        'phone_status': 'unknown', 'phone_type': 'direct_business', 'confidence': .9, 'tags': []}]}
    assert not contact_target_reached(record, [], SPEC)


def test_mx_result_is_reused_across_incremental_extraction(network, monkeypatch):
    content, calls, _ = network
    lookups = []
    monkeypatch.setattr(b2b_contacts, 'extract_company', EXTRACT_COMPANY)
    monkeypatch.setattr(web_dns, 'lookup_mx', lambda host: lookups.append(host) or 'valid')
    content[HOME] = '<p>Freight forwarding</p><a href="mailto:info@forwarding.example.com">Mail</a><a href="/team">Team</a>'
    content[HOME + 'team'] = '<div class="team-member"><h3>Anna Smith</h3><a href="mailto:anna@forwarding.example.com">Email</a></div>'
    result = process_company(task(), [])
    assert calls == [HOME, HOME + 'team'] and lookups == ['forwarding.example.com']
    assert result['pipeline']['stop_reason'] == 'contact_target_reached'


def test_explicit_domain_exclusions_skip_all_requests(network):
    _, calls, extractions = network
    value = task(segment={**SPEC, 'exclusions': ['forwarding.example.com']})
    assert process_company(value, [])['status'] == 'excluded'
    assert calls == extractions == []


def test_workspace_company_suppression_skips_all_requests(network):
    _, calls, extractions = network
    value = task(suppressions=[{'type': 'company', 'value': 'Harbor Company'}])
    result = process_company(value, [])
    assert result['status'] == 'excluded' and calls == extractions == []
    assert result['qualification']['reasons'] == ['命中抑制名单']


@pytest.mark.parametrize('use_workspace_rule', [True, False])
def test_suppressed_named_channel_cannot_end_search_before_alternative(network, use_workspace_rule):
    content, calls, _ = network
    content[HOME] = '<p>Freight forwarding</p><div class="team-member"><h3>Anna Smith</h3><a href="mailto:anna@forwarding.example.com">Mail</a></div><a href="/team">Team</a>'
    content[HOME + 'team'] = '<div class="team-member"><h3>Beth Lee</h3><a href="mailto:beth@forwarding.example.com">Mail</a></div>'
    value = task()
    if use_workspace_rule:
        value['suppressions'] = [{'type': 'email', 'value': 'anna@forwarding.example.com'}]
    else:
        value['segment']['exclusions'] = ['anna@forwarding.example.com']
    result = process_company(value, [])
    assert calls == [HOME, HOME + 'team']
    assert any(c.get('email') == 'beth@forwarding.example.com' for c in result['contacts'])
    assert result['pipeline']['stop_reason'] == 'contact_target_reached'


def test_explicit_wrong_company_type_does_not_need_an_industry_match_to_reject(network):
    _, calls, extractions = network
    value = task(candidate(company_type='retailer'),segment={**SPEC, 'company_types': ['freight forwarder']})
    assert process_company(value, [])['status'] == 'excluded'
    assert calls == extractions == []


def test_foreign_organization_json_ld_is_not_candidate_qualification_evidence():
    value = candidate(country_code='')
    foreign = '<script type="application/ld+json">{"@type":"Organization","url":"https://web-agency.net/","industry":"Freight forwarding","address":{"addressCountry":"US"}}</script>'
    pages = [{'final_url': HOME, 'html': foreign, 'fetched_at': NOW}]
    assert assess_candidate(value, pages, SPEC)['decision'] == 'uncertain'
    owned = '<script type="application/ld+json">{"@type":"Organization","@id":"#organization","url":"https://forwarding.example.com/","industry":"Freight forwarding","address":{"addressCountry":"US"}}</script>'
    other_country = foreign.replace('"US"', '"CA"')
    pages[0]['html'] = owned + other_country
    result = assess_candidate(value, pages, SPEC)
    assert result['decision'] == 'qualified' and result['profile']['country_code'] == 'US'


def test_published_size_intervals_keep_overlap_through_qualification_and_extraction(network):
    content, calls, _ = network
    content[HOME] = '''<p>Freight forwarding</p><script type="application/ld+json">{
      "@type":"Organization","url":"https://forwarding.example.com/",
      "numberOfEmployees":{"minValue":10,"maxValue":100},
      "annualRevenue":{"minValue":1,"maxValue":5,"unitText":"million","currency":"USD"}
    }</script>'''
    value = task(segment={**SPEC, 'employee_range': '50-100', 'revenue_range': '2-3 million'})
    result = process_company(value, [])
    assert calls == [HOME] and result['qualification']['decision'] == 'qualified'
    assert result['employee_count'] == '10-100' and result['revenue'] == '1-5 million USD'


def test_qualification_facts_from_secondary_sources_reach_company_profile(network):
    _, calls, _ = network
    value = candidate(website_url='',country_code='',sources=[{'source_url':'https://association.net/harbor',
        'source_type':'association','retrieved_at':NOW,'industry_text':'freight forwarding','country_code':'US',
        'source_excerpt': 'Harbor Company provides freight forwarding services in the US.'}])
    result = process_company(task(value), [])
    assert result['qualification']['decision']=='qualified' and calls==[]
    assert result['country_code']=='US' and result['industry_text']=='freight forwarding'


@pytest.mark.parametrize('value, code', [
    ({'company_name': 'No source'}, 'MISSING_SOURCE'),
    ({'website_url': 'http://127.0.0.1/'}, 'INVALID_URL'),
    ({'website_url': 'https://public.example.com/', 'country_code': 'XX'}, 'INVALID_COUNTRY'),
    ({'website_url': 'https://public.example.com/', 'email': [12]}, 'VALIDATION_ERROR'),
    ({'website_url': 'https://public.example.com/', 'sources': [{}] * 101}, 'VALIDATION_ERROR'),
    ({'website_url': 'https://public.example.com/', 'retrieved_at': '2026-10-23T00:00:00'}, 'VALIDATION_ERROR'),
])
def test_candidate_validation_does_not_require_database_or_network(value, code, monkeypatch):
    import socket
    from app.pipeline import normalize_candidate
    from app.errors import AppError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('candidate normalization resolved DNS'))
    with pytest.raises(AppError) as failure:
        normalize_candidate(value, SPEC)
    assert failure.value.code == code


def test_normalized_candidate_copies_evidence_and_fixture_requires_explicit_flag():
    from app.pipeline import normalize_candidate
    from app.errors import AppError
    value = {'website_url': 'sample.test', 'sources': [{'source_url': 'https://directory.test/a'}]}
    with pytest.raises(AppError):
        normalize_candidate(value, SPEC)
    normalized = normalize_candidate(value, SPEC, fixture=True)
    assert normalized['website_url'] == 'https://sample.test/'
    assert normalized['company_name_is_domain_hint'] and normalized['demo_only']
    normalized['sources'][0]['source_url'] = 'https://changed.test/'
    assert value['sources'][0]['source_url'] == 'https://directory.test/a'


def test_company_budget_exhaustion_is_explicit_without_network(network):
    from app.errors import AppError
    _, calls, extractions = network
    with pytest.raises(AppError) as failure:
        process_company(task(pages_used=100), [])
    assert failure.value.code == 'PAGE_BUDGET_EXCEEDED'
    assert calls == extractions == []


def test_pipeline_enforces_page_cap_even_if_plan_requests_more(network):
    # spec b2b-leads-2.6 O3：contacts 阶段硬上限 3 页（首页 + 2 个 contact 链接），
    # plan 请求再多也不能突破。
    content, calls, _ = network
    content[HOME] = '<p>Freight forwarding</p>' + ''.join(f'<a href="/contact-{i}">Contact</a>' for i in range(20))
    pages = []
    result = process_company(task(plan={'page_limit_per_company': 100, 'page_limit_total': 1000}), pages)
    assert len(calls) == len(pages) == 3
    # pipeline 的计划上限仍是 10（plan 100 取 core 上限），抓取在 3 页处停止，
    # 因此按 pipeline 视角是"没有更多页面"而不是计划预算耗尽。
    assert result['pipeline']['stop_reason'] == 'no_more_pages'


@pytest.mark.parametrize('excerpt', [
    'We are not a freight forwarding company. We make software.',
    'We provide software for freight forwarding companies.',
    'Our customers are freight forwarding companies.',
    'We no longer provide freight forwarding services.',
    'Harbor Company specializes in freight forwarding software.',
    'Harbor Company publishes news: freight forwarding demand is falling.',
    'Harbor Company builds accounting tools. Freight forwarding is an industry we serve.',
    'Harbor Company company profile. Contact our team for more information.',
])
def test_industry_denials_and_customer_industry_never_endorse_own_activity(excerpt):
    value = candidate(website_url='', industry_text='freight forwarding', source_excerpt=excerpt)
    assert assess_candidate(value, [], SPEC)['decision'] == 'uncertain'


def test_company_name_alone_does_not_establish_industry():
    value = candidate(website_url='', company_name='Freight Forwarding LLC')
    assert assess_candidate(value, [], SPEC)['decision'] == 'uncertain'


@pytest.mark.parametrize('name,observed', [
    ('Harbor Logistics Inc.', 'Harbor Logistics Inc.'),
    ('Harbor Logistics Inc', 'Harbor Logistics Inc.'),
    ('Harbor Logistics Co., Ltd.', 'Harbor Logistics Co., Ltd.'),
    ('Harbor Logistics Co Ltd', 'Harbor Logistics Co., Ltd.'),
])
def test_legal_suffix_periods_do_not_split_the_company_from_its_service_claim(name, observed):
    value = candidate(website_url='', company_name=name,
        source_excerpt=observed + ' provides freight forwarding services in the US.')
    assert assess_candidate(value, [], SPEC)['decision'] == 'qualified'
    value['source_excerpt'] = observed + ' specializes in freight forwarding software.'
    assert assess_candidate(value, [], SPEC)['decision'] == 'uncertain'


def test_unrelated_official_subject_overrides_a_stale_directory_website_link(network):
    content, calls, extractions = network
    content[HOME] = '<meta property="og:site_name" content="Other Company"><p>Freight forwarding</p><a href="mailto:info@forwarding.example.com">Mail</a>'
    result = process_company(task(), [])
    assert calls == [HOME] and extractions == []
    assert result['qualification']['decision'] == 'uncertain'
    assert result['website_binding_status'] == 'conflicting'
    assert result['contacts'] == []


def test_official_other_subject_cannot_be_overridden_by_customer_name_and_city():
    from app.rules.evidence import website_identity
    value = candidate(city='Boston')
    page = {'final_url': HOME, 'fetched_at': NOW, 'html':
        '<meta property="og:site_name" content="Other Company"><h1>Other Company</h1>'
        '<p>Freight forwarding provider. Our customer Harbor Company operates in Boston.</p>'}
    assert website_identity(value, [page])['status'] == 'conflicting'
    assert assess_candidate(value, [page], SPEC)['decision'] == 'uncertain'


def test_shared_group_site_accepts_the_explicit_subsidiary_organization():
    from app.rules.evidence import website_identity
    value = candidate(sources=[], city='Boston')
    page = {'final_url': HOME, 'fetched_at': NOW, 'html':
        '<meta property="og:site_name" content="Harbor Group"><h1>Harbor Group</h1>'
        '<script type="application/ld+json">{"@type":"Organization","name":"Harbor Company",'
        '"url":"https://forwarding.example.com/","address":{"addressLocality":"Boston"}}</script>'}
    assert website_identity(value, [page])['status'] == 'confirmed'


def test_explicit_directory_category_requires_actual_excerpt_and_page_subject():
    value = candidate(website_url='', sources=[
        {'field': 'company_name', 'value': 'Harbor Company', 'source_url': 'https://directory.org/harbor',
         'source_excerpt': 'Harbor Company'},
        {'field': 'industry_text', 'value': 'freight forwarding', 'source_url': 'https://directory.org/harbor',
         'source_excerpt': 'Industry: freight forwarding'}])
    assert assess_candidate(value, [], SPEC)['decision'] == 'qualified'
    value['sources'][1]['source_excerpt'] = 'General company profile.'
    assert assess_candidate(value, [], SPEC)['decision'] == 'uncertain'


def test_unbound_website_cannot_supply_industry_or_country_even_when_agent_claims_confirmed():
    value = candidate(country_code='', sources=[], website_binding_status='confirmed')
    page = {'final_url': HOME, 'fetched_at': NOW, 'html': '<script type="application/ld+json">{"@type":"Organization","name":"Other Company","industry":"freight forwarding","address":{"addressCountry":"US"}}</script>'}
    result = assess_candidate(value, [page], SPEC)
    assert result['decision'] == 'uncertain'
    assert not result['profile']['country_code']


def test_official_name_requires_a_corresponding_operating_location():
    from app.rules.evidence import website_identity
    value = candidate(sources=[], address='10 Harbor Avenue')
    page = {'final_url': HOME, 'fetched_at': NOW,
        'html': '<meta property="og:site_name" content="Harbor Company"><p>United States</p>'}
    assert website_identity(value, [page])['status'] == 'unconfirmed'
    page['html'] += '<p>10 Harbor Avenue</p>'
    assert website_identity(value, [page])['status'] == 'confirmed'


def test_two_distinct_entities_can_have_observed_associations_to_one_domain():
    from app.rules.evidence import website_identity
    first = candidate()
    second = candidate(company_name='Harbor Subsidiary LLC')
    assert website_identity(first)['status'] == website_identity(second)['status'] == 'confirmed'


def test_transport_failure_remains_retryable_and_preserves_attempt(network, monkeypatch):
    from app.web.errors import WebError
    from app.errors import AppError
    monkeypatch.setattr(b2b_sources, 'safe_http', lambda *a, **k: (_ for _ in ()).throw(
        WebError('CONNECTION_ERROR', 'Network unavailable', retryable=True)))
    pages = []
    with pytest.raises(AppError) as failure:
        process_company(task(), pages)
    assert failure.value.code == 'CONNECTION_ERROR' and failure.value.retryable
    assert len(pages) == 1 and pages[0]['error']['retryable']


@pytest.mark.parametrize('qualified', [False, True])
def test_raw_phone_assertion_never_swallows_all_page_rate_limit(network, monkeypatch, qualified):
    from app.web.errors import WebError
    from app.errors import AppError
    monkeypatch.setattr(b2b_sources, 'safe_http', lambda *a, **k: (_ for _ in ()).throw(
        WebError('RATE_LIMITED', 'Try later', retryable=True, retry_after_s=120, upstream_status=429)))
    value = candidate(phone='+12135550123', **({'industry_text': 'freight forwarding'} if qualified else {}))
    value['sources'] = [source for source in value['sources'] if source.get('field') != 'phone']
    with pytest.raises(AppError) as failure:
        process_company(task(value), [])
    assert failure.value.code == 'RATE_LIMITED' and failure.value.retryable
    assert failure.value.retry_after_s == 120 and failure.value.upstream_status == 429


def test_qualified_sourced_directory_channel_survives_optional_site_rate_limit(network, monkeypatch):
    from app.web.errors import WebError
    from app.delivery.selection import delivery_companies
    monkeypatch.setattr(b2b_sources, 'safe_http', lambda *a, **k: (_ for _ in ()).throw(
        WebError('RATE_LIMITED', 'Try later', retryable=True, retry_after_s=120, upstream_status=429)))
    value = candidate(industry_text='freight forwarding', phone='+12135550123')
    result = process_company(task(value), [])
    assert result['qualification']['decision'] == 'qualified'
    assert result['main_phone'] == '+12135550123'
    assert result['fetch_errors'][0]['retry_after_s'] == 120
    assert len(delivery_companies([result])) == 1
