"""数据源适配器（spec b2b-leads-2.6 §3.1）。

把"查询条件"翻译成"候选公司列表"：

    输入：{ countries: ["US"], industry: "air freight forwarding", keywords: [], limit: 200 }
    输出：[{ name, domain?, country, city?, source, source_ref, raw }]

三个公开免费源：
- ``eu_ted``            欧盟招标公告库 TED Search API（正在公开采购的机构/企业）
- ``eurostat_comext``   欧盟贸易统计（HS 编码进口金额与趋势 → 国家/品类优先级信号）
- ``google_places``     Google Places Text Search（国家+行业+城市捞实体企业；需 API key）

覆盖不到东南亚，2.6 不解决，记进遗留（§7）。

失败处理（§3.1.6）由 service 层实现：source_error / source_empty /
source_limit / source_parse_error；三源全挂 → run 置 ``partial_no_source``。
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ..errors import AppError
from ..web.http import safe_http

USER_TIMEOUT_HINT = 'adapter 请求超时或被拒；记 source_error，不阻塞其他源'

TED_SEARCH_URL = 'https://api.ted.europa.eu/v3/notices/search'
PLACES_TEXT_SEARCH_URL = 'https://places.googleapis.com/v1/places:searchText'
# Eurostat Comext："EU trade since 1988 by HS2-4-6 and CN8"（ds-045409）。
# 维度：freq/reporter/partner/product/flow/time；数值单位千欧元。
EUROSTAT_COMEXT_URL = ('https://ec.europa.eu/eurostat/api/comext/dissemination'
                       '/statistics/1.0/data/ds-045409')

SOURCES = ('eu_ted', 'eurostat_comext', 'google_places')

# Eurostat data endpoints accept one ``time`` value per request in this
# adapter. The multi-period contract is not verified against the live API, so
# keep the serial path and cache each (geo, partner, product, period) answer
# instead of silently treating every error as "no data for that month".
_EUROSTAT_CACHE = {}
_EUROSTAT_CACHE_LIMIT = 1000


def _eurostat_cached(url, key):
    if key in _EUROSTAT_CACHE:
        return _EUROSTAT_CACHE[key]
    if len(_EUROSTAT_CACHE) >= _EUROSTAT_CACHE_LIMIT:
        _EUROSTAT_CACHE.clear()
    data = _api_json(url)[1]
    _EUROSTAT_CACHE[key] = data
    return data


def _api_json(url: str, *, method: str = 'GET', body: dict | None = None,
              headers: dict | None = None) -> tuple[int, dict]:
    """有界公开 API JSON 调用；复用 safe_http 的 DNS 固定、robots 与限流路径。"""
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
    merged = {'Accept': 'application/json'}
    if body is not None:
        merged['Content-Type'] = 'application/json'
    merged.update(headers or {})
    page = safe_http(url, method=method, body=payload, headers=merged, delay=1)
    text = page.get('html') or page.get('text') or ''  # JSON 用原始响应文本，避免实体转义
    try:
        return page.get('http_status', 200), json.loads(text)
    except json.JSONDecodeError as error:
        raise AppError('SOURCE_PARSE_ERROR', '数据源返回了无法解析的 JSON',
                       details={'url': url}) from error


def _query_text(industry: str, keywords: list[str]) -> str:
    words = [str(industry or '').strip()] + [str(item).strip() for item in (keywords or [])]
    return ' '.join(dict.fromkeys(word for word in words if word))


ISO2_TO_ISO3 = {
    'AT': 'AUT', 'BE': 'BEL', 'BG': 'BGR', 'HR': 'HRV', 'CY': 'CYP', 'CZ': 'CZE',
    'DK': 'DNK', 'EE': 'EST', 'FI': 'FIN', 'FR': 'FRA', 'DE': 'DEU', 'GR': 'GRC',
    'HU': 'HUN', 'IE': 'IRL', 'IT': 'ITA', 'LV': 'LVA', 'LT': 'LTU', 'LU': 'LUX',
    'MT': 'MLT', 'NL': 'NLD', 'PL': 'POL', 'PT': 'PRT', 'RO': 'ROU', 'SK': 'SVK',
    'SI': 'SVN', 'ES': 'ESP', 'SE': 'SWE',
}


def _ted_value(value) -> str:
    """TED 字段可能是 {'eng': ['...']} 或列表或标量；取英文优先。"""
    if isinstance(value, dict):
        for key in ('eng', 'en', 'fra'):
            if value.get(key):
                return _ted_value(value[key])
        for item in value.values():
            text = _ted_value(item)
            if text:
                return text
        return ''
    if isinstance(value, list):
        return _ted_value(value[0]) if value else ''
    return str(value or '').strip()


def discover_eu_ted(query: dict) -> list[dict]:
    """TED 公开招标：正在采购的机构/买家（buyer 名称 + 国家）。

    专家查询语法：``buyer-country in (DEU) AND FT ~ "关键词"``（实测 v3 API）。
    """
    countries = [str(item).upper() for item in (query.get('countries') or []) if len(str(item)) == 2]
    limit = max(1, min(int(query.get('limit') or 50), 200))
    text = _query_text(query.get('industry'), query.get('keywords')) or 'supply'
    clauses = []
    iso3 = [ISO2_TO_ISO3[code] for code in countries if code in ISO2_TO_ISO3]
    if iso3:
        clauses.append('buyer-country in (' + ', '.join(iso3[:10]) + ')')
    clauses.append(f'FT ~ "{text}"')
    body = {
        'query': ' AND '.join(clauses),
        'fields': ['notice-title', 'buyer-name', 'buyer-country', 'publication-number'],
        'limit': limit, 'page': 1,
    }
    _, data = _api_json(TED_SEARCH_URL, method='POST', body=body)
    results = []
    for notice in (data.get('notices') or [])[:limit]:
        fields = notice.get('fields') or notice
        name = _ted_value(fields.get('buyer-name'))
        if not name:
            continue
        country_raw = _ted_value(fields.get('buyer-country'))
        country = country_raw[:2].upper() if len(country_raw) == 2 else ''
        results.append({
            'name': name[:500],
            'domain': None,
            'country': country,
            'city': _ted_value(fields.get('place-of-performance'))[:255] or None,
            'source': 'eu_ted',
            'source_ref': 'ted:' + str(_ted_value(fields.get('publication-number'))
                                       or notice.get('id') or ''),
            'raw': notice,
        })
    return results[:limit]


def discover_eurostat_comext(query: dict) -> list[dict]:
    """Eurostat Comext：某 reporter 从 partner 进口某 HS 编码的月度金额与趋势。

    产出的是"国家/品类优先级信号"（未核实候选），不是公司名单——
    每条候选代表一个值得人工核实的进口流（国家 × 近 6 个月）。
    """
    from datetime import datetime, timezone
    countries = [str(item).upper() for item in (query.get('countries') or []) if len(str(item)) == 2]
    if not countries:
        return []
    limit = max(1, min(int(query.get('limit') or 50), 200))
    keywords = [str(item).strip() for item in (query.get('keywords') or [])]
    product = next((item for item in keywords if item.isdigit() and 2 <= len(item) <= 6), '84')
    partner = str(query.get('partner') or 'CN').upper()[:2]
    now = datetime.now(timezone.utc)
    months = []
    year, month = now.year, now.month
    for _ in range(6):  # 近 6 个月：金额与趋势（§3.1.2）
        months.append(f'{year}-{month:02d}')
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    results = []
    consecutive_failures = 0
    for geo in countries[:5]:
        for period in months:
            if len(results) >= limit:
                break
            url = (f'{EUROSTAT_COMEXT_URL}?format=JSON&freq=M&reporter={geo}'
                   f'&partner={partner}&product={product}&flow=1&time={period}')
            key = (geo, partner, product, period)
            try:
                data = _eurostat_cached(url, key)
            except AppError:
                consecutive_failures += 1
                # A single unpublished month is not a source outage. Repeated
                # failures with no usable result are classified for the caller.
                if consecutive_failures >= 3 and not results:
                    raise AppError('SOURCE_ERROR', 'Eurostat 连续请求失败；保留串行路径并停止该源',
                                   details={'source': 'eurostat_comext', 'failures': consecutive_failures})
                continue
            consecutive_failures = 0
            value = data.get('value') or {}
            for index, amount in value.items():
                results.append({
                    'name': f'{geo} 自 {partner} 进口 HS{product}（{period}）',
                    'domain': None, 'country': geo, 'city': None,
                    'source': 'eurostat_comext',
                    'source_ref': f'eurostat:ds-045409/{geo}/{partner}/HS{product}/{period}',
                    'raw': {'reporter': geo, 'partner': partner, 'product': product,
                            'time': period, 'amount_thousand_eur': amount, 'label':
                            data.get('label')},
                })
                break  # 每个时间段取一个聚合值
    return results[:limit]


def discover_google_places(query: dict) -> list[dict]:
    """Google Places Text Search：国家+行业+城市捞实体企业。需 PROVIDER 凭据。"""
    import os
    api_key = os.getenv('PROVIDER_CREDENTIAL_KEY', '').strip()
    if not api_key:
        raise AppError('SOURCE_LIMIT', 'google_places 需要 PROVIDER_CREDENTIAL_KEY；超限或缺失时停该源，不重试',
                       details={'source': 'google_places'})
    countries = [str(item).upper() for item in (query.get('countries') or []) if len(str(item)) == 2]
    limit = max(1, min(int(query.get('limit') or 20), 20))
    text = _query_text(query.get('industry'), query.get('keywords'))
    if countries:
        text = f'{text} in ' + ' or '.join(countries)
    headers = {'X-Goog-Api-Key': api_key,
               'X-Goog-FieldMask': 'places.displayName,places.formattedAddress,places.websiteUri,places.addressComponents'}
    _, data = _api_json(PLACES_TEXT_SEARCH_URL, method='POST', body={'textQuery': text, 'pageSize': limit},
                        headers=headers)
    results = []
    for place in (data.get('places') or [])[:limit]:
        components = place.get('addressComponents') or []
        country = next((item.get('shortText', '')[:2].upper() for item in components
                        if 'country' in (item.get('types') or [])), None)
        city = next((item.get('longText') for item in components
                     if 'locality' in (item.get('types') or [])), None)
        website = place.get('websiteUri') or None
        domain = urlsplit(website).hostname if website else None
        display = (place.get('displayName') or {}).get('text') if isinstance(place.get('displayName'), dict) else None
        results.append({
            'name': str(display or '').strip()[:500],
            'domain': domain,
            'country': country,
            'city': str(city)[:255] if city else None,
            'source': 'google_places',
            'source_ref': 'places:' + re.sub(r'\s+', ' ', str(place.get('formattedAddress') or ''))[:200],
            'raw': place,
        })
    return [item for item in results if item['name']]


ADAPTERS = {
    'eu_ted': discover_eu_ted,
    'eurostat_comext': discover_eurostat_comext,
    'google_places': discover_google_places,
}
