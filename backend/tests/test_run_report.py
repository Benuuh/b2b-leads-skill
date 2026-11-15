"""Execution cost report: counters, method estimate, tokens and file location."""
import json

from app import runs
from app.config import settings
from app.runs import report as run_report


def view(**overrides):
    return {
        'id': 'run-fixture', 'status': 'partial',
        'spec': {'name': '示例批次', 'industry': 'freight forwarding', 'countries': ['US'],
                 'quantity': 200, 'spec_version': 2},
        'plan': {},
        'research': {'active_wall_seconds': 300.0, 'discovered': 12, 'matched': 9,
                     'contactable_count': 7},
        'stats': {'pages': 40, 'paused_seconds': 120.0, 'append_ready': 15},
        'created_at': '2026-11-15T01:00:00+00:00',
        'finished_at': '2026-11-15T01:10:00+00:00',
        **overrides,
    }


def test_report_has_the_required_counters_and_no_guessed_tokens(monkeypatch):
    monkeypatch.delenv(run_report.TOKEN_ENV, raising=False)
    result = run_report.from_view(view(), exported=6)
    assert result['report_version'] == 'run-report-1'
    assert result['elapsed_wall_seconds'] == 600
    assert result['actual']['hours'] == 0.167
    assert result['active_seconds'] == 300.0 and result['paused_seconds'] == 120.0
    assert (result['candidates'], result['admitted'], result['qualified'],
            result['contactable'], result['exported']) == (15, 12, 9, 7, 6)
    assert result['pages'] == 40
    assert result['actual']['tokens'] is None
    assert result['token_source'] == 'unavailable'
    assert result['estimate']['method'] == 'per_company'
    assert result['estimate']['tokens'] == {'low': 2400000, 'high': 4000000}
    assert result['summary_text'].startswith('总用时 10 分 0.0 秒；总 token 未取到')
    assert '未用估算冒充实测' in result['summary_text']


def test_runtime_tokens_keep_scope_and_total(monkeypatch):
    monkeypatch.setenv(run_report.TOKEN_ENV, json.dumps(
        {'input': 1000, 'output': 200, 'cache': 50, 'token_scope': 'whole run'}))
    result = run_report.from_view(view())
    assert result['actual']['tokens'] == {'input': 1000, 'output': 200, 'cache': 50,
                                          'total': 1200, 'token_scope': 'whole run'}
    assert result['token_source'] == 'runtime'
    assert result['token_scope'] == 'whole run'
    assert '1200' in result['summary_text'] and 'whole run' in result['summary_text']


def test_invalid_or_empty_token_payload_is_not_guessed(monkeypatch):
    monkeypatch.setenv(run_report.TOKEN_ENV, 'not-json')
    assert run_report.from_view(view())['token_source'] == 'unavailable'
    monkeypatch.setenv(run_report.TOKEN_ENV, json.dumps({'token_scope': 'whole run'}))
    assert run_report.from_view(view())['actual']['tokens'] is None


def test_publish_writes_a_plain_file_beside_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'artifact_root', tmp_path)
    monkeypatch.delenv(run_report.TOKEN_ENV, raising=False)
    report = run_report.from_view(view())
    run_report.publish(report)
    path = tmp_path / 'reports' / 'run-run-fixture.json'
    assert path.is_file()
    stored = json.loads(path.read_text(encoding='utf-8'))
    assert stored['report_version'] == 'run-report-1'
    assert stored['path'] == str(path)
    assert run_report.read_report('run-fixture')['run_id'] == 'run-fixture'


def test_finish_and_export_write_the_report(clean_db, packaging_spec, packaging_candidate,
                                            monkeypatch):
    import app.web.crawler as crawler
    import app.web.http as web_http

    def forbidden(*args, **kwargs):
        raise AssertionError('saved evidence must not trigger network work')

    monkeypatch.setattr(crawler, 'safe_http', forbidden)
    monkeypatch.setattr(web_http, 'safe_http', forbidden)
    monkeypatch.delenv(run_report.TOKEN_ENV, raising=False)
    # The fixture database marks created runs as demo; a real delivery export
    # needs the non-demo path, which this test opts into explicitly.
    monkeypatch.setattr(settings, 'fixture_mode', False)
    runs.create('report-flow', packaging_spec)
    runs.append('report-flow', [packaging_candidate])
    finished = runs.finish('report-flow')
    report = finished['run_report']
    assert report['run_id'] == 'report-flow'
    assert report['token_source'] == 'unavailable'
    assert report['qualified'] >= 1
    assert (settings.artifact_root / 'reports' / 'run-report-flow.json').is_file()
    exported = runs.export('report-flow')
    manifest_report = exported['manifest']['run_report']
    assert manifest_report['exported'] == 1
    assert manifest_report['contactable'] >= 1
    assert manifest_report['summary_text'].startswith('总用时 ')
