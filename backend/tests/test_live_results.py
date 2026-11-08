"""AC-01..AC-07: committed-state page, export parity, security and lifecycle."""
import copy
import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from app import cli, runs
from app.live import projection as live
from app.live import viewer
from app.persistence import transaction, utcnow
from app.persistence.models import Run


def _variants(candidate, count=4):
    values = []
    for index in range(count):
        value = copy.deepcopy(candidate)
        name = 'Klarwerk Verpackungen %d GmbH' % index
        email = 'sales@klarwerk-%d.de' % index
        url = 'https://packaging-association.org/members/klarwerk-%d' % index
        value['company_name'] = name
        value['email'] = email
        value['source_url'] = url
        value['source_excerpt'] = '%s, Düsseldorf, Germany, manufactures reusable industrial packaging.' % name
        for source in value['sources']:
            source['source_url'] = url
            source['source_excerpt'] = source['source_excerpt'].replace('Klarwerk Verpackungen GmbH', name)
            if source['field'] == 'email':
                source['value'] = email
                source['source_excerpt'] = '%s — Business email: %s' % (name, email)
        values.append(value)
    return values


def _uncertain(candidate):
    value = copy.deepcopy(candidate)
    name = 'Klarwerk Prüfung GmbH'
    value['company_name'] = name
    value['email'] = 'info@klarwerk-pruefung.de'
    value['source_excerpt'] = '%s, Düsseldorf, Germany, offers industrial services.' % name
    value.pop('offering_products', None)
    value['sources'] = [source for source in value['sources'] if source['field'] != 'offering.products']
    for source in value['sources']:
        source['source_excerpt'] = source['source_excerpt'].replace('Klarwerk Verpackungen GmbH', name)
        if source['field'] == 'email':
            source['value'] = value['email']
            source['source_excerpt'] = '%s — Business email: %s' % (name, value['email'])
    return value


def _prepare(run_id, spec, candidates):
    runs.create(run_id, copy.deepcopy(spec))
    with transaction() as session:
        session.get(Run, run_id).demo_only = False
    runs.append(run_id, candidates)
    runs.process(run_id, max_jobs=20, workers=1)
    return runs.status(run_id)


@pytest.fixture
def live_run(clean_db, packaging_spec, packaging_candidate):
    status = _prepare('live-parity', packaging_spec, _variants(packaging_candidate) + [_uncertain(packaging_candidate)])
    assert status['summary']['contactable'] == 4
    return status


def test_projection_matches_core_selection_and_formal_export(live_run):
    page = live.build_projection('live-parity')
    counts = page['counts']
    assert counts['target'] == 200
    assert counts['admitted'] == 5
    assert counts['deliverable'] == 4
    assert counts['pending'] == 1
    assert counts['shortfall'] == 196
    assert counts['discovered'] is None and counts['discovered_note']
    assert page['consistency'] == {'core_contactable': 4, 'page_deliverable': 4, 'match': True}
    assert page['run']['status'] == 'running'
    assert page['run']['status_label'] == '运行中'

    deliverable_ids = {item['company_id'] for item in page['deliverables']}
    pending_ids = {item['company_id'] for item in page['pending']}
    assert len(deliverable_ids) == 4 and len(pending_ids) == 1
    assert not deliverable_ids & pending_ids
    for item in page['deliverables']:
        assert item['status'] == 'deliverable'
        assert item['channels'], item['company_name']
        assert item['sources'] and all(source['url'].startswith('https://') for source in item['sources'])
        assert item['pending_reasons'] == []
    pending = page['pending'][0]
    assert pending['status'] == 'pending'
    assert pending['pending_reasons'], pending
    assert any('缺少事实' in reason for reason in pending['pending_reasons'])

    exported = runs.export('live-parity')
    assert exported['manifest']['row_counts']['companies'] == counts['exportable'] == 4
    assert exported['manifest']['delivery']['policy_version']
    assert exported['snapshot_at']
    from openpyxl import load_workbook
    workbook = load_workbook(exported['file'])
    names = {row[1] for row in workbook['Companies'].values if row and row[0] != '企业ID'}
    assert pending['company_name'] not in names
    assert len(names) == 4


def test_page_filters_match_the_core_filtered_export(live_run):
    page = live.build_projection('live-parity')
    cases = [
        ({'q': 'Klarwerk Verpackungen 2'}, 1),
        ({'countries': ['DE']}, 4),
        ({'channels': ['email']}, 4),
        ({'q': 'no-such-company'}, 0),
    ]
    for filters, expected in cases:
        preview = len(live.filter_items(page['deliverables'], filters))
        assert preview == expected, filters
        exported = runs.export('live-parity', filters=filters)
        assert exported['manifest']['row_counts']['companies'] == preview, filters
        recorded = exported['manifest']['filters']
        assert recorded.get('q', '').lower() == filters.get('q', '').lower()
        assert recorded.get('channels') == filters.get('channels') or filters.get('channels') is None
        assert exported['manifest']['delivery']['policy_version']


def test_projection_write_is_atomic_idempotent_and_bounded(live_run):
    path = live.projection_path('live-parity')
    if path.exists():
        path.unlink()
    first = live.write_projection('live-parity')
    assert first['changed'] is True and path.is_file()
    stamp = path.stat().st_mtime_ns
    before = path.read_bytes()
    second = live.write_projection('live-parity')
    assert second['changed'] is False
    assert path.stat().st_mtime_ns == stamp
    assert path.read_bytes() == before
    assert not list(path.parent.glob('.projection.*.tmp'))
    saved = json.loads(before)
    assert saved['revision'] == live.build_projection('live-parity')['revision']


def test_interrupted_replace_keeps_the_previous_page(live_run, monkeypatch):
    live.write_projection('live-parity')
    path = live.projection_path('live-parity')
    before = path.read_bytes()
    changed = live.build_projection('live-parity')
    changed['counts']['admitted'] += 1
    changed['revision'] = live._revision(changed)
    def blocked(*args, **kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', blocked)
    with pytest.raises(OSError):
        live.write_projection('live-parity', changed)
    assert path.read_bytes() == before
    assert json.loads(before)['projection_version'] == live.PROJECTION_VERSION
    assert not list(path.parent.glob('.projection.*.tmp'))


def test_failed_rebuild_marks_the_page_as_possibly_stale(live_run, monkeypatch):
    live.refresh('live-parity', force=True)
    assert live.read_state('live-parity')['pending'] is None

    def broken(*args, **kwargs):
        raise RuntimeError('projection failed')
    monkeypatch.setattr(live, 'build_projection', broken)
    result = live.refresh('live-parity', force=True)
    assert result['error'] == 'RuntimeError'
    state = live.read_state('live-parity')
    assert state['pending'] and state['pending']['code'] == 'RuntimeError'
    assert state['projection'] is not None  # the old page remains readable

    monkeypatch.undo()
    assert live.refresh('live-parity', force=True)['changed'] is False
    assert live.read_state('live-parity')['pending'] is None


def test_stale_running_status_is_reported_as_unknown(live_run):
    page = live.build_projection('live-parity')
    fresh = live.status_view(page)
    assert fresh['value'] == 'running' and fresh['label'] == '运行中'
    old = copy.deepcopy(page)
    old['generated_at'] = '2026-01-01T00:00:00+00:00'
    stale = live.status_view(old)
    assert stale['value'] == 'unknown' and stale['label'] == '未知' and stale['stale'] is True
    assert '最后更新于' in stale['note']
    paused = copy.deepcopy(old)
    paused['run']['status'] = 'paused'
    assert live.status_view(paused)['value'] == 'paused'
    assert live.status_view(None)['value'] == 'unknown'


def test_suppression_and_delete_rebuild_every_saved_page(live_run):
    live.refresh('live-parity', force=True)
    page = live.build_projection('live-parity')
    removed = page['deliverables'][0]['company_name']
    runs.suppression_add('company', removed, '用户排除')
    after = live.read_projection('live-parity')
    assert after['counts']['deliverable'] == 3
    assert removed not in {item['company_name'] for item in after['deliverables'] + after['pending']}

    runs.suppression_remove(runs.suppression_list()[-1]['id'])
    restored = live.read_projection('live-parity')
    assert restored['counts']['deliverable'] == 4

    doomed = live.read_projection('live-parity')['deliverables'][0]
    company_id, deleted_name = doomed['company_id'], doomed['company_name']
    runs.delete_company(company_id)
    after_delete = live.read_projection('live-parity')
    # 已准入 is an admission ledger; deleting a retained record does not rewrite it.
    assert after_delete['counts']['admitted'] == 5
    assert after_delete['counts']['deliverable'] == 3
    assert deleted_name not in {item['company_name'] for item in after_delete['deliverables'] + after_delete['pending']}
    assert company_id not in {item['company_id'] for item in after_delete['deliverables'] + after_delete['pending']}


def test_unsafe_urls_and_control_bytes_never_reach_the_page():
    assert live.safe_url('https://example.org/a') == 'https://example.org/a'
    assert live.safe_url('http://example.org/a?b=1') == 'http://example.org/a?b=1'
    for value in ('javascript:alert(1)', 'data:text/html,<script>alert(1)</script>',
                  '//example.org/x', '/relative', 'ftp://example.org/x', 'https://exa mple.org'):
        assert live.safe_url(value) is None, value
    assert live._text('a\x00b\x1fc') == 'abc'
    assert live._text('<script>alert(1)</script>') == '<script>alert(1)</script>'  # stored as text, never markup


def test_page_assets_have_no_markup_injection_and_no_remote_dependency():
    root = Path(live.__file__).resolve().parent / 'assets'
    script = (root / 'app.js').read_text(encoding='utf-8')
    for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
        assert sink not in script, sink
    assert 'textContent' in script and 'createElement' in script
    page = (root / 'index.html').read_text(encoding='utf-8')
    assert 'http://' not in page and 'https://' not in page
    assert '<script src="/assets/app.js"' in page
    assert 'onclick=' not in page


def test_replay_does_not_duplicate_rows_or_change_the_revision(clean_db, packaging_spec, packaging_candidate):
    candidates = _variants(packaging_candidate, 2)
    runs.create('live-replay', copy.deepcopy(packaging_spec))
    with transaction() as session:
        session.get(Run, 'live-replay').demo_only = False
    first = runs.append('live-replay', candidates, chunk_id='round-1')
    runs.process('live-replay', max_jobs=4, workers=1)
    again = runs.append('live-replay', candidates, chunk_id='round-1')
    assert again['admission']['accepted'] == first['admission']['accepted']
    runs.process('live-replay', max_jobs=4, workers=1)
    page = live.build_projection('live-replay')
    assert page['counts']['admitted'] == 2 and page['counts']['deliverable'] == 2
    assert live.build_projection('live-replay')['revision'] == page['revision']


def test_v1_runs_project_without_reinterpreting_their_policy(clean_db, monkeypatch):
    import app.pipeline

    def record(task, pages):
        return dict(company_id=task['input']['company_id'], company_name=task['input']['company_name'],
                    domain='harbor.example.org', country_code='US', status='qualified', contacts=[], sources=[],
                    qualification={'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    demo_only=False, pipeline={'stop_reason': 'contact_target_reached'})
    monkeypatch.setattr(app.pipeline, 'process_company', record)
    spec = {'name': '美国货代', 'industry': 'freight forwarding', 'countries': ['US'], 'quantity': 50}
    candidate = {'company_name': 'Harbor Logistics LLC', 'country_code': 'US', 'industry_text': 'freight forwarding',
                 'website_url': 'https://harbor.example.org', 'source_url': 'https://directory.example.org/harbor',
                 'source_type': 'directory', 'source_excerpt': 'Harbor Logistics LLC provides freight forwarding',
                 'retrieved_at': utcnow().isoformat()}
    runs.create('live-v1', spec)
    with transaction() as session:
        session.get(Run, 'live-v1').demo_only = False
    runs.append('live-v1', [candidate])
    runs.process('live-v1', max_jobs=1)
    page = live.build_projection('live-v1')
    assert page['run']['spec_version'] == 1
    assert page['counts']['target'] == 50 and page['counts']['admitted'] == 1
    assert page['counts']['deliverable'] == 0 and page['counts']['pending'] == 1
    assert page['pending'][0]['status'] == 'pending'
    assert page['consistency']['match'] is True


def _request(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    head = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, head, data


@pytest.fixture
def served(live_run):
    server, config, token, _ = viewer.create_server('live-parity', idle_timeout=60)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_viewer_requires_token_host_and_origin(served):
    port, token = served
    status, _, _ = _request(port, 'GET', '/')
    assert status == 403
    status, _, _ = _request(port, 'GET', '/?token=wrong')
    assert status == 403
    status, _, _ = _request(port, 'GET', '/api/projection')
    assert status == 403

    status, head, body = _request(port, 'GET', '/?token=' + token)
    assert status == 303 and head['location'] == '/'
    assert token in head['set-cookie'] and 'httponly' in head['set-cookie'].casefold()
    cookie = head['set-cookie'].split(';', 1)[0]
    assert body == b''

    status, head, body = _request(port, 'GET', '/', headers={'Cookie': cookie})
    assert status == 200 and '实时成果页面' in body.decode('utf-8')
    assert token not in body.decode('utf-8')
    assert 'content-security-policy' in head and "default-src 'none'" in head['content-security-policy']
    assert 'access-control-allow-origin' not in head

    status, head, body = _request(port, 'GET', '/', headers={'Cookie': cookie, 'Host': 'attacker.example'})
    assert status == 403

    status, head, body = _request(port, 'GET', '/api/projection', headers={'Cookie': cookie})
    assert status == 200
    payload = json.loads(body)
    assert payload['page']['counts']['deliverable'] == 4
    assert payload['server']['status_view']['value'] == 'running'
    assert token not in body.decode('utf-8')
    etag = head['etag']
    status, _, _ = _request(port, 'GET', '/api/projection', headers={'Cookie': cookie, 'If-None-Match': etag})
    assert status == 304

    status, _, _ = _request(port, 'POST', '/api/export', headers={'Cookie': cookie},
                            body=json.dumps({'format': 'xlsx'}))
    assert status == 403
    status, _, _ = _request(port, 'POST', '/api/export',
                            headers={'Cookie': cookie, 'Origin': 'http://evil.example',
                                     'X-Live-Request': '1', 'Content-Type': 'application/json'},
                            body=json.dumps({'format': 'xlsx'}))
    assert status == 403
    status, _, body = _request(port, 'POST', '/api/export',
                               headers={'Cookie': cookie, 'X-Live-Request': '1', 'Content-Type': 'application/json'},
                               body=json.dumps({'format': 'xlsx', 'filters': {'q': 'Klarwerk Verpackungen 2'}}))
    assert status == 200, body
    result = json.loads(body)
    assert result['count'] == 1
    assert result['snapshot_at']
    assert Path(result['file']).is_file()
    assert result['filters'].get('q', '').lower() == 'klarwerk verpackungen 2'


def test_viewer_shows_offline_assets_without_remote_calls(served):
    port, token = served
    status, head, body = _request(port, 'GET', '/?token=' + token)
    cookie = head['set-cookie'].split(';', 1)[0]
    for path, content_type in (('/assets/app.js', 'text/javascript'), ('/assets/app.css', 'text/css')):
        status, head, body = _request(port, 'GET', path, headers={'Cookie': cookie})
        assert status == 200 and content_type in head['content-type']
        assert body
    status, _, _ = _request(port, 'GET', '/../app/config.py', headers={'Cookie': cookie})
    assert status == 404
    status, _, _ = _request(port, 'PUT', '/api/export', headers={'Cookie': cookie})
    assert status == 405


def test_viewer_exits_after_inactivity(live_run):
    result = {}
    def run():
        result.update(viewer.serve('live-parity', idle_timeout=1, announce=False))
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(8)
    assert not thread.is_alive()
    assert result['closed'] is True and result['idle_seconds'] >= 1


def test_cli_rebuilds_the_projection(capsys, live_run):
    assert cli.main(['live', '--run', 'live-parity', '--rebuild-only']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['run_id'] == 'live-parity'
    assert Path(payload['rebuilt']['path']).is_file()


def test_cleanup_only_removes_expired_projection_files(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'artifact_root', tmp_path)
    old = tmp_path / 'live' / 'old-run'
    old.mkdir(parents=True)
    (old / 'projection.json').write_text('{}', encoding='utf-8')
    (old / 'pending.json').write_text('{}', encoding='utf-8')
    (old / 'notes.txt').write_text('keep', encoding='utf-8')
    past = time.time() - 10 * 86400
    os.utime(old / 'projection.json', (past, past))
    os.utime(old / 'pending.json', (past, past))
    os.utime(old, (past, past))
    fresh = tmp_path / 'live' / 'fresh-run'
    fresh.mkdir()
    (fresh / 'projection.json').write_text('{}', encoding='utf-8')
    os.utime(fresh, (past, past))  # active file, old folder: must not expire

    result = live.cleanup(retention_days=1)
    assert result['removed'] == ['old-run']
    assert not (old / 'projection.json').exists()
    assert (old / 'notes.txt').exists()  # unknown files are never deleted
    assert (fresh / 'projection.json').exists()
