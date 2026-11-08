"""Bounded channel-policy normalization shared by stop decisions and delivery."""
from __future__ import annotations

from copy import deepcopy

CHANNEL_TYPES = ('business_email', 'business_phone', 'contact_form')
DEFAULT_POLICY = {'mode': 'any', 'allowed_types': list(CHANNEL_TYPES),
                  'named_person_required': False, 'required_roles': []}


def normalize_channel_policy(policy):
    """Return a complete policy; an absent policy keeps the legacy baseline."""
    if not policy:
        return deepcopy(DEFAULT_POLICY)
    if not isinstance(policy, dict):
        raise ValueError('渠道政策必须是对象')
    mode = policy.get('mode', 'any')
    if mode not in {'any', 'all'}:
        raise ValueError('渠道政策 mode 仅支持 any 或 all')
    allowed = list(policy.get('allowed_types') or CHANNEL_TYPES)
    if not allowed or any(item not in CHANNEL_TYPES for item in allowed):
        raise ValueError('渠道类型不在支持范围内')
    roles = [str(role).strip() for role in policy.get('required_roles') or [] if str(role).strip()]
    named = bool(policy.get('named_person_required'))
    if roles and not named:
        raise ValueError('指定岗位要求 named_person_required 为 true')
    return {'mode': mode, 'allowed_types': list(dict.fromkeys(allowed)),
            'named_person_required': named, 'required_roles': list(dict.fromkeys(roles))}


LEGACY_CHANNEL_NAMES = {'business_email': 'email', 'business_phone': 'phone',
                        'contact_form': 'contact_form'}


def legacy_channels(present):
    """Keep the historical delivery_channels vocabulary for existing consumers."""
    return [LEGACY_CHANNEL_NAMES[name] for name in present if name in LEGACY_CHANNEL_NAMES]


# TODO(dead?): 全仓零引用（S10 扫描）；保留以兼容外部调用，确认后删除。
def allowed_channels(policy):
    return normalize_channel_policy(policy)['allowed_types']
