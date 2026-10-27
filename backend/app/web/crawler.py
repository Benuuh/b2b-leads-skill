"""Same-site website enrichment, capped at ten page attempts per company."""
from collections.abc import Callable
from copy import deepcopy
import html as html_lib
import re
from urllib.parse import unquote, urljoin, urlsplit
from bs4 import BeautifulSoup
from .errors import WebError
from .security import normalize_url
from .http import safe_http, utc_now, _restricted_url
from .html import page_content, rank_links
from .observation import observation_context

MAX_COMPANY_PAGES = 10


def _safe_website(value: str, base_url: str = '', *, fixture=False) -> str:
    value = str(value or '').strip()
    if not value:
        return ''
    if not value.startswith(('http://', 'https://', '//', '/')):
        value = 'https://' + value
    try:
        return normalize_url(urljoin(base_url, value), fixture=fixture)
    except WebError:
        return ''


def _company_link_priority(url: str, stage: str, segment=None) -> tuple:
    path = unquote(urlsplit(url).path).casefold()
    judgment = ('about', 'service', 'company', 'product', 'unternehmen', 'servicios',
                'nosotros', '关于', '服务', '产品', '会社')
    people = ('team', 'staff', 'people', 'leadership', 'management', 'employee',
              'person', 'equipe', 'équipe', '团队', '人员')
    contact = ('contact', 'kontakt', 'contacto', '联系', 'お問い合わせ')
    groups = (judgment, people, contact) if stage == 'qualification' else (people, contact, judgment)
    policy = ((segment or {}).get('qualification_policy') or {}).get('contact_policy') or {}
    if stage == 'contacts' and (segment or {}).get('spec_version') == 2 and not (
            policy.get('named_person_required') or policy.get('required_roles')):
        groups = (contact, judgment, people)
    priority = next((index for index, words in enumerate(groups) if any(word in path for word in words)), 3)
    return priority, len(path), url


def fetch_company(candidate: dict, segment: dict, *, fixture=False, max_pages=10,
                  should_stop: Callable[[list[dict]], bool] | None = None,
                  stage: str = 'contacts', initial_pages: list[dict] | None = None) -> list[dict]:
    """Fetch a bounded stage, reusing prior attempts and their discovered links.

    ``max_pages`` counts prior and failed page attempts, rather than granting a
    fresh allowance for each stage. Qualification always stops within two pages.
    The callback sees all retained pages after each successful fetch; reused
    successful pages are checked once before making another network request.
    """
    if stage not in ('qualification', 'contacts'):
        raise ValueError('无效的企业抓取阶段')
    limit = min(MAX_COMPANY_PAGES, max(0, int(max_pages)))
    if stage == 'qualification':
        limit = min(2, limit)
    pages = deepcopy(list(initial_pages or [])[:limit])
    website = _safe_website(candidate.get('website_url') or '', fixture=fixture)
    if not website or not limit:
        return pages
    if should_stop and any(page.get('fetch_status') == 'ok' for page in pages) and should_stop(pages):
        return pages
    if len(pages) >= limit:
        return pages
    fatal_statuses = {'access_blocked', 'login_required', 'robots_unknown', 'unsafe_url', 'rate_limited'}
    if any(page.get('fetch_status') in fatal_statuses for page in pages):
        return pages
    if fixture:
        # Fixture companies expose one synthetic homepage. A second stage reuses
        # it and must never manufacture a duplicate or perform a network request.
        if pages:
            return pages
        name = html_lib.escape(str(candidate.get('company_name') or '演示企业'))
        email = html_lib.escape(str(candidate.get('email') or f'info@{urlsplit(website).hostname}'))
        phone = html_lib.escape(str(candidate.get('phone') or '+12025550100'))
        industry = html_lib.escape(str(segment.get('industry') or ' '.join(segment.get('keywords') or [])))
        content = (f'<html><head><title>{name} - 演示</title></head><body><h1>{name}</h1>'
                   f'<p>{industry}</p><p>演示数据，仅用于功能测试。</p>'
                   f'<a href="mailto:{email}">{email}</a><a href="tel:{phone}">{phone}</a></body></html>')
        pages.append({'final_url': website, 'request_url': website, 'html': content,
                      'text': BeautifulSoup(content, 'html.parser').get_text(' ', strip=True),
                      'fetched_at': utc_now(), 'source_type': 'official_site', 'fetch_status': 'ok',
                      'http_status': 200, 'robots_status': 'fixture_not_checked', 'demo_only': True,
                      'depth': 0, 'crawl_stage': stage})
        if should_stop:
            should_stop(pages)
        return pages
    # Only the entered host and its www counterpart are accepted for website redirects.
    host = urlsplit(website).hostname
    bare = host[4:] if host.startswith('www.') else host
    allowed = {host, bare, 'www.' + bare}
    pending, seen = [], set()

    def safe_page_url(value: str) -> str:
        normalized = _safe_website(value, fixture=fixture)
        return normalized if normalized and urlsplit(normalized).hostname in allowed else ''

    def enqueue_links(page: dict, depth: int):
        final_url = safe_page_url(page.get('final_url') or '')
        if depth >= 2 or not final_url or _restricted_url(final_url):
            return
        _, links, _ = page_content(page.get('html') or '', final_url, tuple(allowed))
        queued = {item[0]: item[1] for item in pending}
        for link in rank_links(links):
            if link not in seen and not _restricted_url(link):
                queued[link] = min(queued.get(link, depth + 1), depth + 1)
        pending[:] = sorted(queued.items(), key=lambda item: (_company_link_priority(item[0], stage, segment), item[1]))[:limit * 3]

    for page in pages:
        for key in ('request_url', 'final_url'):
            prior_url = safe_page_url(page.get(key) or '')
            if prior_url:
                seen.add(prior_url)
    # Rebuild the frontier from every retained success so a resumed crawl can
    # follow links it has already paid to discover, even after a redirect.
    for page in pages:
        if page.get('fetch_status') == 'ok':
            depth = page.get('depth', 0 if safe_page_url(page.get('final_url') or '') == website else 1)
            if isinstance(depth, int) and depth >= 0:
                enqueue_links(page, depth)
    if website not in seen:
        pending.insert(0, (website, 0))
    while pending and len(pages) < limit:
        url, depth = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            with observation_context(purpose='qualification' if stage == 'qualification' else 'contact',
                                     source_id=candidate.get('source_id', 'unknown'),
                                     query_id=candidate.get('query_id', 'unknown')):
                page = safe_http(url, delay=max(1, float(segment.get('rate_limit_seconds') or 2)), allowed_hosts=allowed)
        except WebError as error:
            pages.append({'final_url': url, 'request_url': url, 'html': '', 'text': '', 'fetched_at': utc_now(),
                          'source_type': 'official_site', 'fetch_status': error.code.lower(),
                          'error': error.to_dict(), 'demo_only': False, 'depth': depth, 'crawl_stage': stage})
            if error.code.lower() in fatal_statuses:
                break
            continue
        page['depth'] = depth
        page['request_url'] = url
        page['crawl_stage'] = stage
        # The adapter observed this public page directly. Discovery origin stays
        # on the candidate; it is not substituted for a page's source identity.
        page['source_id'] = 'official-site'
        page['query_id'] = 'unknown'
        pages.append(page)
        seen.add(page['final_url'])
        if should_stop and should_stop(pages):
            break
        enqueue_links(page, depth)
    return pages
