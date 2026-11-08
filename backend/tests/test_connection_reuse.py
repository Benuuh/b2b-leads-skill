"""Spec b2b-leads-2.6 §4.2.3 O1 robots 域名缓存 与 O2 keep-alive 连接复用。"""
import http.client
import socket
from urllib.parse import urlsplit

import pytest

from app.web import http as sources


@pytest.fixture(autouse=True)
def isolated_network_state(monkeypatch):
    sources._robots_cache.clear()
    sources._last_request.clear()
    sources._blocked_until.clear()
    with sources._pool_lock:
        sources._connection_pool.clear()
    monkeypatch.setattr(sources.time, 'sleep', lambda seconds: None)


class FakeResponse:
    def __init__(self, body=b'hello', will_close=False, status=200,
                 headers=(('content-type', 'text/html'),)):
        self.status = status
        self.will_close = will_close
        self._body = body
        self._headers = list(headers)

    def getheaders(self):
        return self._headers

    def read1(self, size):
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk


class FakeConnection:
    """Records construction; failures are programmed per instance request number."""

    factory_calls = 0
    instances = []
    programmed_failures = {}  # {instance_index: {request_number, ...}}

    def __init__(self, *args, **kwargs):
        type(self).factory_calls += 1
        type(self).instances.append(self)
        self.closed = False
        self.sock = object()
        self.request_count = 0
        self.fail_on = type(self).programmed_failures.get(type(self).factory_calls - 1, set())

    def request(self, *args, **kwargs):
        self.request_count += 1
        if self.request_count in self.fail_on:
            raise http.client.RemoteDisconnected('stale keep-alive socket')

    def getresponse(self):
        return FakeResponse()

    def close(self):
        self.closed = True


@pytest.fixture
def pooled_transport(monkeypatch):
    FakeConnection.factory_calls = 0
    FakeConnection.instances = []
    FakeConnection.programmed_failures = {}
    monkeypatch.setattr(sources.socket, 'create_connection', lambda *a, **k: object())
    monkeypatch.setattr(sources.http.client, 'HTTPConnection', FakeConnection)
    monkeypatch.setattr(sources, 'validate_public_url',
                        lambda url: (url, ['93.184.216.34']))
    monkeypatch.setattr(sources, '_robots_rule', lambda url, delay: delay)
    return FakeConnection


def test_same_origin_reuses_keep_alive_connection(pooled_transport):
    first = sources._open_pinned('http://public.example.com/a', ['93.184.216.34'])
    second = sources._open_pinned('http://public.example.com/b', ['93.184.216.34'])
    assert first[0] == second[0] == 200
    assert pooled_transport.factory_calls == 1, '第二次请求必须复用已建立的连接'
    assert not pooled_transport.instances[0].closed, '归还池中的连接不应被关闭'


def test_will_close_response_is_not_pooled(pooled_transport):
    original_getresponse = FakeConnection.getresponse

    def closing_response(self):
        return FakeResponse(will_close=True)

    FakeConnection.getresponse = closing_response
    try:
        sources._open_pinned('http://public.example.com/a', ['93.184.216.34'])
        sources._open_pinned('http://public.example.com/b', ['93.184.216.34'])
    finally:
        FakeConnection.getresponse = original_getresponse
    assert pooled_transport.factory_calls == 2, '声明关闭的响应不得复用连接'
    assert pooled_transport.instances[0].closed


def test_stale_pooled_connection_retries_once_with_fresh(pooled_transport):
    # 第 1 个实例（首连）在其第 2 次请求（即被复用时）抛出陈旧断连。
    pooled_transport.programmed_failures = {0: {2}}
    sources._open_pinned('http://public.example.com/a', ['93.184.216.34'])
    sources._open_pinned('http://public.example.com/b', ['93.184.216.34'])
    # 第一次成功建连入池；第二次复用陈旧连接失败，恰好用全新连接重试一次。
    assert pooled_transport.factory_calls == 2
    assert pooled_transport.instances[0].closed, '失败的陈旧连接必须被丢弃'
    assert not pooled_transport.instances[1].closed


def test_stale_failure_on_fresh_connection_does_not_retry(pooled_transport):
    pooled_transport.programmed_failures = {0: {1}}
    with pytest.raises(sources.WebError) as error:
        sources._open_pinned('http://public.example.com/a', ['93.184.216.34'])
    assert error.value.code == 'CONNECTION_ERROR'
    assert pooled_transport.factory_calls == 1, '全新连接失败不得传输层重试'


def test_pool_is_bounded_per_origin(pooled_transport):
    for index in range(5):
        def getresponse(self, index=index):
            return FakeResponse(will_close=index % 2 == 0)

        FakeConnection.getresponse = getresponse
        sources._open_pinned('http://public.example.com/page', ['93.184.216.34'])
    with sources._pool_lock:
        assert len(sources._connection_pool.get(
            ('http', 'public.example.com', None, '93.184.216.34'), [])) <= sources._POOL_MAX_PER_ORIGIN
    FakeConnection.getresponse = lambda self: FakeResponse()


def test_pinned_factory_keeps_all_validated_addresses_for_both_schemes(monkeypatch):
    """M0: one definition, and HTTPS/HTTP share the same fallback contract."""
    captured = {}

    class FakeHTTPS:
        def __init__(self, host, port, addresses, timeout=12):
            captured.update(host=host, port=port, addresses=list(addresses), timeout=timeout)

    monkeypatch.setattr(sources, '_PinnedHTTPSConnection', FakeHTTPS)
    sources._new_pinned_connection(urlsplit('https://public.example.com/'), ['203.0.113.1', '203.0.113.2'], 4)
    assert captured == {'host': 'public.example.com', 'port': 443,
                        'addresses': ['203.0.113.1', '203.0.113.2'], 'timeout': 4}

    connection = sources._new_pinned_connection(
        urlsplit('http://public.example.com/'), ['203.0.113.1', '203.0.113.2'], 4)
    assert connection._public_addresses == ['203.0.113.1', '203.0.113.2']
    assert connection.host == 'public.example.com' and connection.port == 80


def test_http_pinned_connection_falls_back_to_the_next_validated_address(monkeypatch):
    attempts = []

    def create_connection(address, timeout=None):
        attempts.append((address[0], address[1], timeout))
        if address[0] == '203.0.113.1':
            raise OSError('first address unreachable')
        return object()

    monkeypatch.setattr(sources.socket, 'create_connection', create_connection)
    connection = sources._new_pinned_connection(
        urlsplit('http://public.example.com/'), ['203.0.113.1', '203.0.113.2'], 6)
    connection.connect()
    assert attempts == [('203.0.113.1', 80, 6), ('203.0.113.2', 80, 6)]
    assert connection._connected_address == '203.0.113.2'
    assert connection.sock is not None


def test_robots_fetched_once_per_domain_per_run(monkeypatch):
    """O1：同一次 run 内同域名只抓一次 robots.txt。"""
    fetches = []

    def fake_rate_limited_request(url, *, delay, method='GET', body=None):
        fetches.append(url)
        return url, 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /'

    monkeypatch.setattr(sources, '_rate_limited_request', fake_rate_limited_request)
    first_delay = sources._robots_rule('https://one.example.com/contact', 1)
    second_delay = sources._robots_rule('https://one.example.com/about', 1)
    sources._robots_rule('https://two.example.com/', 1)
    assert len(fetches) == 2, 'one.example.com 第二次不得重新抓取 robots.txt'
    assert [urlsplit(url).hostname for url in fetches] == ['one.example.com', 'two.example.com']
    assert first_delay == second_delay == 1


def test_robots_cache_ttl_expiry_refetches(monkeypatch):
    fetches = []

    def fake_rate_limited_request(url, *, delay, method='GET', body=None):
        fetches.append(url)
        return url, 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /'

    monkeypatch.setattr(sources, '_rate_limited_request', fake_rate_limited_request)
    sources._robots_rule('https://one.example.com/contact', 1)
    cached = sources._robots_cache['https://one.example.com']
    sources._robots_cache['https://one.example.com'] = (cached[0] - 3601, cached[1], cached[2])
    sources._robots_rule('https://one.example.com/about', 1)
    assert len(fetches) == 2, 'TTL 过期后必须重新抓取'
