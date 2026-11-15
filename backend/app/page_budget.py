"""Shared-directory page ledger for runs that froze ``shared-page-budget-1``.

Company crawls keep their existing reservation math. This module is the
directory side of the same run counters: one list page is one unit, a detail
page also counts against that company's page cap, and ``pages + reserved``
never exceeds the frozen total. It does not fetch pages and it does not
start automatic directory collection.

Cache hits are run-local and explicit. Robots cache is not a page cache.
A replay of the same operation key and input does not charge again. A
different input conflicts. An unknown send keeps the page unit occupied.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.persistence import transaction, utcnow, uid
from app.persistence.models import Job, Observation, PageReservation
from app.runs._core import _hash, _run

CONTRACT = 'shared-page-budget-1'
COMPANY_PAGE_CAP = 10
_PAGE_TYPES = {'list', 'detail'}
_SOURCES = {'shared_directory'}


def contract_of(run):
    return (run.plan_json or {}).get('page_budget_contract')


def refuse_automatic_collection(run_id):
    """Slice C is not implemented. A frozen budget is not a collector."""
    with transaction() as session:
        run = _run(session, run_id)
        if contract_of(run) != CONTRACT:
            raise AppError(
                'SHARED_COLLECTION_REFUSED',
                '该批次没有冻结 shared-page-budget-1，拒绝自动共享目录采集',
                409, details={'page_budget_contract': contract_of(run)})
        raise AppError(
            'SHARED_COLLECTION_UNAVAILABLE',
            '页面预算合同已冻结，但目录自动采集未启用。预算可用不等于自动采集，也不等于 JS 渲染可用。',
            409, details={'page_budget_contract': CONTRACT,
                          'automatic_shared_directory_collection': 'unavailable',
                          'automatic_js_rendering': 'unavailable'})


def directory_detail_pages(session, run_id, company_key):
    """Detail units already occupying this company's page cap."""
    if not company_key:
        return 0
    total = 0
    rows = session.scalars(select(PageReservation).where(
        PageReservation.run_id == run_id,
        PageReservation.company_key == company_key,
        PageReservation.page_type == 'detail',
        PageReservation.cache_hit.is_(False),
        PageReservation.state.in_(('reserved', 'settled', 'unknown'))))
    for row in rows:
        if row.state == 'reserved':
            total += int(row.pages_reserved or 0)
        else:
            total += int(row.pages_consumed or 0)
    return total


def reserve(run_id, *, operation_key, url, page_type, attempt=1, companies=None,
            company_key=None, source='shared_directory', use_cache=False):
    """Hold one page unit before a directory attempt. No network."""
    body = _checked_input(operation_key, url, page_type, attempt, companies, company_key, source)
    try:
        try:
            with transaction() as session:
                return _reserve(session, run_id, body, use_cache=use_cache)
        except IntegrityError:
            with transaction() as session:
                run = _require_contract(session, run_id)
                existing = _existing(session, run.id, body['operation_key'], body['attempt'])
                if existing is None:
                    raise
                _same_input(existing, body['input_hash'])
                return _public(existing, replay=True)
    except AppError as error:
        if error.code == 'PAGE_BUDGET_EXCEEDED' and error.details.get('scope') == 'run':
            _close_for_budget(run_id)
        raise


def settle(run_id, *, operation_key, attempt=1, outcome, send_state, physical_attempts=0,
           observation_ref=None):
    """Settle one reserved attempt. Physical HTTP count is recorded, not added."""
    if send_state not in {'started', 'not_started', 'unknown'}:
        raise AppError('VALIDATION_ERROR', 'send_state 仅支持 started、not_started 或 unknown')
    if outcome not in {'ok', 'denied', 'timeout', 'failed', 'cancelled'}:
        raise AppError('VALIDATION_ERROR', '页面结果不在预算合同范围内')
    if isinstance(physical_attempts, bool) or not isinstance(physical_attempts, int) or physical_attempts < 0:
        raise AppError('VALIDATION_ERROR', 'physical_attempts 必须是非负整数')
    if send_state == 'not_started' and outcome != 'cancelled':
        raise AppError('VALIDATION_ERROR', '未开始的尝试只能显式取消并释放预留')
    with transaction() as session:
        run = _require_contract(session, run_id)
        row = _existing(session, run.id, operation_key, attempt)
        if row is None:
            raise AppError('NOT_FOUND', '没有对应的页面预留', 404)
        if row.state != 'reserved':
            return _public(row, replay=True)
        _require_running(run)
        if row.cache_hit:
            row.state = 'settled'
            row.pages_consumed = 0
            row.send_state = 'not_started'
            row.result = 'cache_hit'
            row.settled_at = utcnow()
            return _public(row, replay=False)
        stats = dict(run.stats_json or {})
        reserved = int(stats.get('reserved_pages') or 0)
        held = int(row.pages_reserved or 0)
        stats['reserved_pages'] = max(0, reserved - held)
        if send_state == 'not_started':
            row.state = 'released'
            row.pages_consumed = 0
            row.result = 'cancelled'
            if row.page_type == 'detail':
                stats['directory_detail_holds'] = max(0, int(stats.get('directory_detail_holds') or 0) - held)
        else:
            # started and unknown both occupy the unit. unknown is reported
            # separately and is not returned to the pool.
            row.state = 'unknown' if send_state == 'unknown' else 'settled'
            row.pages_consumed = held
            row.result = outcome
            stats['pages'] = int(stats.get('pages') or 0) + held
        row.send_state = send_state
        row.physical_attempts = physical_attempts
        row.observation_ref = observation_ref
        row.settled_at = utcnow()
        run.stats_json = stats
        _guard_total(run)
        return _public(row, replay=False)


def recover(run_id, *, operation_key, attempt=1, observation_fault=False):
    """Close an attempt whose worker died before settle.

    A durable observation with ``dispatched=false`` is the only proof that
    the send did not start, and only then is the reservation released.
    Anything else, including a failed observation read, keeps the unit.
    """
    with transaction() as session:
        run = _require_contract(session, run_id)
        row = _existing(session, run.id, operation_key, attempt)
        if row is None:
            raise AppError('NOT_FOUND', '没有对应的页面预留', 404)
        if row.state != 'reserved':
            return _public(row, replay=True)
        send_state, ref = 'unknown', None
        if not observation_fault:
            try:
                observation = _observation(session, run.id, operation_key, attempt)
            except Exception:
                observation = None
            else:
                if observation is not None:
                    ref = observation.id
                    dispatched = (observation.data_json or {}).get('dispatched')
                    send_state = 'not_started' if dispatched is False else (
                        'unknown' if dispatched is None else 'started')
        outcome = 'cancelled' if send_state == 'not_started' else 'failed'
        # settle opens its own transaction; apply the same transition here so
        # the run lock is not dropped between the read and the charge.
        return _apply_settle(session, run, row, outcome=outcome, send_state=send_state,
                             physical_attempts=int((row.physical_attempts or 0)),
                             observation_ref=ref)


def summarize(session, run):
    rows = list(session.scalars(select(PageReservation).where(PageReservation.run_id == run.id)))
    buckets = {name: 0 for name in ('reserved', 'settled', 'released', 'unknown')}
    for row in rows:
        buckets[row.state] = buckets.get(row.state, 0) + int(
            row.pages_reserved if row.state == 'reserved' else (row.pages_consumed or 0))
    stats = run.stats_json or {}
    limit = int((run.plan_json or {}).get('page_limit_total') or 0)
    consumed = int(stats.get('pages') or 0)
    reserved = int(stats.get('reserved_pages') or 0)
    return {
        'contract': CONTRACT,
        'page_limit_total': limit,
        'consumed': consumed,
        'reserved': reserved,
        'unsettled': buckets['reserved'],
        'unknown': buckets['unknown'],
        'settled_directory_pages': buckets['settled'],
        'released': buckets['released'],
        'within_budget': consumed + reserved <= limit,
        'automatic_shared_directory_collection': 'unavailable',
        'automatic_js_rendering': 'unavailable',
    }


def _reserve(session, run_id, body, *, use_cache):
    run = _require_contract(session, run_id)
    existing = _existing(session, run.id, body['operation_key'], body['attempt'])
    if existing is not None:
        _same_input(existing, body['input_hash'])
        return _public(existing, replay=True)
    _require_running(run)
    if use_cache:
        hit = _cache_hit(session, run.id, body['url'], body['page_type'])
        if hit is not None:
            row = _insert(session, run, body, pages=0, state='settled', cache_hit=True, cache_of=hit.id)
            row.pages_consumed = 0
            row.send_state = 'not_started'
            row.result = 'cache_hit'
            row.settled_at = utcnow()
            return _public(row, replay=False)
    _charge(session, run, body)
    row = _insert(session, run, body, pages=1, state='reserved', cache_hit=False, cache_of=None)
    row.send_state = 'not_started'
    _guard_total(run)
    return _public(row, replay=False)


def _charge(session, run, body):
    stats = dict(run.stats_json or {})
    limit = int((run.plan_json or {}).get('page_limit_total') or 0)
    used = int(stats.get('pages') or 0) + int(stats.get('reserved_pages') or 0)
    if used + 1 > limit:
        raise AppError('PAGE_BUDGET_EXCEEDED', '共享目录页面预算已用尽；停止新的目录派发', 409,
                       details={'scope': 'run', 'page_limit_total': limit,
                                'consumed': int(stats.get('pages') or 0),
                                'reserved': int(stats.get('reserved_pages') or 0)})
    if body['page_type'] == 'detail':
        held = _company_hold(session, run, body['company_key'])
        if held + 1 > COMPANY_PAGE_CAP:
            raise AppError('PAGE_BUDGET_EXCEEDED', '该企业的页面额度已用尽', 409,
                           details={'scope': 'company', 'company_key': body['company_key'],
                                    'company_pages': held, 'company_page_cap': COMPANY_PAGE_CAP})
        stats['directory_detail_holds'] = int(stats.get('directory_detail_holds') or 0) + 1
    stats['reserved_pages'] = int(stats.get('reserved_pages') or 0) + 1
    run.stats_json = stats


def _company_hold(session, run, company_key):
    job = session.scalar(select(Job).where(Job.run_id == run.id, Job.entity_key == company_key))
    used = reserved = 0
    if job is not None:
        output = job.output_json or {}
        used = int(output.get('pages_used') or 0)
        if job.state == 'running':
            reserved = int(output.get('reserved_pages') or 0)
    return used + reserved + directory_detail_pages(session, run.id, company_key)


def _close_for_budget(run_id):
    """Persist the stop after the refused reservation has rolled back."""
    with transaction() as session:
        run = _run(session, run_id)
        stats = run.stats_json or {}
        limit = int((run.plan_json or {}).get('page_limit_total') or 0)
        if int(stats.get('pages') or 0) + int(stats.get('reserved_pages') or 0) < limit:
            return
        plan = dict(run.plan_json or {})
        research = dict(plan.get('research') or {})
        research['input_open'] = False
        research['stop_reason'] = research.get('stop_reason') or 'page_budget'
        plan['research'] = research
        run.plan_json = plan


def _apply_settle(session, run, row, *, outcome, send_state, physical_attempts, observation_ref):
    if row.state != 'reserved':
        return _public(row, replay=True)
    stats = dict(run.stats_json or {})
    held = int(row.pages_reserved or 0)
    stats['reserved_pages'] = max(0, int(stats.get('reserved_pages') or 0) - held)
    if send_state == 'not_started':
        row.state = 'released'
        row.pages_consumed = 0
        row.result = 'cancelled'
        if row.page_type == 'detail' and not row.cache_hit:
            stats['directory_detail_holds'] = max(0, int(stats.get('directory_detail_holds') or 0) - held)
    else:
        row.state = 'unknown' if send_state == 'unknown' else 'settled'
        row.pages_consumed = held
        row.result = outcome
        stats['pages'] = int(stats.get('pages') or 0) + held
    row.send_state = send_state
    row.physical_attempts = physical_attempts
    row.observation_ref = observation_ref
    row.settled_at = utcnow()
    run.stats_json = stats
    _guard_total(run)
    return _public(row, replay=False)


def _insert(session, run, body, *, pages, state, cache_hit, cache_of):
    row = PageReservation(
        id=uid('page'), run_id=run.id, operation_key=body['operation_key'], attempt=body['attempt'],
        source=body['source'], page_type=body['page_type'], url=body['url'],
        company_key=body['company_key'], subjects_json=list(body['companies']),
        input_hash=body['input_hash'], pages_reserved=pages, pages_consumed=0 if cache_hit else None,
        state=state, cache_hit=cache_hit, cache_of=cache_of, physical_attempts=0)
    session.add(row)
    session.flush()
    return row


def _existing(session, run_id, operation_key, attempt):
    return session.scalar(select(PageReservation).where(
        PageReservation.run_id == run_id,
        PageReservation.operation_key == operation_key,
        PageReservation.attempt == attempt).with_for_update())


def _cache_hit(session, run_id, url, page_type):
    """A settled successful page in this run only. Never robots cache, never another run."""
    return session.scalar(select(PageReservation).where(
        PageReservation.run_id == run_id,
        PageReservation.url == url,
        PageReservation.page_type == page_type,
        PageReservation.cache_hit.is_(False),
        PageReservation.state == 'settled',
        PageReservation.result == 'ok').limit(1))


def _observation(session, run_id, operation_key, attempt):
    rows = session.scalars(select(Observation).where(
        Observation.run_id == run_id, Observation.kind == 'http'))
    matched = [row for row in rows
               if (row.data_json or {}).get('operation_key') == operation_key
               and (row.data_json or {}).get('attempt') == attempt]
    return matched[-1] if matched else None


def _require_contract(session, run_id):
    run = _run(session, run_id)
    if contract_of(run) != CONTRACT:
        raise AppError('PAGE_BUDGET_UNAVAILABLE',
                       '该批次没有冻结 shared-page-budget-1，不能记共享目录页面', 409,
                       details={'page_budget_contract': contract_of(run)})
    return run


def _require_running(run):
    if run.status == 'paused':
        raise AppError('RUN_PAUSED', '批次已暂停；共享目录页面不再派发', 409)
    if run.status != 'running':
        raise AppError('INVALID_STATE', '批次未在运行，不能预留共享目录页面', 409)


def _same_input(existing, digest):
    if existing.input_hash != digest:
        raise AppError('IDEMPOTENCY_CONFLICT', '同一页面操作键不能用于不同输入', 409)


def _guard_total(run):
    stats = run.stats_json or {}
    limit = int((run.plan_json or {}).get('page_limit_total') or 0)
    if int(stats.get('pages') or 0) + int(stats.get('reserved_pages') or 0) > limit:
        raise AppError('PAGE_BUDGET_EXCEEDED', '页面预留越过冻结总预算', 409)


def _checked_input(operation_key, url, page_type, attempt, companies, company_key, source):
    if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 160:
        raise AppError('INVALID_KEY', '页面操作键必须为 1 至 160 字符')
    if not isinstance(url, str) or not url.strip() or len(url) > 2000:
        raise AppError('VALIDATION_ERROR', '页面 URL 不能为空且不超过 2000 字符')
    if page_type not in _PAGE_TYPES:
        raise AppError('VALIDATION_ERROR', 'page_type 仅支持 list 或 detail')
    if source not in _SOURCES:
        raise AppError('VALIDATION_ERROR', '共享目录预算来源仅支持 shared_directory')
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 20:
        raise AppError('VALIDATION_ERROR', 'attempt 必须为 1 至 20 的整数')
    if page_type == 'detail':
        if not isinstance(company_key, str) or not company_key.strip():
            raise AppError('VALIDATION_ERROR', '详情页必须绑定企业')
        if companies:
            raise AppError('VALIDATION_ERROR', '详情页不接受同页企业列表')
    else:
        if company_key:
            raise AppError('VALIDATION_ERROR', '列表页按页计一次，不绑定单个企业额度')
    if companies is None:
        companies = []
    if not isinstance(companies, list) or len(companies) > 200 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in companies):
        raise AppError('VALIDATION_ERROR', '同页企业列表最多 200 个非空名称')
    companies = list(dict.fromkeys(item.strip() for item in companies))
    payload = {'url': url.strip(), 'page_type': page_type, 'companies': companies,
               'company_key': company_key.strip() if isinstance(company_key, str) else None,
               'source': source}
    return {**payload, 'operation_key': operation_key, 'attempt': attempt, 'input_hash': _hash(payload)}


def _public(row, *, replay):
    return {
        'id': row.id, 'run_id': row.run_id, 'operation_key': row.operation_key,
        'attempt': row.attempt, 'page_type': row.page_type, 'url': row.url,
        'company_key': row.company_key, 'subjects': list(row.subjects_json or []),
        'state': row.state, 'pages_reserved': row.pages_reserved,
        'pages_consumed': row.pages_consumed, 'cache_hit': bool(row.cache_hit),
        'cache_of': row.cache_of, 'send_state': row.send_state,
        'physical_attempts': row.physical_attempts, 'result': row.result,
        'input_hash': row.input_hash, 'replay': replay,
    }
