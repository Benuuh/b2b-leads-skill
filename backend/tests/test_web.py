"""Free adapters, real provenance and mocked outbound safety; no live coverage claim."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import socket
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import pytest

from app.web import http as sources, crawler
from app.web.errors import WebError as ProviderError
from app.web.security import validate_public_url


@pytest.fixture(autouse=True)
def isolated_network_state(monkeypatch):
    sources._robots_cache.clear()
    sources._last_request.clear()
    sources._blocked_until.clear()
    monkeypatch.setattr(sources.time, 'sleep', lambda seconds: None)


@pytest.fixture
def segment():
    return {'industry': 'freight forwarding', 'countries': ['US'], 'target_count': 200,
            'languages': ['en'], 'keywords': ['customs brokerage']}


@pytest.fixture
def fake_network(monkeypatch):
    routes, calls = {}, []

    def public_resolver(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    def open_pinned(url, addresses, method='GET', body=None):
        calls.append((url, addresses, method, body))
        if urlsplit(url).path == '/robots.txt':
            return routes.get(url, (200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /'))
        return routes[url]

    monkeypatch.setattr(sources, 'validate_public_url', lambda url: validate_public_url(url, resolver=public_resolver))
    monkeypatch.setattr(sources, '_open_pinned', open_pinned)
    return routes, calls


def ok(content, content_type='text/html'):
    return 200, {'content-type': content_type}, content.encode()


def test_robots_disallow_prevents_page_request(fake_network):
    routes, calls = fake_network
    routes['https://public.example.com/robots.txt'] = ok('User-agent: *\nDisallow: /private', 'text/plain')
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/private')
    assert exc.value.code == 'ROBOTS_DISALLOWED'
    assert len(calls) == 1 and calls[0][0].endswith('/robots.txt')


def test_unknown_or_redirected_robots_stops_source(fake_network):
    routes, calls = fake_network
    routes['https://public.example.com/robots.txt'] = (302, {'location': 'http://127.0.0.1/secret'}, b'')
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'ROBOTS_UNKNOWN' and len(calls) == 1


@pytest.mark.parametrize('target', ['http://127.0.0.1/secret', 'http://[::1]/secret',
                                    'http://169.254.169.254/latest/meta-data', 'http://localhost/secret'])
def test_redirect_private_is_rejected_before_private_request(fake_network, target):
    routes, calls = fake_network
    routes['https://public.example.com/'] = (302, {'location': target}, b'')
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'UNSAFE_URL'
    assert len(calls) == 2 and all('public.example.com' in call[0] for call in calls)


def test_public_redirect_rechecks_robots_dns_and_same_site_policy(fake_network):
    routes, calls = fake_network
    routes['https://public.example.com/'] = (302, {'location': 'https://other.example.com/contact'}, b'')
    routes['https://other.example.com/contact'] = ok('<p>Business</p>')
    result = sources.safe_http('https://public.example.com/')
    assert result['final_url'] == 'https://other.example.com/contact'
    assert len(calls) == 4 and calls[2][0] == 'https://other.example.com/robots.txt'
    calls.clear()
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/', allowed_hosts={'public.example.com'})
    assert exc.value.code == 'OFFSITE_REDIRECT' and len(calls) == 1


def test_dns_mixed_public_private_stops_before_socket(monkeypatch):
    def mixed(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))
                for address in ['93.184.216.34', '10.0.0.1']]
    monkeypatch.setattr(socket, 'getaddrinfo', mixed)
    monkeypatch.setattr(sources, '_open_pinned', lambda *args: pytest.fail('unsafe outbound socket'))
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'UNSAFE_URL'


def test_https_connection_pins_validated_ip_and_preserves_tls_hostname(monkeypatch):
    calls = []
    fake_socket = object()

    class Context:
        def wrap_socket(self, connection, *, server_hostname):
            calls.append(('tls', connection, server_hostname))
            return connection

    monkeypatch.setattr(sources.socket, 'create_connection', lambda address, timeout: calls.append(('socket', address)) or fake_socket)
    connection = sources._PinnedHTTPSConnection('public.example.com', 443, '93.184.216.34')
    connection._context = Context()
    connection.connect()
    assert calls == [('socket', ('93.184.216.34', 443)), ('tls', fake_socket, 'public.example.com')]


@pytest.mark.parametrize('body', ['<p>Verify you are human</p>', '<input type="password">',
                                  '<p>Sign in to continue</p>', '<iframe src="https://example.com/recaptcha"></iframe>'])
def test_captcha_or_login_stops_no_workaround(fake_network, body):
    routes, calls = fake_network
    routes['https://public.example.com/'] = ok(body)
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'ACCESS_BLOCKED' and len(calls) == 2


def test_rate_limit_rejects_subsequent_attempt_without_request(fake_network):
    routes, calls = fake_network
    routes['https://public.example.com/'] = (429, {'retry-after': '120'}, b'')
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'RATE_LIMITED'
    count = len(calls)
    with pytest.raises(ProviderError) as exc:
        sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'RATE_LIMITED' and len(calls) == count


@pytest.mark.parametrize(('value', 'expected'), [
    ('120', 120), ('0', 1), ('Sat, 24 Oct 2026 01:02:00 GMT', 120),
    ('Fri, 23 Oct 2026 01:00:00 GMT', 1), ('invalid', 60), (None, 60),
])
def test_retry_after_delta_and_http_date(value, expected):
    now = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)
    assert sources.retry_after_seconds(value, now=now) == expected


def test_injected_durable_cooldown_prevents_fresh_process_state_send(fake_network):
    _, calls = fake_network
    events = []
    def limiter(event, payload):
        events.append((event, payload))
        return {'wait_seconds': 120}
    with observation.observation_context(host_limiter=limiter):
        with pytest.raises(ProviderError) as exc:
            sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'RATE_LIMITED' and exc.value.retry_after_s == 120
    assert calls == [] and [event for event, _ in events] == ['robots_get', 'claim']


def test_upstream_cooldown_is_persisted_before_reservation_release(fake_network):
    routes, calls = fake_network
    routes['https://public.example.com/'] = (429, {'retry-after': '120'}, b'')
    events = []
    def limiter(event, payload):
        events.append((event, deepcopy(payload)))
        return {'token': 'one'} if event == 'claim' else {'ready': True}
    with observation.observation_context(host_limiter=limiter):
        with pytest.raises(ProviderError) as exc:
            sources.safe_http('https://public.example.com/')
    assert exc.value.retry_after_s == 120 and exc.value.upstream_status == 429
    assert len(calls) == 2  # robots and page, both using the injected limiter
    assert [event for event, _ in events] == ['robots_get', 'claim', 'dispatch', 'release',
                                            'robots_put', 'claim', 'dispatch', 'release']
    assert events[-1][1]['retry_after_s'] == 120


def test_recheck_before_dispatch_rejects_revoked_reservation(fake_network):
    _, calls = fake_network
    def limiter(event, payload):
        if event == 'claim':
            return {'token': 'one'}
        return {'wait_seconds': 90, 'revoked': True} if event == 'dispatch' else {'released': True}
    with observation.observation_context(host_limiter=limiter):
        with pytest.raises(ProviderError) as exc:
            sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'RATE_LIMITED' and exc.value.retry_after_s == 90
    assert calls == []


def test_unavailable_durable_limiter_prevents_send(fake_network):
    _, calls = fake_network
    def unavailable(*_):
        raise RuntimeError('storage unavailable')
    with observation.observation_context(host_limiter=unavailable):
        with pytest.raises(ProviderError) as exc:
            sources.safe_http('https://public.example.com/')
    assert exc.value.code == 'HOST_LIMIT_UNAVAILABLE' and calls == []


def test_durable_robots_rules_are_rechecked_without_a_robots_refetch(fake_network):
    _, calls = fake_network
    events = []
    def limiter(event, payload):
        events.append(event)
        if event == 'robots_get':
            return {'status': 200, 'rules_text': 'User-agent: *\nDisallow: /private', 'ttl_seconds': 60}
        raise AssertionError('a denied page must not reserve or send')
    with observation.observation_context(host_limiter=limiter):
        with pytest.raises(ProviderError) as exc:
            sources.safe_http('https://public.example.com/private')
    assert exc.value.code == 'ROBOTS_DISALLOWED'
    assert events == ['robots_get'] and calls == []


def test_company_fetch_is_contacts_capped_and_no_login_external_links(fake_network, segment):
    # spec b2b-leads-2.6 O3：contacts 阶段硬上限 3 页（原 10 页）。
    routes, calls = fake_network
    home = 'https://public.example.com/'
    links = ''.join(f'<a href="/contact-{index}">Contact</a>' for index in range(30))
    routes[home] = ok(links + '<a href="https://other.example.com/">Outside</a><a href="/login">Login</a>')
    for index in range(30):
        routes[f'https://public.example.com/contact-{index}'] = ok('<p>sales@public.example.com</p>')
    pages = crawler.fetch_company({'website_url': home}, segment, max_pages=100)
    assert len(pages) == crawler.CONTACTS_PAGE_CAP and all(page['fetch_status'] == 'ok' for page in pages)
    assert len(calls) == crawler.CONTACTS_PAGE_CAP + 1  # robots.txt 不计入页面
    assert all('other.example.com' not in call[0] and '/login' not in call[0] for call in calls)


def test_fetch_reports_failures_without_fake_live_pages(fake_network, segment):
    routes, calls = fake_network
    routes['https://public.example.com/'] = (403, {}, b'Forbidden')
    pages = crawler.fetch_company({'website_url': 'https://public.example.com'}, segment)
    assert len(pages) == 1 and pages[0]['fetch_status'] == 'access_blocked'
    assert pages[0]['html'] == '' and not pages[0]['demo_only']


def test_open_pinned_limits_declared_and_streamed_response_bytes(monkeypatch):
    class Response:
        status = 200

        def getheaders(self):
            return [('content-length', str(sources.MAX_RESPONSE_BYTES + 1))]

        def read(self, size):
            pytest.fail('oversized body should not be read')

    class Connection:
        closed = False

        def request(self, *args):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(sources.http.client, 'HTTPConnection', lambda *args, **kwargs: connection)
    with pytest.raises(ProviderError) as exc:
        sources._open_pinned('http://public.example.com/', ['93.184.216.34'])
    assert exc.value.code == 'CONTENT_TOO_LARGE' and connection.closed


def test_open_pinned_streamed_response_cannot_exceed_cap_without_content_length(monkeypatch):
    class Response:
        status = 200
        remaining = sources.MAX_RESPONSE_BYTES + 100

        def getheaders(self):
            return [('content-type', 'text/html')]

        def read1(self, size):
            count = min(size, self.remaining)
            self.remaining -= count
            return b'x' * count

    class Connection:
        closed = False

        def request(self, *args):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(sources.http.client, 'HTTPConnection', lambda *args, **kwargs: connection)
    with pytest.raises(ProviderError) as exc:
        sources._open_pinned('http://public.example.com/', ['93.184.216.34'])
    assert exc.value.code == 'CONTENT_TOO_LARGE' and connection.closed

from app.web import observation


@pytest.fixture
def observed_network(monkeypatch):
    from app.web import observation
    sources._robots_cache.clear()
    sources._last_request.clear()
    sources._blocked_until.clear()
    events, calls, routes = [], [], {}
    monkeypatch.setattr(sources, 'validate_public_url', lambda url: (url, ['93.184.216.34']))
    monkeypatch.setattr(sources.time, 'sleep', lambda _: None)
    monkeypatch.setattr(sources, 'utc_now', lambda: '2026-10-23T00:00:00Z')
    def transport(url, addresses, method='GET', body=None):
        calls.append((url, method))
        outcome = routes.get(url, (200, {'content-type':'text/plain'}, b'User-agent: *\nAllow: /'))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(sources, '_open_pinned', transport)
    def sink(event, payload):
        events.append((event, deepcopy(payload)))
    return sink, events, calls, routes

def test_observation_preserves_pages_and_covers_robots_redirect_each_hop(observed_network):
    sink, events, calls, routes = observed_network
    routes['https://public.example.com/start'] = (302, {'location':'/contact'}, b'')
    routes['https://public.example.com/contact'] = (200, {'content-type':'text/html'}, b'<h1>Freight forwarding</h1>')
    baseline = sources.safe_http('https://public.example.com/start')
    sources._robots_cache.clear()
    calls.clear()
    with observation.observation_context(sink=sink, source_id='directory', query_id='q_frozen', attempt=2,
                                         purpose='qualification'):
        actual = sources.safe_http('https://public.example.com/start')
    assert actual == baseline
    starts = [payload for event, payload in events if event == 'start']
    finishes = [payload for event, payload in events if event == 'finish']
    assert len(starts) == len(finishes) == len(calls) == 3
    assert [row['purpose'] for row in starts] == ['robots', 'qualification', 'qualification']
    assert [row['http_status'] for row in finishes] == [200, 302, 200]
    assert all(row['attempt'] == 2 and row['query_id'] == 'q_frozen' for row in starts)


def test_304_failure_and_unsent_policy_rejection_are_distinct(observed_network):
    sink, events, calls, routes = observed_network
    routes['https://public.example.com/not-modified'] = (304, {}, b'')
    routes['https://public.example.com/broken'] = ProviderError('CONNECTION_ERROR', 'may contain private response')
    with observation.observation_context(sink=sink, purpose='contact'):
        with pytest.raises(ProviderError):
            sources.safe_http('https://public.example.com/not-modified')
        with pytest.raises(ProviderError):
            sources.safe_http('https://public.example.com/broken')
        with pytest.raises(ProviderError):
            sources.safe_http('https://public.example.com/login')
    finishes = [payload for event, payload in events if event == 'finish']
    assert len(calls) == len(finishes) == 3  # robots, 304, failed socket; no login request
    assert finishes[1]['http_status'] == 304 and finishes[1]['state'] == 'completed'
    assert finishes[2]['state'] == 'error' and finishes[2]['error_code'] == 'CONNECTION_ERROR'
    assert 'private response' not in str(events)


def test_url_redaction_and_demo_never_record_contact_payload(observed_network):
    sink, events, calls, routes = observed_network
    url = 'https://username:password@public.example.com/contact/sales%40example.com?phone=%2B12125550123&token=secret#email'
    assert observation.sanitize_url(url) == 'https://public.example.com/[redacted]'
    with observation.observation_context(sink=sink):
        with observation.observe_http(url, body=b'email=private@example.com') as result:
            result.update(http_status=200, response_bytes=12)
        crawler.fetch_company({'website_url':'https://public.example.com'}, {'countries':['US']}, fixture=True)
    encoded = json.dumps(events, default=str)
    assert len(events) == 2 and calls == []
    assert all(secret not in encoded for secret in ('username', 'password', 'sales', '12125550123', 'token', 'secret', 'private@example.com'))


def test_failed_initial_observation_prevents_unrecorded_send(observed_network):
    _, events, calls, routes = observed_network
    def unavailable(*_):
        raise RuntimeError('storage unavailable')
    with observation.observation_context(sink=unavailable):
        with pytest.raises(ProviderError) as failure:
            sources.safe_http('https://public.example.com/')
    assert failure.value.code == 'OBSERVATION_UNAVAILABLE'
    assert calls == []


def test_serial_pilot_stop_guard_survives_nested_context_without_record_or_send(observed_network):
    sink, events, calls, routes = observed_network
    checked = []
    def cap_guard(context, payload):
        checked.append((context['source_id'], payload['purpose']))
        raise ProviderError('PILOT_HTTP_CAP_REACHED', 'Frozen serial cap reached')
    with observation.observation_context(before_send=cap_guard):
        with observation.observation_context(sink=sink, source_id='directory', purpose='discovery'):
            with pytest.raises(ProviderError) as failure:
                sources.safe_http('https://public.example.com/')
    assert failure.value.code == 'PILOT_HTTP_CAP_REACHED'
    assert checked == [('directory', 'robots')] and events == [] and calls == []




def test_lookup_observation_uses_injected_sink_without_host_values():
    events = []
    with observation.observation_context(sink=lambda event, payload: events.append((event, payload)),
                                         run_id='run_public', job_id='job_one'):
        with observation.observe_lookup('mx'):
            pass
    assert [event for event, _ in events] == ['lookup_start', 'lookup_finish']
    assert events[0][1]['run_id'] == 'run_public'
    assert events[-1][1]['state'] == 'completed'
    assert not any('host' in payload for _, payload in events)


def test_nested_observation_context_restores_caller_lineage():
    with observation.observation_context(source_id='directory', query_id='query_one'):
        with observation.observation_context(source_id='official-site'):
            assert observation.current_lineage()['source_id'] == 'official-site'
        assert observation.current_lineage() == {'source_id': 'directory', 'query_id': 'query_one'}
    assert observation.current_lineage() == {'source_id': 'unknown', 'query_id': 'unknown'}
