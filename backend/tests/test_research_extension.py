from datetime import timedelta

import pytest
from sqlalchemy import select

from app import runs
from app.errors import AppError
from app.persistence import transaction, utcnow
from app.persistence.models import Audit, Operation, Run


SPEC = {'spec_version': 1, 'name':'Research extension', 'industry':'freight forwarding', 'countries':['US'], 'quantity':200}


def test_extension_is_idempotent_and_preserves_scope_and_clock(clean_db):
    runs.create('longer', SPEC)
    with transaction() as session:
        session.get(Run, 'longer').active_started_at = utcnow() - timedelta(hours=7)
    before = runs.status('longer', action='pause')
    result = runs.extend_research('longer', hours=24, operation_key='user-continued')
    assert result['status'] == 'paused'
    assert result['spec'] == before['spec']
    assert result['research']['active_wall_seconds'] == before['research']['active_wall_seconds']
    assert result['research']['time_limit_seconds'] == 86400
    assert result['research']['candidate_limit'] == before['research']['candidate_limit'] == 600
    assert result['plan']['page_limit_total'] == before['plan']['page_limit_total']
    assert result['plan']['page_limit_per_company'] == before['plan']['page_limit_per_company']
    assert runs.extend_research('longer', hours=24, operation_key='user-continued') == result
    with transaction() as session:
        assert session.scalar(select(Audit).where(Audit.action == 'run.extend_research'))
        assert len(list(session.scalars(select(Operation).where(Operation.command == 'extend-research')))) == 1
    with pytest.raises(AppError) as exc:
        runs.extend_research('longer', hours=48, operation_key='user-continued')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('reason', ['closed', 'expired', 'candidate_limit', 'busy', 'finished'])
def test_extension_cannot_reopen_or_reset_research_constraints(clean_db, reason):
    runs.create('limited', SPEC)
    with transaction() as session:
        run = session.get(Run, 'limited')
        if reason == 'expired':
            run.active_started_at = utcnow() - timedelta(hours=9)
        elif reason == 'busy':
            run.process_token = 'processing'
        elif reason == 'finished':
            run.status = 'partial'
        else:
            state = dict(run.plan_json['research'])
            state['input_open' if reason == 'closed' else 'admitted_count'] = False if reason == 'closed' else 600
            run.plan_json = {**run.plan_json, 'research':state}
    with pytest.raises(AppError) as exc:
        runs.extend_research('limited', hours=24, operation_key='blocked')
    assert exc.value.code == ('PROCESS_BUSY' if reason == 'busy' else 'INVALID_STATE')
    with transaction() as session:
        assert session.get(Run, 'limited').plan_json['research']['time_limit_seconds'] == 28800
        assert not session.scalar(select(Operation).where(Operation.command == 'extend-research'))


@pytest.mark.parametrize('hours', [0, -1, 73, 24.5, True, '24'])
def test_extension_has_an_explicit_integer_hour_limit(clean_db, hours):
    with pytest.raises(AppError) as exc:
        runs.extend_research('unused', hours=hours, operation_key='invalid')
    assert exc.value.code == 'VALIDATION_ERROR'


def test_extension_only_increases_limit(clean_db):
    runs.create('increase', SPEC)
    for hours in (7, 8):
        with pytest.raises(AppError) as exc:
            runs.extend_research('increase', hours=hours, operation_key='hours-' + str(hours))
        assert exc.value.code == 'INVALID_EXTENSION'
