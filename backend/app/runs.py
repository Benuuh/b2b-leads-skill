"""Durable batch application service. No network request holds a database lock."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from app.config import settings
from app.errors import AppError
from app.release import release_identity
from app.persistence.host_limits import limit as host_limit
from app.persistence import (transaction, utcnow, uid, put, artifact_path, observation_sink,
                             queue_artifact_delete, drain_pending_deletes)
from app.persistence.models import Run, Job, Company, Snapshot, Suppression, Export, Operation, Observation, Audit, MaintenanceState


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                                    separators=(',', ':')).encode()).hexdigest()


def _iso(value):
    return value.isoformat() if value else None


def _run(session, run_id):
    row = session.get(Run, run_id, with_for_update=True)
    if row is None:
        raise AppError('NOT_FOUND', '批次不存在', 404)
    return row


def _version(run, expected):
    if expected is not None and run.version != expected:
        raise AppError('VERSION_CONFLICT', '批次已更新，请读取最新状态', 409)


def _audit(session, run_id, action, **details):
    session.add(Audit(id=uid('audit'), run_id=run_id, action=action, details_json=details))


def _delivery_lock(session):
    # Always acquire before a Run lock: selection, suppression and file
    # revocation must be one serial order, including an export being encoded.
    from sqlalchemy import text
    session.execute(text('SELECT pg_advisory_xact_lock(7334003)'))


def _operation(session, run, command, key, request):
    if not isinstance(key, str) or not 1 <= len(key) <= 160:
        raise AppError('INVALID_KEY', '操作键必须为 1 至 160 字符')
    old = session.scalar(select(Operation).where(Operation.run_id == run.id,
        Operation.command == command, Operation.key == key))
    digest = _hash(request)
    if old and old.request_hash != digest:
        raise AppError('IDEMPOTENCY_CONFLICT', '同一操作键不能用于不同输入', 409)
    if old:
        return old, True
    row = Operation(id=uid('op'), run_id=run.id, command=command, key=key,
                    request_hash=digest, result_json={})
    session.add(row)
    session.flush()
    return row, False


def _rows(session, run):
    return [deepcopy(row.data_json) for row in session.scalars(
        select(Company).where(Company.run_id == run.id).order_by(Company.created_at, Company.id))]


def _suppression_view(row):
    return dict(id=row.id, type=row.type, value=row.value, reason=row.reason, created_at=_iso(row.created_at))


def _suppressions(session, spec=None):
    result = [_suppression_view(row) for row in session.scalars(select(Suppression))]
    for value in (spec or {}).get('exclusions', []):
        kind = 'email' if '@' in value else 'phone' if value.startswith('+') else 'domain' if '.' in value and ' ' not in value else 'company'
        result.append(dict(type=kind, value=value, reason='客群排除名单'))
    return result


def _elapsed(run, now=None):
    return run.active_seconds + (max(0, ((now or utcnow()) - run.active_started_at).total_seconds())
        if run.status == 'running' and run.active_started_at else 0)


def _settle(run, now=None):
    run.active_seconds = _elapsed(run, now)
    run.active_started_at = None


def _summary(session, run, now=None):
    from app.delivery.selection import delivery_companies, filter_companies
    now = now or utcnow()
    rows = _rows(session, run)
    kwargs = dict(suppressions=_suppressions(session, run.spec_json),
                  freshness_days=run.spec_json.get('freshness_days', 180), now=now)
    qualified_rows = [row for row in rows if row.get('qualification', {}).get('decision') == 'qualified']
    qualified = filter_companies(qualified_rows, mode='max_coverage', **kwargs)
    contactable = delivery_companies(rows, **kwargs)
    states = Counter(session.scalars(select(Job.state).where(Job.run_id == run.id)))
    next_available = session.scalar(select(Job.available_at).where(Job.run_id == run.id,
        Job.state == 'queued').order_by(Job.available_at).limit(1))
    decisions = Counter(row.get('qualification', {}).get('decision', 'uncertain') for row in rows)
    state = deepcopy(run.plan_json['research'])
    state.update(active_wall_seconds=round(_elapsed(run, now), 3), qualified_count=len(qualified),
        contactable_count=len(contactable), goal_count=len(contactable),
        target_quantity=run.spec_json['quantity'], shortfall=max(0, run.spec_json['quantity'] - len(contactable)),
        discovered=state['admitted_count'], matched=len(qualified), excluded=decisions['excluded'],
        uncertain=decisions['uncertain'], missing=max(0, len(qualified) - len(contactable)),
        processing=bool(run.process_token and run.process_until and run.process_until > now),
        queued=states['queued'], running=states['running'], failed=states['failed'],
        processed=states['completed'], skipped=states['skipped'],
        next_retry_at=_iso(next_available) if next_available and next_available > now else None)
    return state


def _refresh(session, run, now=None):
    now = now or utcnow()
    session.flush()
    research = _summary(session, run, now)
    plan = deepcopy(run.plan_json)
    state = plan['research']
    stop = ('goal_reached' if research['goal_count'] >= run.spec_json['quantity'] else
            'time_limit' if research['active_wall_seconds'] >= state['time_limit_seconds'] else
            'candidate_limit' if state['admitted_count'] >= state['candidate_limit'] else None)
    if stop and run.status in {'running', 'paused'}:
        state['input_open'] = False
        state['stop_reason'] = stop
        if stop in {'goal_reached', 'time_limit'}:
            for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'queued')):
                job.state = 'skipped'
    session.flush()
    pending = session.scalar(select(Job.id).where(Job.run_id == run.id, Job.state.in_(['queued', 'running'])).limit(1))
    if run.status == 'running' and not state['input_open'] and not pending:
        _settle(run, now)
        run.status = 'completed' if research['goal_count'] >= run.spec_json['quantity'] else 'partial'
        run.finished_at = now
        run.version += 1
    run.plan_json = plan


def _view(session, run):
    research = _summary(session, run)
    stats = {**deepcopy(run.stats_json), **{key: research[key] for key in
        ('discovered', 'processed', 'failed', 'qualified_count', 'contactable_count', 'shortfall')}}
    summary = {key: research[key] for key in ('discovered', 'matched', 'excluded', 'uncertain', 'missing')}
    summary.update(contactable=research['contactable_count'], target_quantity=research['target_quantity'],
                   target_shortfall=research['shortfall'])
    return dict(id=run.id, run_id=run.id, batch_id=run.id, name=run.spec_json['name'],
        status=run.status, version=run.version, segment=deepcopy(run.spec_json), spec=deepcopy(run.spec_json),
        plan=deepcopy(run.plan_json), research=research, checkpoint=research['checkpoint'],
        summary=summary, stats=stats, demo_only=run.demo_only, errors=deepcopy(run.errors_json),
        input_closed=not research['input_open'],
        requires_more_processing=bool(research['queued'] or research['running']),
        external_tool_usage=deepcopy(research['external_tool_usage']),
        created_at=_iso(run.created_at), updated_at=_iso(run.updated_at), finished_at=_iso(run.finished_at))


def create(run_id, spec, **kwargs):
    from app.schemas import SegmentSpec
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', run_id):
        raise AppError('INVALID_RUN_ID', 'run 仅支持 1 至 80 位字母、数字、点、横线和下划线')
    spec = SegmentSpec.model_validate(spec).model_dump()
    with transaction() as session:
        # Lock even before the row exists to serialize concurrent create calls.
        from sqlalchemy import text
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'run:' + run_id})
        old = session.get(Run, run_id, with_for_update=True)
        if old:
            if old.spec_hash != _hash(spec):
                raise AppError('IDEMPOTENCY_CONFLICT', '该 run 已用于不同客群', 409)
            return _view(session, old)
        run = Run(id=run_id, spec_json=spec, spec_hash=_hash(spec), demo_only=settings.fixture_mode,
            active_started_at=utcnow(), plan_json={'execution_mode': 'skill_research', 'completion_goal': 'contactable',
            'page_limit_per_company': 10, 'page_limit_total': 10000,
            'request_observation_protocol': 'observed-2', 'lookup_observation_protocol': 'observed-2',
            'timing_protocol': 'observed-1', 'release': release_identity(),
            'research': {'input_open': True, 'candidate_limit': spec['quantity'] * 3,
            'time_limit_seconds': 28800, 'admitted_count': 0, 'checkpoint': {}, 'external_tool_usage': {}, 'stop_reason': None}},
            stats_json={'pages': 0, 'duplicates': 0, 'active_seconds': 0, 'paused_seconds': 0})
        session.add(run)
        session.flush()
        _audit(session, run.id, 'run.created')
        return _view(session, run)


def _identity(candidate):
    host = (urlsplit(candidate.get('website_url') or '').hostname or '').lower().removeprefix('www.')
    name = re.sub(r'[^\w]', '', candidate.get('company_name', '').casefold())
    return _hash([host, name, candidate.get('country_code', ''), candidate.get('branch_id', ''),
                  '' if host else re.sub(r'\W', '', candidate.get('address', '').casefold())])


def append(run_id, candidates, chunk_id=None, checkpoint=None, external_tool_usage=None,
           finalize=False, expected_version=None):
    from app.pipeline import normalize_candidate
    from app.schemas import ResearchInput
    request = ResearchInput.model_validate(dict(candidates=candidates, checkpoint=checkpoint,
        external_tool_usage=external_tool_usage, finalize=finalize)).model_dump()
    chunk_id = chunk_id or _hash(request)
    with transaction() as session:
        run = _run(session, run_id)
        operation, replay = _operation(session, run, 'append', chunk_id, request)
        if replay:
            return deepcopy(operation.result_json)
        _version(run, expected_version)
        if run.status not in {'running', 'paused'}:
            raise AppError('INVALID_STATE', '批次已结束，不能追加候选', 409)
        _refresh(session, run)
        state = deepcopy(run.plan_json['research'])
        admission = dict(accepted=0, duplicates=0, skipped=0, admitted_count=state['admitted_count'])
        normalized = []
        for value in candidates:
            candidate = normalize_candidate(value, run.spec_json, fixture=run.demo_only)
            refs = candidate.get('sources', [])
            excerpts = [candidate.get('source_excerpt'), *[ref.get('source_excerpt') for ref in refs]]
            if not value.get('retrieved_at') or not any(isinstance(text, str) and text.strip() for text in excerpts):
                raise AppError('MISSING_EVIDENCE', '候选必须包含页面 source_excerpt 和带时区 retrieved_at')
            if any(ref.get('source_type') in {'search_snippet', 'search_result', 'search_summary'} for ref in [candidate, *refs]):
                raise AppError('MISSING_EVIDENCE', '搜索摘要不能作为候选页面证据')
            normalized.append(candidate)
        jobs = list(session.scalars(select(Job).where(Job.run_id == run.id).order_by(Job.created_at, Job.id)))
        by_key = {job.entity_key: job for job in jobs}
        for candidate in normalized:
            key = _identity(candidate)
            old = by_key.get(key)
            host = urlsplit(candidate.get('website_url', '')).hostname
            if not old and host and not candidate.get('branch_id'):
                matches = [job for job in jobs if not job.input_json.get('branch_id')
                    and urlsplit(job.input_json.get('website_url', '')).hostname == host
                    and (candidate.get('company_name_is_domain_hint') or job.input_json.get('company_name_is_domain_hint'))]
                if len(matches) == 1:
                    old = matches[0]
            if old:
                admission['duplicates'] += 1
                if old.state != 'queued' or old.attempt:
                    admission['skipped'] += 1
                    continue
                prior = deepcopy(old.input_json)
                refs = prior.setdefault('sources', [])
                source = {key: deepcopy(value) for key, value in candidate.items()
                          if key not in {'sources', 'conflicts', 'company_id'}}
                for ref in [source, *candidate.get('sources', [])]:
                    if ref not in refs:
                        refs.append(deepcopy(ref))
                for field in ('email', 'phone'):
                    combined = []
                    for value in (prior.get(field), candidate.get(field)):
                        for item in value if isinstance(value, list) else [value]:
                            if item and item not in combined:
                                combined.append(item)
                    if combined:
                        prior[field] = combined
                for field, value in candidate.items():
                    if field not in {'company_id', 'email', 'phone', 'sources'} and not prior.get(field):
                        prior[field] = value
                    elif field in {'address', 'region', 'city', 'country_code', 'industry_text',
                                   'company_type', 'employee_count', 'revenue'} and value and prior.get(field) != value:
                        conflict = {'field': field, 'values': [prior[field], value], 'source_url': candidate['source_url']}
                        if conflict not in prior.setdefault('conflicts', []):
                            prior['conflicts'].append(conflict)
                if prior.get('company_name_is_domain_hint') and not candidate.get('company_name_is_domain_hint'):
                    prior['company_name'] = candidate['company_name']
                    prior['company_name_is_domain_hint'] = False
                    by_key.pop(old.entity_key, None)
                    old.entity_key = _identity(prior)
                    by_key[old.entity_key] = old
                old.input_json = prior
                continue
            if not state['input_open'] or admission['admitted_count'] >= state['candidate_limit']:
                admission['skipped'] += 1
                continue
            candidate['company_id'] = uid('company')
            candidate['candidate_ordinal'] = admission['admitted_count'] + 1
            job = Job(id=uid('job'), run_id=run.id, entity_key=key, input_json=candidate)
            session.add(job)
            jobs.append(job)
            by_key[key] = job
            admission['accepted'] += 1
            admission['admitted_count'] += 1
        state['admitted_count'] = admission['admitted_count']
        if checkpoint is not None:
            state['checkpoint'] = deepcopy(checkpoint)
        if external_tool_usage is not None:
            usage = state['external_tool_usage']
            for key, count in external_tool_usage.items():
                usage[key] = None if count is None or key in usage and usage[key] is None else usage.get(key, 0) + count
        if finalize:
            state['input_open'] = False
            state['stop_reason'] = state['stop_reason'] or 'input_finalized'
        run.plan_json = {**run.plan_json, 'research': state}
        run.stats_json = {**run.stats_json, 'duplicates': run.stats_json.get('duplicates', 0) + admission['duplicates']}
        run.version += 1
        _refresh(session, run)
        result = {**_view(session, run), 'admission': admission}
        operation.result_json = result
        _audit(session, run.id, 'run.append', **admission)
        return result


def _action(session, run, action):
    allowed = {'pause': {'running'}, 'resume': {'paused'}, 'cancel': {'running', 'paused'},
               'retry': {'running', 'partial', 'failed', 'completed'}}
    if action not in allowed or run.status not in allowed[action]:
        raise AppError('INVALID_STATE', '当前状态不能执行此操作', 409)
    now = utcnow()
    stats = deepcopy(run.stats_json)
    if stats.get('paused_at') and action in {'resume', 'cancel'}:
        from datetime import datetime
        stats['paused_seconds'] = stats.get('paused_seconds', 0) + max(0,
            (now - datetime.fromisoformat(stats.pop('paused_at'))).total_seconds())
    if action == 'pause':
        stats['paused_at'] = _iso(now)
    run.stats_json = stats
    if action in {'pause', 'cancel'}:
        _settle(run, now)
        run.status = 'paused' if action == 'pause' else 'cancelled'
        run.process_token = run.process_until = None
        for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'running')):
            # The old process may still be inside an in-flight request. Reserve
            # its full remaining allowance before resume can claim another job.
            _reclaim_expired(session, run, job, now, force_unknown=True)
            if action == 'cancel':
                job.state = 'skipped'
        if action == 'cancel':
            run.plan_json = {**run.plan_json, 'research': {**run.plan_json['research'],
                'input_open': False, 'stop_reason': 'cancelled'}}
            for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'queued')):
                job.state = 'skipped'
            run.finished_at = now
    elif action == 'retry':
        failed = list(session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'failed')))
        if not failed:
            raise AppError('NOTHING_TO_RETRY', '没有失败任务', 409)
        for job in failed:
            job.state, job.attempt, job.available_at, job.error_json = 'queued', 0, now, None
        run.status, run.active_started_at, run.finished_at = 'running', now, None
    else:
        run.status, run.active_started_at = 'running', now
    run.version += 1
    _audit(session, run.id, 'run.' + action)


def status(run_id, action=None, expected_version=None):
    with transaction() as session:
        run = _run(session, run_id)
        if action:
            _version(run, expected_version)
            _action(session, run, action)
        _refresh(session, run)
        return _view(session, run)


def _reclaim_expired(session, run, job, now, force_unknown=False):
    """Account an interrupted attempt from durable physical-send observations.

    Each start reserves at least one page conservatively, including starts
    without an outcome and redirects. Missing/pruned attribution consumes the
    entire remaining enterprise allowance rather than reopening ten pages.
    A completed attempt is never reclaimed, so settled page counts are not
    charged again.
    """
    previous = deepcopy(job.output_json or {})
    used = min(10, previous.get('pages_used', 0))
    remaining = max(0, min(10, run.plan_json.get('page_limit_per_company', 10)) - used)
    observations = list(session.scalars(select(Observation).where(
        Observation.job_id == job.id, Observation.kind == 'http')))
    attempt_rows = [row for row in observations if row.data_json.get('attempt') == job.attempt]
    permitted = {'qualification', 'contact', 'website_confirmation'}
    page_rows = [row for row in attempt_rows if row.data_json.get('purpose') in permitted]
    unknown = force_unknown or bool(run.stats_json.get('observation_pruned')) or not page_rows or any(
        row.data_json.get('purpose') not in permitted | {'robots'} for row in attempt_rows)
    consumed = remaining if unknown else min(remaining, len(page_rows))
    used += consumed
    previous.update(pages_used=used, interrupted_attempt=job.attempt,
        recovered_page_reservation=consumed, recovery_policy='remaining_budget_reserved' if unknown else 'physical_starts_reserved')
    job.output_json = previous
    run.stats_json = {**run.stats_json, 'pages': run.stats_json.get('pages', 0) + consumed}
    if unknown or used >= min(10, run.plan_json.get('page_limit_per_company', 10)) or job.attempt >= 3:
        job.state = 'failed'
        job.error_json = dict(code='INTERRUPTED_PAGE_BUDGET' if unknown else 'PAGE_BUDGET_EXHAUSTED',
            message='中断任务的未结算页面已保守计入预算；该企业不再自动抓取', retryable=False)
    else:
        job.state, job.available_at = 'queued', now
    job.lease_token = job.lease_until = None


def _claim(run_id, token):
    with transaction() as session:
        run = _run(session, run_id)
        now = utcnow()
        if run.status != 'running' or run.process_token != token:
            return None
        for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'running')):
            if job.lease_until and job.lease_until <= now:
                _reclaim_expired(session, run, job, now)
            else:
                return None
        _refresh(session, run, now)
        if run.status != 'running':
            return None
        job = session.scalar(select(Job).where(Job.run_id == run.id, Job.state == 'queued',
            Job.available_at <= now).order_by(Job.created_at, Job.id).limit(1).with_for_update())
        if not job:
            return None
        job.state, job.lease_token = 'running', uid('lease')
        job.lease_until = now + timedelta(seconds=settings.lease_seconds)
        job.attempt += 1
        run.process_until = job.lease_until
        plan = deepcopy(run.plan_json)
        # The ten-page ceiling belongs to the enterprise, including retries.
        # Completed attempts consume budget even when their contact stage fails.
        plan['page_limit_per_company'] = max(0, min(10, plan.get('page_limit_per_company', 10))
                                              - job.output_json.get('pages_used', 0))
        return dict(id=job.id, job_id=job.id, run_id=run.id, batch_id=run.id, lease_token=job.lease_token,
            process_token=token, attempt=job.attempt, entity_key=job.entity_key,
            input=deepcopy(job.input_json), segment=deepcopy(run.spec_json), plan=plan,
            pages_used=run.stats_json.get('pages', 0), demo_only=run.demo_only,
            suppressions=_suppressions(session, run.spec_json))


def _heartbeat(task, done):
    while not done.wait(settings.heartbeat_seconds):
        try:
            with transaction() as session:
                run = _run(session, task['run_id'])
                job = session.get(Job, task['id'], with_for_update=True)
                now = utcnow()
                if run.status != 'running' or run.process_token != task['process_token'] or not job \
                        or job.lease_token != task['lease_token'] or job.lease_until <= now:
                    return
                job.lease_until = now + timedelta(seconds=settings.lease_seconds)
                run.process_until = job.lease_until
        except Exception:
            return


def _before_send(task):
    def guard(context, payload):
        with transaction() as session:
            run = session.get(Run, task['run_id'])
            job = session.get(Job, task['id'])
            if not run or run.status != 'running' or run.process_token != task['process_token'] \
                    or not job or job.state != 'running' or job.lease_token != task['lease_token'] \
                    or not job.lease_until or job.lease_until <= utcnow():
                raise AppError('LEASE_REVOKED', '任务租约已撤销，停止后续官网请求', 409)
    return guard


def _complete(task, result, pages, error, elapsed, operation_id):
    with transaction() as session:
        run = _run(session, task['run_id'])
        job = session.get(Job, task['id'], with_for_update=True)
        now = utcnow()
        if run.status != 'running' or run.process_token != task['process_token'] or not job \
                or job.state != 'running' or job.lease_token != task['lease_token'] or job.lease_until <= now:
            return False
        stats = deepcopy(run.stats_json)
        stats['pages'] = stats.get('pages', 0) + len(pages)
        stats['active_seconds'] = stats.get('active_seconds', 0) + elapsed
        stages = stats.setdefault('stage_processing_seconds', {})
        for stage, seconds in task.get('_stage_seconds', {}).items():
            stages[stage] = stages.get(stage, 0) + seconds
        run.stats_json = stats
        company_pages = job.output_json.get('pages_used', 0) + len(pages)
        if error:
            job.error_json = error
            job.output_json = {'pages_used': company_pages, 'elapsed_seconds': elapsed,
                               'stage_processing_seconds': deepcopy(task.get('_stage_seconds', {}))}
            job.state = 'queued' if error['retryable'] and job.attempt < 3 and company_pages < 10 else 'failed'
            delay = max(min(300, 2 ** job.attempt), error.get('retry_after_s') or 0)
            job.available_at = now + timedelta(seconds=delay)
            run.errors_json = (run.errors_json or [])[-99:] + [{**error, 'job_id': job.id, 'at': _iso(now)}]
            if error['code'] == 'PAGE_BUDGET_EXCEEDED' and stats['pages'] >= run.plan_json.get('page_limit_total', 10000):
                _settle(run, now)
                run.status = 'paused'
        else:
            result['company_id'] = task['input']['company_id']
            for key in ('candidate_ordinal', 'source_id', 'query_id'):
                if key in task['input']:
                    result[key] = task['input'][key]
            related = [row.id for row in session.scalars(select(Company).where(Company.run_id == run.id))
                       if row.data_json.get('domain') and row.data_json.get('domain') == result.get('domain')]
            if related:
                result['related_company_ids'] = related
                result.setdefault('conflicts', []).append(dict(field='domain', values=[result.get('domain')],
                    reason='shared_domain_different_identity', related_company_ids=related))
            company = Company(id=result['company_id'], run_id=run.id, identity_key=job.entity_key,
                data_json=result, expires_at=now + timedelta(days=int(os.getenv('B2B_CONTACT_RETENTION_DAYS', '365'))))
            session.add(company)
            session.flush()
            for page in pages:
                ref, digest = put(json.dumps(page, ensure_ascii=False, default=str).encode(), 'json')
                session.add(Snapshot(id=uid('snapshot'), run_id=run.id, company_id=company.id,
                    source_url=page.get('final_url') or task['input'].get('source_url') or '',
                    artifact_ref=ref, artifact_hash=digest,
                    expires_at=now + timedelta(days=int(os.getenv('B2B_RAW_RETENTION_DAYS', '30')))))
            job.output_json, job.state = {'company_id': company.id, 'pages_used': company_pages}, 'completed'
        job.lease_token = job.lease_until = None
        operation = session.get(Operation, operation_id, with_for_update=True)
        progress = deepcopy(operation.result_json)
        progress['processed_jobs'] = progress.get('processed_jobs', 0) + 1
        operation.result_json = progress
        run.version += 1
        _refresh(session, run, now)
        return True


def process(run_id, max_jobs=20, max_seconds=60, resume=False, operation_key=None, expected_version=None):
    from app.pipeline import process_company
    if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 20 \
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not 1 <= max_seconds <= 300:
        raise AppError('VALIDATION_ERROR', 'max_jobs 范围 1 至 20，max_seconds 范围 1 至 300')
    request = dict(max_jobs=max_jobs, max_seconds=max_seconds, resume=resume)
    key = operation_key or uid('process')
    token = uid('process')
    with transaction() as session:
        run = _run(session, run_id)
        operation, replay = _operation(session, run, 'process', key, request)
        if replay and operation.state == 'completed':
            return deepcopy(operation.result_json)
        _version(run, expected_version)
        if resume and run.status == 'paused':
            _action(session, run, 'resume')
        now = utcnow()
        if run.process_token and run.process_until and run.process_until > now:
            raise AppError('PROCESS_BUSY', '已有进程正在处理此批次，稍后读取状态', 409, retryable=True)
        _refresh(session, run, now)
        if run.status == 'paused':
            raise AppError('RUN_PAUSED', '批次已暂停；使用 --resume 继续', 409)
        if run.status == 'cancelled':
            raise AppError('INVALID_STATE', '批次已取消', 409)
        operation.state = 'running'
        operation.result_json = {**(operation.result_json or {'processed_jobs': 0}), '_owner': token}
        done_count = operation.result_json.get('processed_jobs', 0)
        operation_id = operation.id
        run.process_token = token
        run.process_until = now + timedelta(seconds=settings.lease_seconds)
    started = time.monotonic()
    try:
        while done_count < max_jobs and time.monotonic() - started < max_seconds:
            task = _claim(run_id, token)
            if not task:
                break
            done = threading.Event()
            heartbeat = threading.Thread(target=_heartbeat, args=(task, done), daemon=True)
            heartbeat.start()
            pages, result, error = [], None, None
            begin = time.monotonic()
            try:
                from app.web.observation import observation_context
                with observation_context(sink=observation_sink, run_id=run_id, batch_id=run_id,
                    job_id=task['id'], attempt=task['attempt'], purpose='qualification',
                    before_send=_before_send(task), host_limiter=host_limit,
                    source_id=task['input'].get('source_id', 'unknown'), query_id=task['input'].get('query_id', 'unknown')):
                    result = process_company(task, pages)
            except Exception as exc:
                error = dict(code=getattr(exc, 'code', type(exc).__name__),
                    message=getattr(exc, 'message', '官网处理失败；可检查任务错误并重试'),
                    retryable=bool(getattr(exc, 'retryable', False)),
                    retry_after_s=getattr(exc, 'retry_after_s', None),
                    upstream_status=getattr(exc, 'upstream_status', None))
            finally:
                done.set()
                heartbeat.join(timeout=1)
            if not _complete(task, result, pages, error, round(time.monotonic() - begin, 3), operation_id):
                break
            done_count += 1
    finally:
        with transaction() as session:
            run = _run(session, run_id)
            if run.process_token == token:
                # A local interruption leaves no live network worker. Revoke
                # its lease so the next command can resume immediately.
                for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'running')):
                    _reclaim_expired(session, run, job, utcnow())
                run.process_token = run.process_until = None
                _refresh(session, run)
    with transaction() as session:
        run = _run(session, run_id)
        operation = session.get(Operation, operation_id, with_for_update=True)
        if operation.result_json.get('_owner') != token:
            return {**_view(session, run), 'interrupted': True, 'operation_key': key}
        # A pause/cancel revokes the old token. Its result is still a valid,
        # replayable interrupted operation; no stale company is committed.
        result = {**_view(session, run), 'processed_jobs': operation.result_json.get('processed_jobs', 0), 'operation_key': key}
        operation.result_json, operation.state = result, 'completed'
        return result


def finish(run_id, checkpoint=None, external_tool_usage=None, max_jobs=20, max_seconds=60,
           resume=False, operation_key=None):
    from app.schemas import ResearchInput
    ResearchInput.model_validate(dict(checkpoint=checkpoint, external_tool_usage=external_tool_usage))
    request = dict(checkpoint=checkpoint, external_tool_usage=external_tool_usage)
    key = operation_key or _hash(request)
    # Finalization is independently committed before any network work starts.
    with transaction() as session:
        run = _run(session, run_id)
        operation, replay = _operation(session, run, 'finish', key, request)
        if not replay:
            if run.status == 'cancelled':
                raise AppError('INVALID_STATE', '批次已取消', 409)
            state = deepcopy(run.plan_json['research'])
            if checkpoint is not None:
                state['checkpoint'] = deepcopy(checkpoint)
            for name, count in (external_tool_usage or {}).items():
                usage = state['external_tool_usage']
                usage[name] = None if count is None or name in usage and usage[name] is None else usage.get(name, 0) + count
            state['input_open'] = False
            state['stop_reason'] = state['stop_reason'] or 'input_finalized'
            run.plan_json = {**run.plan_json, 'research': state}
            run.version += 1
            _refresh(session, run)
            operation.result_json = {'run_id': run_id, 'finalized': True}
            _audit(session, run.id, 'run.finish')
    return process(run_id, max_jobs=max_jobs, max_seconds=max_seconds, resume=resume,
                   operation_key=('finish:' + _hash(key)) if operation_key else None)


def list_runs():
    with transaction() as session:
        return [_view(session, run) for run in session.scalars(select(Run).order_by(Run.created_at.desc()))]


def _export_view(row, current_progress=None):
    if row.revoked_at or not row.artifact_ref or row.expires_at <= utcnow():
        raise AppError('EXPORT_REVOKED', '导出已撤销或过期，请使用新的 export-key 重新导出', 409)
    path = artifact_path(row.artifact_ref)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != row.artifact_hash:
        raise AppError('ARTIFACT_MISSING', '导出文件缺失或校验失败，请重新导出', 409)
    if row.report_ref:
        report_path = artifact_path(row.report_ref)
        if not report_path.is_file():
            raise AppError('ARTIFACT_MISSING', '导出报告缺失或校验失败，请重新导出', 409)
        report_bytes = report_path.read_bytes()
        try:
            valid_report = json.loads(report_bytes) == row.manifest_json
        except (ValueError, UnicodeError):
            valid_report = False
        if not valid_report or (row.report_hash and hashlib.sha256(report_bytes).hexdigest() != row.report_hash):
            raise AppError('ARTIFACT_MISSING', '导出报告缺失或校验失败，请重新导出', 409)
    research = row.manifest_json.get('research_summary', {})
    counts = row.manifest_json.get('row_counts', {})
    delivery = row.manifest_json.get('delivery', {})
    exported = counts.get('companies', 0)
    summary = {key: research.get(key) for key in ('discovered', 'matched', 'excluded', 'uncertain', 'missing')}
    summary.update(contactable=exported, exported_count=exported,
        target_quantity=research.get('target_quantity'),
        target_shortfall=max(0, research.get('target_quantity', 0) - exported))
    return dict(id=row.id, export_id=row.id, run_id=row.run_id, format=row.format, filename=row.filename,
        file=str(path), path=str(path), report_file=str(artifact_path(row.report_ref)) if row.report_ref else None,
        artifact_hash=row.artifact_hash, manifest=deepcopy(row.manifest_json),
        summary=summary, snapshot_at=row.manifest_json.get('generated_at'),
        delivery_policy=deepcopy(delivery), delivery_policy_version=delivery.get('policy_version'),
        current_progress=current_progress,
        external_tool_usage=deepcopy(research.get('external_tool_usage', {})),
        quality_report=deepcopy(row.manifest_json.get('quality_report') or {
            'metrics': row.manifest_json.get('metrics'), 'delivery': row.manifest_json.get('delivery')}))


def _metrics_context(session, run, now):
    rows = list(session.scalars(select(Observation).where(Observation.run_id == run.id, Observation.started_at <= now)))
    http = [row for row in rows if row.kind == 'http']
    unfinished = [row for row in http if row.state == 'started' or not row.finished_at or row.finished_at > now]
    unfinished_ids = {row.id for row in unfinished}
    settled = [row for row in http if row.id not in unfinished_ids]
    denied = [row for row in settled if row.data_json.get('dispatched') is False]
    # Settled observed-1 rows predate admission/dispatch separation and retain
    # their original physical-attempt contract. Explicit new unknowns never do.
    dispatched = [row for row in settled if row.data_json.get('dispatched') is True
                  or ('dispatched' not in row.data_json
                      and row.data_json.get('protocol_version') in {None, 'observed-1'})]
    unknown = len(http) - len(denied) - len(dispatched)
    pruned = run.stats_json.get('observation_pruned', False)
    lookup_pruned = run.stats_json.get('lookup_observation_pruned', False)
    summary = dict(enabled=True, protocol_version='observed-2',
        actual_http_attempts=None if pruned or unknown else len(dispatched), recorded_attempts=len(http),
        confirmed_http_attempts=None if pruned else len(dispatched),
        dispatch_denied_attempts=None if pruned else len(denied),
        dispatch_unknown_attempts=None if pruned else unknown,
        by_purpose=dict(Counter(row.data_json.get('purpose', 'unknown') for row in dispatched)),
        source_counts=dict(Counter(row.data_json.get('source_id', 'unknown') for row in dispatched)),
        query_counts=dict(Counter(row.data_json.get('query_id', 'unknown') for row in dispatched)),
        status_counts=dict(Counter(str(row.data_json.get('http_status') or row.data_json.get('error_code') or 'unknown') for row in dispatched)),
        retry_attempts=sum(row.data_json.get('attempt', 1) > 1 for row in dispatched),
        unfinished_attempts=len(unfinished), outcome_unknown=unknown,
        completeness='historical_missing' if pruned else 'incomplete' if unknown else 'observed',
        cache_hits=None, throttle_wait_seconds=sum(row.data_json.get('throttle_seconds', 0) for row in http),
        response_bytes=sum(row.data_json.get('response_bytes') or 0 for row in dispatched) if not unknown and not pruned else None)
    for kind in ('dns', 'mx'):
        selected = [row for row in rows if row.kind == kind]
        pending = sum(row.state == 'started' or not row.finished_at or row.finished_at > now for row in selected)
        summary[kind + '_attempts'] = None if lookup_pruned or pending else len(selected)
        summary[kind + '_recorded_attempts'] = len(selected)
        summary[kind + '_unknown_count'] = pending
    summary['dns_mx_completeness'] = 'historical_missing' if lookup_pruned else 'incomplete' if any(
        summary[kind + '_unknown_count'] for kind in ('dns', 'mx')) else 'observed'
    state = _summary(session, run, now)
    return dict(as_of=now, started_at=run.created_at, finished_at=run.finished_at, batch_status=run.status,
        paused_seconds=run.stats_json.get('paused_seconds', 0), paused_at=run.stats_json.get('paused_at'),
        task_processing_seconds=run.stats_json.get('active_seconds', 0),
        stage_processing_seconds=run.stats_json.get('stage_processing_seconds', {}),
        raw_candidate_count=state['admitted_count'] + run.stats_json.get('duplicates', 0),
        discovered=state['admitted_count'], failed=state['failed'], observation_summary=summary)


def export(run_id, format='xlsx', export_key=None):
    from app.delivery.exporting import build_export
    if format not in {'xlsx', 'csv'}:
        raise AppError('VALIDATION_ERROR', '导出格式仅支持 xlsx 或 csv')
    with transaction() as session:
        _delivery_lock(session)
        run = _run(session, run_id)
        now = utcnow()
        records = _rows(session, run)
        suppressed = _suppressions(session, run.spec_json)
        from app.delivery.selection import select_export_companies
        projection, _ = select_export_companies(records, mode='balanced', suppressions=suppressed,
            freshness_days=run.spec_json.get('freshness_days', 180), now=now,
            delivery_only=True, delivery_limit=run.spec_json['quantity'])
        key = export_key or 'auto:' + _hash({'projection': projection, 'version': run.version, 'format': format,
            'suppressions': suppressed})
        operation, replay = _operation(session, run, 'export', key, {'format': format})
        if replay:
            row = session.get(Export, operation.result_json['export_id'])
            if row and not row.revoked_at and row.artifact_ref and row.expires_at > now:
                return _export_view(row, _summary(session, run, now))
            if export_key:
                raise AppError('EXPORT_REVOKED', '导出已撤销或过期，请使用新的 export-key', 409)
        if run.demo_only:
            raise AppError('DEMO_EXPORT_BLOCKED', '演示数据不能正式导出', 409)
        if not records:
            raise AppError('EXPORT_EMPTY', '批次暂无可导出企业', 409)
        manifest = dict(batch_id=run.id, run_id=run.id, segment_hash=run.spec_hash,
            source_strategy=run.plan_json, generated_at=_iso(now), tool_version='skill-v2.0.1',
            runtime_release=release_identity(), research_release=run.plan_json.get('release'),
            freshness_days=run.spec_json.get('freshness_days', 180), budget=0, cost=0,
            research_summary=_summary(session, run, now))
        data, filename, media_type, manifest = build_export(records, manifest, format=format,
            mode='balanced', language='zh', suppressions=suppressed,
            now=now, delivery_only=True, delivery_limit=run.spec_json['quantity'],
            metrics_context=_metrics_context(session, run, now))
        if len(data) > 100 * 1024 ** 2:
            raise AppError('EXPORT_TOO_LARGE', '导出超过 100 MiB，请缩小批次')
        ref, digest = put(data, 'xlsx' if format == 'xlsx' else 'zip')
        report_ref, report_hash = put(json.dumps(manifest, ensure_ascii=False, default=str, indent=2).encode(), 'json')
        row = Export(id=uid('export'), run_id=run.id, format=format, filename=filename,
            artifact_ref=ref, artifact_hash=digest, report_ref=report_ref,
            report_hash=report_hash, manifest_json=manifest,
            expires_at=now + timedelta(days=int(os.getenv('B2B_EXPORT_RETENTION_DAYS', '30'))))
        session.add(row)
        session.flush()
        result = _export_view(row, _summary(session, run, now))
        operation.result_json = {'export_id': row.id}
        _audit(session, run.id, 'export.created', export_id=row.id)
        return result


def suppression_list():
    with transaction() as session:
        return _suppressions(session)


def _revoke_exports(session, run_id=None):
    query = select(Export).where(Export.revoked_at.is_(None))
    if run_id:
        query = query.where(Export.run_id == run_id)
    count = 0
    for row in session.scalars(query.with_for_update()):
        queue_artifact_delete(session, row.artifact_ref)
        queue_artifact_delete(session, row.report_ref)
        row.artifact_ref = row.report_ref = None
        row.revoked_at = utcnow()
        count += 1
    return count


def suppression_add(type, value, reason='用户排除'):
    from app.schemas import SuppressionInput, domain
    body = SuppressionInput(type=type, value=value, reason=reason)
    value = domain(body.value) if type == 'domain' else body.value.casefold() if type == 'email' else body.value
    with transaction() as session:
        _delivery_lock(session)
        old = session.scalar(select(Suppression).where(Suppression.type == type, Suppression.value == value))
        if old:
            return _suppression_view(old)
        row = Suppression(id=uid('suppression'), type=type, value=value, reason=body.reason)
        session.add(row)
        _revoke_exports(session)
        session.flush()
        return _suppression_view(row)


def suppression_remove(id):
    with transaction() as session:
        _delivery_lock(session)
        row = session.get(Suppression, id, with_for_update=True)
        if not row:
            raise AppError('NOT_FOUND', '抑制记录不存在', 404)
        session.delete(row)
        _revoke_exports(session)
        return {'deleted': id}


def _delete_company(session, row):
    for snapshot in session.scalars(select(Snapshot).where(Snapshot.company_id == row.id)):
        queue_artifact_delete(session, snapshot.artifact_ref)
    for job in session.scalars(select(Job).where(Job.run_id == row.run_id)):
        if job.input_json.get('company_id') == row.id:
            job.input_json = {'company_id': row.id, 'deleted': True}
            job.output_json = {'deleted': True}
            job.error_json = None
            job.state = 'deleted'
            job.lease_token = job.lease_until = None
    _revoke_exports(session, row.run_id)
    session.delete(row)


def delete_company(id):
    with transaction() as session:
        _delivery_lock(session)
        row = session.get(Company, id)
        if not row:
            raise AppError('NOT_FOUND', '企业不存在', 404)
        run = _run(session, row.run_id)
        _delete_company(session, row)
        run.version += 1
        _audit(session, run.id, 'company.deleted', company_id=id)
        return {'deleted': id, 'exports_revoked': True}


def maintenance(apply=False, periodic=False):
    if apply:
        # Retry committed deletion intents even when periodic cleanup is still
        # throttled. This also recovers a process exit just after revocation.
        drain_pending_deletes()
    with transaction() as session:
        _delivery_lock(session)
        now = utcnow()
        state = session.get(MaintenanceState, 'periodic', with_for_update=True)
        if periodic and state and state.last_run_at and (now - state.last_run_at).total_seconds() < 60:
            return {'skipped': True}
        if apply:
            if not state:
                state = MaintenanceState(id='periodic')
                session.add(state)
            state.last_run_at = now
        cutoff = now - timedelta(days=int(os.getenv('B2B_LOG_RETENTION_DAYS', '30')))
        snapshots = list(session.scalars(select(Snapshot).where(Snapshot.expires_at <= now, Snapshot.artifact_ref.is_not(None))))
        exports = list(session.scalars(select(Export).where(Export.expires_at <= now, Export.artifact_ref.is_not(None))))
        companies = list(session.scalars(select(Company).where(Company.expires_at <= now)))
        result = dict(dry_run=not apply, expired_snapshots=len(snapshots), expired_exports=len(exports), expired_companies=len(companies))
        if apply:
            for row in snapshots:
                queue_artifact_delete(session, row.artifact_ref)
                row.artifact_ref = None
            for row in exports:
                queue_artifact_delete(session, row.artifact_ref)
                queue_artifact_delete(session, row.report_ref)
                row.artifact_ref = row.report_ref = None
                row.revoked_at = now
            for row in companies:
                _run(session, row.run_id)
                _delete_company(session, row)
            expired_observations = list(session.scalars(select(Observation).where(Observation.created_at < cutoff)))
            for observation in expired_observations:
                if observation.run_id:
                    run = _run(session, observation.run_id)
                    key = 'observation_pruned' if observation.kind == 'http' else 'lookup_observation_pruned'
                    run.stats_json = {**run.stats_json, key: True}
                session.delete(observation)
            session.execute(delete(Audit).where(Audit.created_at < cutoff))
            # Remove contacts still present only in terminal task inputs.
            contact_cutoff = now - timedelta(days=int(os.getenv('B2B_CONTACT_RETENTION_DAYS', '365')))
            session.execute(delete(Job).where(Job.created_at < contact_cutoff,
                Job.run_id.in_(select(Run.id).where(Run.status.in_(['completed', 'partial', 'cancelled', 'failed'])))))
            # Uncommitted artifact writes can leave files after process death.
            refs = set(session.scalars(select(Snapshot.artifact_ref).where(Snapshot.artifact_ref.is_not(None))))
            refs.update(session.scalars(select(Export.artifact_ref).where(Export.artifact_ref.is_not(None))))
            refs.update(session.scalars(select(Export.report_ref).where(Export.report_ref.is_not(None))))
            from pathlib import Path
            root = Path(settings.artifact_root)
            if root.exists():
                for path in root.glob('object_*'):
                    if path.is_file() and path.name not in refs and path.stat().st_mtime < time.time() - 3600:
                        queue_artifact_delete(session, path.name)
    return result
