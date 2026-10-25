"""Small, deterministic checks for literal observations and website association.

These checks cannot prove that an Agent opened a page. They prevent an assertion
or an unrelated excerpt from silently becoming field-level public evidence.
"""
from datetime import datetime, timezone
import html
import json
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import phonenumbers


UNOBSERVED_TYPES = {'inferred', 'search', 'search_snippet', 'search_result', 'search_summary'}


def _name(value):
    return re.sub(r'[\W_]+', '', str(value or '').casefold())


def _host(value):
    try:
        return (urlsplit(str(value or '')).hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ''


def company_name_pattern(name):
    """Keep ordinary legal-suffix punctuation from changing the subject."""
    name = str(name or '').strip()
    words = list(re.finditer(r'\w+', name))
    suffixes = {'inc', 'co', 'corp', 'ltd', 'llc', 'plc'}
    suffix = len(words)
    while suffix and words[suffix - 1].group().casefold() in suffixes:
        suffix -= 1
    if suffix < len(words) and suffix:
        base = name[:words[suffix].start()].rstrip(' ,.')
        separator = r'(?:\s*,\s*|\s+)'
        legal = separator.join(re.escape(word.group()) + r'\.?' for word in words[suffix:])
        return re.escape(base).replace(r'\ ', r'\s+') + separator + legal
    return re.escape(name).replace(r'\ ', r'\s+')


def observation_valid(source, *, now=None, require_excerpt=True):
    if not isinstance(source, dict) or not source.get('source_url'):
        return False
    from ..web.security import normalize_url
    try:
        normalize_url(source['source_url'], fixture=bool(source.get('demo_only')))
        when = datetime.fromisoformat(str(source.get('retrieved_at') or '').replace('Z', '+00:00'))
        current = now or datetime.now(timezone.utc)
        if isinstance(current, str):
            current = datetime.fromisoformat(current.replace('Z', '+00:00'))
        if when.tzinfo is None or when > current:
            return False
    except Exception:
        # URL validation uses the local WebError; no DNS or HTTP is performed.
        return False
    method = str(source.get('method') or '').casefold()
    return (source.get('source_type') not in UNOBSERVED_TYPES and 'inferred' not in method
            and (not require_excerpt or bool(str(source.get('source_excerpt') or '').strip())))


def literal_value(field, value, excerpt, country=None):
    text = unquote(html.unescape(str(excerpt or '')))
    if not text or value in (None, ''):
        return False
    if field == 'email':
        target = unquote(html.unescape(str(value))).removeprefix('mailto:').split('?', 1)[0].strip()
        return bool(re.search(r'(?<![\w.+-])' + re.escape(target) + r'(?![\w.-])', text, re.I))
    if field == 'phone':
        try:
            target = phonenumbers.parse(str(value), country or None)
            number = phonenumbers.format_number(target, phonenumbers.PhoneNumberFormat.E164)
            return any(phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164) == number
                       and (not target.extension or target.extension == match.number.extension)
                       for match in phonenumbers.PhoneNumberMatcher(text, country or None,
                           leniency=phonenumbers.Leniency.POSSIBLE))
        except (ValueError, phonenumbers.NumberParseException):
            return False
    tokens = re.findall(r'\w+', str(value).casefold())
    return bool(tokens and re.search(r'(?<!\w)' + r'[\W_]*'.join(re.escape(token) for token in tokens) + r'(?!\w)', text.casefold()))


def subject_excerpt(name, excerpt):
    """The named enterprise must be the excerpt's subject, not a mention.

    Observations should retain the company's heading followed by the relevant
    fact. A customer mention elsewhere on another enterprise's page is not a
    subject association, even when both the name and fact are present.
    """
    text = unquote(html.unescape(str(excerpt or ''))).strip()
    tokens = re.findall(r'\w+', str(name or '').casefold())
    if not tokens:
        return False
    label = r'(?:(?:company|legal|registered|business)(?:\s+name)?|profile(?:\s+for)?)\s*[:\-]\s*'
    pattern = r'^\s*(?:' + label + r')?' + company_name_pattern(name) + r'(?!\w)'
    match = re.search(pattern, text, re.I)
    if not match:
        return False
    first_clause = re.split(r'[.!?;。；\n]', text[match.end():], maxsplit=1)[0]
    return not re.search(r'\b(?:customer|client|user|supplier)\s+(?:of|is|for)|\b(?:our|a|the)\s+(?:customer|client|user)\b', first_clause, re.I)


def channel_excerpt_owned(name, field, value, excerpt, country=None):
    if subject_excerpt(name, excerpt):
        return True
    if literal_value('company_name', name, excerpt):
        return False  # Incidental mentions cannot attest to channel ownership.
    text = unquote(html.unescape(str(excerpt or ''))).strip()
    if not literal_value(field, value, text, country):
        return False
    # A short literal channel/label is admissible with separate page-subject
    # evidence. Prose headed by another company's name is not such a label.
    prefix = re.sub(r'^(?:business\s+)?(?:email|e-mail|telephone|phone|tel|contact|call\s+us(?:\s+on)?)\s*[:\-]?\s*', '', text, flags=re.I)
    return literal_value(field, value, prefix, country) and (
        prefix.casefold().startswith(str(value).casefold()) if field == 'email'
        else bool(re.match(r'^[+(\d]', prefix)))


def imported_channel_sources(candidate, field, value, *, now=None):
    """A top-level channel is a request to retain observations, never evidence."""
    result = []
    for source in candidate.get('sources') or []:
        if not observation_valid(source, now=now):
            continue
        observed = source.get('value') if source.get('field') == field else source.get(field)
        values = observed if isinstance(observed, list) else [observed]
        if any(literal_value(field, value, str(item), candidate.get('country_code')) for item in values if item):
            if (literal_value(field, value, source.get('source_excerpt'), candidate.get('country_code'))
                    and channel_excerpt_owned(candidate.get('company_name'), field, value,
                                              source.get('source_excerpt'), candidate.get('country_code'))):
                result.append(source)
    return result


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        if '@graph' in value:
            yield from _nodes(value['@graph'])


def website_identity(candidate, pages=(), *, now=None):
    """Confirm explicit observed links or a name plus corroborated location.

    A matching company name, domain-shaped name, or a self-declared status alone
    never establishes ownership. Group domains may be explicitly associated with
    more than one enterprise, so a confirmed directory link remains admissible.
    """
    website = candidate.get('website_url') or ''
    own_host = _host(website)
    name = candidate.get('company_name') or ''
    result = {'status': 'unconfirmed' if own_host else 'not_supplied', 'evidence': []}
    if not own_host or candidate.get('company_name_is_domain_hint'):
        return result
    for conflict in candidate.get('conflicts') or []:
        if isinstance(conflict, dict) and conflict.get('field') in {'domain', 'website_url', 'website'}:
            hosts = {_host(value) for value in conflict.get('values') or []} - {''}
            if len(hosts | {own_host}) > 1:
                return {'status': 'conflicting', 'evidence': [], 'reason': 'conflicting_website_claims'}
    # The excerpt must actually show the company-to-domain association. Merely
    # placing website_url next to an unrelated excerpt does not count.
    for source in candidate.get('sources') or []:
        if not observation_valid(source, now=now):
            continue
        declared = source.get('value') if source.get('field') == 'website_url' else source.get('website_url')
        text = unquote(html.unescape(str(source.get('source_excerpt') or '')))
        if (_host(declared) == own_host and subject_excerpt(name, text)
                and re.search(r'(?<![\w.-])(?:www\.)?' + re.escape(own_host) + r'(?![\w.-])', text, re.I)):
            result['evidence'].append({'source_url': source['source_url'], 'retrieved_at': source['retrieved_at'],
                'source_excerpt': source['source_excerpt'], 'method': 'observed_company_website_association'})
        elif (_host(declared) == own_host and _host(source['source_url']) == own_host
                and subject_excerpt(name, text)
                and any(candidate.get(field) and literal_value(field, candidate[field], text) for field in ('address', 'city'))):
            result['evidence'].append({'source_url': source['source_url'], 'retrieved_at': source['retrieved_at'],
                'source_excerpt': source['source_excerpt'], 'method': 'official_name_and_corroborated_location'})
    if result['evidence']:
        result['status'] = 'confirmed'
    mismatched = []
    matching_page_subject = False
    for page in pages:
        if page.get('error') or page.get('fetch_status') not in (None, 'ok', 'success', 'completed', 'fetched', 200, '200'):
            continue
        url = page.get('final_url') or page.get('url')
        if _host(url) != own_host:
            continue
        soup = BeautifulSoup(page.get('html') or '', 'html.parser')
        names = [tag.get('content') for tag in soup.select('meta[property="og:site_name"]') if tag.get('content')]
        heading_match = any(_name(tag.get_text(' ', strip=True)) == _name(name) for tag in soup.select('h1'))
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                for node in _nodes(json.loads(script.get_text())):
                    kinds = node.get('@type') or []
                    kinds = [kinds] if isinstance(kinds, str) else kinds
                    if any(str(kind).casefold() in {'organization', 'corporation', 'localbusiness', 'professionalservice'} for kind in kinds):
                        if any(isinstance(node.get(key), str) and _host(urljoin(url, node[key])) != own_host
                               for key in ('url', '@id')):
                            continue
                        if node.get('name'):
                            names.append(node['name'])
            except (ValueError, TypeError):
                pass
        text = soup.get_text(' ', strip=True) + ' ' + str(page.get('text') or '')
        # Explicit publisher/Organization names take precedence over incidental
        # prose. A subsidiary needs its own heading/Organization association.
        name_seen = heading_match or any(_name(item) == _name(name) for item in names)
        if not names and not soup.select('h1'):
            name_seen = subject_excerpt(name, text)
        matching_page_subject = matching_page_subject or name_seen
        # Corroborate an actual operating address or locality, rather than a broad
        # country shared by thousands of unrelated companies.
        location = next((str(candidate[field]) for field in ('address', 'city')
                         if candidate.get(field) and literal_value(field, candidate[field], str(page.get('html') or '') + ' ' + text)), None)
        if name_seen and location:
            result['evidence'].append({'source_url': url, 'retrieved_at': page.get('fetched_at'),
                'source_excerpt': f'{name}; {location}', 'method': 'official_name_and_corroborated_location'})
        elif names and not name_seen:
            mismatched.append({'source_url': url, 'observed_names': names})
    if mismatched and not matching_page_subject:
        result.update(status='conflicting', reason='official_company_name_mismatch', mismatches=mismatched)
    elif result['evidence']:
        result['status'] = 'confirmed'
    return result
