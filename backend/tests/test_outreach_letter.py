"""Spec b2b-leads-2.6 §3.2 开发信 skill：outreach-letter。"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'skills' / 'outreach-letter' / 'scripts' / 'generate.py'

EVIDENCE = [{'field': 'offering.services', 'value': 'air freight',
             'source_url': 'https://example.com/services',
             'excerpt': 'we operate daily air freight charters from Leipzig'}]


def letter(company_id, role='procurement', stage='first_touch', evidence=None, name=None):
    return {'company_id': company_id, 'company_name': name or f'Company {company_id}',
            'evidence': EVIDENCE if evidence is None else evidence,
            'contact': {'name': 'Anna', 'role': role}, 'stage': stage}


def run_cli(payload, tmp_path, check_only=False):
    input_path = tmp_path / 'input.json'
    input_path.write_text(json.dumps(payload), encoding='utf-8')
    command = [sys.executable, str(SCRIPT), '--input', str(input_path),
               '--out-dir', str(tmp_path / 'letters')]
    if check_only:
        command.append('--check-only')
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8')


def test_no_evidence_never_generates(tmp_path):
    result = run_cli(letter('c1', evidence=[]), tmp_path)
    payload = json.loads(result.stdout)
    assert payload['status'] == 'ok' and payload['count'] == 0
    assert payload['failures'][0]['status'] == 'insufficient_evidence'
    assert payload['failures'][0]['missing'] == ['evidence[].source_url', 'evidence[].excerpt']


def test_letter_cites_evidence_and_role_stage(tmp_path):
    result = run_cli([letter('c1'), letter('c2', role='owner', stage='after_quote')], tmp_path)
    payload = json.loads(result.stdout)
    assert payload['status'] == 'ok' and payload['count'] == 2
    for path in payload['written']:
        document = Path(path).read_text(encoding='utf-8')
        assert '- 收件人：' in document and '- 阶段：' in document
        assert '引用证据：https://example.com/services' in document
        assert 'charters' in document.split('## 正文')[1], '正文必须能看出引用的哪条证据'
        assert '保证' not in document and '100%' not in document


def test_batch_ngram_uniqueness_enforced(tmp_path):
    # 同一批次里两封信正文若共享 12 字片段，整批拒绝。
    shared = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike'
    evidence_a = [{'field': 'x', 'value': 'y', 'source_url': 'https://a.example.com',
                   'excerpt': shared}]
    evidence_b = [{'field': 'x', 'value': 'y', 'source_url': 'https://b.example.com',
                   'excerpt': shared}]
    result = run_cli([letter('ca', evidence=evidence_a), letter('cb', evidence=evidence_b)],
                     tmp_path)
    payload = json.loads(result.stdout)
    assert payload['status'] == 'duplicate_ngrams' and payload['duplicates']


def test_twelve_letters_all_roles_stages(tmp_path):
    roles = ('procurement', 'owner', 'technical', 'unknown')
    stages = ('first_touch', 'follow_up', 'after_quote')
    batch = [letter(f'c-{role}-{stage}', role=role, stage=stage)
             for role in roles for stage in stages]
    result = run_cli(batch, tmp_path)
    payload = json.loads(result.stdout)
    assert payload['status'] == 'ok' and payload['count'] == 12
    assert payload['failures'] == []


def test_forbidden_promise_rejected(tmp_path):
    # 模板本身不承诺；若证据摘录含承诺措辞也拒绝。
    evidence = [{'field': 'x', 'value': 'y', 'source_url': 'https://a.example.com',
                 'excerpt': 'guarantee lowest price always'}]
    result = run_cli(letter('cp', evidence=evidence), tmp_path)
    payload = json.loads(result.stdout)
    assert payload['failures'][0]['status'] == 'invalid_letter'
