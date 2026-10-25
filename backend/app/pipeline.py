"""Company processing without database access or batch lifecycle decisions.

``process_company(task, pages)`` returns a detached company result and fills
``pages`` with the bounded page attempts for the caller to persist. Network and
MX behavior live in ``web``; deterministic business decisions live in ``rules``.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
import time
from urllib.parse import urlsplit
from app.errors import AppError


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def normalize_candidate(value, spec, *, fixture=False):
    from app.web.security import normalize_url
    if not isinstance(value,dict): raise AppError('VALIDATION_ERROR','候选企业必须是对象')
    allowed = {'company_name','website_url','country_code','region','city','address',
        'source_url','source_type','retrieved_at','email','phone','company_type','industry_text',
        'employee_count','revenue','branch_id','linkedin_url','external_id','demo_only','sources','conflicts','company_name_is_domain_hint',
        'source_excerpt','source_title','rating','review_count','source_id','query_id','candidate_ordinal','contact_form_url'}
    result = {k: deepcopy(v) for k, v in value.items() if k in allowed and v is not None}
    if any(len(str(v))>10000 for v in result.values()): raise AppError('VALIDATION_ERROR', '导入字段过长')
    for field in ['email','phone']:
        supplied=result.get(field)
        if supplied is not None and not (isinstance(supplied,str) or isinstance(supplied,list) and len(supplied)<=100 and all(isinstance(v,str) for v in supplied)):
            raise AppError('VALIDATION_ERROR','邮箱/电话应为文本或文本列表')
    if not isinstance(result.get('sources',[]),list) or len(result.get('sources',[]))>100:
        raise AppError('VALIDATION_ERROR','来源记录必须是列表且最多100项')
    for source in result.get('sources',[]):
        if not isinstance(source,dict): raise AppError('VALIDATION_ERROR','来源记录格式无效')
        if source.get('source_url'):
            try: normalize_url(source['source_url'],fixture=bool(fixture))
            except Exception: raise AppError('INVALID_URL','来源记录必须使用公开HTTP(S)地址')
    fixture = bool(fixture)
    url = str(result.get('website_url', '')).strip()
    if url:
        url = url if '://' in url else 'https://' + url
        try: result['website_url'] = normalize_url(url, fixture=fixture)
        except Exception: raise AppError('INVALID_URL', '官网必须是无凭据的公网HTTP(S)地址')
    if result.get('contact_form_url'):
        try: result['contact_form_url']=normalize_url(str(result['contact_form_url']),fixture=fixture)
        except Exception: raise AppError('INVALID_URL','联系表单必须是公开HTTP(S)地址')
    if not (result.get('company_name') or result.get('website_url')):
        raise AppError('VALIDATION_ERROR', '企业名或官网至少填一项')
    country = str(result.get('country_code') or '').strip().upper()
    if country:
        import pycountry
        if not pycountry.countries.get(alpha_2=country): raise AppError('INVALID_COUNTRY','无效国家代码')
    result['country_code'] = country
    result['company_name_is_domain_hint']=bool(result.get('company_name_is_domain_hint') or not result.get('company_name'))
    result['company_name'] = str(result.get('company_name') or urlsplit(result['website_url']).hostname).strip()[:500]
    if result.get('source_url'):
        try: result['source_url'] = normalize_url(str(result['source_url']), fixture=fixture)
        except Exception: raise AppError('INVALID_URL','来源必须是公开HTTP(S)地址')
    else: result['source_url'] = result.get('website_url')
    if not result['source_url']:
        raise AppError('MISSING_SOURCE','无官网的企业必须提供公开来源 source_url，才能追溯企业与联系方式')
    result.setdefault('source_type', 'user_import')
    result.setdefault('source_id', 'unknown')
    result.setdefault('query_id', 'unknown')
    if result.get('retrieved_at'):
        try:
            when = datetime.fromisoformat(str(result['retrieved_at']).replace('Z','+00:00'))
            if not when.tzinfo or when > utcnow()+timedelta(minutes=5): raise ValueError()
        except (ValueError, TypeError): raise AppError('VALIDATION_ERROR','采集时间必须含时区且不能在未来')
    result.setdefault('retrieved_at', iso(utcnow()))
    result['demo_only'] = bool(fixture or result.get('demo_only'))
    return result

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


def contact_target_reached(record, pages, spec, suppressed=None):
    """Stop on a usable named channel, or both company channels after a contact-page attempt.

    Role accounts on a homepage alone do not end the search for a direct contact.
    Pattern-inferred, stale and invalid channels never satisfy this target.
    """
    from app.rules.ranking import rank_contacts
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


def process_company(task, pages):
    started=time.monotonic()
    task['_contact_stage_started']=None
    try:
        result=_process_company(task,pages)
    finally:
        ended=time.monotonic()
        contact_started=task.pop('_contact_stage_started',None)
        task['_stage_seconds']={
            'qualification':round((contact_started or ended)-started,6),
            'contact':round(ended-contact_started,6) if contact_started else 0}
    result.setdefault('pipeline',{})['stage_processing_seconds']=task['_stage_seconds']
    return result


def _process_company(task, pages):
    """Qualify cheaply, reuse shallow pages, then enrich and rank only eligible leads."""
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
