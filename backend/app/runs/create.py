"""批次创建、规范校验与离线预检（spec 2.6 S10 拆分目标 runs/create.py）。"""
import re
from app.config import settings
from app.errors import AppError
from app.release import release_identity
from app.persistence import (transaction, utcnow)
from app.persistence.models import Run
from app.schemas import parse_segment
from app.segment_brief import build_run_plan, historical_plan_matches, require_confirmation
from ._core import (_hash, _iso, _audit, _view)

_CONFLICT = '该 run 已用于不同客群或确认范围与已保存记录不一致；请使用 status 查看已冻结批次'


def create(run_id, spec, confirm_hash=None, **kwargs):
    """Create a run only when ``confirm_hash`` matches the current confirmation scope.

    The check runs before the transaction. There is no bypass that writes a run
    without a hash. A matching hash means the caller submitted the current
    config; it does not record who confirmed. Historical runs without
    ``segment_brief`` are returned only when the spec and the initial fixed
    plan still match, and this function does not invent a confirmation record
    for them.
    """
    del kwargs
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', run_id):
        raise AppError('INVALID_RUN_ID', 'run 仅支持 1 至 80 位字母、数字、点、横线和下划线')
    prepared = require_confirmation(spec, confirm_hash)
    spec, version = prepared['spec'], prepared['spec_version']
    spec_hash, confirm = prepared['spec_hash'], prepared['confirm_hash']
    with transaction() as session:
        # Lock even before the row exists to serialize concurrent create calls.
        from sqlalchemy import text
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'run:' + run_id})
        old = session.get(Run, run_id, with_for_update=True)
        if old:
            if old.spec_hash != spec_hash:
                raise AppError('IDEMPOTENCY_CONFLICT', _CONFLICT, 409)
            recorded = (old.plan_json or {}).get('segment_brief') or {}
            if recorded:
                if recorded.get('confirm_hash') != confirm or recorded.get('spec_hash') != spec_hash \
                        or recorded.get('brief_version') != prepared['confirmation_summary']['brief_version']:
                    raise AppError('IDEMPOTENCY_CONFLICT', _CONFLICT, 409)
                return _view(session, old)
            if historical_plan_matches(old.plan_json, spec, version):
                return _view(session, old)
            raise AppError('IDEMPOTENCY_CONFLICT', _CONFLICT, 409)
        plan = build_run_plan(spec, version, spec_hash=spec_hash, confirm_hash=confirm,
                              recorded_at=_iso(utcnow()), release=release_identity())
        run = Run(id=run_id, spec_json=spec, spec_hash=spec_hash, demo_only=settings.fixture_mode,
            active_started_at=utcnow(), plan_json=plan,
            stats_json={'pages': 0, 'duplicates': 0, 'active_seconds': 0, 'paused_seconds': 0})
        session.add(run)
        session.flush()
        _audit(session, run.id, 'run.created', spec_version=version,
               spec_hash=run.spec_hash, confirm_hash=confirm,
               brief_version=plan['segment_brief']['brief_version'],
               rules_version=plan.get('rules_version'), release=plan['release'])
        return _view(session, run)


def _identity(candidate, version=2):
    from app.rules.identity import identity_key
    return identity_key(candidate, version=version)


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
