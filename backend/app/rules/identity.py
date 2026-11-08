"""One identity resolution shared by admission, core records and export.

Legal suffixes are normalized; aliases merge only when a retained observation
mentions both names; headquarters, branches and offices stay separate; two legal
entities on one domain are never merged by domain alone.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from app.rules.evidence import _name as _evidence_name
from app.rules.evidence import literal_value, observation_valid

LEGAL_SUFFIXES = {
    'inc', 'incorporated', 'llc', 'ltd', 'limited', 'corp', 'corporation', 'co', 'company',
    'gmbh', 'mbh', 'ag', 'kg', 'kgaa', 'ug', 'ohg', 'gbr', 'se',
    'bv', 'nv', 'cv', 'vof', 'sarl', 'sas', 'sa', 'sci', 'snc',
    'srl', 'srls', 'spa', 'sapa', 'sociedad', 'sl', 'slu',
    'plc', 'llp', 'lp', 'pty', 'pte', 'pvt', 'private',
    'oy', 'oyj', 'ab', 'as', 'asa', 'aps', 'kk', 'kabushiki',
    'ltda', 'sdn', 'bhd', 'sro', 'zrt', 'kft', 'sp', 'zoo', 'eood', 'ood', 'doo', 'ad',
    'jsc', 'ojsc', 'pjsc', 'ooo', 'zao', 'fze', 'fzc', 'llc-fz',
}


def normalize_legal_name(value) -> str:
    """Casefold, strip punctuation and drop trailing legal-form tokens."""
    words = re.findall(r'[0-9a-z\u00c0-\u024f\u4e00-\u9fff]+', str(value or '').casefold())
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return ''.join(words)


def _host(value) -> str:
    return (urlsplit(str(value or '')).hostname or '').lower().removeprefix('www.')


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def identity_key(candidate, *, version: int = 2) -> str:
    host = _host(candidate.get('website_url'))
    name = normalize_legal_name(candidate.get('company_name')) if version == 2 else \
        re.sub(r'[^\w]', '', str(candidate.get('company_name') or '').casefold())
    return _digest([host, name, str(candidate.get('country_code') or '').upper(),
                    candidate.get('branch_id') or candidate.get('branch_name') or '',
                    '' if host else re.sub(r'\W', '', str(candidate.get('address') or '').casefold())])


# TODO(dead?): 全仓零引用（S10 扫描）；保留以兼容外部调用，确认后删除。
def legacy_identity_key(candidate) -> str:
    return identity_key(candidate, version=1)


def verified_aliases(candidate):
    """Aliases proven by one observation that names both the entity and alias."""
    name = str(candidate.get('company_name') or '')
    result = []
    for entry in candidate.get('aliases') or []:
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict) or not observation_valid(entry):
            continue
        alias = str(entry.get('name') or '').strip()
        excerpt = str(entry.get('source_excerpt') or '')
        if not alias or not name:
            continue
        if literal_value('company_name', alias, excerpt) and literal_value('company_name', name, excerpt):
            result.append(alias)
    return list(dict.fromkeys(result))


def alias_keys(candidate, *, version: int = 2) -> list[str]:
    """Identity keys for verified aliases, so an alias cannot mint a candidate."""
    keys = []
    for alias in verified_aliases(candidate):
        probe = {**candidate, 'company_name': alias}
        keys.append(identity_key(probe, version=version))
    return list(dict.fromkeys(keys))


def plain_name(value) -> str:
    return re.sub(r'[\W_]+', '', str(value or '').casefold())


def display_identity(company, *, version: str = 'legacy'):
    """Export-facing identity. ``v2`` applies the same legal-name normalization."""
    domain = (company.get('domain') or company.get('website_url') or '')
    domain = _host(domain) if '://' in str(domain) else str(domain or '').lower().removeprefix('www.')
    name = normalize_legal_name(company.get('company_name')) if version == 'v2' else plain_name(company.get('company_name'))
    return (domain, name, str(company.get('country_code') or '').upper(),
            str(company.get('branch_id') or company.get('branch_name') or company.get('location_id')
                or (plain_name(company.get('address')) if not domain else '')))
