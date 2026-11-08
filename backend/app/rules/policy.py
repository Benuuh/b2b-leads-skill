"""Shared qualification-policy evaluation for versioned task configurations.

Every fact must be bound to the enterprise and backed by an observed source
row.  Values that cannot be verified stay ``unknown``; only an evidenced fact
that contradicts a necessary condition becomes ``fail``.  Absence of evidence
is never rewritten as a negative fact.
"""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
from hashlib import sha256
import json
import re
import time

import pycountry

from app.capabilities import BUSINESS_MODELS, COUNTRY_SCOPES, FIELDS, RULE_VERSION
from app.rules.evidence import observation_valid, subject_excerpt, website_identity
from app.rules.qualification import (_contains, _industry_hits, lightweight_profile,
                                     observations, range_bounds, source_domain)

COUNTRY_FIELDS = {'entity.country_code', 'entity.headquarters_country', 'entity.registered_country'}
COUNTRY_DECLARED = {'entity.country_code': ('country_code',),
                    'entity.headquarters_country': ('headquarters_country',),
                    'entity.registered_country': ('registered_country',)}
LEGACY_FIELDS = {
    'entity.country_code': ('country_code',),
    'entity.headquarters_country': ('headquarters_country',),
    'entity.registered_country': ('registered_country',),
    'entity.region': ('region', 'regions'),
    'business.models': ('business_models', 'business_models_text'),
    'business.type': ('company_type', 'company_types'),
    'offering.products': ('offering_products', 'products'),
    'offering.services': ('offering_services', 'services'),
    'customer.industries': ('customer_industries', 'customer_industry'),
    'entity.relationship': ('entity_relationship',),
    'size.employees': ('employee_count',),
    'size.revenue': ('revenue',),
}
RELATIONSHIP_WORDS = {
    'independent': ('independent', 'family-owned', 'privately held', 'privately-owned'),
    'subsidiary': ('subsidiary', 'subsidiaries', 'part of', 'owned by', 'group company', 'member of'),
    'branch': ('branch', 'local office', 'representative office', 'regional office', 'filial'),
    'unknown': (),
}
MODEL_LOOKUP = {synonym.casefold(): name for name, words in BUSINESS_MODELS.items() for synonym in words}


def _country_names(code):
    record = pycountry.countries.get(alpha_2=str(code or '').upper())
    if record is None:
        return []
    return [item for item in (record.name, getattr(record, 'official_name', None),
                              getattr(record, 'common_name', None)) if item]


def country_supported(code, excerpt):
    """A country fact needs the ISO code or the country's own name in the text."""
    text = str(excerpt or '')
    code = str(code or '').upper()
    if not text or len(code) != 2:
        return False
    if re.search(r'(?<![A-Za-z])' + re.escape(code) + r'(?![A-Za-z])', text):
        return True
    folded = text.casefold()
    return any(name and re.search(r'(?<!\w)' + re.escape(name.casefold()) + r'(?!\w)', folded)
               for name in _country_names(code))


def _canonical(field, value):
    if field in COUNTRY_FIELDS:
        text = str(value or '').strip()
        if len(text) == 2 and text.isalpha():
            return text.upper()
        try:
            return pycountry.countries.lookup(text).alpha_2
        except LookupError:
            return ''
    if field == 'business.models':
        text = str(value or '').strip()
        return MODEL_LOOKUP.get(text.casefold(), text.casefold())
    return str(value or '').strip()


def _supported(field, value, excerpt):
    text = str(excerpt or '')
    if not text.strip():
        return False
    if field in COUNTRY_FIELDS:
        return country_supported(value, text)
    if field == 'business.models' or field.startswith('offering.'):
        terms = BUSINESS_MODELS.get(value, (value,)) if field == 'business.models' else (value,)
        for term in terms:
            target = re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.I)
            if not target:
                continue
            before = text[max(0, target.start() - 100):target.start()]
            if re.search(r'\b(?:not|never|no\s+longer)\s+(?:(?:a|an|the|provide|provides|offer|offers|manufacture|manufactures|produce|produces)\s+){0,3}$', before, re.I):
                return False
            if re.search(r'\bsoftware\b[^.;]{0,45}\b(?:to|for)\b[^.;]{0,35}$', before, re.I):
                return False
    if field == 'business.models':
        return any(_contains(text, word) for word in BUSINESS_MODELS.get(value) or (value,))
    if field == 'entity.relationship':
        return any(_contains(text, word) for word in RELATIONSHIP_WORDS.get(value, (value,)))
    return _contains(text, value)


def _row_values(row, field):
    if row.get('field') == field or row.get('field') in LEGACY_FIELDS.get(field, ()):
        value = row.get('value')
    else:
        value = row.get(field)
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _declared(candidate, field):
    keys = COUNTRY_DECLARED.get(field, ()) or LEGACY_FIELDS.get(field, ())
    values = []
    for key in keys:
        value = candidate.get(key)
        if value not in (None, ''):
            values.extend(value if isinstance(value, (list, tuple)) else [value])
    return values


class Facts:
    """Collected fact values with the observations that actually support them."""

    def __init__(self, candidate, pages, spec, profile, binding):
        self.name = str(candidate.get('company_name') or '')
        self.country = str(profile.get('country_code') or '').upper()
        self.values, self.evidenced, self.unevidenced = {}, {}, {}
        self.texts, self.industry_refs = [], []
        self._collect(candidate, pages, binding)
        self.profile = profile

    def _collect(self, candidate, pages, binding):
        refs = [candidate, *[item for item in candidate.get('sources') or [] if isinstance(item, dict)]]
        rows = []
        for ref in refs:
            if not observation_valid(ref):
                continue
            excerpt = str(ref.get('source_excerpt') or '')
            # A shared directory URL is not a shared enterprise subject.
            # Versioned facts retain their own subject excerpt, per contract.
            bound = subject_excerpt(self.name, excerpt)
            rows.append({'ref': ref, 'excerpt': excerpt, 'bound': bound,
                         'source_url': ref.get('source_url'), 'retrieved_at': ref.get('retrieved_at'),
                         'source_type': ref.get('source_type'), 'method': ref.get('method'),
                         'source_id': ref.get('source_id') or 'unknown'})
            if bound:
                self.texts.append(excerpt)
        for page in pages:
            if page.get('error') or page.get('fetch_status') not in (None, 'ok', 'success', 'fetched', 200, '200'):
                continue
            self.texts.append(str(page.get('text') or ''))
        self.industry_refs = [ref for ref in observations(candidate, pages)
                              if not (ref.get('source_type') in {'official_site', 'company_website'}
                                      and binding['status'] != 'confirmed')]
        for field in FIELDS:
            evidenced, unevidenced = [], []
            for row in rows:
                for value in _row_values(row['ref'], field):
                    normalized = _canonical(field, value)
                    if not normalized:
                        continue
                    if row['bound'] and _supported(field, normalized, row['excerpt']):
                        evidenced.append({'value': normalized, 'source_url': row['source_url'],
                                          'retrieved_at': row['retrieved_at'],
                                          'source_type': row['source_type'], 'method': row['method'],
                                          'source_id': row['source_id'],
                                          'excerpt': row['excerpt'][:400]})
                    else:
                        unevidenced.append(normalized)
            declared = [value for value in (_canonical(field, item) for item in _declared(candidate, field)) if value]
            supported = {item['value'] for item in evidenced}
            self.evidenced[field] = evidenced
            self.unevidenced[field] = [value for value in dict.fromkeys(unevidenced) if value not in supported]
            self.values[field] = list(dict.fromkeys([*supported, *declared]))
        if self.country and self.country not in self.values.get('entity.country_code', []):
            self.values['entity.country_code'] = [*self.values.get('entity.country_code', []), self.country]

    def evidenced_values(self, field):
        return [item['value'] for item in self.evidenced.get(field) or []]

    def evidence_for(self, field, values):
        wanted = set(values)
        return [item for item in self.evidenced.get(field) or [] if item['value'] in wanted][:20]


def _base(condition):
    return {'id': condition['id'], 'field': condition['field'],
            'operator': condition['operator'], 'value': condition['value']}


def _industry_status(condition, facts):
    words = condition['value'] if isinstance(condition['value'], list) else [condition['value']]
    hits = sorted({hit for ref in facts.industry_refs if ref.get('subject_bound')
                   for hit in _industry_hits(ref, words)})
    if hits:
        return {'status': 'pass', 'evidence': hits, 'reason': '公开内容命中行业条件'}
    return {'status': 'unknown', 'evidence': [], 'reason': '公开内容不足以确认行业匹配'}


def _exclude_terms_status(condition, facts):
    terms = condition['value'] if isinstance(condition['value'], list) else [condition['value']]
    hits = sorted({term for term in terms for text in facts.texts if _contains(text, term)})
    return {'status': 'pass' if hits else 'unknown', 'evidence': hits,
            'reason': ('命中排除词: ' + ', '.join(hits)) if hits else '尚无命中排除词的证据'}


def _range_status(condition, facts):
    actual_key = 'employee_count' if condition['field'] == 'size.employees' else 'revenue'
    requested = range_bounds(condition['value'])
    actual = range_bounds(facts.profile.get(actual_key))
    if not actual or not requested:
        return {'status': 'unknown', 'evidence': [], 'reason': '缺少可比较的规模证据'}
    matched = not (actual[1] < requested[0] or actual[0] > requested[1])
    return {'status': 'pass' if matched else 'fail',
            'evidence': [facts.profile.get(actual_key)],
            'reason': '规模在区间内' if matched else '规模不符'}


def _matches(operator, values, wanted):
    if operator in {'in', 'includes_any', 'contains_any', 'matches'}:
        return any(value in set(wanted) for value in values)
    if operator == 'not_in':
        return all(value not in set(wanted) for value in values)
    if operator in {'equals', 'includes', 'provides'}:
        return any(value == wanted for value in values)
    if operator == 'within':
        return False
    return False


def _fact_status(condition, facts):
    field = condition['field']
    evidenced = facts.evidenced_values(field)
    declared = facts.values.get(field) or []
    if FIELDS[field]['single'] and len(set(evidenced)) > 1:
        return {'status': 'conflict', 'evidence': facts.evidence_for(field, evidenced),
                'reason': '同一事实存在多个有来源的取值，需复核'}
    values = evidenced or declared
    if not values:
        return {'status': 'unknown', 'evidence': [], 'reason': '缺少该事实的公开证据'}
    if not evidenced:
        return {'status': 'unknown', 'evidence': [],
                'reason': '该事实只有声明，没有可核验的来源摘录'}
    matched = _matches(condition['operator'], values, condition['value'])
    return {'status': 'pass' if matched else 'fail',
            'evidence': facts.evidence_for(field, values),
            'reason': '必要条件满足' if matched else '有证据表明该条件不满足'}


def evaluate_condition(condition, facts):
    field = condition['field']
    if field == 'industry.match':
        status = _industry_status(condition, facts)
    elif field == 'evidence.exclude_terms':
        status = _exclude_terms_status(condition, facts)
    elif field in {'size.employees', 'size.revenue'}:
        status = _range_status(condition, facts)
    else:
        status = _fact_status(condition, facts)
    return {**_base(condition), **status}


def evaluate_exclusion(condition, facts):
    status = evaluate_condition(condition, facts)
    # An unmet exclusion is never rewritten as a negative fact; it stays unknown
    # unless the enumerated fact set demonstrably does not include the value.
    if status['status'] == 'fail':
        status = {**status, 'status': 'unknown', 'reason': '尚无命中排除条件的证据'}
    return {**status, 'unknown_policy': condition.get('unknown_policy', 'review')}


def _evidence_rows(statuses):
    rows = []
    for status in statuses:
        for item in status.get('evidence') or []:
            if isinstance(item, dict) and item.get('source_url'):
                rows.append({'source_url': item['source_url'], 'value': item.get('value'),
                             'retrieved_at': item.get('retrieved_at'), 'condition': status['id']})
    return rows


def assess_policy(candidate, pages, spec):
    """Evaluate the frozen v2 qualification policy without network or DNS."""
    policy = (spec or {}).get('qualification_policy') or {}
    binding = website_identity(candidate, pages)
    profile = lightweight_profile(candidate, pages)
    facts = Facts(candidate, pages, spec, profile, binding)
    required = [evaluate_condition(condition, facts) for condition in policy.get('required_all') or []]
    excluded = [evaluate_exclusion(condition, facts) for condition in policy.get('exclude_any') or []]
    failing = [item for item in required if item['status'] == 'fail']
    hits = [item for item in excluded if item['status'] == 'pass']
    conflicts = [item for item in [*required, *excluded] if item['status'] == 'conflict']
    unresolved = [item for item in required if item['status'] == 'unknown']
    review_exclusions = [item for item in excluded if item['status'] == 'conflict'
                         or (item['status'] == 'unknown' and item.get('unknown_policy') != 'allow_unconfirmed')]
    reasons = []
    if failing:
        decision = 'excluded'
        reasons.extend(f"必要条件不满足: {item['id']}（{item['reason']}）" for item in failing)
    elif hits:
        decision = 'excluded'
        reasons.extend(f"命中排除条件: {item['id']}（{item['reason']}）" for item in hits)
    elif conflicts or unresolved or review_exclusions:
        decision = 'uncertain'
        for item in {item['id']: item for item in [*unresolved, *conflicts, *review_exclusions]}.values():
            reasons.append(f"待核: {item['id']}（{item['reason']}）")
    else:
        decision = 'qualified'
        reasons.append('必要资格均有来源证据且未命中排除条件')
        passed = [item['id'] for item in required if item['status'] == 'pass']
        if passed:
            reasons.append('满足条件: ' + ', '.join(passed))
        allowed_unknown = [item['id'] for item in excluded if item['status'] == 'unknown']
        if allowed_unknown:
            reasons.append('排除条件保留未知并可交付: ' + ', '.join(allowed_unknown))
    missing = [{'id': item['id'], 'field': item['field'], 'status': item['status'],
                'reason': item['reason'],
                'reason_code': 'fact_conflict' if item['status'] == 'conflict' else 'fact_unknown'}
               for item in {item['id']: item
                            for item in [*unresolved, *conflicts, *review_exclusions]}.values()]
    evidence = _evidence_rows([*required, *excluded])
    origins = {source_domain(item['source_url']) for item in evidence} - {''}
    confidence = 0.0
    if decision == 'qualified':
        confidence = min(0.97, 0.72 + 0.05 * max(0, len(origins) - 1) + (0.05 if evidence else 0))
    elif decision == 'uncertain':
        confidence = 0.35
    return {'decision': decision, 'confidence': round(confidence, 3), 'reasons': reasons,
            'conditions': required, 'exclusions': excluded, 'missing': missing,
            'rule_version': RULE_VERSION, 'independent_sources': len(origins),
            'evidence': evidence, 'profile': profile, 'website_identity': binding}


_CACHE = OrderedDict()
_CACHE_LIMIT = 128
_CACHE_STATS = {'hits': 0, 'misses': 0, 'fingerprint_seconds': 0.0, 'fingerprints': 0}


def policy_cache_stats():
    """Diagnostics only: hit rate and fingerprint time for the policy LRU."""
    result = dict(_CACHE_STATS)
    total = result['hits'] + result['misses']
    result['hit_rate'] = (result['hits'] / total) if total else None
    result['fingerprint_p50_seconds'] = (
        result['fingerprint_seconds'] / result['fingerprints']) if result['fingerprints'] else None
    return result


def reset_policy_cache_stats():
    _CACHE_STATS.update(hits=0, misses=0, fingerprint_seconds=0.0, fingerprints=0)


def assess_policy_cached(candidate, pages, spec):
    """Bounded, evidence-keyed reuse of one assessment of identical inputs.

    The fingerprint covers the entity, every retained page and the frozen
    policy, so another entity or another rule version can never share a result.
    """
    began = time.perf_counter()
    key = evidence_fingerprint(candidate, pages, spec)
    _CACHE_STATS['fingerprints'] += 1
    _CACHE_STATS['fingerprint_seconds'] += max(0.0, time.perf_counter() - began)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE_STATS['hits'] += 1
        _CACHE.move_to_end(key)
        return deepcopy(cached)
    _CACHE_STATS['misses'] += 1
    result = assess_policy(candidate, pages, spec)
    _CACHE[key] = deepcopy(result)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return result


def evidence_fingerprint(candidate, pages, spec):
    """Stable key for an offline assessment of identical retained evidence."""
    payload = {'candidate': _stable(candidate),
               'pages': _stable(pages),
               'spec': _stable(spec), 'rule': RULE_VERSION,
               'window': int(time.time() // 300)}
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _stable(value):
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


def policy_summary(spec):
    policy = (spec or {}).get('qualification_policy') or {}
    return {'spec_version': 2, 'rule_version': RULE_VERSION,
            'country_scope': policy.get('country_scope', 'operating'),
            'country_field': COUNTRY_SCOPES.get(policy.get('country_scope', 'operating')),
            'required_all': [item['id'] for item in policy.get('required_all') or []],
            'exclude_any': [item['id'] for item in policy.get('exclude_any') or []],
            'contact_policy': policy.get('contact_policy'),
            'derived_from': policy.get('derived_from', 'explicit')}


