"""Spec b2b-leads-2.6 §4.2.3 O3 预算拿到就停 / O4 sitemap 定位 / O5 三级熔断。"""
import socket
from urllib.parse import urlsplit

import pytest

from app.web import crawler, http as sources
from app.web.errors import WebError


@pytest.fixture(autouse=True)
def isolated_network_state(monkeypatch):
    sources._robots_cache.clear()
    sources._last_request.clear()
    sources._blocked_until.clear()
    monkeypatch.setattr(sources.time, 'sleep', lambda seconds: None)


@pytest.fixture
def routed_network(monkeypatch):
    routes, calls = {}, []

    def public_resolver(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    def open_pinned(url, addresses, method='GET', body=None):
        calls.append(url)
        if urlsplit(url).path == '/robots.txt':
            return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /'
        status, headers, body = routes.get(url, (404, {'content-type': 'text/plain'}, b'not found'))
        return status, headers, body

    monkeypatch.setattr(sources, 'validate_public_url',
                        lambda url: (url, ['93.184.216.34']))
    monkeypatch.setattr(sources, '_open_pinned', open_pinned)
    return routes, calls


def ok(content, content_type='text/html'):
    return 200, {'content-type': content_type}, content.encode()


def test_o4_sitemap_contact_path_is_tried_before_link_parsing(routed_network):
    routes, calls = routed_network
    home = 'https://public.example.com/'
    routes[home] = ok('<p>Freight forwarding</p>')  # 首页没有任何 contact 链接（JS 导航）
    routes[home + 'sitemap.xml'] = ok(
        '<urlset><url><loc>https://public.example.com/contact</loc></url>'
        '<url><loc>https://public.example.com/products</loc></url></urlset>', 'text/xml')
    routes[home + 'contact'] = ok('<a href="mailto:sales@public.example.com">sales</a>')
    segment = {'industry': 'freight', 'spec_version': 2, 'qualification_policy': {'contact_policy': {}}}
    pages = crawler.fetch_company({'website_url': home}, segment, max_pages=3)
    # 首页必抓 → 无 contact 线索才读 sitemap → 直达 /contact（sitemap 不计入页面）。
    assert calls == [home + 'robots.txt', home, home + 'sitemap.xml', home + 'contact']
    assert len(pages) == 2 and all(page['fetch_status'] == 'ok' for page in pages)


def test_o4_sitemap_miss_falls_back_to_homepage_links(routed_network):
    routes, calls = routed_network
    home = 'https://public.example.com/'
    routes[home] = ok('<p>Freight forwarding</p><a href="/contact">Contact</a>')
    segment = {'industry': 'freight', 'spec_version': 2, 'qualification_policy': {'contact_policy': {}}}
    crawler.fetch_company({'website_url': home}, segment, max_pages=2)
    # 首页本身有 contact 链接：不读 sitemap，直接解析链接。
    assert calls == [home + 'robots.txt', home, home + 'contact']


def test_o4_sitemap_error_falls_back_silently(routed_network):
    routes, calls = routed_network
    home = 'https://public.example.com/'
    # 首页无任何 contact/team 线索（/history 非线索），sitemap 抓不到时回落链接解析。
    routes[home] = ok('<p>Freight forwarding</p><a href="/history">History</a>')
    routes[home + 'history'] = ok('<p>Our history</p>')
    segment = {'industry': 'freight', 'spec_version': 2, 'qualification_policy': {'contact_policy': {}}}
    pages = crawler.fetch_company({'website_url': home}, segment, max_pages=2)
    assert home + 'robots.txt' in calls and home + 'sitemap.xml' in calls  # 尝试过 sitemap
    assert home + 'history' in calls  # 回落到首页链接解析
    assert all(page['fetch_status'] == 'ok' for page in pages)


def test_o5_domain_breaker_opens_after_three_consecutive_failures(monkeypatch):
    monkeypatch.setattr(sources, '_robots_rule', lambda url, delay: delay)
    attempts = []

    def failing_request(url, *, delay, method='GET', body=None, headers=None):
        attempts.append(url)
        raise WebError('CONNECTION_ERROR', '公开来源网络连接失败', retryable=True)

    monkeypatch.setattr(sources, '_rate_limited_request', failing_request)
    for _ in range(3):
        with pytest.raises(WebError):
            sources.safe_http('https://broken.example.com/page')
    assert len(attempts) == 3
    # 第 4 次不再发起任何网络请求：域名级熔断已开。
    with pytest.raises(WebError) as error:
        sources.safe_http('https://broken.example.com/other')
    assert error.value.code == 'DOMAIN_CIRCUIT_OPEN'
    assert len(attempts) == 3
    state = sources.domain_breakers()
    assert state['broken.example.com']['open'] is True


def test_o5_success_resets_domain_failure_counter(monkeypatch):
    monkeypatch.setattr(sources, '_robots_rule', lambda url, delay: delay)
    responses = {'fail': True}

    def request(url, *, delay, method='GET', body=None, headers=None):
        if responses['fail']:
            raise WebError('CONNECTION_ERROR', '公开来源网络连接失败', retryable=True)
        return url, 200, {'content-type': 'text/html'}, b'<html><body>fine</body></html>'

    monkeypatch.setattr(sources, '_rate_limited_request', request)
    for _ in range(2):
        with pytest.raises(WebError):
            sources.safe_http('https://flaky.example.com/page')
    responses['fail'] = False
    sources.safe_http('https://flaky.example.com/page')
    assert sources.domain_breakers().get('flaky.example.com') is None
    responses['fail'] = True
    for _ in range(3):
        with pytest.raises(WebError):
            sources.safe_http('https://flaky.example.com/page')
    assert sources.domain_breakers()['flaky.example.com']['open'] is True


def test_o5_non_transport_errors_do_not_trip_breaker(monkeypatch):
    monkeypatch.setattr(sources, '_robots_rule', lambda url, delay: delay)

    def request(url, *, delay, method='GET', body=None, headers=None):
        return url, 404, {'content-type': 'text/plain'}, b'not found'

    monkeypatch.setattr(sources, '_rate_limited_request', request)
    for _ in range(4):
        with pytest.raises(WebError) as error:
            sources.safe_http('https://missing.example.com/page')
        assert error.value.code == 'HTTP_ERROR'
    assert sources.domain_breakers().get('missing.example.com') is None


def test_o5_homepage_required_before_contact_links(routed_network):
    """O3：首页必抓，拿到联系方式即停（enough 回调），不追加内页。"""
    routes, calls = routed_network
    home = 'https://public.example.com/'
    routes[home] = ok('<a href="mailto:sales@public.example.com">sales</a>'
                      '<a href="/contact">Contact</a><a href="/team">Team</a>')
    segment = {'industry': 'freight', 'spec_version': 2, 'qualification_policy': {'contact_policy': {}}}
    from app.rules.contacts import extract_company
    pages = crawler.fetch_company(
        {'website_url': home}, segment, max_pages=3, stage='contacts',
        should_stop=lambda current: bool(extract_company({}, current, segment)['contacts']))
    assert calls == [home + 'robots.txt', home], '首页已有邮箱，必须立刻停，不得再抓内页'
