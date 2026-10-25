"""Pure URL validation and public DNS checks for bounded website enrichment."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .errors import WebError

def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not address.is_multicast and not address.is_reserved and not (
            isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
    except ValueError:
        return False


def normalize_url(url: str, *, fixture: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url) or '\\' in url:
        raise WebError('UNSAFE_URL', 'URL 含无效或混淆字符')
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError('scheme or authority')
        if parsed.port not in (None, 80, 443):
            raise ValueError('port')
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if '%' in host or ':' in host and not public_ip(host):
            raise ValueError('encoded or private IP')
        if host in ('localhost', 'localhost.localdomain') or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')):
            raise ValueError('internal host')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Resolver-accepted decimal, octal, shortened and hexadecimal IPv4 are forbidden.
            if re.fullmatch(r'[0-9.]+', host) or any(part.lower().startswith('0x') for part in host.split('.')) or '.' not in host:
                raise ValueError('ambiguous IP')
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
                raise ValueError('invalid host')
            if host.endswith(('.test', '.invalid', '.example')) and not fixture:
                raise ValueError('test host')
        else:
            if not public_ip(host):
                raise ValueError('non-public IP')
        netloc = f'[{host}]' if ':' in host else host
        if parsed.port and parsed.port != (443 if parsed.scheme.lower() == 'https' else 80):
            netloc += ':' + str(parsed.port)
        # Preserve content parameters. Remove only recognized tracking keys; bound traps.
        path = parsed.path or '/'
        if any(ord(c) < 32 for c in unquote(path)):
            raise ValueError('encoded control character')
        parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20)
        tracking = {'gclid', 'fbclid', 'msclkid', 'mc_cid', 'mc_eid'}
        parameters = sorted((key, value) for key, value in parameters
                            if not key.lower().startswith('utm_') and key.lower() not in tracking)
        if any(len(key) > 100 or len(value) > 500 for key, value in parameters):
            raise ValueError('query trap')
        return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(parameters), ''))
    except (ValueError, UnicodeError) as error:
        raise WebError('UNSAFE_URL', '仅接受无凭据的公网 HTTP(S) 标准 URL') from error


def validate_public_url(url: str, resolver=None) -> tuple[str, list[str]]:
    normalized = normalize_url(url)
    host = urlsplit(normalized).hostname
    port = urlsplit(normalized).port or (443 if normalized.startswith('https:') else 80)
    resolver = resolver or socket.getaddrinfo
    try:
        addresses = sorted({record[4][0] for record in resolver(host, port, type=socket.SOCK_STREAM)})
    except (OSError, ValueError) as error:
        raise WebError('CONNECTION_ERROR', 'DNS 解析失败', retryable=True) from error
    if not addresses or any(not public_ip(address) for address in addresses):
        raise WebError('UNSAFE_URL', 'DNS 含非公网地址')
    return normalized, addresses


def check_redirect(url: str, allowed_hosts: tuple[str, ...], *, fixture=False, resolver=None) -> str:
    normalized = normalize_url(url, fixture=fixture)
    if urlsplit(normalized).hostname not in allowed_hosts:
        raise WebError('OFFSITE_REDIRECT', '跳转离开已确认官网，需人工核查', scope='company')
    if not fixture:
        validate_public_url(normalized, resolver=resolver)
    return normalized
