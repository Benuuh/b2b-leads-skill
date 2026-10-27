"""Company processing without database access or batch lifecycle decisions.

``process_company(task, pages)`` returns a detached company result and fills
``pages`` with the bounded page attempts for the caller to persist. Network and
MX behavior live in ``web``; deterministic business decisions live in ``rules``.

Versioned runs add an evidence-first path: retained, verifiable evidence may
complete a company with zero network requests, and only the missing items are
fetched afterwards.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pycountry
import re
import time
import json
from urllib.parse import urlsplit
from app.errors import AppError

CANDIDATE_FIELDS = {
    'company_id', 'company_name', 'website_url', 'country_code', 'region', 'city', 'address',
    'source_url', 'source_type', 'retrieved_at', 'email', 'phone', 'company_type', 'industry_text',
    'employee_count', 'revenue', 'branch_id', 'linkedin_url', 'external_id', 'demo_only', 'sources',
    'conflicts', 'company_name_is_domain_hint', 'source_excerpt', 'source_title', 'rating',
    'review_count', 'source_id', 'query_id', 'candidate_ordinal', 'contact_form_url',
}
# Formal version-2 facts. Each one needs its own observed source row; declaring
# the field alone never satisfies a condition.
V2_CANDIDATE_FIELDS = {
    'headquarters_country', 'registered_country', 'operating_countries', 'business_models',
    'offering_products', 'offering_services', 'customer_industries', 'entity_relationship',
    'parent_company', 'aliases', 'extensions',
}
COUNTRY_KEYS = ('country_code', 'headquarters_country', 'registered_country')


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def _path(index):
    return f'candidates[{index}]' if index is not None else 'candidate'


def _country(value, path, field):
    code = str(value or '').strip().upper()
    if not code:
        return ''
    if not (len(code) == 2 and pycountry.countries.get(alpha_2=code)):
        raise AppError('INVALID_COUNTRY', f'{field} 必须是有效 ISO 两位代码', details={'path': path, 'field': field})
    return code


def _extensions(value, path):
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 50:
        raise AppError('VALIDATION_ERROR', 'extensions 必须是最多 50 项的对象', details={'path': path})
    import json
    if len(json.dumps(value, ensure_ascii=False, default=str)) > 20000:
        raise AppError('VALIDATION_ERROR', 'extensions 最多 20000 字符', details={'path': path})
    return deepcopy(value)


def normalize_candidate(value, spec, *, fixture=False, index=None):
    from app.web.security import normalize_url
    path = _path(index)
    if not isinstance(value, dict):
        raise AppError('VALIDATION_ERROR', '候选企业必须是对象', details={'path': path})
    unknown = sorted(set(value) - CANDIDATE_FIELDS - V2_CANDIDATE_FIELDS)
    if unknown:
        raise AppError('UNKNOWN_FIELD', f'候选包含未声明的正式字段: {", ".join(unknown)}；请放入 extensions 扩展区',
                       details={'path': path, 'fields': unknown})
    result = {k: deepcopy(v) for k, v in value.items() if v is not None}
    if any(len(str(v)) > 10000 for k, v in result.items() if k != 'extensions'):
        raise AppError('VALIDATION_ERROR', '导入字段过长', details={'path': path})
    for field in ['email', 'phone']:
        supplied=result.get(field)
        if supplied is not None and not (isinstance(supplied,str) or isinstance(supplied,list) and len(supplied)<=100 and all(isinstance(v,str) for v in supplied)):
            raise AppError('VALIDATION_ERROR','邮箱/电话应为文本或文本列表', details={'path': path, 'field': field})
    if not isinstance(result.get('sources',[]),list) or len(result.get('sources',[]))>100:
        raise AppError('VALIDATION_ERROR','来源记录必须是列表且最多100项', details={'path': path})
    for source in result.get('sources',[]):
        if not isinstance(source,dict): raise AppError('VALIDATION_ERROR','来源记录格式无效', details={'path': path})
        if source.get('source_url'):
            try: normalize_url(source['source_url'],fixture=bool(fixture))
            except Exception: raise AppError('INVALID_URL','来源记录必须使用公开HTTP(S)地址', details={'path': path})
    fixture = bool(fixture)
    url = str(result.get('website_url', '')).strip()
    if url:
        url = url if '://' in url else 'https://' + url
        try: result['website_url'] = normalize_url(url, fixture=fixture)
        except Exception: raise AppError('INVALID_URL', '官网必须是无凭据的公网HTTP(S)地址', details={'path': path})
    if result.get('contact_form_url'):
        try: result['contact_form_url']=normalize_url(str(result['contact_form_url']),fixture=fixture)
        except Exception: raise AppError('INVALID_URL','联系表单必须是公开HTTP(S)地址', details={'path': path})
    if not (result.get('company_name') or result.get('website_url')):
        raise AppError('VALIDATION_ERROR', '企业名或官网至少填一项', details={'path': path})
    for field in COUNTRY_KEYS:
        if result.get(field):
            result[field] = _country(result[field], path, field)
    if result.get('operating_countries') is not None:
        values = result['operating_countries']
        if not isinstance(values, list) or len(values) > 30:
            raise AppError('VALIDATION_ERROR', 'operating_countries 必须是最多 30 项的国家列表', details={'path': path})
        result['operating_countries'] = list(dict.fromkeys(_country(item, path, 'operating_countries') for item in values))
    if result.get('entity_relationship') is not None:
        from app.capabilities import ENTITY_RELATIONSHIPS
        if result['entity_relationship'] not in ENTITY_RELATIONSHIPS:
            raise AppError('VALIDATION_ERROR', 'entity_relationship 不在支持范围内', details={'path': path})
    if result.get('business_models') is not None:
        from app.capabilities import BUSINESS_MODELS
        values = result['business_models']
        values = values if isinstance(values, list) else [values]
        canonical = []
        from app.rules.policy import MODEL_LOOKUP
        for item in values:
            name = MODEL_LOOKUP.get(str(item).strip().casefold(), str(item).strip().casefold())
            if name not in BUSINESS_MODELS:
                raise AppError('VALIDATION_ERROR', f'business_models 不支持: {item}', details={'path': path})
            canonical.append(name)
        result['business_models'] = list(dict.fromkeys(canonical))
    for field in ('offering_products', 'offering_services', 'customer_industries', 'aliases'):
        if result.get(field) is not None and (not isinstance(result[field], list) or len(result[field]) > 50):
            raise AppError('VALIDATION_ERROR', f'{field} 必须是最多 50 项的列表', details={'path': path, 'field': field})
    if result.get('extensions') is not None:
        result['extensions'] = _extensions(result['extensions'], path)
    result['country_code'] = result.get('country_code') or ''
    result['company_name_is_domain_hint']=bool(result.get('company_name_is_domain_hint') or not result.get('company_name'))
    result['company_name'] = str(result.get('company_name') or urlsplit(result['website_url']).hostname).strip()[:500]
    if result.get('source_url'):
        try: result['source_url'] = normalize_url(str(result['source_url']), fixture=fixture)
        except Exception: raise AppError('INVALID_URL','来源必须是公开HTTP(S)地址', details={'path': path})
    else: result['source_url'] = result.get('website_url')
    if not result['source_url']:
        raise AppError('MISSING_SOURCE','无官网的企业必须提供公开来源 source_url，才能追溯企业与联系方式', details={'path': path})
    result.setdefault('source_type', 'user_import')
    result.setdefault('source_id', 'unknown')
    result.setdefault('query_id', 'unknown')
    if result.get('retrieved_at'):
        try:
            when = datetime.fromisoformat(str(result['retrieved_at']).replace('Z','+00:00'))
            if not when.tzinfo or when > utcnow()+timedelta(minutes=5): raise ValueError()
        except (ValueError, TypeError): raise AppError('VALIDATION_ERROR','采集时间必须含时区且不能在未来', details={'path': path})
    result.setdefault('retrieved_at', iso(utcnow()))
    result['demo_only'] = bool(fixture or result.get('demo_only'))
    return result


def precheck_candidate(value, spec, *, fixture=False, index=None):
    """Normalize one candidate and verify its entry evidence contract."""
    path = _path(index)
    if (spec or {}).get('spec_version', 1) != 2 and isinstance(value, dict):
        unsupported = sorted(set(value) & (V2_CANDIDATE_FIELDS - {'extensions'}))
        if unsupported:
            raise AppError('UNSUPPORTED_FIELD',
                           f'该批次为 v1 半结构化输入，不接受版本化事实字段: {", ".join(unsupported)}',
                           details={'path': path, 'fields': unsupported, 'spec_version': 1})
    candidate = normalize_candidate(value, spec, fixture=fixture, index=index)
    refs = candidate.get('sources', [])
    excerpts = [candidate.get('source_excerpt'), *[ref.get('source_excerpt') for ref in refs]]
    if not value.get('retrieved_at') or not any(isinstance(text, str) and text.strip() for text in excerpts):
        raise AppError('MISSING_EVIDENCE', '候选必须包含页面 source_excerpt 和带时区 retrieved_at',
                       details={'path': path, 'fields': ['source_excerpt', 'retrieved_at']})
    if any(ref.get('source_type') in {'search_snippet', 'search_result', 'search_summary'} for ref in [candidate, *refs]):
        raise AppError('MISSING_EVIDENCE', '搜索摘要不能作为候选页面证据', details={'path': path})
    return candidate


def qualify(record, candidate, pages, spec):
    from app.rules.qualification import assess_candidate
    assessment = assess_candidate(candidate, pages, spec, observed=record)
    record['website_binding_status'] = assessment['website_identity']['status']
    record['website_binding_evidence'] = deepcopy(assessment['website_identity']['evidence'])
    if assessment['website_identity']['status'] == 'conflicting':
        record.setdefault('conflicts', []).append({'field': 'website_url',
            'reason': assessment['website_identity'].get('reason'), 'values': [candidate.get('website_url')]})
    # Cross-source facts used to qualify the company must also reach its
    # profile, rather than leaving the displayed/exported field empty.
    for field, value in assessment['profile'].items():
        if record.get(field) in (None, '') and value not in (None, ''):
            record[field] = deepcopy(value)
    record['qualification'] = {k: v for k, v in assessment.items() if k != 'profile'}
    record['qualification_score'] = assessment['confidence']
    record['status'] = ('qualified' if record.get('contacts') else 'insufficient') if assessment['decision']=='qualified' else (
        'excluded' if assessment['decision']=='excluded' else 'needs_review')
    record['match_reasons'] = assessment['reasons']
    for conflict in candidate.get('conflicts', []):
        if conflict not in record.setdefault('conflicts', []): record['conflicts'].append(deepcopy(conflict))
    source=next((p for p in pages if p.get('final_url') and not p.get('error')),candidate)
    record['sources']=[v for v in record.get('sources',[]) if not(v.get('entity_id')==record['company_id'] and v.get('field')=='status')]
    record['sources'].append({'entity_id':record['company_id'],'field':'status','value':record['status'],
        'source_type':source.get('source_type','official_site'),'source_url':source.get('final_url') or candidate.get('source_url'),
        'retrieved_at':source.get('fetched_at') or candidate.get('retrieved_at'),'method':'segment_match','confidence':assessment['confidence']})
    record['demo_only']=bool(record.get('demo_only') or candidate.get('demo_only'))
    return record


def qualify_v2(record, candidate, pages, spec, assessment=None, channel=None):
    """Apply the frozen v2 policy to one extracted record."""
    from app.rules.policy import assess_policy, policy_summary
    assessment = assessment or assess_policy(candidate, pages, spec)
    record['website_binding_status'] = assessment['website_identity']['status']
    record['website_binding_evidence'] = deepcopy(assessment['website_identity']['evidence'])
    if assessment['website_identity']['status'] == 'conflicting':
        record.setdefault('conflicts', []).append({'field': 'website_url',
            'reason': assessment['website_identity'].get('reason'), 'values': [candidate.get('website_url')]})
    for field, value in assessment['profile'].items():
        if record.get(field) in (None, '') and value not in (None, ''):
            record[field] = deepcopy(value)
    record['qualification'] = {k: v for k, v in assessment.items() if k != 'profile'}
    record['qualification_score'] = assessment['confidence']
    decision = assessment['decision']
    if decision == 'excluded':
        record['status'] = 'excluded'
    elif decision == 'uncertain':
        record['status'] = 'needs_review'
    elif channel and channel.get('satisfied'):
        from app.delivery.policy import legacy_channels
        record['status'] = 'qualified'
        record['delivery_channels'] = legacy_channels(channel.get('present') or [])
    else:
        record['status'] = 'insufficient'
    if channel:
        record['channel_policy'] = channel
        record['missing'] = [item['field'] for item in assessment['missing']]
        record['missing'] += [name for name in channel.get('missing') or [] if name not in record['missing']]
    record['match_reasons'] = assessment['reasons']
    for conflict in candidate.get('conflicts', []):
        if conflict not in record.setdefault('conflicts', []): record['conflicts'].append(deepcopy(conflict))
    source = next((p for p in pages if p.get('final_url') and not p.get('error')), candidate)
    record['sources'] = [v for v in record.get('sources', [])
                         if not (v.get('entity_id') == record['company_id'] and v.get('field') == 'status')]
    record['sources'].append({'entity_id': record['company_id'], 'field': 'status', 'value': record['status'],
        'source_type': source.get('source_type', 'official_site'),
        'source_url': source.get('final_url') or candidate.get('source_url'),
        'retrieved_at': source.get('fetched_at') or candidate.get('retrieved_at'),
        'method': 'segment_match', 'confidence': assessment['confidence']})
    record['research'] = {'spec_version': 2, 'rule_version': assessment['rule_version'],
        'policy': policy_summary(spec), 'decision': decision,
        'conditions': [{key: item.get(key) for key in ('id', 'field', 'status', 'reason')}
                       for item in assessment['conditions']],
        'exclusions': [{key: item.get(key) for key in ('id', 'field', 'status', 'unknown_policy', 'reason')}
                       for item in assessment['exclusions']],
        'missing': assessment['missing'], 'evidence_version': 'evidence-2'}
    record['demo_only'] = bool(record.get('demo_only') or candidate.get('demo_only'))
    return record


def reassess_company(task, pages, previous):
    """Re-evaluate retained evidence without fetching pages or checking DNS.

    Extraction, qualification and ranking are the same deterministic rules used
    by processing. Previously observed MX outcomes remain authoritative; newly
    extracted emails receive unknown MX status. Reassessment does not freshen
    any channel's verification time.
    """
    from app.rules.contacts import extract_company
    from app.rules.qualification import assess_candidate, qualification_record
    from app.rules.ranking import score_company
    from app.delivery.selection import filter_companies

    candidate, spec = task['input'], task['segment']
    blocked = task.get('suppressions', [])
    seed_record = qualification_record(candidate, pages)
    suppressed = not filter_companies([seed_record], mode='max_coverage', suppressions=blocked)
    version = (spec or {}).get('spec_version', 1)
    if version == 2:
        from app.rules.policy import assess_policy
        from app.delivery.selection import channel_status
        policy = (spec.get('qualification_policy') or {}).get('contact_policy')
        freshness = int((spec.get('evidence_policy') or {}).get('freshness_days', spec.get('freshness_days', 180)))
        if assess_policy(candidate, pages, spec)['decision'] == 'qualified' and not suppressed:
            mx = {}
            for contact in previous.get('contacts') or []:
                if contact.get('email') and contact.get('mx_status') in {'valid', 'invalid'}:
                    host = contact['email'].rsplit('@', 1)[-1].casefold()
                    if mx.get(host) != 'invalid':
                        mx[host] = contact['mx_status']
            record = extract_company(candidate, pages, spec,
                                     mx_lookup=lambda host: mx.get(host.casefold(), 'unknown'))
            _preserve_contact_times(record, previous, candidate, pages)
            channel = channel_status(record, policy, freshness_days=freshness)
            result = qualify_v2(record, candidate, pages, spec, channel=channel)
        else:
            result = qualify_v2(seed_record, candidate, pages, spec,
                                channel=channel_status(seed_record, policy, freshness_days=freshness))
        if suppressed:
            result['qualification'].update(decision='excluded', confidence=0, reasons=['命中抑制名单'])
            result.update(status='excluded', qualification_score=0, match_reasons=['命中抑制名单'])
        for key in ('candidate_ordinal', 'source_id', 'query_id'):
            if key in candidate:
                result[key] = candidate[key]
        _preserve_previous(result, previous)
        result['pipeline'] = {**deepcopy(previous.get('pipeline') or {}),
            'stage': 'final' if result['qualification']['decision'] == 'qualified' else 'qualification',
            'stop_reason': 'saved_evidence_reassessed'}
        return score_company(result)
    if assess_candidate(candidate, pages, spec)['decision'] == 'qualified' and not suppressed:
        mx = {}
        for contact in previous.get('contacts') or []:
            if contact.get('email') and contact.get('mx_status') in {'valid', 'invalid'}:
                host = contact['email'].rsplit('@', 1)[-1].casefold()
                if mx.get(host) != 'invalid':
                    mx[host] = contact['mx_status']
        record = extract_company(candidate, pages, spec, mx_lookup=lambda host: mx.get(host.casefold(), 'unknown'))
        _preserve_contact_times(record, previous, candidate, pages)
    else:
        record = seed_record
    result = qualify(record, candidate, pages, spec)
    if suppressed:
        result['qualification'].update(decision='excluded', confidence=0, reasons=['命中抑制名单'])
        result.update(status='excluded', qualification_score=0, match_reasons=['命中抑制名单'])
    for key in ('candidate_ordinal', 'source_id', 'query_id'):
        if key in candidate:
            result[key] = candidate[key]
    _preserve_previous(result, previous)
    result['pipeline'] = {**deepcopy(previous.get('pipeline') or {}),
        'stage': 'final' if result['qualification']['decision'] == 'qualified' else 'qualification',
        'stop_reason': 'saved_evidence_reassessed'}
    return score_company(result)


def _preserve_contact_times(record, previous, candidate, pages):
    old_contacts = {contact.get('contact_id'): contact for contact in previous.get('contacts') or []}
    observed_times = {}
    for contact in record.get('contacts') or []:
        old = old_contacts.get(contact.get('contact_id')) or {}
        contact['last_verified_at'] = old.get('last_verified_at') or contact.get('retrieved_at')
        contact['opt_out'] = bool(old.get('opt_out', False))
        observed_times[contact['contact_id']] = contact['last_verified_at']
    dates = [value for value in [candidate.get('retrieved_at'),
             *[page.get('fetched_at') for page in pages]] if value]
    record['last_verified_at'] = previous.get('last_verified_at') or max(dates,
        key=lambda value: datetime.fromisoformat(str(value).replace('Z', '+00:00')), default=None)
    observed_times[record['company_id']] = record['last_verified_at']
    for source in record.get('sources') or []:
        if source.get('field') == 'last_verified_at':
            source['value'] = observed_times.get(source.get('entity_id'))


def _preserve_previous(result, previous):
    if previous.get('related_company_ids'):
        result['related_company_ids'] = deepcopy(previous['related_company_ids'])
    for conflict in previous.get('conflicts') or []:
        if conflict.get('reason') == 'shared_domain_different_identity' and conflict not in result.setdefault('conflicts', []):
            result['conflicts'].append(deepcopy(conflict))
    if previous.get('fetch_errors'):
        result['fetch_errors'] = deepcopy(previous['fetch_errors'])


def contact_target_reached(record, pages, spec, suppressed=None):
    """Stop on a usable named channel, or both company channels after a contact-page attempt.

    Role accounts on a homepage alone do not end the search for a direct contact.
    Pattern-inferred, stale and invalid channels never satisfy this target.
    """
    from app.rules.ranking import rank_contacts
    if (spec or {}).get('spec_version') == 2:
        from app.delivery.selection import channel_status
        policy = ((spec.get('qualification_policy') or {}).get('contact_policy')) or {}
        return channel_status(record, policy, suppressions=suppressed,
                              freshness_days=(spec.get('evidence_policy') or {}).get(
                                  'freshness_days', spec.get('freshness_days', 180)))['satisfied']
    if suppressed:
        from app.delivery.selection import filter_companies
        retained = filter_companies([record],mode='max_coverage',suppressions=suppressed,
            freshness_days=spec.get('freshness_days',180))
        if not retained: return False
        record = retained[0]
    contacts = rank_contacts(record.get('contacts', []))
    usable = [c for c in contacts if c.get('contact_priority') in {'direct_business','company_channel'}
        and c.get('priority_score',0)>=60 and not c.get('inferred') and 'stale' not in c.get('tags', [])
        and ((c.get('email') and c.get('syntax_status')=='valid' and c.get('mx_status')!='invalid'
            and c.get('email_status') not in {'invalid','risky'}) or (c.get('phone') and c.get('phone_status')=='valid'))]
    if spec.get('roles'):
        direct = [c for c in usable if c.get('contact_priority')=='direct_business' and any(
            str(role).casefold() in str(c.get('title') or '').casefold() for role in spec['roles'])]
    else:
        direct = [c for c in usable if c.get('contact_priority')=='direct_business']
    if direct: return True
    contact_page_seen = any(not p.get('error') and re.search(r'contact|team|staff|people|leadership|about|联系|团队',
        urlsplit(p.get('final_url','')).path,re.I) for p in pages)
    return bool(contact_page_seen and not spec.get('roles')
        and any(c.get('email') and c.get('email_status')!='invalid' for c in usable)
        and any(c.get('phone') and c.get('phone_status')=='valid' for c in usable))


def process_company(task, pages, *, deadline=None):
    started=time.monotonic()
    task['_contact_stage_started']=None
    from app.web.http import request_deadline
    from app.web.parsing import parsing_scope
    try:
        with parsing_scope() as parse_stats, request_deadline(
                None if deadline is None else max(0.0, deadline - time.monotonic())):
            result=_process_company(task,pages)
    finally:
        ended=time.monotonic()
        contact_started=task.pop('_contact_stage_started',None)
        task['_stage_seconds']={
            'qualification':round((contact_started or ended)-started,6),
            'contact':round(ended-contact_started,6) if contact_started else 0}
    result.setdefault('pipeline',{})['stage_processing_seconds']=task['_stage_seconds']
    result['pipeline'].update({key: round(parse_stats[key], 6) if key.endswith('seconds') else parse_stats[key]
                               for key in ('html_parse_count', 'html_cache_hits', 'html_parse_seconds')})
    return result


def _excluded_result(candidate, spec, stop_reason, pages=0):
    from app.rules.qualification import qualification_record
    from app.rules.ranking import score_company
    result=qualify(qualification_record(candidate,[]),candidate,[],spec)
    result['qualification'].update(decision='excluded',confidence=0,reasons=['命中抑制名单'])
    result.update(status='excluded',qualification_score=0,match_reasons=['命中抑制名单'])
    result['pipeline']={'stage':'qualification','stop_reason':stop_reason,
        'pages_fetched':pages,'qualification_pages':pages,'deep_crawl_skipped':True}
    return score_company(result)


def _process_company(task, pages):
    """Qualify cheaply, reuse shallow pages, then enrich and rank only eligible leads."""
    if (task.get('segment') or {}).get('spec_version') == 2:
        return _process_company_v2(task, pages)
    from app.rules.qualification import assess_candidate, qualification_record
    from app.web.crawler import fetch_company
    from app.rules.contacts import extract_company
    from app.web.dns import lookup_mx
    from app.rules.ranking import score_company
    candidate, spec = task['input'], task['segment']
    plan = task.get('plan') or {}
    remaining = max(0, plan.get('page_limit_total', 10000) - task.get('pages_used', 0))
    limit = min(10, max(0, plan.get('page_limit_per_company', 10)), remaining)
    shallow = 0
    assessment = assess_candidate(candidate, [], spec)
    blocked = list(task.get('suppressions', []))
    for value in spec.get('exclusions', []):
        kind = 'email' if '@' in value else 'phone' if value.startswith('+') else 'domain' if '.' in value and ' ' not in value else 'company'
        blocked.append({'type':kind,'value':value})
    if blocked:
        from app.delivery.selection import filter_companies
        if not filter_companies([qualification_record(candidate,[])],mode='max_coverage',suppressions=blocked):
            result=qualify(qualification_record(candidate,[]),candidate,[],spec)
            result['qualification'].update(decision='excluded',confidence=0,reasons=['命中抑制名单'])
            result.update(status='excluded',qualification_score=0,match_reasons=['命中抑制名单'])
            result['pipeline']={'stage':'qualification','stop_reason':'prequalified_excluded',
                'pages_fetched':0,'qualification_pages':0,'deep_crawl_skipped':True}
            return score_company(result)
    stop_reason = 'prequalified_excluded' if assessment['decision']=='excluded' else 'no_website'
    mx_cache = {}
    def cached_mx(host):
        if task.get('demo_only', False): return 'unknown'
        if host not in mx_cache: mx_cache[host] = lookup_mx(host)
        return mx_cache[host]
    extract_options = {'mx_lookup': cached_mx}
    # Do not do DNS/MX/contact work for clearly rejected candidates.
    if assessment['decision'] != 'excluded' and candidate.get('website_url'):
        if not limit: raise AppError('PAGE_BUDGET_EXCEEDED','已达到批次页面上限')
        def qualification_done(current):
            return assess_candidate(candidate,current,spec)['decision'] != 'uncertain'
        pages[:] = fetch_company(candidate,spec,fixture=task.get('demo_only', False),max_pages=min(2,limit),
            stage='qualification',should_stop=qualification_done)
        shallow = len(pages)
        assessment = assess_candidate(candidate,pages,spec)
        stop_reason = 'qualification_uncertain' if assessment['decision']=='uncertain' else 'prequalified_excluded'
        if assessment['decision']=='qualified':
            task['_contact_stage_started']=time.monotonic()
            # Cache each extraction for a page set: the callback may run again on reused pages.
            cached = {}
            def enriched(current):
                key = tuple((p.get('final_url'),p.get('fetch_status'),p.get('fetched_at')) for p in current)
                if key not in cached:
                    cached[key] = extract_company(candidate,current,spec,**extract_options)
                return cached[key]
            def enough(current):
                if assess_candidate(candidate,current,spec)['decision'] != 'qualified': return True
                decision = assess_candidate(candidate,current,spec,observed=enriched(current))['decision']
                return decision != 'qualified' or contact_target_reached(enriched(current),current,spec,blocked)
            pages[:] = fetch_company(candidate,spec,fixture=task.get('demo_only', False),max_pages=limit,
                stage='contacts',initial_pages=pages,should_stop=enough)
            assessment = assess_candidate(candidate,pages,spec)
            if assessment['decision']=='qualified':
                result = qualify(enriched(pages),candidate,pages,spec)
                stop_reason = 'qualification_uncertain' if result['qualification']['decision']=='uncertain' else (
                    'prequalified_excluded' if result['qualification']['decision']=='excluded' else (
                    'contact_target_reached' if contact_target_reached(result,pages,spec,blocked) else (
                    'page_budget_reached' if len(pages)>=limit else 'no_more_pages')))
            else:
                result = qualify(qualification_record(candidate,pages),candidate,pages,spec)
                stop_reason = 'prequalified_excluded' if assessment['decision']=='excluded' else 'qualification_uncertain'
        else:
            result = qualify(qualification_record(candidate,pages),candidate,pages,spec)
    elif assessment['decision']=='qualified':
        task['_contact_stage_started']=time.monotonic()
        result = qualify(extract_company(candidate,[],spec,**extract_options),candidate,[],spec)
    else:
        result = qualify(qualification_record(candidate,[]),candidate,[],spec)
        if assessment['decision']=='uncertain': stop_reason='qualification_uncertain'
    failures=[p['error'] for p in pages if p.get('error')]
    if pages and len(failures)==len(pages):
        from app.delivery.selection import delivery_companies
        usable = (result.get('qualification', {}).get('decision') == 'qualified'
            and delivery_companies([result], suppressions=blocked, freshness_days=spec.get('freshness_days', 180)))
        if not usable:
            # Raw assertions must not turn a transport failure into completed
            # research. An independently sourced eligible directory channel can
            # still finish when the optional official-site enrichment fails.
            failure=next((failure for failure in failures if failure.get('retryable')), failures[0])
            raise AppError(failure.get('code','SOURCE_FAILED'),failure.get('message','官网采集失败'),
                retryable=bool(failure.get('retryable',False)), retry_after_s=failure.get('retry_after_s'),
                upstream_status=failure.get('upstream_status'))
    if failures: result['fetch_errors']=failures
    result['pipeline'] = {'stage':'final' if result['qualification']['decision']=='qualified' else 'qualification',
        'stop_reason':stop_reason,'pages_fetched':len(pages),'qualification_pages':shallow,
        'deep_crawl_skipped':len(pages)<=shallow}
    return score_company(result)


def _process_company_v2(task, pages):
    """Evidence first: reuse verifiable facts, then fetch only the missing items."""
    from app.delivery.selection import channel_status
    from app.rules.contacts import extract_company
    from app.rules.policy import assess_policy, assess_policy_cached
    from app.rules.qualification import qualification_record
    from app.rules.ranking import score_company
    from app.web.crawler import fetch_company
    from app.web.dns import lookup_mx

    candidate, spec = task['input'], task['segment']
    policy = spec.get('qualification_policy') or {}
    contact_policy = policy.get('contact_policy') or {}
    freshness = int((spec.get('evidence_policy') or {}).get('freshness_days', spec.get('freshness_days', 180)))
    plan = task.get('plan') or {}
    remaining = max(0, plan.get('page_limit_total', 10000) - task.get('pages_used', 0))
    limit = min(10, max(0, plan.get('page_limit_per_company', 10)), remaining)
    blocked = list(task.get('suppressions', []))
    for value in spec.get('exclusions', []):
        kind = 'email' if '@' in value else 'phone' if value.startswith('+') else 'domain' if '.' in value and ' ' not in value else 'company'
        blocked.append({'type': kind, 'value': value})
    if blocked:
        from app.delivery.selection import filter_companies
        if not filter_companies([qualification_record(candidate, [])], mode='max_coverage', suppressions=blocked):
            return _excluded_result(candidate, spec, 'prequalified_excluded')

    mx_cache = {}
    def cached_mx(host):
        if task.get('demo_only', False):
            return 'unknown'
        if host not in mx_cache:
            mx_cache[host] = lookup_mx(host)
        return mx_cache[host]

    assessment = assess_policy_cached(candidate, [], spec)
    extracted = {}
    extraction_count = 0

    def enriched(current, *, offline=False):
        nonlocal extraction_count
        # One company's retained pages are immutable after arrival. Include all
        # page fields so text-only changes and lineage cannot reuse an old result.
        key = (offline, tuple(json.dumps(page, sort_keys=True, default=str)
                              for page in current))
        if key not in extracted:
            extracted.clear()  # Only the latest page set is needed by the callback/final save.
            extracted[key] = extract_company(candidate, current, spec,
                mx_lookup=(lambda _host: 'unknown') if offline else cached_mx)
            extraction_count += 1
        return extracted[key]
    shallow = 0
    stop_reason = 'saved_evidence_insufficient'
    if assessment['decision'] == 'excluded':
        record = qualify_v2(qualification_record(candidate, []), candidate, [], spec, assessment=assessment)
        stop_reason = 'prequalified_excluded'
    elif assessment['decision'] == 'qualified':
        # Zero-network attempt: the retained evidence may already be sufficient.
        record = enriched([], offline=True)
        channel = channel_status(record, contact_policy, freshness_days=freshness)
        if channel['satisfied']:
            record = qualify_v2(record, candidate, [], spec, assessment=assessment, channel=channel)
            stop_reason = 'saved_evidence_sufficient'
        elif candidate.get('website_url') and limit:
            task['_contact_stage_started'] = time.monotonic()
            def enough(current):
                if assess_policy_cached(candidate, current, spec)['decision'] != 'qualified':
                    return True
                return channel_status(enriched(current), contact_policy, freshness_days=freshness)['satisfied']
            pages[:] = fetch_company(candidate, spec, fixture=task.get('demo_only', False),
                max_pages=limit, stage='contacts', should_stop=enough)
            assessment = assess_policy_cached(candidate, pages, spec)
            record = enriched(pages)
            channel = channel_status(record, contact_policy, freshness_days=freshness)
            record = qualify_v2(record, candidate, pages, spec, assessment=assessment, channel=channel)
            stop_reason = ('contact_policy_satisfied' if channel['satisfied'] else
                           'page_budget_reached' if len(pages) >= limit else 'channel_missing')
        else:
            record = qualify_v2(record, candidate, [], spec, assessment=assessment, channel=channel)
            stop_reason = 'channel_missing'
    else:
        record = qualify_v2(qualification_record(candidate, []), candidate, [], spec, assessment=assessment)
        page_fields = {'entity.country_code', 'entity.headquarters_country', 'entity.registered_country',
                       'industry.match', 'business.type', 'entity.region'}
        fetchable = [item for item in assessment['missing'] if item['field'] in page_fields]
        if fetchable and candidate.get('website_url') and limit:
            def decided(current):
                return assess_policy_cached(candidate, current, spec)['decision'] != 'uncertain'
            pages[:] = fetch_company(candidate, spec, fixture=task.get('demo_only', False),
                max_pages=min(2, limit), stage='qualification', should_stop=decided)
            shallow = len(pages)
            assessment = assess_policy_cached(candidate, pages, spec)
            if assessment['decision'] == 'qualified':
                task['_contact_stage_started'] = time.monotonic()
                def enough(current):
                    if assess_policy_cached(candidate, current, spec)['decision'] != 'qualified':
                        return True
                    return channel_status(enriched(current), contact_policy, freshness_days=freshness)['satisfied']
                pages[:] = fetch_company(candidate, spec, fixture=task.get('demo_only', False),
                    max_pages=limit, stage='contacts', initial_pages=pages, should_stop=enough)
                assessment = assess_policy_cached(candidate, pages, spec)
                record = enriched(pages)
                channel = channel_status(record, contact_policy, freshness_days=freshness)
                record = qualify_v2(record, candidate, pages, spec, assessment=assessment, channel=channel)
                stop_reason = ('contact_policy_satisfied' if channel['satisfied'] else
                               'page_budget_reached' if len(pages) >= limit else 'channel_missing')
            else:
                record = qualify_v2(qualification_record(candidate, pages), candidate, pages, spec,
                                    assessment=assessment)
                stop_reason = ('prequalified_excluded' if assessment['decision'] == 'excluded'
                               else 'qualification_uncertain')
        else:
            stop_reason = 'fact_missing'

    failures = [page['error'] for page in pages if page.get('error')]
    if pages and len(failures) == len(pages):
        from app.delivery.selection import delivery_companies
        usable = record.get('status') == 'qualified' and delivery_companies(
            [record], suppressions=blocked, freshness_days=freshness, policy=contact_policy)
        if not usable:
            failure = next((item for item in failures if item.get('retryable')), failures[0])
            raise AppError(failure.get('code', 'SOURCE_FAILED'), failure.get('message', '官网采集失败'),
                retryable=bool(failure.get('retryable', False)), retry_after_s=failure.get('retry_after_s'),
                upstream_status=failure.get('upstream_status'))
    if failures:
        record['fetch_errors'] = failures
    record['pipeline'] = {'stage': 'final' if record.get('status') == 'qualified' else 'qualification',
        'stop_reason': stop_reason, 'pages_fetched': len(pages), 'qualification_pages': shallow,
        'deep_crawl_skipped': len(pages) <= shallow,
        'evidence_first': stop_reason in {'saved_evidence_sufficient', 'prequalified_excluded', 'fact_missing'},
        'network_pages': len(pages), 'contact_extractions': extraction_count}
    return score_company(record)
