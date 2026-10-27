"""Cheap, traceable segment checks before contact extraction or deep crawling."""
from copy import deepcopy
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import pycountry
import tldextract
from .evidence import website_identity, subject_excerpt, company_name_pattern
from app.web.parsing import parsed_html

_suffix = tldextract.TLDExtract(suffix_list_urls=())
PROFILE_FIELDS = ('company_name', 'website_url', 'country_code', 'region', 'city',
                  'address', 'industry_text', 'company_type', 'employee_count', 'revenue')


def source_domain(url):
    host = (urlsplit(str(url or '')).hostname or '').lower().removeprefix('www.')
    if not host:
        return ''
    parsed = _suffix(host)
    return parsed.top_domain_under_public_suffix or host


def range_bounds(value):
    text = str('' if value is None else value).replace(',', '').casefold()
    matches = list(re.finditer(r'(\d+(?:\.\d+)?)\s*(?:(billion|million|thousand|[kmb]|万|亿)(?![a-z]))?', text))
    if not matches or len(matches) > 2:
        return None
    factors = {'billion': 1e9, 'b': 1e9, 'million': 1e6, 'm': 1e6,
               'thousand': 1e3, 'k': 1e3, '万': 1e4, '亿': 1e8}
    unit = matches[-1].group(2)
    values = [float(m.group(1)) * factors.get(m.group(2) or unit, 1) for m in matches]
    if len(values) == 2:
        return min(values), max(values)
    if re.search(r'>|≥|以上|at least|minimum|\+', text):
        return values[0], float('inf')
    if re.search(r'<|≤|以下|up to|maximum', text):
        return 0, values[0]
    return values[0], values[0]


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        if '@graph' in value:
            yield from _nodes(value['@graph'])


def _value(value):
    if isinstance(value, dict):
        if value.get('name') is not None:
            return value['name']
        return quantitative_value(value)
    if isinstance(value, list):
        return ' '.join(str(_value(item) or '') for item in value)
    return value


def quantitative_value(value):
    """Preserve published intervals, zero values and magnitude units."""
    if not isinstance(value, dict):
        return value
    amount = value.get('value')
    if isinstance(amount, dict):
        amount = quantitative_value(amount)
    if amount is None:
        lo, hi = value.get('minValue'), value.get('maxValue')
        if lo is not None and hi is not None:
            amount = f'{lo}-{hi}'
        elif lo is not None:
            amount = f'>={lo}'
        elif hi is not None:
            amount = f'<={hi}'
    if amount is None:
        return None
    units = ' '.join(str(value.get(k) or '') for k in ('unitText', 'currency')).strip()
    return f'{amount} {units}' if units else amount


def _country(value):
    if not value:
        return ''
    try:
        return pycountry.countries.lookup(str(_value(value))).alpha_2
    except LookupError:
        return ''


def observations(candidate, pages):
    """Read identity and business evidence only; no email, phone, MX or people work."""
    refs = [candidate] + [v for v in candidate.get('sources', []) if isinstance(v, dict)]
    for ref in refs:
        item = {key: deepcopy(ref[key]) for key in PROFILE_FIELDS if ref.get(key) is not None}
        if ref.get('field') in PROFILE_FIELDS and 'value' in ref:
            item[ref['field']] = deepcopy(ref['value'])
        # A URL-only reference is provenance, not a second industry endorsement.
        item.update(source_url=ref.get('source_url') or ref.get('url'),
                    source_type=ref.get('source_type', 'user_import'),
                    retrieved_at=ref.get('retrieved_at'),
                    source_id=ref.get('source_id') or 'unknown', query_id=ref.get('query_id') or 'unknown')
        item['text'] = str(ref.get('source_excerpt') or '')
        item['source_excerpt'] = ref.get('source_excerpt')
        item['subject_name'] = candidate.get('company_name')
        item['subject_bound'] = subject_excerpt(candidate.get('company_name'), item['text']) or any(
            (other.get('source_url') or other.get('url')) == item['source_url']
            and subject_excerpt(candidate.get('company_name'), other.get('source_excerpt')) for other in refs)
        item['category_observation'] = ref.get('field') == 'industry_text'
        yield item
    for page in pages:
        if page.get('error'):
            continue
        soup = parsed_html(page.get('html', ''))
        item = {'source_url': page.get('final_url'), 'source_type': 'official_site',
                'retrieved_at': page.get('fetched_at'),
                'source_id':page.get('source_id') or 'unknown', 'query_id':page.get('query_id') or 'unknown'}
        business_text = []
        item['structured_industries'] = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                nodes = _nodes(json.loads(script.get_text()))
                for node in nodes:
                    kinds = node.get('@type', [])
                    kinds = [kinds] if isinstance(kinds, str) else kinds
                    if not any(any(word in str(kind).lower() for word in ('organization', 'corporation', 'business', 'professionalservice')) for kind in kinds):
                        continue
                    # Publisher/customer/web-agency entities are not facts about
                    # the candidate, even when embedded on its own homepage.
                    page_url = page.get('final_url') or candidate.get('website_url') or ''
                    own_host = (urlsplit(str(candidate.get('website_url') or page_url)).hostname or '').removeprefix('www.')
                    foreign = False
                    for key in ('url', '@id'):
                        declared = node.get(key)
                        if isinstance(declared, str):
                            declared_host = (urlsplit(urljoin(page_url, declared)).hostname or '').removeprefix('www.')
                            if declared_host and declared_host != own_host:
                                foreign = True
                    if foreign:
                        continue
                    if node.get('name') and not subject_excerpt(candidate.get('company_name'), str(node['name'])):
                        continue
                    for dest, key in (('company_name', 'name'), ('industry_text', 'industry'),
                                      ('employee_count', 'numberOfEmployees'), ('revenue', 'annualRevenue')):
                        if node.get(key) is not None:
                            item[dest] = _value(node[key])
                    business_text.extend(str(_value(node.get(k)) or '') for k in ('description', 'industry', 'knowsAbout'))
                    if node.get('industry'):
                        item['structured_industries'].append(str(_value(node['industry'])))
                    address = node.get('address')
                    if isinstance(address, dict):
                        for dest, key in (('region', 'addressRegion'), ('city', 'addressLocality'), ('address', 'streetAddress')):
                            if address.get(key):
                                item[dest] = _value(address[key])
                        item['country_code'] = _country(address.get('addressCountry'))
            except (ValueError, TypeError):
                continue
        for meta in soup.select('meta[property="og:site_name"]'):
            if meta.get('content'):
                item.setdefault('company_name', meta['content'])
        business_text.extend(str(meta.get('content') or '') for meta in
                             soup.select('meta[name="description"], meta[property="og:description"]'))
        text_view = parsed_html(page.get('html', ''), remove=('script', 'style', 'noscript'))
        item['text'] = '\n'.join([text_view.get_text('\n', strip=True), *business_text])
        item['subject_name'] = candidate.get('company_name')
        item['subject_bound'] = True  # assess_candidate admits only confirmed official sites.
        # Plain-text adapters may not supply HTML.
        if not page.get('html'):
            item['text'] += ' ' + str(page.get('text') or '')
        yield item


def lightweight_profile(candidate, pages):
    profile = {key: deepcopy(candidate.get(key)) for key in PROFILE_FIELDS}
    binding = website_identity(candidate, pages)
    for item in observations(candidate, pages):
        if item.get('source_type') in {'official_site', 'company_website'} and binding['status'] != 'confirmed':
            continue
        for key in PROFILE_FIELDS:
            if item.get(key) and (not profile.get(key) or
                    key == 'company_name' and candidate.get('company_name_is_domain_hint') and item.get('source_type') == 'official_site'):
                profile[key] = item[key]
    return profile


def _contains(text, term):
    text = re.sub(r'[\s_-]+', ' ', str(text).casefold())
    term = re.sub(r'[\s_-]+', ' ', str(term).casefold()).strip()
    if not term:
        return False
    return bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text)) if term.isascii() else term in text


def _industry_hits(ref, words):
    """Accept an observed category or an explicit own-service sentence only.

    Free keyword mentions, customer industries and editorial commentary remain
    uncertain. The small positive grammar is intentionally conservative rather
    than guessing a business model by accumulating negative keyword patches.
    """
    text = str(ref.get('text') or '')
    if not ref.get('subject_bound'):
        return []
    name_pattern = company_name_pattern(ref.get('subject_name'))
    # A legal suffix is part of the observed subject, not a sentence boundary.
    # Protect only matched company-name spans; editorial prose still segments
    # normally, including the negative cases evaluated by the service grammar.
    protected = re.sub(name_pattern + r'(?!\w)', lambda match: match.group().replace('.', '\ue000'), text, flags=re.I) if name_pattern else text
    sentences = [sentence.replace('\ue000', '.') for sentence in re.split(r'[.!?;。；\n]+', protected)]
    subject = r'(?:we|our company|' + name_pattern + r')'
    hits = []
    for word in words:
        if not _contains(text, word):
            continue
        term = re.escape(str(word).casefold()).replace(r'\ ', r'[\s_-]+')
        category = any(re.fullmatch(term, value.strip(), re.I) for value in ref.get('structured_industries') or [])
        for sentence in sentences:
            sentence = sentence.strip()
            if not _contains(sentence, word):
                continue
            if re.fullmatch(r'(?:industry\s*:\s*)?' + term, sentence, re.I):
                category = True
            # A directory category beside its explicit company heading is also
            # an observation; appending a claimed industry field is never one.
            listing = re.fullmatch(name_pattern + r'\s*[:,]\s*' + term + r'(?:\s*,\s*[A-Za-z ]+)?', sentence, re.I)
            if listing:
                category = True
            verb = r'(?:is|are|provides?|offers?|performs?|handles?|speciali[sz]es?\s+in|operates?\s+in)'
            qualifiers = r'(?:(?:a|an|the|leading|international|global|professional|specialist|family-run|licensed|FMC-licensed|technology-enabled|full-service|independent|privately-owned)\s+){0,6}'
            own_service = re.search(r'^' + subject + r'\s+' + verb + r'\s+' + qualifiers + term
                + r'(?=$|\s+(?:services?|company|firm|provider|operations|in|across|throughout|worldwide|based|headquartered|and|with)\b)', sentence, re.I)
            if own_service:
                category = True
        if category:
            hits.append(word)
    return hits


def assess_candidate(candidate, pages, spec, observed=None):
    binding = website_identity(candidate, pages)
    profile = lightweight_profile(candidate, pages)
    profile.update({k: v for k, v in (observed or {}).items() if k in PROFILE_FIELDS and v not in (None, '')})
    refs = list(observations(candidate, pages))
    words = spec.get('keywords', []) or [w.strip() for w in re.split(r'[/,;，；]', spec.get('industry', '')) if w.strip()]
    evidence, domains, matched, excluded = [], set(), set(), set()
    for ref in refs:
        if ref.get('source_type') in {'official_site', 'company_website'} and binding['status'] != 'confirmed':
            continue
        hits = _industry_hits(ref, words)
        excluded.update(w for w in spec.get('exclude_keywords', []) if _contains(ref.get('text', ''), w))
        if hits and ref.get('source_url'):
            matched.update(hits)
            host = source_domain(ref['source_url'])
            if host:
                domains.add(host)
            evidence.append({k: ref.get(k) for k in ('source_url', 'source_type', 'retrieved_at')} | {'matched_terms': hits})
    country = str(profile.get('country_code') or '').upper()
    reasons, decision = [], 'qualified'
    if excluded:
        decision = 'excluded'; reasons.append('排除关键词: ' + ', '.join(sorted(excluded)))
    elif country and country not in spec.get('countries', []):
        decision = 'excluded'; reasons.append('国家不符')
    elif not matched or not country:
        decision = 'uncertain'; reasons.append('公开信息不足以确认客群匹配')
        if not country:
            reasons.append('缺少企业国家证据')
    else:
        reasons.append('公开内容命中: ' + ', '.join(sorted(matched)))
    if len(domains) > 1:
        reasons.append(f'{len(domains)} 个独立来源交叉确认行业')
    text = ' '.join(str(ref.get('text') or '') for ref in refs)
    for field, actual_key in (('employee_range', 'employee_count'), ('revenue_range', 'revenue')):
        if not spec.get(field):
            continue
        requested, actual = range_bounds(spec[field]), range_bounds(profile.get(actual_key))
        if not requested or not actual:
            if decision == 'qualified':
                decision = 'uncertain'
            reasons.append('缺少可比较的规模证据: ' + field)
        elif actual[1] < requested[0] or actual[0] > requested[1]:
            decision = 'excluded'; reasons.append('规模不符: ' + field)
    if spec.get('company_types'):
        actual = str(profile.get('company_type') or '').casefold().replace('_', ' ')
        if not any(_contains(actual, kind) or _contains(text, kind) for kind in spec['company_types']):
            if actual:
                decision = 'excluded'
            elif decision == 'qualified':
                decision = 'uncertain'
            reasons.append('企业类型不符' if actual else '未确认企业类型')
    if spec.get('regions'):
        location = ' '.join(str(profile.get(k) or '') for k in ('address', 'region', 'city')).strip()
        if not location:
            if decision == 'qualified':
                decision = 'uncertain'
            reasons.append('缺少地区证据')
        elif not any(_contains(location, region) for region in spec['regions']):
            decision = 'excluded'; reasons.append('地区不符')
    conflicts = list((observed or {}).get('conflicts') or []) + list(candidate.get('conflicts') or [])
    countries = {str(ref['country_code']).upper() for ref in refs if ref.get('country_code')}
    if len(countries) > 1:
        conflicts.append({'field': 'country_code'})
        if not excluded and any(value in spec.get('countries', []) for value in countries):
            decision = 'uncertain'
        reasons.append('国家来源证据存在冲突')
    # Several public company phone numbers are alternatives, not contradictory
    # evidence about the company's country, industry or website identity.
    qualifying_conflicts = [item for item in conflicts if not (
        isinstance(item, dict) and item.get('field') == 'main_phone'
        and item.get('reason') == 'multiple_public_values')]
    if qualifying_conflicts and decision == 'qualified':
        decision = 'uncertain'; reasons.append('存在冲突证据，需按来源进一步确认')
    if binding['status'] == 'conflicting' and decision != 'excluded':
        decision = 'uncertain'; reasons.append('官网主体证据与候选企业存在冲突')
    # Explicit domain/company suppressions can avoid even the qualification request.
    for value in spec.get('exclusions', []):
        raw = str(value).strip()
        if '@' in raw or raw.startswith('+'):
            continue  # Channel suppression must not exclude the whole company.
        is_domain = '.' in raw and ' ' not in raw
        company_host = urlsplit(str(candidate.get('website_url') or '')).hostname or ''
        suppressed_host = urlsplit(raw if '://' in raw else 'https://' + raw).hostname or ''
        if (is_domain and company_host.removeprefix('www.') == suppressed_host.removeprefix('www.')) or (
                not is_domain and re.sub(r'\W', '', raw.casefold()) == re.sub(r'\W', '', str(profile.get('company_name') or '').casefold())):
            decision = 'excluded'; reasons.append('命中客群排除名单')
    confidence = min(.97, .68 + .12 * max(0, len(domains) - 1) + .05 * any(v['source_type'] == 'official_site' for v in evidence)) if matched and country else .25
    if decision == 'uncertain':
        confidence = min(confidence, .45)
    if decision == 'excluded':
        confidence = 0
    unique_evidence = {json.dumps(item, sort_keys=True): item for item in evidence}
    return {'decision': decision, 'confidence': round(confidence, 3), 'independent_sources': len(domains),
            'reasons': reasons, 'evidence': list(unique_evidence.values()), 'profile': profile,
            'website_identity': binding}


def qualification_record(candidate, pages):
    """Keep rejected/unknown companies reviewable without validating their contacts."""
    profile = lightweight_profile(candidate, pages)
    record = {**profile, 'company_id': candidate.get('company_id'), 'contacts': [],
              'source_id':candidate.get('source_id') or 'unknown', 'query_id':candidate.get('query_id') or 'unknown',
              'domain': (urlsplit(str(candidate.get('website_url') or '')).hostname or '').removeprefix('www.'),
              'sources': [], 'conflicts': deepcopy(candidate.get('conflicts') or []),
              'missing': ['contacts'], 'demo_only': bool(candidate.get('demo_only'))}
    for item in observations(candidate, pages):
        for field in PROFILE_FIELDS:
            if item.get(field) is not None and item.get('source_url'):
                record['sources'].append({'entity_id': record['company_id'], 'field': field, 'value': item[field],
                    'source_url': item['source_url'], 'source_type': item['source_type'],
                    'retrieved_at': item.get('retrieved_at'), 'method': 'qualification_profile', 'confidence': .65,
                    'source_id':item.get('source_id') or 'unknown', 'query_id':item.get('query_id') or 'unknown'})
    return record
