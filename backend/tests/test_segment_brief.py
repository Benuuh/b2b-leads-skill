"""Slice A: default spec v2, offline brief, and confirm-before-create.

These checks run against the isolated PostgreSQL fixture. A skip is not acceptance.
"""
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from importlib import import_module

import pytest
from pydantic import ValidationError

from app import cli, runs
from app.config import ROOT
from app.errors import AppError
from app.persistence import transaction, utcnow
from app.persistence.models import Company, Run
from sqlalchemy import select
from app.schemas import parse_segment
from app.segment_brief import inspect_segment, require_confirmation


def _new_spec(**extra):
    body = {'name': '新客群', 'industry': 'industrial packaging', 'countries': ['DE'], 'quantity': 50}
    body.update(extra)
    return body


def _v1_spec(**extra):
    body = {'spec_version': 1, 'name': '美国货代', 'industry': 'freight forwarding',
            'countries': ['US'], 'quantity': 50}
    body.update(extra)
    return body


def _candidate():
    return {'company_name': 'Harbor Logistics LLC', 'country_code': 'US',
            'industry_text': 'freight forwarding', 'website_url': 'https://harbor.example.org',
            'source_url': 'https://directory.example.org/harbor', 'source_type': 'directory',
            'source_excerpt': 'Harbor Logistics LLC provides freight forwarding services in the US',
            'retrieved_at': datetime.now(timezone.utc).isoformat()}


def _legacy_plan(spec, version):
    return {
        'execution_mode': 'skill_research', 'completion_goal': 'contactable',
        'page_limit_per_company': 10, 'page_limit_total': 10000,
        'request_observation_protocol': 'observed-2',
        'lookup_observation_protocol': 'observed-2', 'timing_protocol': 'observed-1',
        'release': {'core_version': '2.7.0', 'skill_version': '2.7.0'},
        'spec_version': version,
        'research': {
            'input_open': True, 'candidate_limit': spec['quantity'] * 3,
            'time_limit_seconds': 28800, 'admitted_count': 0, 'checkpoint': {},
            'external_tool_usage': {}, 'stop_reason': None,
        },
    }


def test_unversioned_new_spec_freezes_as_v2(clean_db):
    raw = _new_spec()
    assert 'spec_version' not in raw
    prepared = inspect_segment(raw)
    assert prepared['spec_version'] == 2
    assert prepared['spec']['spec_version'] == 2
    assert prepared['spec']['qualification_policy']['derived_from'] == 'v1_fields'
    created = runs.create('new-v2', raw, confirm_hash=prepared['confirm_hash'])
    assert created['spec_version'] == 2
    assert created['spec'] == prepared['spec']
    assert created['plan']['spec_version'] == 2
    assert created['plan']['rules_version'] == 'b2b-policy-2'
    assert created['plan']['initial_limits']['time_limit_seconds'] == 28800
    assert created['plan']['initial_limits']['candidate_limit'] == 150
    assert created['plan']['research']['candidate_limit'] == created['spec']['quantity'] * 3
    assert created['segment_brief']['confirm_hash'] == prepared['confirm_hash']
    assert created['segment_brief']['spec_hash'] == prepared['spec_hash']
    assert created['time_limits']['initial_recorded'] is True
    with transaction() as session:
        stored = session.get(Run, 'new-v2')
        assert stored.spec_json['spec_version'] == 2
        assert stored.spec_hash == prepared['spec_hash']
        assert 'segment_brief' in stored.plan_json


def test_explicit_v1_still_freezes_the_historical_shape(clean_db):
    raw = _v1_spec()
    prepared = inspect_segment(raw)
    assert prepared['spec_version'] == 1
    assert prepared['confirmation_summary']['rule_version'] == 'v1-historical'
    created = runs.create('explicit-v1', raw, confirm_hash=prepared['confirm_hash'])
    assert created['spec_version'] == 1
    assert 'spec_version' not in created['spec']
    assert 'qualification_policy' not in created['spec']
    assert 'policy' not in created
    assert created['plan']['spec_version'] == 1
    assert 'rules_version' not in created['plan']
    assert 'execution' not in created['plan']
    again = runs.create('explicit-v1', raw, confirm_hash=prepared['confirm_hash'])
    assert again['id'] == created['id']
    assert again['segment_brief']['confirmation_recorded_at'] == created['segment_brief']['confirmation_recorded_at']
    assert again['created_at'] == created['created_at']


def test_historical_v1_run_is_not_rejudged(clean_db, monkeypatch):
    import app.rules.policy as policy
    raw = _v1_spec()
    frozen, version = parse_segment(raw)
    assert version == 1 and 'spec_version' not in frozen
    calls = {'policy': 0}

    def explode(*args, **kwargs):
        calls['policy'] += 1
        raise AssertionError('v2 policy must not judge a frozen v1 run')

    monkeypatch.setattr(policy, 'assess_policy', explode)
    monkeypatch.setattr(policy, 'assess_policy_cached', explode)
    with transaction() as session:
        session.add(Run(
            id='historical-v1', spec_json=frozen, spec_hash=runs._hash(frozen),
            demo_only=True, active_started_at=utcnow(),
            plan_json=_legacy_plan(frozen, 1),
            stats_json={'pages': 0, 'duplicates': 0, 'active_seconds': 0, 'paused_seconds': 0},
        ))
    runs.append('historical-v1', [_candidate()])
    outcome = runs.process('historical-v1', max_jobs=1)
    assert calls['policy'] == 0
    assert outcome['research']['spec_version'] == 1
    with transaction() as session:
        stored_run = session.get(Run, 'historical-v1')
        stored_run.demo_only = False
        for company in session.scalars(select(Company).where(Company.run_id == stored_run.id)):
            company.data_json = {**company.data_json, 'demo_only': False}
    status = runs.status('historical-v1')
    assert status['spec_version'] == 1
    assert status['segment_brief'] == {'recorded': False, 'message': '历史批次，无确认记录'}
    assert 'policy' not in status
    exported = runs.export('historical-v1')
    assert exported['manifest']['spec_version'] == 1
    assert 'policy' not in exported['manifest']
    with transaction() as session:
        stored = session.get(Run, 'historical-v1')
        assert 'segment_brief' not in stored.plan_json
        assert stored.spec_json == frozen


def test_confirm_hash_is_stable_for_equivalent_input(clean_db):
    raw = _new_spec(keywords=['packaging'], notes='公开目录')
    first = inspect_segment(raw)
    reordered = {key: raw[key] for key in reversed(list(raw))}
    second = inspect_segment(reordered)
    assert first['spec_hash'] == second['spec_hash']
    assert first['confirm_hash'] == second['confirm_hash']
    assert first['confirmation_summary']['fields']
    created = runs.create('stable', raw, confirm_hash=first['confirm_hash'])
    budget = first['confirmation_summary']['effective_budget']
    assert created['plan']['research']['time_limit_seconds'] == budget['time_limit_seconds'] == 28800
    assert created['plan']['research']['candidate_limit'] == budget['candidate_limit']
    assert created['plan']['page_limit_per_company'] == budget['page_limit_per_company'] == 10
    assert created['plan']['page_limit_total'] == budget['page_limit_total'] == 10000
    assert created['spec']['quantity'] == budget['quantity']
    # The hash covers the canonical material, not the Chinese prose around it.
    from app.segment_brief import confirm_material
    from app.runs._core import _hash
    material = confirm_material(first['spec'], first['spec_version'], first['spec_hash'])
    assert _hash(material) == first['confirm_hash']
    assert '确认' not in json.dumps(material, ensure_ascii=False)


def test_changed_config_requires_a_new_confirmation(clean_db):
    raw = _new_spec()
    original = inspect_segment(raw)
    wider = _new_spec(quantity=80)
    updated = inspect_segment(wider)
    assert updated['confirm_hash'] != original['confirm_hash']
    assert updated['spec_hash'] != original['spec_hash']
    with pytest.raises(AppError) as exc:
        runs.create('fresh', raw, confirm_hash=updated['confirm_hash'])
    assert exc.value.code == 'SEGMENT_NOT_CONFIRMED'
    assert exc.value.status == 409
    assert exc.value.details['confirm_hash'] == original['confirm_hash']
    assert 'fields' not in exc.value.details
    with transaction() as session:
        assert session.get(Run, 'fresh') is None
    created = runs.create('fresh', raw, confirm_hash=original['confirm_hash'])
    assert created['spec']['quantity'] == 50
    with pytest.raises(AppError) as exc:
        runs.create('fresh', wider, confirm_hash=updated['confirm_hash'])
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    assert 'status' in exc.value.message
    with transaction() as session:
        assert session.get(Run, 'fresh').spec_json['quantity'] == 50
        assert session.get(Run, 'fresh').plan_json['segment_brief']['confirm_hash'] == original['confirm_hash']


def test_same_spec_with_a_different_saved_scope_conflicts(clean_db):
    raw = _new_spec()
    prepared = inspect_segment(raw)
    runs.create('scoped', raw, confirm_hash=prepared['confirm_hash'])
    with transaction() as session:
        run = session.get(Run, 'scoped')
        plan = dict(run.plan_json)
        plan['segment_brief'] = {**plan['segment_brief'], 'confirm_hash': 'f' * 64}
        run.plan_json = plan
    with pytest.raises(AppError) as exc:
        runs.create('scoped', raw, confirm_hash=prepared['confirm_hash'])
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    with transaction() as session:
        assert session.get(Run, 'scoped').plan_json['segment_brief']['confirm_hash'] == 'f' * 64


@pytest.mark.no_auto_confirm
def test_core_create_rejects_a_missing_hash_without_a_row(clean_db, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError('confirmation failure must not open a transaction')

    create_module = import_module('app.runs.create')
    monkeypatch.setattr(create_module, 'transaction', boom)
    create = create_module.create
    with pytest.raises(AppError) as exc:
        create('missing-hash', _new_spec())
    assert exc.value.code == 'SEGMENT_NOT_CONFIRMED'
    assert len(exc.value.details['confirm_hash']) == 64
    with pytest.raises(AppError) as exc:
        create('missing-hash', _new_spec(), confirm_hash='0' * 64)
    assert exc.value.code == 'SEGMENT_NOT_CONFIRMED'
    with pytest.raises(ValidationError):
        require_confirmation({'name': 'x', 'countries': ['NOPE']}, None)


def test_cli_checks_confirmation_before_any_side_effect(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError('create must validate before prepare, maintenance, or secrets')

    monkeypatch.setattr('app.runtime.prepare', boom)
    monkeypatch.setattr('app.runs.maintenance', boom)
    monkeypatch.setattr('app.secrets.validate_startup_secrets', boom)
    monkeypatch.setattr('app.persistence.transaction', boom)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(_new_spec()), encoding='utf-8')
    assert cli.main(['create', '--run', 'sample', '--spec-json', str(path)]) == 1
    failure = json.loads(capsys.readouterr().out)['error']
    assert failure['code'] == 'SEGMENT_NOT_CONFIRMED'
    assert len(failure['details']['confirm_hash']) == 64
    assert cli.main(['create', '--run', 'sample', '--spec-json', str(path),
                     '--confirm-hash', 'ab']) == 1
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'SEGMENT_NOT_CONFIRMED'


def test_real_wrapper_rejects_confirmation_without_a_database(tmp_path):
    env = {**os.environ, 'SCA_DATABASE_URL': 'postgresql://b2b:b2b@127.0.0.1:1/no_such_db',
           'SCA_PROJECT_ROOT': str(ROOT)}
    path = tmp_path / 'spec.json'
    body = _new_spec()
    path.write_text(json.dumps(body), encoding='utf-8')
    script = str(ROOT / 'scripts' / 'b2b_leads.py')

    def invoke(*args):
        return subprocess.run([sys.executable, script, *args], capture_output=True, env=env,
                              cwd=ROOT, timeout=30)

    missing = invoke('create', '--run', 'wrapper', '--spec-json', str(path))
    assert missing.returncode == 1, missing.stderr.decode()
    payload = json.loads(missing.stdout)
    assert payload['error']['code'] == 'SEGMENT_NOT_CONFIRMED'
    brief = invoke('brief', '--spec-json', str(path))
    assert brief.returncode == 0, brief.stderr.decode()
    brief_body = json.loads(brief.stdout)
    assert brief_body['confirm_hash'] == payload['error']['details']['confirm_hash']
    assert brief_body['validation'] == {'ok': True, 'errors': []}
    again = invoke('brief', '--spec-json', str(path))
    assert json.loads(again.stdout)['confirm_hash'] == brief_body['confirm_hash']
    wrong = invoke('create', '--run', 'wrapper', '--spec-json', str(path), '--confirm-hash', '0' * 64)
    assert json.loads(wrong.stdout)['error']['code'] == 'SEGMENT_NOT_CONFIRMED'
    invalid = tmp_path / 'bad.json'
    invalid.write_text(json.dumps({'name': 'x', 'countries': ['NOPE']}), encoding='utf-8')
    rejected = invoke('create', '--run', 'wrapper', '--spec-json', str(invalid), '--confirm-hash', '0' * 64)
    error = json.loads(rejected.stdout)['error']
    assert error['code'] == 'VALIDATION_ERROR'
    assert 'confirm_hash' not in error


def test_brief_command_stays_offline(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError('brief must not touch the database or the network')

    monkeypatch.setattr('app.persistence.transaction', boom)
    monkeypatch.setattr('app.runtime.prepare', boom)
    monkeypatch.setattr('app.runs.maintenance', boom)
    monkeypatch.setattr(socket, 'getaddrinfo', boom)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(_new_spec()), encoding='utf-8')
    assert cli.main(['brief', '--spec-json', str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['spec_version'] == 2
    assert payload['defaults_applied']
    summary = payload['confirmation_summary']
    rendered = json.dumps(summary, ensure_ascii=False)
    for phrase in ('无自动 JS 渲染', '不等于目录自动采集', '拒绝自动共享目录采集', '字面匹配', '未覆盖东南亚', '不承诺'):
        assert phrase in rendered
    assert '保证找满' not in rendered and '将在 8 小时内' not in rendered
    assert summary['evidence_limits']['codes']['structured_discovery_southeast_asia'] == 'not_covered'
    assert summary['global_suppression'].startswith('另适用当前全局抑制名单')


def test_summary_hides_exclusion_identifiers_and_maps_fields(clean_db):
    raw = _new_spec(exclusions=['hidden-buyer@example.com', 'Secret Buyer LLC'])
    prepared = inspect_segment(raw)
    rendered = json.dumps(prepared['confirmation_summary'], ensure_ascii=False)
    assert 'hidden-buyer@example.com' not in rendered
    assert 'Secret Buyer LLC' not in rendered
    file_note = prepared['confirmation_summary']['customer_file_exclusion']
    assert file_note['in_effect'] is False
    assert file_note['exclusions_count'] == 2
    assert file_note['exclusions_sha256']
    paths = {item['path'] for item in prepared['confirmation_summary']['fields']}
    assert 'countries' in paths and 'fixed_plan.time_limit_seconds' in paths
    assert 'execution_policy.native_inflight' in paths
    with pytest.raises(AppError) as exc:
        from app.segment_brief import _fields
        _fields({'not_registered': True}, 1, {'fixed_plan': {}})
    assert exc.value.code == 'BRIEF_MAPPING_GAP'


def test_historical_replay_returns_the_run_without_forging_a_record(clean_db):
    raw = _v1_spec()
    frozen, version = parse_segment(raw)
    prepared = inspect_segment(raw)
    with transaction() as session:
        session.add(Run(
            id='old-replay', spec_json=frozen, spec_hash=runs._hash(frozen),
            demo_only=True, active_started_at=utcnow(),
            plan_json=_legacy_plan(frozen, version),
            stats_json={'pages': 0, 'duplicates': 0, 'active_seconds': 0, 'paused_seconds': 0},
        ))
        started = session.get(Run, 'old-replay').active_started_at
    viewed = runs.create('old-replay', raw, confirm_hash=prepared['confirm_hash'])
    assert viewed['id'] == 'old-replay'
    assert viewed['segment_brief']['message'] == '历史批次，无确认记录'
    with transaction() as session:
        stored = session.get(Run, 'old-replay')
        assert 'segment_brief' not in stored.plan_json
        assert stored.active_started_at == started
        plan = dict(stored.plan_json)
        plan['research'] = {**plan['research'], 'time_limit_seconds': 86400}
        stored.plan_json = plan
    with pytest.raises(AppError) as exc:
        runs.create('old-replay', raw, confirm_hash=prepared['confirm_hash'])
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    assert 'status' in exc.value.message


def test_extend_research_keeps_the_original_confirmation(clean_db):
    raw = _new_spec(quantity=200)
    prepared = inspect_segment(raw)
    created = runs.create('extend-me', raw, confirm_hash=prepared['confirm_hash'])
    extended = runs.extend_research('extend-me', hours=24, operation_key='user-continued')
    assert extended['time_limits']['initial_seconds'] == 28800
    assert extended['time_limits']['current_seconds'] == 86400
    assert extended['research']['time_limit_seconds'] == 86400
    assert extended['segment_brief'] == created['segment_brief']
    assert extended['plan']['segment_brief'] == created['plan']['segment_brief']
    assert extended['plan']['initial_limits']['time_limit_seconds'] == 28800


def test_concurrent_create_with_the_same_confirmation_is_one_run(clean_db):
    raw = _new_spec()
    prepared = inspect_segment(raw)
    barrier = threading.Barrier(2)
    found = []
    errors = []

    def attempt():
        try:
            barrier.wait(timeout=5)
            found.append(runs.create('race', raw, confirm_hash=prepared['confirm_hash']))
        except Exception as exc:  # noqa: BLE001 - the assertion below reports the thread failure
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert len(found) == 2
    assert found[0]['id'] == found[1]['id'] == 'race'
    with transaction() as session:
        assert session.get(Run, 'race').spec_hash == prepared['spec_hash']


def test_resume_export_and_live_do_not_require_confirmation(clean_db, monkeypatch):
    import app.pipeline
    raw = _v1_spec()
    prepared = inspect_segment(raw)
    runs.create('continue', raw, confirm_hash=prepared['confirm_hash'])

    def record(task, pages):
        assert task['segment'].get('spec_version', 1) == 1
        return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
                    domain='harbor.example.org', country_code='US', status='qualified',
                    qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    contacts=[], sources=[], demo_only=False,
                    pipeline={'stop_reason': 'contact_target_reached'})

    monkeypatch.setattr(app.pipeline, 'process_company', record)
    runs.append('continue', [_candidate()])
    assert runs.status('continue', action='pause')['status'] == 'paused'
    assert runs.process('continue', max_jobs=1, resume=True)['processed_jobs'] == 1
    with transaction() as session:
        session.get(Run, 'continue').demo_only = False
    assert runs.export('continue')['manifest']['spec_version'] == 1
    import argparse
    subparsers = next(action for action in cli.parser()._actions
                      if isinstance(action, argparse._SubParsersAction))
    for name in ('advance', 'export', 'extend-research', 'status', 'append', 'process'):
        assert '--confirm-hash' not in subparsers.choices[name].format_help()
    assert '--confirm-hash' in subparsers.choices['create'].format_help()
