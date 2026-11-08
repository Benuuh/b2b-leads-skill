"""Speed changes preserve channels, source evidence, isolation and operation replay."""
import copy
import threading

import pytest
from sqlalchemy import select

from app import runs
from app.errors import AppError
from app.pipeline import process_company
from app.persistence import transaction
from app.persistence.models import Job, Run
from app.schemas import parse_segment
from app.web.parsing import parsed_html, parsing_scope


def test_html_views_preserve_jsonld_forms_and_content_changes():
    html = '<script type="application/ld+json">{"name":"Observed"}</script><form><input type="email"><textarea></textarea></form><p>First</p>'
    with parsing_scope() as stats:
        raw = parsed_html(html)
        cleaned = parsed_html(html, remove=('script', 'form'))
        assert cleaned.select_one('script, form') is None
        assert raw.select_one('script') and raw.select_one('form')
        assert parsed_html(html) is raw
        assert parsed_html(html.replace('First', 'Second')).p.text == 'Second'
        assert stats['html_parse_count'] == 3 and stats['html_cache_hits'] == 1
    with parsing_scope() as fresh:
        assert parsed_html(html) is not raw
        assert fresh['html_cache_hits'] == 0


def test_worker_html_views_are_isolated_and_bounded(monkeypatch):
    import app.web.parsing as parsing
    monkeypatch.setattr(parsing, 'MAX_ENTRIES', 2)
    barrier = threading.Barrier(2, timeout=10)
    trees = []
    def worker():
        with parsing_scope() as stats:
            tree = parsed_html('<p>same</p>')
            trees.append(tree)
            barrier.wait()
            assert tree.p.text == 'same'
            for number in range(5):
                parsed_html(f'<p>{number}</p>')
            assert len(stats['trees']) <= 2
    workers = [threading.Thread(target=worker) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
    assert len(trees) == 2 and trees[0] is not trees[1]


@pytest.mark.parametrize('named,path', [(False, '/contact'), (True, '/team')])
def test_crawl_prioritizes_the_requested_channel_and_reuses_extraction(
        packaging_spec, packaging_candidate, monkeypatch, named, path):
    import app.web.crawler as crawler
    import app.web.dns as dns
    candidate = copy.deepcopy(packaging_candidate)
    candidate.pop('email')
    candidate['sources'] = [s for s in candidate['sources'] if s['field'] != 'email']
    candidate['website_url'] = 'https://klarwerk.de/'
    candidate['sources'].append({
        'field': 'website_url', 'value': candidate['website_url'],
        'source_url': candidate['source_url'], 'source_type': 'directory',
        'retrieved_at': candidate['retrieved_at'],
        'source_excerpt': candidate['company_name'] + '; official website https://klarwerk.de/'})
    packaging_spec['qualification_policy']['contact_policy'] = {
        'mode': 'any', 'allowed_types': ['business_email'], 'named_person_required': named}
    spec, _ = parse_segment(packaging_spec)
    urls = []
    def fetch(url, **kwargs):
        urls.append(url)
        body = '<a href="/team">Team</a><a href="/contact">Contact</a>'
        if url.endswith('/contact'):
            body += '<a href="mailto:sales@klarwerk.de">sales@klarwerk.de</a>'
        if url.endswith('/team'):
            body += '<div class="team-member"><h3>Anna Weber</h3><span class="role">Sales Manager</span><a href="mailto:anna@klarwerk.de">anna@klarwerk.de</a></div>'
        return {'html': '<meta property="og:site_name" content="Klarwerk Verpackungen GmbH">' + body,
                'final_url': url, 'fetch_status': 'ok', 'fetched_at': candidate['retrieved_at'],
                'source_type': 'official_site'}
    monkeypatch.setattr(crawler, 'safe_http', fetch)
    monkeypatch.setattr(dns, 'lookup_mx', lambda _: 'unknown')
    result = process_company({'input': candidate, 'segment': spec, 'demo_only': False,
        'plan': {'page_limit_per_company': 10, 'page_limit_total': 100},
        'pages_used': 0, 'suppressions': []}, [])
    assert result['status'] == 'qualified'
    assert urls == ['https://klarwerk.de/', 'https://klarwerk.de' + path]
    assert result['delivery_channels'] == ['email']
    assert result['pipeline']['contact_extractions'] == 3  # offline, homepage, requested page
    assert result['pipeline']['html_cache_hits'] > 0
    assert any(source.get('source_url') == urls[-1] for source in result['sources'])


def test_advance_admits_processes_and_replays_one_chunk(clean_db, packaging_spec, packaging_candidate):
    runs.create('advance', packaging_spec)
    body = {'candidates': [packaging_candidate], 'checkpoint': {'round': 1}}
    first = runs.advance('advance', body, chunk_id='round-1', workers=4)
    assert first['admission']['accepted'] == first['processed_jobs'] == 1
    assert first['workers'] == 2  # A call cannot raise the run's frozen concurrency.
    assert first['research']['contactable_count'] == 1
    assert runs.advance('advance', body, chunk_id='round-1', workers=4) == first
    with transaction() as session:
        assert len(list(session.scalars(select(Job)))) == 1


def test_advance_continues_after_admission_response_is_lost(
        clean_db, packaging_spec, packaging_candidate, monkeypatch):
    runs.create('advance-retry', packaging_spec)
    process = runs.process
    def interrupted(*args, **kwargs):
        raise RuntimeError('synthetic interruption after append')
    # S10 拆包：advance 在 app.runs.state 内部调用 process，补丁打到该子模块。
    import app.runs.state as runs_state
    monkeypatch.setattr(runs_state, 'process', interrupted)
    body = {'candidates': [packaging_candidate]}
    with pytest.raises(RuntimeError):
        runs.advance('advance-retry', body, chunk_id='same')
    monkeypatch.setattr(runs_state, 'process', process)
    result = runs.advance('advance-retry', body, chunk_id='same')
    assert result['processed_jobs'] == result['research']['discovered'] == 1
    with transaction() as session:
        assert len(list(session.scalars(select(Job)))) == 1


@pytest.mark.parametrize('bad', ['draft', 'options', 'paused'])
def test_advance_rejects_before_admission(clean_db, packaging_spec, packaging_candidate, bad):
    runs.create('advance-bad', packaging_spec)
    body = {'candidates': [packaging_candidate]}
    options = {}
    if bad == 'draft':
        body['candidates'].append({'company_name': 'Unobserved draft'})
    elif bad == 'options':
        options['max_jobs'] = 0
    else:
        runs.status('advance-bad', action='pause')
    with pytest.raises(AppError):
        runs.advance('advance-bad', body, chunk_id='bad', **options)
    with transaction() as session:
        assert not list(session.scalars(select(Job)))


@pytest.mark.parametrize('excerpt', [
    'Klarwerk Verpackungen GmbH is not a manufacturer and does not provide reusable industrial packaging.',
    'Klarwerk Verpackungen GmbH sells software to a manufacturer of reusable industrial packaging.',
    'Other Packaging GmbH manufactures reusable industrial packaging.',
])
def test_small_evidence_guard_does_not_deliver_obvious_false_positives(
        packaging_spec, packaging_candidate, excerpt):
    from app.rules.policy import assess_policy
    candidate = copy.deepcopy(packaging_candidate)
    candidate['source_excerpt'] = candidate['company_name'] + ', Germany.'
    for source in candidate['sources']:
        if source['field'] in ('business.models', 'offering.products'):
            source['source_excerpt'] = excerpt
    spec, _ = parse_segment(packaging_spec)
    assert assess_policy(candidate, [], spec)['decision'] != 'qualified'
