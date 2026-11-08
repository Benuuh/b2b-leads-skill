"""批次创建、规范校验与离线预检（spec 2.6 S10 拆分目标 runs/create.py）。"""
import re
from app.config import settings
from app.errors import AppError
from app.release import release_identity
from app.persistence import (transaction, utcnow)
from app.persistence.models import Run
from app.schemas import parse_segment
from ._core import (_hash, _audit, _view)


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
