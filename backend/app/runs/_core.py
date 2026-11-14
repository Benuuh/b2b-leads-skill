"""共享内核：批次行锁、审计、幂等操作账本、进度投影与实时页面刷新（内部模块）。"""
import hashlib
import json
from collections import Counter
from copy import deepcopy
from sqlalchemy import select
from app.errors import AppError
from app.persistence import (utcnow, uid)
from app.persistence.models import Run, Job, Company, Suppression, Operation, Audit
from app.schemas import spec_version


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


class RunProjectionContext:
    """Per-transaction projection cache for one run revision (internal only).

    The same transaction may need the run summary, the returned view, the
    delivery detail, snapshot metadata and an export manifest.  Reading the
    full cohort and running the delivery selection once per operation is the
    dominant coordinator cost, so every consumer shares these detached rows.

    The context is deliberately not a cross-command cache: rows are loaded
    fresh for each operation, and callers rebuild it when the run revision,
    suppression version or observation time changes.
    """

    def __init__(self, session, run, now=None, *, rows=None, suppressions=None, contactable=None):
        self.session = session
        self.run = run
        self.now = now or utcnow()
        version = spec_version(run.spec_json)
        self.version = version
        self.identity = 'v2' if version == 2 else 'legacy'
        self.policy = ((run.spec_json.get('qualification_policy') or {}).get('contact_policy')
                       if version == 2 else None)
        self.freshness = run.spec_json.get('freshness_days', 180)
        self._rows = rows
        self._suppressions = suppressions
        self._suppression_version = None
        self._qualified_rows = None
        self._qualified = None
        self._uncertain = None
        self._contactable = contactable
        self._pending = None
        self._job_states = None
        self._next_available = None
        self._summaries = {}

    @property
    def rows(self):
        if self._rows is None:
            self._rows = _rows(self.session, self.run)
        return self._rows

    @property
    def suppressions(self):
        if self._suppressions is None:
            self._suppressions = _suppressions(self.session, self.run.spec_json)
        return self._suppressions

    @property
    def suppression_version(self):
        if self._suppression_version is None:
            self._suppression_version = _hash(sorted(
                (str(item.get('type')), str(item.get('value'))) for item in self.suppressions))
        return self._suppression_version

    @property
    def qualified_rows(self):
        if self._qualified_rows is None:
            self._qualified_rows = [row for row in self.rows
                                    if row.get('qualification', {}).get('decision') == 'qualified']
        return self._qualified_rows

    @property
    def qualified(self):
        if self._qualified is None:
            from app.delivery.selection import filter_companies
            self._qualified = filter_companies(self.qualified_rows, mode='max_coverage',
                identity_version=self.identity, suppressions=self.suppressions,
                freshness_days=self.freshness, now=self.now)
        return self._qualified

    @property
    def uncertain(self):
        if self._uncertain is None:
            from app.delivery.selection import filter_companies
            rows = [row for row in self.rows
                    if (row.get('qualification') or {}).get('decision') == 'uncertain']
            self._uncertain = (filter_companies(rows, mode='max_coverage',
                identity_version=self.identity, suppressions=self.suppressions,
                freshness_days=self.freshness, now=self.now) if rows else [])
        return self._uncertain

    @property
    def contactable(self):
        if self._contactable is None:
            from app.delivery.selection import delivery_companies
            self._contactable = delivery_companies(self.rows, policy=self.policy,
                identity_version=self.identity, suppressions=self.suppressions,
                freshness_days=self.freshness, now=self.now)
        return self._contactable

    def _load_job_states(self):
        self._job_states = Counter(self.session.scalars(
            select(Job.state).where(Job.run_id == self.run.id)))
        self._next_available = self.session.scalar(
            select(Job.available_at).where(Job.run_id == self.run.id, Job.state == 'queued')
            .order_by(Job.available_at).limit(1))
        return self._job_states

    def refresh_job_states(self):
        return self._load_job_states()

    def summary(self, detail=False):
        if detail:
            if 'detail' not in self._summaries:
                self._summaries['detail'] = self._build(detail=True)
            return self._summaries['detail']
        if 'plain' in self._summaries:
            return self._summaries['plain']
        if 'detail' in self._summaries:
            plain = {key: value for key, value in self._summaries['detail'].items()
                     if key != '_detail'}
            self._summaries['plain'] = plain
            return plain
        self._summaries['plain'] = self._build(detail=False)
        return self._summaries['plain']

    def _build(self, detail=False):
        run = self.run
        rows = self.rows
        version, identity, policy = self.version, self.identity, self.policy
        qualified = self.qualified
        contactable = self.contactable
        states = self._load_job_states()
        next_available = self._next_available
        decisions = Counter(row.get('qualification', {}).get('decision', 'uncertain') for row in rows)
        state = deepcopy(run.plan_json['research'])
        state.update(active_wall_seconds=round(_elapsed(run, self.now), 3),
            qualified_count=len(qualified), contactable_count=len(contactable), goal_count=len(contactable),
            target_quantity=run.spec_json['quantity'],
            shortfall=max(0, run.spec_json['quantity'] - len(contactable)),
            discovered=state['admitted_count'], matched=len(qualified), excluded=decisions['excluded'],
            uncertain=decisions['uncertain'], missing=max(0, len(qualified) - len(contactable)),
            processing=bool(run.process_token and run.process_until and run.process_until > self.now),
            queued=states['queued'], running=states['running'], failed=states['failed'],
            processed=states['completed'], skipped=states['skipped'],
            spec_version=version, identity_version=identity,
            qualified_by_origin=deepcopy(run.stats_json.get('qualified_by_origin') or {}),
            append_ready=run.stats_json.get('append_ready'),
            discovery_seen=run.stats_json.get('discovery_seen'),
            missing_items=_missing_items(rows),
            next_retry_at=_iso(next_available) if next_available and next_available > self.now else None)
        if detail:
            # 待办口径与 live 页面共用 selection.pending_companies（spec 2.6 §4.1C）。
            from app.delivery.selection import pending_companies
            if self._pending is None:
                self._pending = pending_companies(rows, contactable, identity_version=identity,
                    suppressions=self.suppressions, freshness_days=self.freshness, now=self.now,
                    qualified_view=qualified, uncertain_view=self.uncertain)
            state['_detail'] = {'contactable': contactable, 'pending': self._pending}
        return state


def _summary(session, run, now=None, detail=False, context=None):
    context = context or RunProjectionContext(session, run, now)
    return context.summary(detail)


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


def _refresh(session, run, now=None, detail=False, context=None):
    """Rebuild the authoritative projection once and persist its counters."""
    now = now or utcnow()
    session.flush()
    context = context or RunProjectionContext(session, run, now)
    research = context.summary(detail)
    plan = deepcopy(run.plan_json)
    state = plan['research']
    stop = ('goal_reached' if research['goal_count'] >= run.spec_json['quantity'] else
            'time_limit' if research['active_wall_seconds'] >= state['time_limit_seconds'] else
            'candidate_limit' if state['admitted_count'] >= state['candidate_limit'] else None)
    skipped_queued = False
    if stop and run.status in {'running', 'paused'}:
        state['input_open'] = False
        state['stop_reason'] = stop
        if stop in {'goal_reached', 'time_limit'}:
            for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'queued')):
                job.state = 'skipped'
                skipped_queued = True
    # Persist the latest projection so snapshot decisions do not need to
    # recompute the whole delivery cohort on every completion.
    state['contactable_count'] = research['contactable_count']
    state['qualified_count'] = research['qualified_count']
    state['missing_items'] = research['missing_items']
    run.plan_json = plan
    if skipped_queued:
        states = context.refresh_job_states()
        research.update(queued=states['queued'], running=states['running'],
                        processed=states['completed'], failed=states['failed'])
    session.flush()
    pending = session.scalar(select(Job.id).where(Job.run_id == run.id, Job.state.in_(['queued', 'running'])).limit(1))
    if run.status == 'running' and not state['input_open'] and not pending:
        _settle(run, now)
        run.status = 'completed' if research['goal_count'] >= run.spec_json['quantity'] else 'partial'
        run.finished_at = now
        run.version += 1
        research['processing'] = False
    # Keep the returned view consistent with the stop/settle mutations above.
    research['input_open'] = state['input_open']
    research['stop_reason'] = state['stop_reason']
    research['contactable_count'] = len(context.contactable)
    research['qualified_count'] = len(context.qualified)
    research['shortfall'] = max(0, run.spec_json['quantity'] - research['contactable_count'])
    stats = dict(run.stats_json or {})
    stats['counters'] = {
        'qualified': research['qualified_count'], 'contactable': research['contactable_count'],
        'excluded': research['excluded'], 'uncertain': research['uncertain'],
        'missing': research['missing'], 'queued': research['queued'],
        'running': research['running'], 'processed': research['processed'],
        'failed': research['failed'],
    }
    stats['projection'] = {
        'version': run.version, 'as_of': _iso(now),
        'suppression_version': context.suppression_version,
        'contactable': research['contactable_count'],
        'qualified': research['qualified_count'],
    }
    stats.pop('projection_pending', None)
    run.stats_json = stats
    return context


def _view(session, run, context=None, research=None):
    context = context or RunProjectionContext(session, run)
    research = deepcopy(research if research is not None else context.summary(False))
    public_stats = {key: value for key, value in (run.stats_json or {}).items()
                    if key not in {'counters', 'projection', 'projection_pending'}}
    stats = {**deepcopy(public_stats), **{key: research[key] for key in
        ('discovered', 'processed', 'failed', 'qualified_count', 'contactable_count', 'shortfall')}}
    summary = {key: research[key] for key in ('discovered', 'matched', 'excluded', 'uncertain', 'missing')}
    summary.update(contactable=research['contactable_count'], target_quantity=research['target_quantity'],
                   target_shortfall=research['shortfall'])
    version = spec_version(run.spec_json)
    stored_brief = (run.plan_json or {}).get('segment_brief')
    if isinstance(stored_brief, dict) and stored_brief.get('confirm_hash'):
        segment_brief = deepcopy(stored_brief)
    else:
        segment_brief = {'recorded': False, 'message': '历史批次，无确认记录'}
    initial = (run.plan_json or {}).get('initial_limits') or {}
    current_limit = research.get('time_limit_seconds')
    result = dict(id=run.id, run_id=run.id, batch_id=run.id, name=run.spec_json['name'],
        status=run.status, version=run.version, segment=deepcopy(run.spec_json), spec=deepcopy(run.spec_json),
        plan=deepcopy(run.plan_json), research=research, checkpoint=research['checkpoint'],
        summary=summary, stats=stats, demo_only=run.demo_only, errors=deepcopy(run.errors_json),
        input_closed=not research['input_open'],
        requires_more_processing=bool(research['queued'] or research['running']),
        external_tool_usage=deepcopy(research['external_tool_usage']),
        spec_version=version,
        segment_brief=segment_brief,
        time_limits={
            'initial_seconds': initial.get('time_limit_seconds', current_limit),
            'current_seconds': current_limit,
            'initial_recorded': 'time_limit_seconds' in initial,
        },
        created_at=_iso(run.created_at), updated_at=_iso(run.updated_at), finished_at=_iso(run.finished_at))
    if version == 2:
        from app.rules.policy import policy_summary
        result['policy'] = policy_summary(run.spec_json)
        result['evidence_policy'] = deepcopy(run.spec_json.get('evidence_policy'))
        result['execution_policy'] = deepcopy(run.spec_json.get('execution_policy'))
    return result


def _projection_stale(run, now):
    """Whether persisted counters are too old for a claim/stop decision."""
    stats = run.stats_json or {}
    projection = stats.get('projection') or {}
    if not projection or not projection.get('as_of'):
        return True
    try:
        as_of = datetime_from(projection['as_of'])
    except (TypeError, ValueError):
        return True
    if (now - as_of).total_seconds() > 300:
        # The persisted count can be older than a freshness window after a long
        # in-flight request. Recompute once rather than treating it as current.
        return True
    pending = stats.get('projection_pending')
    if pending and pending.get('at'):
        try:
            age = (now - datetime_from(pending['at'])).total_seconds()
        except (TypeError, ValueError):
            return True
        if age >= 1.0:
            return True
    return False


def _refresh_light(session, run, now=None):
    """Claim-path refresh that never runs the full delivery selection.

    Counters persisted by the last authoritative checkpoint are used only when
    the projection is still within its freshness/pending window and its
    suppression version matches. Otherwise the caller gets a full rebuild.
    """
    now = now or utcnow()
    stats = run.stats_json or {}
    projection = stats.get('projection') or {}
    if _projection_stale(run, now):
        _refresh(session, run, now)
        return False
    suppressions = _suppressions(session, run.spec_json)
    current_suppression = _hash(sorted(
        (str(item.get('type')), str(item.get('value'))) for item in suppressions))
    if projection.get('suppression_version') != current_suppression:
        _refresh(session, run, now)
        return False
    state = deepcopy(run.plan_json['research'])
    contactable = int(projection.get('contactable') or 0)
    stop = ('goal_reached' if contactable >= run.spec_json['quantity'] else
            'time_limit' if _elapsed(run, now) >= state['time_limit_seconds'] else
            'candidate_limit' if state['admitted_count'] >= state['candidate_limit'] else None)
    if stop and run.status in {'running', 'paused'}:
        state['input_open'] = False
        state['stop_reason'] = stop
        if stop in {'goal_reached', 'time_limit'}:
            for job in session.scalars(select(Job).where(Job.run_id == run.id, Job.state == 'queued')):
                job.state = 'skipped'
        run.plan_json = {**run.plan_json, 'research': state}
        session.flush()
    pending = session.scalar(select(Job.id).where(
        Job.run_id == run.id, Job.state.in_(['queued', 'running'])).limit(1))
    if run.status == 'running' and not state['input_open'] and not pending:
        _settle(run, now)
        run.status = 'completed' if contactable >= run.spec_json['quantity'] else 'partial'
        run.finished_at = now
        run.version += 1
    return True


def _list_view(session, run, now=None):
    """Lightweight list row from persisted counters; no cohort read/selection."""
    now = now or utcnow()
    state = deepcopy(run.plan_json.get('research') or {})
    stats_json = run.stats_json or {}
    counters = stats_json.get('counters') or {}
    quantity = int(run.spec_json.get('quantity') or 0)
    admitted = int(state.get('admitted_count') or 0)
    qualified = int(counters.get('qualified', state.get('qualified_count') or 0))
    contactable = int(counters.get('contactable', state.get('contactable_count') or 0))
    excluded = int(counters.get('excluded') or 0)
    uncertain = int(counters.get('uncertain') or 0)
    missing = int(counters.get('missing', max(0, qualified - contactable)) or 0)
    queued = int(counters.get('queued') or 0)
    running = int(counters.get('running') or 0)
    research = {
        **state,
        'active_wall_seconds': round(_elapsed(run, now), 3),
        'qualified_count': qualified, 'contactable_count': contactable, 'goal_count': contactable,
        'target_quantity': quantity, 'shortfall': max(0, quantity - contactable),
        'discovered': admitted, 'matched': qualified, 'excluded': excluded, 'uncertain': uncertain,
        'missing': missing, 'queued': queued, 'running': running,
        'processed': int(counters.get('processed') or 0), 'failed': int(counters.get('failed') or 0),
        'spec_version': spec_version(run.spec_json),
        'identity_version': 'v2' if spec_version(run.spec_json) == 2 else 'legacy',
        'processing': bool(run.process_token and run.process_until and run.process_until > now),
    }
    summary = {key: research[key] for key in ('discovered', 'matched', 'excluded', 'uncertain', 'missing')}
    summary.update(contactable=contactable, target_quantity=quantity,
                   target_shortfall=max(0, quantity - contactable))
    public_stats = {key: value for key, value in stats_json.items()
                    if key not in {'counters', 'projection', 'projection_pending'}}
    public_stats.update({key: research[key] for key in
                         ('discovered', 'processed', 'failed', 'qualified_count',
                          'contactable_count', 'shortfall')})
    return dict(id=run.id, run_id=run.id, batch_id=run.id, name=run.spec_json['name'],
        status=run.status, version=run.version, summary=summary, stats=public_stats,
        research=research, checkpoint=research.get('checkpoint'),
        input_closed=not research.get('input_open'),
        requires_more_processing=bool(queued or running),
        spec_version=research['spec_version'],
        created_at=_iso(run.created_at), updated_at=_iso(run.updated_at),
        finished_at=_iso(run.finished_at))


def _live_refresh(run_id, minimum_interval=0.0):
    """Project the just-committed state for the local page; never raise here."""
    try:
        from app.live.projection import refresh
        return refresh(run_id, minimum_interval=minimum_interval)
    except Exception:
        return None


def _live_signature(session, run_id):
    try:
        from app.live.projection import company_signature
        return company_signature(session, run_id)
    except Exception:
        return ""


def _live_remember(run_id, signature, detail):
    try:
        from app.live.projection import remember
        remember(run_id, signature, detail)
    except Exception:
        pass


def _live_forget(run_id=None):
    try:
        from app.live.projection import forget
        forget(run_id)
    except Exception:
        pass


def _live_refresh_existing():
    try:
        from app.live.projection import refresh_existing
        return refresh_existing()
    except Exception:
        return None


def datetime_from(value):
    from datetime import datetime
    text = str(value).replace('Z', '+00:00')
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)
