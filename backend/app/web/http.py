"""Bounded public HTTP with pinned DNS, robots, redirects and request observation."""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
import http.client
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from .errors import WebError
from .security import normalize_url, validate_public_url as _validate_public_url
from .observation import observation_context, observe_http, observe_lookup, current_host_limiter
from .parsing import parsed_html

USER_AGENT = 'PublicB2BResearchBot/1.0 (public business research; no login)'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
_DEADLINE = ContextVar('b2b_request_deadline', default=None)
_host_locks = defaultdict(threading.Lock)
_last_request = {}
_blocked_until = {}
_robots_cache = {}


@contextmanager
def request_deadline(seconds):
    """Bound every physical request inside the command's remaining budget."""
    if seconds is None:
        yield
        return
    token = _DEADLINE.set(time.monotonic() + max(0.0, float(seconds)))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_seconds():
    """Remaining command budget in seconds, or ``None`` when unbounded."""
    value = _DEADLINE.get()
    return None if value is None else max(0.0, value - time.monotonic())


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float = 12):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._public_address = address

    def connect(self):
        connection = socket.create_connection((self._public_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def _open_pinned(url: str, addresses: list[str], method: str = 'GET', body: bytes | None = None) -> tuple[int, dict, bytes]:
    """No environment proxies or implicit redirects; never resolve the hostname again."""
    budget = remaining_seconds()
    if budget is not None and budget <= 0:
        raise WebError('DEADLINE_EXCEEDED', '命令剩余时间不足以发送请求', retryable=True)
    connect_timeout = 12 if budget is None else max(0.5, min(12, budget))
    parsed = urlsplit(url)
    if parsed.scheme == 'https':
        connection = _PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, addresses[0], timeout=connect_timeout)
    else:
        connection = http.client.HTTPConnection(addresses[0], parsed.port or 80, timeout=connect_timeout)
    headers = {'User-Agent': USER_AGENT, 'Accept': 'text/html,text/plain,text/csv,application/json',
               'Accept-Encoding': 'identity', 'Host': parsed.netloc, 'Connection': 'close'}
    if body is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    timed_out = threading.Event()
    active_socket = []

    def abort_slow_response():
        # A socket read timeout alone can be defeated by an endless slow drip.
        timed_out.set()
        current_socket = active_socket[0] if active_socket else getattr(connection, 'sock', None)
        if current_socket is not None:
            try:
                current_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    timer = threading.Timer(30 if budget is None else max(1.0, min(30.0, budget)), abort_slow_response)
    timer.daemon = True
    timer.start()
    try:
        connection.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''), body, headers)
        if getattr(connection, 'sock', None) is not None:
            active_socket.append(connection.sock)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        if response_headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
            raise WebError('UNSUPPORTED_CONTENT', '来源返回压缩内容，本安全采集器只接受有界原文响应')
        length = response_headers.get('content-length', '')
        if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            raise WebError('CONTENT_TOO_LARGE', '来源响应超过 2 MiB 上限')
        chunks, size, deadline = [], 0, time.monotonic() + min(20, budget) if budget is not None else time.monotonic() + 20
        while True:
            if time.monotonic() > deadline:
                raise WebError('NETWORK_TIMEOUT', '来源响应超过时间上限')
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise WebError('CONTENT_TOO_LARGE', '来源响应超过 2 MiB 上限')
            chunks.append(chunk)
            if not chunk:
                break
        if timed_out.is_set():
            raise WebError('NETWORK_TIMEOUT', '来源整次网络请求超过 30 秒上限')
        return response.status, response_headers, b''.join(chunks)
    except (OSError, http.client.HTTPException) as error:
        if timed_out.is_set():
            raise WebError('NETWORK_TIMEOUT', '来源整次网络请求超过 30 秒上限', retryable=True) from error
        raise WebError('CONNECTION_ERROR', '公开来源网络连接失败', retryable=True) from error
    finally:
        timer.cancel()
        connection.close()


def _observed_getaddrinfo(*args, **kwargs):
    with observe_lookup('dns'):
        return socket.getaddrinfo(*args, **kwargs)


def validate_public_url(url):
    return _validate_public_url(url, resolver=_observed_getaddrinfo)


def retry_after_seconds(value, *, now=None):
    """Honor delta-seconds and HTTP-date forms, with a conservative fallback."""
    now = now or datetime.now(timezone.utc)
    value = str(value or '').strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = math.ceil((when - now).total_seconds())
        # Saturate only at datetime's representable ceiling. Very large valid
        # values must not silently become an early retry.
        maximum = int((datetime.max.replace(tzinfo=timezone.utc) - now).total_seconds()) - 1
        return max(1, min(seconds, maximum))
    except (ValueError, TypeError, OverflowError):
        return 60


def _limit_call(limiter, event, payload):
    try:
        return limiter(event, payload)
    except Exception as error:
        raise WebError('HOST_LIMIT_UNAVAILABLE', '主机限流状态暂不可写，停止网络请求', retryable=True) from error


@contextmanager
def _durable_reservation(host, delay):
    limiter = current_host_limiter()
    if limiter is None:
        yield None, None, 0, {}
        return
    started, waited = time.monotonic(), 0
    while True:
        claim = _limit_call(limiter, 'claim', {'host': host, 'delay': delay})
        token = claim.get('token')
        if token:
            break
        wait = max(0.001, float(claim.get('wait_seconds', 90)))
        budget = remaining_seconds()
        limit = 30 if budget is None else min(30.0, budget)
        if budget is not None and budget <= 0:
            raise WebError('DEADLINE_EXCEEDED', '命令剩余时间不足以等待来源限流', retryable=True)
        if wait > limit or waited + wait > limit:
            raise WebError('RATE_LIMITED', '来源限流冷却中，稍后恢复', retryable=True,
                           retry_after_s=math.ceil(wait))
        time.sleep(wait)
        waited += wait
    outcome = {}
    try:
        yield limiter, token, max(0, time.monotonic() - started), outcome
    finally:
        _limit_call(limiter, 'release', {'host': host, 'token': token, **outcome})


def _rate_limited_request(url: str, *, delay: float, method: str = 'GET', body: bytes | None = None):
    normalized, addresses = validate_public_url(url)
    host = urlsplit(normalized).hostname
    with _host_locks[host]:
        if _blocked_until.get(host, 0) > time.monotonic():
            raise WebError('RATE_LIMITED', '来源限流冷却中，稍后恢复', retryable=True,
                           retry_after_s=math.ceil(_blocked_until[host] - time.monotonic()))
        wait = 0 if current_host_limiter() else max(0, _last_request.get(host, 0) + max(1.0, delay) - time.monotonic())
        # A huge crawl-delay must become a resumable error, not a blocked worker thread.
        budget = remaining_seconds()
        if budget is not None and wait > max(0.0, budget):
            raise WebError('DEADLINE_EXCEEDED', '来源速率等待超出命令剩余时间', retryable=True,
                           retry_after_s=int(wait) + 1)
        if wait > 30:
            raise WebError('RATE_LIMITED', '来源速率等待超过 30 秒，稍后恢复', retryable=True,
                                retry_after_s=int(wait) + 1)
        waiting_started = time.monotonic()
        if wait:
            time.sleep(wait)
        throttle_seconds = max(0, time.monotonic() - waiting_started) if wait else 0
        with _durable_reservation(host, delay) as (limiter, token, durable_wait, outcome):
            retry_seconds = None
            with observe_http(normalized, method=method, body=body,
                              throttle_seconds=throttle_seconds + durable_wait) as observed:
                if limiter:
                    dispatch = _limit_call(limiter, 'dispatch', {'host': host, 'token': token, 'delay': delay})
                    if not dispatch.get('ready'):
                        raise WebError('RATE_LIMITED', '来源限流状态已更新，稍后恢复', retryable=True,
                                       retry_after_s=math.ceil(dispatch.get('wait_seconds', 90)))
                # Anchor the local fallback immediately before dispatch too.
                _last_request[host] = time.monotonic()
                observed['dispatched'] = True
                status, headers, data = _open_pinned(normalized, addresses, method, body)
                observed.update(http_status=status, response_bytes=len(data))
                if status == 429:
                    retry_seconds = retry_after_seconds(headers.get('retry-after'))
                    _blocked_until[host] = time.monotonic() + retry_seconds
                    # The reservation releases only after this cooldown commits.
                    outcome['retry_after_s'] = retry_seconds
        if status == 429:
            raise WebError('RATE_LIMITED', '来源限流，停止请求等待恢复', retryable=True,
                           retry_after_s=retry_seconds, upstream_status=429)
    return normalized, status, headers, data


def _robots_rule(url: str, delay: float) -> float:
    parsed = urlsplit(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    cached = _robots_cache.get(origin)
    if cached is None or time.monotonic() >= cached[0]:
        limiter = current_host_limiter()
        saved = _limit_call(limiter, 'robots_get', {'origin': origin}) if limiter else None
        if saved and saved.get('status') in {200, 404, 410}:
            status, rules = saved['status'], saved.get('rules_text')
            ttl = max(0, min(3600, float(saved.get('ttl_seconds', 0))))
        else:
            # Redirected robots are denied conservatively. No implicit external
            # request occurs, and only a verified bounded response is cached.
            with observation_context(purpose='robots'):
                _, status, _, data = _rate_limited_request(origin + '/robots.txt', delay=delay)
            if len(data) > 256 * 1024:
                raise WebError('ROBOTS_UNKNOWN', 'robots.txt 超过大小限制，停止该来源')
            rules, ttl = data.decode('utf-8', errors='replace'), 3600
        if status in (404, 410):
            parser = None
        elif status == 200:
            if not isinstance(rules, str) or len(rules) > 256 * 1024:
                raise WebError('ROBOTS_UNKNOWN', 'robots.txt 缓存无效，停止该来源')
            parser = RobotFileParser()
            parser.parse(rules.splitlines())
        else:
            raise WebError('ROBOTS_UNKNOWN', f'无法确认 robots.txt（HTTP {status}），停止该来源')
        crawl_delay = (parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 0) if parser else 0
        if limiter and not (saved and saved.get('status') in {200, 404, 410}):
            _limit_call(limiter, 'robots_put', {'origin': origin, 'status': status,
                        'rules_text': rules, 'crawl_delay': float(crawl_delay)})
        cached = (time.monotonic() + ttl, parser, float(crawl_delay))
        _robots_cache[origin] = cached
    _, parser, crawl_delay = cached
    if parser and not parser.can_fetch(USER_AGENT, url):
        raise WebError('ROBOTS_DISALLOWED', 'robots.txt 不允许采集当前页面')
    return max(delay, crawl_delay)


def _restricted_url(url: str) -> bool:
    return bool(re.search(r'/(?:login|log-in|signin|sign-in|auth|account|checkout|captcha)(?:[/?#.]|$)',
                          unquote(urlsplit(url).path), re.I))


def safe_http(url: str, *, delay: float = 2, allowed_hosts: set[str] | None = None,
              method: str = 'GET', body: bytes | None = None) -> dict:
    """Check robots and public DNS for every hop and connect only to that validated IP."""
    current = normalize_url(url)
    for hop in range(MAX_REDIRECTS + 1):
        current_host = urlsplit(current).hostname
        if allowed_hosts and current_host not in allowed_hosts:
            raise WebError('OFFSITE_REDIRECT', '跳转离开已确认官网，停止自动采集')
        if _restricted_url(current):
            raise WebError('LOGIN_REQUIRED', '登录 / 私密 / 验证页面不采集')
        actual_delay = _robots_rule(current, max(1, float(delay)))
        current, status, headers, raw = _rate_limited_request(current, delay=actual_delay, method=method, body=body)
        if status in (301, 302, 303, 307, 308):
            if hop == MAX_REDIRECTS or not headers.get('location'):
                raise WebError('REDIRECT_LIMIT', '跳转次数超限或缺少目标')
            current = normalize_url(urljoin(current, headers['location']))
            if status in (301, 302, 303):
                method, body = 'GET', None
            continue
        if status in (401, 403):
            raise WebError('ACCESS_BLOCKED', f'来源拒绝访问（HTTP {status}），不尝试绕过')
        if status == 429:
            raise WebError('RATE_LIMITED', '来源限流，停止请求等待恢复', retryable=True)
        if status != 200:
            raise WebError('HTTP_ERROR', f'来源返回 HTTP {status}', retryable=status >= 500)
        content_type = headers.get('content-type', 'text/html').lower()
        if not any(part in content_type for part in ('html', 'text/plain', 'text/csv', 'application/csv',
                                                     'application/json', 'application/octet-stream')):
            raise WebError('UNSUPPORTED_CONTENT', '当前适配器仅解析公开 HTML / 文本 / CSV / JSON')
        charset = re.search(r'charset=([\w-]+)', content_type)
        encoding = charset.group(1) if charset else 'utf-8-sig'
        try:
            html = raw.decode(encoding, errors='replace')
        except LookupError:
            html = raw.decode('utf-8', errors='replace')
        soup = parsed_html(html)
        text = soup.get_text(' ', strip=True)
        if soup.select_one('input[type="password"], iframe[src*="recaptcha"], [class*="g-recaptcha"]') or any(
            marker in text.lower() for marker in ('verify you are human', 'complete the captcha',
                'sign in to continue', 'log in to continue', 'access denied', 'checking your browser',
                'enable javascript and cookies to continue')):
            raise WebError('ACCESS_BLOCKED', '页面要求登录 / 验证或拒绝访问，停止采集')
        return {'final_url': current, 'html': html, 'text': text, 'fetched_at': utc_now(),
                'source_type': 'official_site', 'fetch_status': 'ok', 'http_status': status,
                'robots_status': 'allowed', 'demo_only': False}
    raise WebError('REDIRECT_LIMIT', '跳转次数超过限制')
