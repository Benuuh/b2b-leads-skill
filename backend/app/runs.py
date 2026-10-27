"""Durable batch application service. No network request holds a database lock."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from app.config import settings
from app.capabilities import LIMITS, METRICS_VERSION
from app.errors import AppError
from app.failures import classify, is_circuit_failure
from app.release import release_identity
from app.persistence.host_limits import limit as host_limit
from app.persistence import (transaction, utcnow, uid, put, artifact_path, observation_sink,
                             queue_artifact_delete, drain_pending_deletes)
from app.persistence.models import Run, Job, Company, Snapshot, Suppression, Export, Operation, Observation, Audit, MaintenanceState
from app.schemas import parse_segment, spec_version


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
    version = spec_version(run.spec_json)
    policy = ((run.spec_json.get('qualification_policy') or {}).get('contact_policy')
              if version == 2 else None)
    identity = 'v2' if version == 2 else 'legacy'
    freshness = run.spec_json.get('freshness_days', 180)
    common = dict(suppressions=_suppressions(session, run.spec_json), freshness_days=freshness, now=now)
    qualified_rows = [row for row in rows if row.get('qualification', {}).get('decision') == 'qualified']
    qualified = filter_companies(qualified_rows, mode='max_coverage', identity_version=identity, **common)
    contactable = delivery_companies(rows, policy=policy, identity_version=identity, **common)
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
        spec_version=version, identity_version=identity,
        qualified_by_origin=deepcopy(run.stats_json.get('qualified_by_origin') or {}),
        append_ready=run.stats_json.get('append_ready'),
        discovery_seen=run.stats_json.get('discovery_seen'),
        missing_items=_missing_items(rows),
        next_retry_at=_iso(next_available) if next_available and next_available > now else None)
    return state


def _missing_items(rows):
    """Bounded, non-identifying summary of field/channel gaps for reporting."""
    counter = Counter()
    for row in rows:
        qualification = row.get('qualification') or {}
        if qualification.get('decision') == 'qualified' and row.get('status') != 'qualified':
            for item in list(qualification.get('missing') or [])[:20]:
                counter['condition:' + str(item.get('field') or item.get('id') or 'unknown')] += 1
            for name in (row.get('channel_policy') or {}).get('missing') or []:
                counter['channel:' + str(name)] += 1
        elif qualification.get('decision') == 'uncertain':
            for item in list(qualification.get('missing') or [])[:20]:
                counter['condition:' + str(item.get('field') or item.get('id') or 'unknown')] += 1
    return dict(sorted(counter.items()))


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
    # Persist the latest projection so snapshot decisions do not need to
    # recompute the whole delivery cohort on every completion.
    state['contactable_count'] = research['contactable_count']
    state['qualified_count'] = research['qualified_count']
    state['missing_items'] = research['missing_items']
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
    version = spec_version(run.spec_json)
    result = dict(id=run.id, run_id=run.id, batch_id=run.id, name=run.spec_json['name'],
        status=run.status, version=run.version, segment=deepcopy(run.spec_json), spec=deepcopy(run.spec_json),
        plan=deepcopy(run.plan_json), research=research, checkpoint=research['checkpoint'],
        summary=summary, stats=stats, demo_only=run.demo_only, errors=deepcopy(run.errors_json),
        input_closed=not research['input_open'],
        requires_more_processing=bool(research['queued'] or research['running']),
        external_tool_usage=deepcopy(research['external_tool_usage']),
        spec_version=version,
        created_at=_iso(run.created_at), updated_at=_iso(run.updated_at), finished_at=_iso(run.finished_at))
    if version == 2:
        from app.rules.policy import policy_summary
        result['policy'] = policy_summary(run.spec_json)
        result['evidence_policy'] = deepcopy(run.spec_json.get('evidence_policy'))
        result['execution_policy'] = deepcopy(run.spec_json.get('execution_policy'))
    return result


def create(run_id, spec, **kwargs):
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', run_id):
        raise AppError('INVALID_RUN_ID', 'run 仅支持 1 至 80 位字母、数字、点、横线和下划线')
    spec, version = parse_segment(spec)
    with transaction() as session:
        # Lock even before the row exists to serialize concurrent create calls.
        from sqlalchemy import text
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'run:' + run_id})
        old = session.get(Run, run_id, with_for_update=True)
        if old:
            if old.spec_hash != _hash(spec):
                raise AppError('IDEMPOTENCY_CONFLICT', '该 run 已用于不同客群', 409)
            return _view(session, old)
        plan = {'execution_mode': 'skill_research', 'completion_goal': 'contactable',
            'page_limit_per_company': 10, 'page_limit_total': 10000,
            'request_observation_protocol': 'observed-2', 'lookup_observation_protocol': 'observed-2',
            'timing_protocol': 'observed-1', 'release': release_identity(),
            'spec_version': version,
            'research': {'input_open': True, 'candidate_limit': spec['quantity'] * 3,
            'time_limit_seconds': 28800, 'admitted_count': 0, 'checkpoint': {}, 'external_tool_usage': {},
            'stop_reason': None}}
        if version == 2:
            from app.rules.policy import policy_summary
            plan['rules_version'] = policy_summary(spec)['rule_version']
            plan['evidence_version'] = (spec.get('evidence_policy') or {}).get('evidence_version')
            plan['execution'] = {**(spec.get('execution_policy') or {}), 'circuit': {'open': False}}
        run = Run(id=run_id, spec_json=spec, spec_hash=_hash(spec), demo_only=settings.fixture_mode,
            active_started_at=utcnow(), plan_json=plan,
            stats_json={'pages': 0, 'duplicates': 0, 'active_seconds': 0, 'paused_seconds': 0})
        session.add(run)
        session.flush()
        _audit(session, run.id, 'run.created', spec_version=version,
               spec_hash=run.spec_hash, rules_version=plan.get('rules_version'),
               release=plan['release'])
        return _view(session, run)


def _identity(candidate, version=2):
    from app.rules.identity import identity_key
    return identity_key(candidate, version=version)


def append(run_id, candidates, chunk_id=None, checkpoint=None, external_tool_usage=None,
           finalize=False, expected_version=None):
    from app.pipeline import precheck_candidate
    from app.rules.identity import alias_keys
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
        version = spec_version(run.spec_json)
        admission = dict(accepted=0, duplicates=0, skipped=0, append_ready=0,
                         admitted_count=state['admitted_count'])
        normalized = []
        rejected = []
        for index, value in enumerate(candidates):
            try:
                normalized.append(precheck_candidate(value, run.spec_json,
                    fixture=run.demo_only, index=index))
            except AppError as error:
                # ``append`` stays atomic. The coordinator isolates invalid drafts
                # with validate-input and resubmits only the admissible subset.
                rejected.append({'index': index, 'code': error.code,
                                 'path': error.details.get('path'), 'message': error.message})
                if len(rejected) == 1:
                    raise AppError(error.code, error.message,
                        details={**error.details, 'rejected': rejected, 'index': index}) from error
        admission['append_ready'] = len(normalized)
        jobs = list(session.scalars(select(Job).where(Job.run_id == run.id).order_by(Job.created_at, Job.id)))
        by_key = {job.entity_key: job for job in jobs}
        by_legacy = {}
        for job in jobs:
            by_legacy.setdefault(_identity(job.input_json, version=1), job)
        for candidate in normalized:
            key = _identity(candidate, version)
            old = by_key.get(key) or by_legacy.get(_identity(candidate, version=1))
            for alias_key in alias_keys(candidate, version=version):
                old = old or by_key.get(alias_key)
                if old is None and version == 2:
                    old = next((job for job in jobs
                                if _identity(job.input_json, version=version) == alias_key), None)
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
                    old.entity_key = _identity(prior, version)
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
            by_legacy.setdefault(_identity(candidate, version=1), job)
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
        stats = {**run.stats_json, 'duplicates': run.stats_json.get('duplicates', 0) + admission['duplicates'],
                 'append_ready': (run.stats_json.get('append_ready') or 0) + admission['append_ready'],
                 'discovery_seen': (run.stats_json.get('discovery_seen') or 0) + len(candidates)}
        run.stats_json = stats
        run.version += 1
        _refresh(session, run)
        result = {**_view(session, run), 'admission': admission}
        operation.result_json = result
        _audit(session, run.id, 'run.append', **admission)
        return result


def validate_input(spec_payload, input_payload):
    """Per-record offline precheck. No database, HTTP, DNS or maintenance."""
    from app.capabilities import RULE_VERSION
    from app.pipeline import precheck_candidate
    from app.schemas import ResearchInput
    spec, version = parse_segment(spec_payload or {})
    body = ResearchInput.model_validate(input_payload or {})
    records, submittable = [], []
    for index, value in enumerate(body.candidates):
        try:
            candidate = precheck_candidate(value, spec, index=index)
        except AppError as error:
            records.append({'index': index, 'admissible': False, 'reason_code': error.code,
                            'path': error.details.get('path') or f'candidates[{index}]',
                            'message': error.message, 'normalized_hash': None})
            continue
        records.append({'index': index, 'admissible': True, 'reason_code': None, 'path': None,
                        'message': None, 'normalized_hash': _hash(candidate)})
        submittable.append(index)
    return {'spec_version': version, 'rule_version': RULE_VERSION if version == 2 else None,
            'records': records, 'submittable_indexes': submittable,
            'counts': {'input': len(body.candidates), 'append_ready': len(submittable),
                       'rejected': len(body.candidates) - len(submittable)},
            'network_requests': 0, 'database_writes': 0}


def advance(run_id, payload, chunk_id=None, max_jobs=20, max_seconds=60, workers=None, resume=False):
    """One startup for atomic admission and a replayable bounded processing step.

    Append already prechecks every record before committing any candidate.
    The two durable operations keep their own ledgers: a crash after admission
    safely replays append before continuing the same process operation.
    """
    from app.schemas import ResearchInput
    if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 20 \
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not 1 <= max_seconds <= 300:
        raise AppError('VALIDATION_ERROR', 'max_jobs 范围 1 至 20，max_seconds 范围 1 至 300')
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                or not 1 <= workers <= LIMITS['company_workers']['max']):
        raise AppError('VALIDATION_ERROR', 'workers 必须为 1 至 4 的整数')
    body = ResearchInput.model_validate(payload).model_dump()
    key = chunk_id or _hash(body)
    if not isinstance(key, str) or not 1 <= len(key) <= 160:
        raise AppError('INVALID_KEY', 'chunk 必须为 1 至 160 字符')
    with transaction() as session:
        run = _run(session, run_id)
        if spec_version(run.spec_json) == 1 and workers is not None and workers > 1:
            raise AppError('UNSUPPORTED_OPTION', 'v1 批次仅支持串行处理')
        if run.status == 'paused' and not resume:
            raise AppError('RUN_PAUSED', '批次已暂停；使用 --resume 继续', 409)
    admitted = append(run_id, chunk_id=key, **body)
    processed = process(run_id, max_jobs=max_jobs, max_seconds=max_seconds,
                        workers=workers, resume=resume,
                        operation_key='advance:' + _hash(key))
    result = {name: processed[name] for name in (
        'run_id', 'status', 'version', 'summary', 'research', 'processed_jobs',
        'workers', 'requires_more_processing', 'errors') if name in processed}
    result['snapshots'] = {name: processed.get('stats', {}).get(name)
                           for name in ('last_snapshot', 'snapshots')}
    return {**result, 'advanced': True, 'chunk': key, 'admission': admitted['admission']}


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
        plan = deepcopy(run.plan_json)
        execution = plan.get('execution') or {}
        if (execution.get('circuit') or {}).get('open'):
            execution['circuit'] = {**execution['circuit'], 'open': False,
                                    'closed_at': _iso(now), 'closed_by': 'retry'}
            plan['execution'] = execution
        if plan.get('research', {}).get('stop_reason') == 'circuit_open':
            plan['research']['stop_reason'] = None
        run.plan_json = plan
        stats['failure_streak'] = 0
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
    charged again. Any unconsumed page reservation returns to the batch budget.
    """
    previous = deepcopy(job.output_json or {})
    reserved = int(previous.pop('reserved_pages', 0) or 0)
    if reserved:
        run.stats_json = {**run.stats_json,
                          'reserved_pages': max(0, (run.stats_json.get('reserved_pages') or 0) - reserved)}
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
            message='中断任务的未结算页面已保守计入预算；该企业不再自动抓取', retryable=False,
            category='budget_exhausted')
    else:
        job.state, job.available_at = 'queued', now
    job.lease_token = job.lease_until = None


def _claim(run_id, token, workers=1):
    with transaction() as session:
        run = _run(session, run_id)
        now = utcnow()
        if run.status != 'running' or run.process_token != token:
            return None
        for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'running')):
            if job.lease_until and job.lease_until <= now:
                _reclaim_expired(session, run, job, now)
        session.flush()
        running = list(session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'running')))
        if len(running) >= max(1, workers):
            return None
        execution = run.plan_json.get('execution') or {}
        if (execution.get('circuit') or {}).get('open'):
            return None
        _refresh(session, run, now)
        if run.status != 'running':
            return None
        job = session.scalar(select(Job).where(Job.run_id == run.id, Job.state == 'queued',
            Job.available_at <= now).order_by(Job.created_at, Job.id).limit(1).with_for_update(skip_locked=True))
        if not job:
            return None
        plan = deepcopy(run.plan_json)
        # The ten-page ceiling belongs to the enterprise, including retries.
        # Completed attempts consume budget even when their contact stage fails.
        per_company = max(0, min(10, plan.get('page_limit_per_company', 10))
                          - job.output_json.get('pages_used', 0))
        stats = deepcopy(run.stats_json)
        reserved = int(stats.get('reserved_pages') or 0)
        available = max(0, plan.get('page_limit_total', 10000) - stats.get('pages', 0) - reserved)
        reservation = min(per_company, available)
        if per_company and not reservation:
            # The batch page budget is fully reserved. Do not dispatch a request
            # that cannot be paid for; close input and report the real gap.
            job.state = 'queued'
            state = deepcopy(run.plan_json['research'])
            state.update(input_open=False, stop_reason=state.get('stop_reason') or 'page_budget')
            plan['research'] = state
            for queued in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'queued')):
                queued.state = 'skipped'
            run.plan_json = plan
            _refresh(session, run, now)
            return None
        if job.available_at:
            stats['queue_wait_seconds'] = stats.get('queue_wait_seconds', 0) + max(
                0.0, (now - job.available_at).total_seconds())
        job.state, job.lease_token = 'running', uid('lease')
        job.lease_until = now + timedelta(seconds=settings.lease_seconds)
        job.attempt += 1
        job.output_json = {**job.output_json, 'reserved_pages': reservation}
        stats['reserved_pages'] = reserved + reservation
        run.stats_json = stats
        run.process_until = job.lease_until
        # This job may only spend what it reserved, and its own total-budget view
        # excludes reservations already held by the other workers.
        plan['page_limit_per_company'] = min(per_company, reservation)
        plan['page_limit_total'] = max(0, plan.get('page_limit_total', 10000) - reserved)
        return dict(id=job.id, job_id=job.id, run_id=run.id, batch_id=run.id, lease_token=job.lease_token,
            process_token=token, attempt=job.attempt, entity_key=job.entity_key,
            input=deepcopy(job.input_json), segment=deepcopy(run.spec_json), plan=plan,
            pages_used=run.stats_json.get('pages', 0), demo_only=run.demo_only,
            reserved_pages=reservation, workers=workers,
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
        stats['reserved_pages'] = max(0, (stats.get('reserved_pages') or 0)
                                      - int(task.get('reserved_pages') or 0))
        stats['pages'] = stats.get('pages', 0) + len(pages)
        stats['active_seconds'] = stats.get('active_seconds', 0) + elapsed
        stages = stats.setdefault('stage_processing_seconds', {})
        for stage, seconds in task.get('_stage_seconds', {}).items():
            stages[stage] = stages.get(stage, 0) + seconds
        company_pages = job.output_json.get('pages_used', 0) + len(pages)
        if error:
            failure = classify(error.get('code'), bool(error.get('retryable')))
            error = {**error, **failure}
            job.error_json = error
            job.output_json = {'pages_used': company_pages, 'elapsed_seconds': elapsed,
                               'stage_processing_seconds': deepcopy(task.get('_stage_seconds', {}))}
            job.state = 'queued' if error['retryable'] and job.attempt < 3 and company_pages < 10 else 'failed'
            delay = max(min(300, 2 ** job.attempt), error.get('retry_after_s') or 0)
            job.available_at = now + timedelta(seconds=delay)
            run.errors_json = (run.errors_json or [])[-99:] + [{**error, 'job_id': job.id,
                                                                'at': _iso(now)}]
            stats['failure_streak'] = (stats.get('failure_streak') or 0) + 1 \
                if is_circuit_failure(failure['category']) else stats.get('failure_streak', 0)
            stats.setdefault('failure_categories', {})
            stats['failure_categories'][failure['category']] = stats['failure_categories'].get(
                failure['category'], 0) + 1
            threshold = int((run.plan_json.get('execution') or {}).get(
                'circuit_breaker_failures') or LIMITS['circuit_breaker_failures']['default'])
            if stats['failure_streak'] >= threshold:
                plan = deepcopy(run.plan_json)
                plan.setdefault('execution', {})['circuit'] = {
                    'open': True, 'opened_at': _iso(now), 'failures': stats['failure_streak'],
                    'last_code': error.get('code'), 'category': failure['category'],
                    'threshold': threshold,
                    'checkpoint_saved': bool(plan.get('research', {}).get('checkpoint'))}
                plan['research']['stop_reason'] = 'circuit_open'
                run.plan_json = plan
                stats['circuit_opened'] = True
            if error['code'] == 'PAGE_BUDGET_EXCEEDED' and stats['pages'] >= run.plan_json.get('page_limit_total', 10000):
                _settle(run, now)
                run.status = 'paused'
        else:
            stats['failure_streak'] = 0
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
            result.setdefault('revision', 1)
            result.setdefault('evidence_revision', 0)
            stop_reason = (result.get('pipeline') or {}).get('stop_reason')
            if (result.get('qualification') or {}).get('decision') == 'qualified':
                origin = ('offline_evidence' if stop_reason in {'saved_evidence_sufficient', 'saved_evidence_reassessed'}
                          else 'supplemented_evidence' if stop_reason == 'supplemented_evidence'
                          else 'live_research')
                origins = deepcopy(stats.get('qualified_by_origin') or {})
                origins[origin] = origins.get(origin, 0) + 1
                stats['qualified_by_origin'] = origins
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
        run.stats_json = stats
        run.version += 1
        _refresh(session, run, now)
        return True


def process(run_id, max_jobs=20, max_seconds=60, resume=False, operation_key=None,
            expected_version=None, workers=None):
    from app.pipeline import process_company
    if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 20 \
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not 1 <= max_seconds <= 300:
        raise AppError('VALIDATION_ERROR', 'max_jobs 范围 1 至 20，max_seconds 范围 1 至 300')
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                or not 1 <= workers <= LIMITS['company_workers']['max']):
        raise AppError('VALIDATION_ERROR', 'workers 必须为 1 至 4 的整数')
    # Workers are an execution choice, not part of the replay identity: a
    # repeated operation key returns the same bounded result.
    request = dict(max_jobs=max_jobs, max_seconds=max_seconds, resume=resume)
    key = operation_key or uid('process')
    token = uid('process')
    with transaction() as session:
        run = _run(session, run_id)
        version = spec_version(run.spec_json)
        configured = int((run.spec_json.get('execution_policy') or {}).get('company_workers') or 1)
        requested = configured if workers is None else workers
        if version == 1:
            if requested > 1:
                raise AppError('UNSUPPORTED_OPTION', '该批次未冻结并发执行政策；仅支持串行处理',
                               details={'spec_version': 1, 'requested_workers': requested})
            requested = 1
        workers = max(1, min(requested, configured, max_jobs))
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
        operation.result_json = {**(operation.result_json or {'processed_jobs': 0}),
                                 '_owner': token, 'workers': workers}
        done_count = operation.result_json.get('processed_jobs', 0)
        operation_id = operation.id
        run.process_token = token
        run.process_until = now + timedelta(seconds=settings.lease_seconds)
    started = time.monotonic()
    deadline = started + max_seconds
    stop = threading.Event()
    signal = threading.Semaphore(0)
    state_lock = threading.Lock()
    threads, counters = [], {'started': 0, 'completed': done_count, 'lost': 0}

    def work(task):
        done = threading.Event()
        heartbeat = threading.Thread(target=_heartbeat, args=(task, done), daemon=True)
        heartbeat.start()
        pages, result, error = [], None, None
        begin = time.monotonic()
        try:
            from app.web.http import request_deadline
            from app.web.observation import observation_context
            with request_deadline(max(0.0, deadline - time.monotonic())):
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
        try:
            committed = _complete(task, result, pages, error, round(time.monotonic() - begin, 3), operation_id)
        except Exception:
            # Never leave an unhandled commit failure invisible: the attempt is
            # reclaimed conservatively by the coordinator's final fence.
            logging.getLogger(__name__).exception('b2b completion commit failed')
            committed = False
        with state_lock:
            if committed:
                counters['completed'] += 1
            else:
                counters['lost'] += 1
                stop.set()
        signal.release()

    try:
        while True:
            with state_lock:
                alive = [thread for thread in threads if thread.is_alive()]
                completed = counters['completed']
                started_jobs = counters['started']
            if completed >= max_jobs or time.monotonic() >= deadline or stop.is_set():
                if not alive:
                    break
            admitted = 0
            while True:
                with state_lock:
                    alive = [thread for thread in threads if thread.is_alive()]
                    started_jobs = counters['started']
                    allowed = (len(alive) < workers and started_jobs < max_jobs
                               and not stop.is_set() and time.monotonic() < deadline)
                if not allowed:
                    break
                task = _claim(run_id, token, workers=workers)
                if not task:
                    break
                with state_lock:
                    counters['started'] += 1
                thread = threading.Thread(target=work, args=(task,), daemon=True)
                thread.start()
                threads.append(thread)
                admitted += 1
            if not any(thread.is_alive() for thread in threads):
                break
            progressed = signal.acquire(timeout=0.2)
            if admitted or progressed:
                _maybe_snapshot(run_id)
    finally:
        for thread in threads:
            thread.join(timeout=settings.lease_seconds)
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
        result = {**_view(session, run), 'processed_jobs': operation.result_json.get('processed_jobs', 0),
                  'workers': workers, 'operation_key': key}
        operation.result_json, operation.state = result, 'completed'
        return result


def _snapshot_due(run_id):
    """Decide whether a bounded delivery snapshot is due; never exports here."""
    with transaction() as session:
        run = _run(session, run_id)
        if run.demo_only or run.status not in {'running', 'paused'}:
            return None
        spec = run.spec_json
        if spec_version(spec) != 2:
            return None
        execution = spec.get('execution_policy') or {}
        every = int(execution.get('snapshot_every_new_qualified') or 0)
        interval = int(execution.get('snapshot_interval_seconds') or LIMITS['snapshot_interval_seconds']['default'])
        if not every and not interval:
            return None
        now = utcnow()
        stats = deepcopy(run.stats_json)
        last = stats.get('last_snapshot') or {}
        suppressed = _suppressions(session, spec)
        qualified = int(run.plan_json.get('research', {}).get('contactable_count') or 0)
        baseline = last.get('at') or _iso(run.created_at)
        baseline_count = int(last.get('qualified') or 0)
        elapsed = (now - datetime_from(baseline)).total_seconds() if baseline else 0
        changed = qualified > baseline_count
        due_new = bool(every) and changed and qualified - baseline_count >= every
        due_time = bool(interval) and changed and elapsed >= interval
        if not (due_new or due_time):
            return None
        projection = _hash({'qualified': qualified, 'suppressed': suppressed,
                            'records': sorted(row.get('company_id') or '' for row in _rows(session, run))})
        return {'key': 'auto-snapshot:' + projection[:32], 'reason': 'new_qualified' if due_new else 'interval',
                'qualified': qualified, 'at': _iso(now)}


def datetime_from(value):
    from datetime import datetime
    text = str(value).replace('Z', '+00:00')
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)


def _maybe_snapshot(run_id):
    """Produce a real snapshot at a coordinator safety checkpoint."""
    decision = _snapshot_due(run_id)
    if not decision:
        return None
    try:
        result = export(run_id, export_key=decision['key'])
    except Exception as error:
        # A failed snapshot must never abort research; record it and continue.
        logging.getLogger(__name__).warning('b2b snapshot deferred: %s', type(error).__name__)
        with transaction() as session:
            run = _run(session, run_id)
            stats = deepcopy(run.stats_json)
            stats['last_snapshot'] = {'at': decision['at'], 'qualified': decision['qualified'],
                                      'error': getattr(error, 'code', type(error).__name__),
                                      'reason': decision['reason']}
            stats['snapshot_checked_at'] = decision['at']
            run.stats_json = stats
        return None
    with transaction() as session:
        run = _run(session, run_id)
        stats = deepcopy(run.stats_json)
        stats['last_snapshot'] = {'at': decision['at'], 'qualified': decision['qualified'],
                                  'export_id': result.get('export_id'), 'reason': decision['reason'],
                                  'rows': (result.get('manifest') or {}).get('row_counts', {}).get('companies')}
        stats['snapshot_checked_at'] = decision['at']
        history = stats.setdefault('snapshots', [])
        history.append({'export_id': result.get('export_id'), 'at': decision['at'],
                        'qualified': decision['qualified'], 'reason': decision['reason'],
                        'rows': (result.get('manifest') or {}).get('row_counts', {}).get('companies')})
        stats['snapshots'] = history[-20:]
        run.stats_json = stats
        _audit(session, run.id, 'run.snapshot', export_id=result.get('export_id'),
               qualified=decision['qualified'], reason=decision['reason'])
    return result


def finish(run_id, checkpoint=None, external_tool_usage=None, max_jobs=20, max_seconds=60,
           resume=False, operation_key=None, workers=None):
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
                   operation_key=('finish:' + _hash(key)) if operation_key else None, workers=workers)


def list_runs():
    with transaction() as session:
        return [_view(session, run) for run in session.scalars(select(Run).order_by(Run.created_at.desc()))]


def reassess(run_id, company_ids, operation_key, expected_version=None):
    """Bounded, atomic replay of completed review rows from retained evidence.

    This never queues work, consumes page allowance, or performs HTTP/DNS. The
    original input and checksum-verified snapshots remain unchanged, and every
    result transition records its evidence hashes and executing release.
    """
    from app.pipeline import reassess_company
    if not isinstance(company_ids, list) or not 1 <= len(company_ids) <= 20 or any(
            not isinstance(value, str) or not value for value in company_ids) or len(set(company_ids)) != len(company_ids):
        raise AppError('VALIDATION_ERROR', 'company_ids 必须为 1 至 20 个不重复的企业 ID')
    request = {'company_ids': sorted(company_ids)}
    with transaction() as session:
        _delivery_lock(session)
        run = _run(session, run_id)
        operation, replay = _operation(session, run, 'reassess', operation_key, request)
        if replay:
            return deepcopy(operation.result_json)
        _version(run, expected_version)
        if run.status == 'cancelled':
            raise AppError('INVALID_STATE', '批次已取消', 409)
        if run.process_token or session.scalar(select(Job.id).where(
                Job.run_id == run.id, Job.state == 'running').limit(1)):
            raise AppError('PROCESS_BUSY', '请等待当前处理结束后再重评保存证据', 409, retryable=True)
        now, changed = utcnow(), []
        release = release_identity()
        for company_id in sorted(company_ids):
            company = session.get(Company, company_id, with_for_update=True)
            job = session.scalar(select(Job).where(Job.run_id == run.id,
                Job.output_json['company_id'].astext == company_id).with_for_update())
            if not company or company.run_id != run.id or not job or job.state != 'completed':
                raise AppError('INVALID_REASSESSMENT', '只可重评本批次已完成且保留原始输入的企业', 409)
            previous = deepcopy(company.data_json)
            if previous.get('status') not in {'needs_review', 'insufficient'}:
                raise AppError('INVALID_REASSESSMENT', '只可重评待确认或证据不足的企业', 409)
            if company.expires_at <= now:
                raise AppError('EVIDENCE_EXPIRED', '企业证据已过期，不能重评', 409)
            snapshots = list(session.scalars(select(Snapshot).where(Snapshot.company_id == company_id)
                .order_by(Snapshot.created_at, Snapshot.id)))
            if job.output_json.get('pages_used', 0) and not snapshots:
                raise AppError('SNAPSHOT_UNAVAILABLE', '原始网页快照已缺失，不能重评', 409)
            pages, evidence = [], []
            for snapshot in snapshots:
                try:
                    if snapshot.expires_at <= now or not snapshot.artifact_ref or not snapshot.artifact_hash:
                        raise ValueError()
                    raw = artifact_path(snapshot.artifact_ref).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != snapshot.artifact_hash:
                        raise ValueError()
                    page = json.loads(raw)
                    if not isinstance(page, dict):
                        raise ValueError()
                except (OSError, ValueError, TypeError):
                    raise AppError('SNAPSHOT_UNAVAILABLE', '原始网页快照已过期、缺失或校验失败，不能重评', 409)
                pages.append(page)
                evidence.append({'snapshot_id': snapshot.id, 'sha256': snapshot.artifact_hash})
            task = dict(input=deepcopy(job.input_json), segment=deepcopy(run.spec_json),
                        suppressions=_suppressions(session, run.spec_json))
            result = reassess_company(task, pages, previous)
            result['reassessment'] = dict(at=_iso(now), operation_key=operation_key, release=release,
                original_input_hash=_hash(job.input_json), snapshots=evidence, previous_result_hash=_hash(previous))
            company.data_json = result
            transition = dict(company_id=company_id, before=previous.get('qualification', {}).get('decision'),
                after=result['qualification']['decision'], previous_result_hash=_hash(previous),
                result_hash=_hash(result), original_input_hash=_hash(job.input_json), snapshots=evidence)
            changed.append(transition)
        run.version += 1
        _refresh(session, run, now)
        result = {**_view(session, run), 'reassessed_companies': len(changed), 'transitions': changed,
                  'operation_key': operation_key, 'network_requests': 0}
        operation.result_json = result
        _audit(session, run.id, 'run.reassess', operation_key=operation_key, release=release, transitions=changed)
        return result


SUPPLEMENT_IMMUTABLE = {'company_name', 'website_url', 'country_code', 'branch_id',
                        'headquarters_country', 'registered_country'}
SUPPLEMENT_FACT_FIELDS = ('business_models', 'offering_products', 'offering_services',
                          'customer_industries', 'entity_relationship', 'operating_countries',
                          'company_type', 'industry_text', 'employee_count', 'revenue',
                          'region', 'city', 'address', 'contact_form_url', 'parent_company')


def supplement(run_id, company_id, payload, operation_key, expected_version=None):
    """Append incremental evidence to a retained enterprise and re-decide offline.

    The identity of the enterprise is immutable here, no unique candidate is
    created, and no page/HTTP/DNS work is performed. The operation is idempotent
    and refuses to overwrite a newer revision.
    """
    from app.pipeline import precheck_candidate, reassess_company
    from app.schemas import SupplementInput
    body = SupplementInput.model_validate(payload)
    if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 160:
        raise AppError('INVALID_KEY', '操作键必须为 1 至 160 字符')
    request = {'company_id': company_id, 'expected_revision': body.expected_revision,
               'evidence_hash': _hash(body.candidates)}
    with transaction() as session:
        _delivery_lock(session)
        run = _run(session, run_id)
        if spec_version(run.spec_json) != 2:
            raise AppError('UNSUPPORTED_OPTION', '增量补证仅支持 spec_version 2 批次',
                           details={'spec_version': spec_version(run.spec_json)})
        operation, replay = _operation(session, run, 'supplement', operation_key, request)
        if replay:
            return deepcopy(operation.result_json)
        _version(run, expected_version)
        state = deepcopy(run.plan_json['research'])
        if not state['input_open']:
            raise AppError('INPUT_CLOSED', '批次输入已关闭，拒绝新增证据', 409)
        if run.process_token or session.scalar(select(Job.id).where(
                Job.run_id == run.id, Job.state == 'running').limit(1)):
            raise AppError('PROCESS_BUSY', '请等待当前处理结束后再补充证据', 409, retryable=True)
        company = session.get(Company, company_id, with_for_update=True)
        job = session.scalar(select(Job).where(Job.run_id == run.id,
            Job.output_json['company_id'].astext == company_id).with_for_update())
        if not company or company.run_id != run.id or not job or job.state != 'completed':
            raise AppError('INVALID_SUPPLEMENT', '只可为本批次已完成且保留原始输入的企业补证', 409)
        previous = deepcopy(company.data_json)
        revision = int(previous.get('revision') or 1)
        if body.expected_revision != revision:
            raise AppError('VERSION_CONFLICT', '企业证据版本已更新，请重新读取后补证', 409,
                           details={'expected_revision': body.expected_revision, 'revision': revision})
        merged = deepcopy(job.input_json)
        added, new_facts = [], []
        for index, value in enumerate(body.candidates):
            raw = dict(value)
            for field in SUPPLEMENT_IMMUTABLE:
                if raw.get(field) and merged.get(field) and str(raw[field]) != str(merged[field]):
                    raise AppError('IMMUTABLE_IDENTITY', '补证不能修改企业身份字段',
                                   details={'path': f'candidates[{index}]', 'field': field})
            partial = {'company_name': merged.get('company_name'), 'website_url': merged.get('website_url'),
                       'country_code': merged.get('country_code'), 'source_url': merged.get('source_url'),
                       'source_type': merged.get('source_type'), **raw}
            candidate = precheck_candidate(partial, run.spec_json, fixture=run.demo_only, index=index)
            for ref in candidate.get('sources') or []:
                if ref not in merged.setdefault('sources', []):
                    merged['sources'].append(deepcopy(ref))
                    added.append(ref)
            for field in ('email', 'phone'):
                values = candidate.get(field)
                current = merged.get(field)
                current = current if isinstance(current, list) else ([current] if current else [])
                for item in values if isinstance(values, list) else [values]:
                    if item and item not in current:
                        current.append(item)
                        added.append({'field': field, 'value': item})
                merged[field] = current or None
            for field in SUPPLEMENT_FACT_FIELDS:
                value = candidate.get(field)
                if value in (None, '', []):
                    continue
                current = merged.get(field)
                if isinstance(value, list) or isinstance(current, list):
                    combined = list(dict.fromkeys([*(current or []), *(value if isinstance(value, list) else [value])]))
                    if combined != (current or []):
                        new_facts.append(field)
                    merged[field] = combined
                elif not current:
                    merged[field] = value
                    new_facts.append(field)
                elif current != value:
                    conflict = {'field': field, 'values': [current, value],
                                'source_url': candidate.get('source_url')}
                    if conflict not in merged.setdefault('conflicts', []):
                        merged['conflicts'].append(conflict)
        if not added and not new_facts:
            raise AppError('NO_NEW_EVIDENCE', '没有可用的新增证据；未重复抓取或改写记录', 409)
        evidence_hash = _hash(added)
        snapshots = list(session.scalars(select(Snapshot).where(Snapshot.company_id == company_id)
            .order_by(Snapshot.created_at, Snapshot.id)))
        if job.output_json.get('pages_used', 0) and not snapshots:
            raise AppError('SNAPSHOT_UNAVAILABLE', '原始网页快照已缺失，不能离线补证', 409)
        now, pages, evidence = utcnow(), [], []
        for snapshot in snapshots:
            try:
                if snapshot.expires_at <= now or not snapshot.artifact_ref or not snapshot.artifact_hash:
                    raise ValueError()
                raw = artifact_path(snapshot.artifact_ref).read_bytes()
                if hashlib.sha256(raw).hexdigest() != snapshot.artifact_hash:
                    raise ValueError()
                page = json.loads(raw)
                if not isinstance(page, dict):
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise AppError('SNAPSHOT_UNAVAILABLE', '原始网页快照已过期、缺失或校验失败，不能离线补证', 409)
            pages.append(page)
            evidence.append({'snapshot_id': snapshot.id, 'sha256': snapshot.artifact_hash})
        task = dict(input=merged, segment=deepcopy(run.spec_json),
                    suppressions=_suppressions(session, run.spec_json))
        result = reassess_company(task, pages, previous)
        result['revision'] = revision + 1
        result['evidence_revision'] = int(previous.get('evidence_revision') or 0) + 1
        result['pipeline'] = {**(result.get('pipeline') or {}), 'stop_reason': 'supplemented_evidence'}
        result['supplement'] = dict(at=_iso(now), operation_key=operation_key,
            release=release_identity(), previous_revision=revision, revision=revision + 1,
            evidence_hash=evidence_hash, added=len(added), new_facts=new_facts,
            snapshots=evidence, previous_result_hash=_hash(previous),
            network_requests=0, dns_requests=0, dispatch_resumed=False,
            paused=run.status == 'paused')
        company.data_json = result
        job.input_json = merged
        if previous.get('qualification', {}).get('decision') != 'qualified' \
                and result['qualification']['decision'] == 'qualified':
            origins = deepcopy(run.stats_json.get('qualified_by_origin') or {})
            origins['supplemented_evidence'] = origins.get('supplemented_evidence', 0) + 1
            run.stats_json = {**run.stats_json, 'qualified_by_origin': origins}
        run.version += 1
        _refresh(session, run, now)
        answer = {**_view(session, run), 'company_id': company_id, 'expected_revision': body.expected_revision,
                  'revision': result['revision'], 'decision': result['qualification']['decision'],
                  'status': result['status'], 'evidence_hash': evidence_hash,
                  'network_requests': 0, 'dns_requests': 0, 'dispatch_resumed': False,
                  'operation_key': operation_key}
        operation.result_json = answer
        _audit(session, run.id, 'run.supplement', operation_key=operation_key, company_id=company_id,
               release=release_identity(), previous_revision=revision, revision=result['revision'],
               evidence_hash=evidence_hash, decision=result['qualification']['decision'],
               network_requests=0)
        return answer


def extend_research(run_id, hours, operation_key, expected_version=None):
    """Increase the total active-time allowance only on a still-open run."""
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 72:
        raise AppError('VALIDATION_ERROR', 'hours 必须为 1 至 72 的整数，表示累计研究时长上限')
    with transaction() as session:
        run = _run(session, run_id)
        operation, replay = _operation(session, run, 'extend-research', operation_key, {'hours': hours})
        if replay:
            return deepcopy(operation.result_json)
        _version(run, expected_version)
        now = utcnow()
        if run.process_token or session.scalar(select(Job.id).where(
                Job.run_id == run.id, Job.state == 'running').limit(1)):
            raise AppError('PROCESS_BUSY', '请等待当前处理结束后再延长研究时限', 409, retryable=True)
        state = deepcopy(run.plan_json['research'])
        if run.status not in {'running', 'paused'} or not state['input_open'] \
                or state['admitted_count'] >= state['candidate_limit'] \
                or _elapsed(run, now) >= state['time_limit_seconds']:
            raise AppError('INVALID_STATE', '只可延长尚未达到时限或候选上限且输入仍开放的批次', 409)
        old_limit, new_limit = state['time_limit_seconds'], hours * 3600
        if new_limit <= old_limit:
            raise AppError('INVALID_EXTENSION', '新累计研究时限必须大于当前时限', 409)
        state['time_limit_seconds'] = new_limit
        run.plan_json = {**run.plan_json, 'research': state}
        run.version += 1
        _refresh(session, run, now)
        change = dict(previous_limit_seconds=old_limit, time_limit_seconds=new_limit,
                      active_wall_seconds=round(_elapsed(run, now), 3))
        result = {**_view(session, run), 'extension': change, 'operation_key': operation_key}
        operation.result_json = result
        _audit(session, run.id, 'run.extend_research', operation_key=operation_key,
               release=release_identity(), **change)
        return result


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
        discovered=state['admitted_count'], failed=state['failed'], observation_summary=summary,
        metrics_version=METRICS_VERSION, research=_research_metrics(session, run, state, summary))


def _research_metrics(session, run, state, observation_summary):
    """Versioned research metrics; unknown values stay null, never zero."""
    stats = run.stats_json
    rows = list(session.scalars(select(Company).where(Company.run_id == run.id)))
    decisions = Counter((row.data_json.get('qualification') or {}).get('decision') for row in rows)
    origins = deepcopy(stats.get('qualified_by_origin') or {})
    return {
        'metrics_version': METRICS_VERSION,
        'spec_version': spec_version(run.spec_json),
        'discovery_seen': stats.get('discovery_seen'),
        'append_ready': stats.get('append_ready'),
        'admitted': state['admitted_count'],
        'qualified': decisions['qualified'],
        'excluded': decisions['excluded'],
        'needs_review': decisions['uncertain'],
        'qualified_by_origin': origins or None,
        'new_qualified': origins.get('live_research'),
        'supplement_qualified': origins.get('supplemented_evidence'),
        'offline_reassess_qualified': origins.get('saved_evidence_reassessed'),
        'offline_evidence_qualified': origins.get('offline_evidence'),
        'missing_items': state.get('missing_items') or None,
        'failure_categories': stats.get('failure_categories') or None,
        'tool_invocations': state.get('external_tool_usage') or None,
        'physical_http_attempts': observation_summary.get('actual_http_attempts'),
        'dns_attempts': observation_summary.get('dns_attempts'),
        'mx_attempts': observation_summary.get('mx_attempts'),
        'cache_hits': observation_summary.get('cache_hits'),
        'throttle_wait_seconds': observation_summary.get('throttle_wait_seconds'),
        'snapshots': deepcopy(stats.get('snapshots') or None),
        'reserved_pages': stats.get('reserved_pages'),
        'queue_wait_seconds': stats.get('queue_wait_seconds'),
        'stage_processing_seconds': deepcopy(stats.get('stage_processing_seconds') or None),
        'units': {'counts': 'enterprises', 'timing': 'seconds', 'requests': 'physical_attempts'},
        'semantics': 'discovered=admitted_count unchanged; discovery_seen counts submitted candidates; '
                     'tool_invocations are Agent-side calls and never substitute physical HTTP attempts',
    }


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
        version = spec_version(run.spec_json)
        policy = ((run.spec_json.get('qualification_policy') or {}).get('contact_policy')
                  if version == 2 else None)
        identity = 'v2' if version == 2 else 'legacy'
        from app.delivery.selection import select_export_companies
        projection, _ = select_export_companies(records, mode='balanced', suppressions=suppressed,
            freshness_days=run.spec_json.get('freshness_days', 180), now=now,
            delivery_only=True, delivery_limit=run.spec_json['quantity'], policy=policy,
            identity_version=identity)
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
            source_strategy=run.plan_json, generated_at=_iso(now), tool_version='skill-v2.1.0',
            runtime_release=release_identity(), research_release=run.plan_json.get('release'),
            freshness_days=run.spec_json.get('freshness_days', 180), budget=0, cost=0,
            spec_version=version, metrics_version=METRICS_VERSION,
            research_summary=_summary(session, run, now))
        if version == 2:
            from app.rules.policy import policy_summary
            manifest['policy'] = policy_summary(run.spec_json)
            manifest['evidence_policy'] = deepcopy(run.spec_json.get('evidence_policy'))
        data, filename, media_type, manifest = build_export(records, manifest, format=format,
            mode='balanced', language='zh', suppressions=suppressed,
            now=now, delivery_only=True, delivery_limit=run.spec_json['quantity'],
            metrics_context=_metrics_context(session, run, now), policy=policy,
            identity_version=identity)
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
