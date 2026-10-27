"""Observed website variations must not turn true own-service evidence into a conflict."""
from app.rules.contacts import extract_company
from app.rules.evidence import website_identity
from app.rules.qualification import assess_candidate

AT = '2026-10-23T00:00:00Z'
SITE = 'https://variant-forwarder.example.com/'


def seed(name='Variant Forwarder'):
    return {'company_id': 'variant', 'company_name': name, 'country_code': 'US',
            'website_url': SITE, 'city': 'Houston', 'source_url': SITE,
            'source_type': 'official_site', 'retrieved_at': AT,
            'sources': [{'field': 'website_url', 'value': SITE, 'source_url': SITE,
                         'source_type': 'official_site', 'retrieved_at': AT,
                         'source_excerpt': name + ': ' + SITE}]}


def test_different_descriptions_keep_provenance_without_identity_conflict():
    html = '''<meta property="og:site_name" content="Variant Forwarder">
        <meta name="description" content="Freight services for US importers">
        <meta property="og:description" content="International logistics from Houston">
        <p>We provide freight forwarding services.</p>'''
    page = {'final_url': SITE, 'html': html, 'fetched_at': AT, 'fetch_status': 'ok'}
    result = extract_company(seed(), [page], {'countries': ['US']}, mx_lookup=lambda _: 'unknown', now=AT)
    assert not any(c['field'] == 'description' for c in result['conflicts'])
    descriptions = {s['value'] for s in result['sources'] if s['field'] == 'description'}
    assert descriptions == {'Freight services for US importers', 'International logistics from Houston'}


def test_legal_suffix_does_not_conflict_with_corroborated_brand():
    page = {'final_url': SITE, 'html': '<meta property="og:site_name" content="Variant Forwarder"><h1>Variant Forwarder</h1><p>Houston</p>', 'fetched_at': AT}
    assert website_identity(seed('Variant Forwarder Inc.'), [page], now=AT)['status'] == 'confirmed'
    page['html'] = '<meta property="og:site_name" content="Different Forwarder"><p>Houston</p>'
    assert website_identity(seed('Variant Forwarder Inc.'), [page], now=AT)['status'] == 'conflicting'


def test_legal_suffix_is_optional_in_own_service_prose_but_other_words_are_not():
    value = seed('Variant Forwarder Inc.')
    spec = {'countries':['US'],'keywords':['freight forwarding']}
    value['source_excerpt'] = 'Variant Forwarder provides freight forwarding services.'
    assert assess_candidate(value,[],spec)['decision'] == 'qualified'
    for sentence in ('Variant Forwarder is not a freight forwarding company.',
                     'Variant Forwarder is a customer of a freight forwarding company.',
                     'Variant Forwarder offers software for freight forwarding companies.',
                     'Variant Forwarder Global provides freight forwarding services.',
                     'Variant ForwarderX provides freight forwarding services.'):
        value['source_excerpt'] = sentence
        assert assess_candidate(value,[],spec)['decision'] == 'uncertain'


def test_qualified_own_service_adjectives_preserve_denials():
    spec = {'countries': ['US'], 'keywords': ['freight forwarder']}
    for sentence in ('Variant Forwarder is a family-run freight forwarder and licensed NVOCC.',
                     'Variant Forwarder is a technology-enabled freight forwarder headquartered in Houston.'):
        value = seed()
        value['source_excerpt'] = sentence
        assert assess_candidate(value, [], spec)['decision'] == 'qualified'
    value['source_excerpt'] = 'Variant Forwarder is not a freight forwarder.'
    assert assess_candidate(value, [], spec)['decision'] == 'uncertain'


def test_address_punctuation_and_us_state_alias_keep_observations():
    value = seed()
    value.update(city='Ontario', region='California', address='611 South Palmetto Avenue, Ontario, CA 91762, USA')
    html = '''<meta property="og:site_name" content="Variant Forwarder">
    <script type="application/ld+json">{"@type":"Organization","name":"Variant Forwarder",
    "address":{"streetAddress":"611 South Palmetto Avenue", "addressLocality":"Ontario",
    "addressRegion":"CA", "postalCode":"91762", "addressCountry":"US"}}</script>'''
    result = extract_company(value, [{'final_url':SITE,'html':html,'fetched_at':AT}],
                             {'countries':['US']},mx_lookup=lambda _:'unknown',now=AT)
    assert not any(c['field'] in {'region','address'} for c in result['conflicts'])
    assert any(s['field']=='region' and s['value']=='CA' for s in result['sources'])


def test_public_phone_alternatives_do_not_override_country_conflicts():
    value = seed()
    value['source_excerpt'] = 'Variant Forwarder provides freight forwarding services.'
    spec = {'countries':['US'],'keywords':['freight forwarding']}
    observed = {'conflicts':[{'field':'main_phone','reason':'multiple_public_values',
                              'values':['+12135550123','+12135550124']}]}
    assert assess_candidate(value,[],spec,observed=observed)['decision']=='qualified'
    observed['conflicts'].append({'field':'country_code','values':['US','CN']})
    assert assess_candidate(value,[],spec,observed=observed)['decision']=='uncertain'


def test_multiple_observed_forms_are_channels_without_identity_conflict():
    value = seed()
    value['source_excerpt'] = 'Variant Forwarder provides freight forwarding services.'
    html = '<meta property="og:site_name" content="Variant Forwarder"><form><input type="email"><textarea name="message"></textarea></form>'
    pages = [{'final_url': SITE, 'html': html, 'fetched_at': AT},
             {'final_url': SITE + 'contact/', 'html': html, 'fetched_at': AT}]
    result = extract_company(value, pages, {'countries': ['US']}, mx_lookup=lambda _: 'unknown', now=AT)
    assert result['contact_form_url'] == SITE
    assert not any(c['field'] == 'contact_form_url' for c in result['conflicts'])
    forms = [s for s in result['sources'] if s['field'] == 'contact_form_url' and s['method'] == 'observed_contact_form']
    assert {s['value'] for s in forms} == {SITE, SITE + 'contact/'}
    assert all(s['form_fields'] for s in forms)
    assert assess_candidate(value, pages, {'countries':['US'], 'keywords':['freight forwarding']}, observed=result)['decision'] == 'qualified'


def test_us_address_aliases_do_not_hide_different_streets_suites_or_zip_codes():
    value = seed()
    value.update(region='California', city='Los Angeles',
                 address='13500 S. Figueroa St., Suite 404, Los Angeles, CA 90061')
    def extract(street='13500 South Figueroa Street, Suite 404', postal='90061-1035'):
        import json
        node = {'@type':'Organization', 'name':'Variant Forwarder', 'address':{
            'streetAddress':street, 'addressLocality':'Los Angeles',
            'postalCode':postal, 'addressCountry':'US'}}
        page = {'final_url':SITE,'fetched_at':AT,'html':'<script type="application/ld+json">' + json.dumps(node) + '</script>'}
        return extract_company(value,[page],{'countries':['US']},mx_lookup=lambda _:'unknown',now=AT)
    assert not any(c['field']=='address' for c in extract()['conflicts'])
    for result in (extract(street='13501 South Figueroa Street, Suite 404'),
                   extract(street='13500 South Figueroa Street, Suite 405'), extract(postal='90062')):
        assert any(c['field']=='address' for c in result['conflicts'])


def test_address_arrays_keep_office_evidence_without_scalar_hq_claim():
    import json
    value = seed()
    value.update(region='California', city='Westlake Village', address='2629 Townsgate Road, Suite 150, Westlake Village, CA 91361')
    addresses = [{'@type':'PostalAddress','streetAddress':'2629 Townsgate Road, Suite 150',
        'addressLocality':'Westlake Village','addressRegion':'CA','postalCode':'91361','addressCountry':'US'},
        {'@type':'PostalAddress','streetAddress':'Foreign office','addressLocality':'Chennai','addressCountry':'IN'}]
    page = {'final_url':SITE,'fetched_at':AT,'html':'<script type="application/ld+json">' + json.dumps({
        '@type':'Organization','name':'Variant Forwarder','address':addresses}) + '</script>'}
    result = extract_company(value,[page],{'countries':['US']},mx_lookup=lambda _:'unknown',now=AT)
    assert result['address'] == value['address'] and result['country_code'] == 'US'
    assert result['office_addresses'] == addresses
    assert not any(c['field'] in {'address','country_code'} for c in result['conflicts'])
    assert any(s['field']=='office_addresses' and s['value']==addresses for s in result['sources'])
