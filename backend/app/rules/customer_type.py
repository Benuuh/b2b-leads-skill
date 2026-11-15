"""Deterministic customer-nature classification for readable delivery.

The result is a reading aid, not a qualification fact: it never changes a
decision, a score or a count.  ``business.models`` evidence wins when the caller
has it; otherwise the company name and the observed text are matched against a
small, ordered phrase table.  The rules are literal, case-insensitive phrase
matches with word boundaries, so the same input always yields the same label.
"""
from __future__ import annotations

from app.capabilities import BUSINESS_MODELS
from app.rules.policy import phrase_present

CUSTOMER_TYPES = ('租赁/活动', '制造/工厂', '批发/进口', '零售/门店', '园艺中心',
                  '泳池/庭院服务', '电商', '设计/安装', '其他')

# Observed business models are the strongest available signal.
MODEL_TYPES = {
    'manufacturer': '制造/工厂',
    'oem': '制造/工厂',
    'distributor': '批发/进口',
    'trading_company': '批发/进口',
    'retailer': '零售/门店',
    'reseller': '零售/门店',
    'service_provider': '设计/安装',
    'integrator': '设计/安装',
    'logistics_provider': '其他',
}

# Ordered from the most specific trade to the most generic word.  A name like
# "Patio Furniture Manufacturing" is a factory before it is a store; a plain
# "Patio" business is read as a patio/furniture retailer.
RULES = (
    ('园艺中心', ('garden center', 'garden centre', 'nursery', 'plant nursery')),
    ('租赁/活动', ('party rental', 'rental', 'rentals', 'event rental', 'event services',
                 'events', 'tents', 'bounce house')),
    ('制造/工厂', ('manufacturing', 'manufacturer', 'mfg', 'factory', 'fabricat',
                 'millwork', 'woodworks')),
    ('泳池/庭院服务', ('pool service', 'pool & spa', 'pool and spa', 'patio service',
                     'landscap', 'irrigation', 'lawn care')),
    ('电商', ('e-commerce', 'ecommerce', 'online store', 'web shop', 'webshop')),
    ('设计/安装', ('design', 'interior', 'install', 'architect', 'studio', 'contractor')),
    ('批发/进口', ('wholesale', 'distribut', 'supply', 'supplies', 'import', 'export',
                 'trading', 'warehouse')),
    ('零售/门店', ('store', 'shop', 'retail', 'outlet', 'showroom', 'furniture', 'patio')),
)

_SYNONYM = {word.casefold(): name for name, words in BUSINESS_MODELS.items() for word in words}


def _model(value):
    text = str(value or '').strip().casefold()
    return _SYNONYM.get(text, text)


def classify_customer_type(name='', business_models=(), text=''):
    """Return ``{'type': 分类, 'basis': 依据, 'matched': 命中短语}``.

    ``basis`` is ``business_models`` when an observed model decided the label,
    ``name_text`` for a keyword hit, and ``none`` for the ``其他`` fallback.
    """
    for value in business_models or ():
        label = MODEL_TYPES.get(_model(value))
        if label:
            return {'type': label, 'basis': 'business_models', 'matched': str(value)}
    haystack = ' '.join(str(item) for item in (name, text) if item)
    for label, phrases in RULES:
        for phrase in phrases:
            if phrase_present(haystack, phrase):
                return {'type': label, 'basis': 'name_text', 'matched': phrase}
    return {'type': '其他', 'basis': 'none', 'matched': None}
