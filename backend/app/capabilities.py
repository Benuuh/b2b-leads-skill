"""Versioned capability registry shared by configuration, rules and the CLI.

The registry is the only place that enumerates supported fact fields, operators
and bounded execution limits.  Nothing here performs I/O, so ``capabilities``
and input validation stay offline and side-effect free.
"""
from __future__ import annotations

from copy import deepcopy

from app.errors import AppError

SPEC_VERSIONS = (1, 2)
EVIDENCE_VERSIONS = ('evidence-1', 'evidence-2')
DEFAULT_EVIDENCE_VERSION = 'evidence-2'
RULE_VERSION = 'b2b-policy-2'
METRICS_VERSION = 'b2b-research-metrics-1'
CHANNEL_POLICY_VERSION = 'b2b-channel-policy-1'
IDENTITY_VERSION = 'b2b-identity-2'

# Bounded core limits. A task configuration may narrow these values, never
# widen them: the saved run remains the single denominator for budgets.
LIMITS = {
    'quantity': {'min': 50, 'max': 1000, 'default': 200},
    'candidate_multiplier': 3,
    'page_limit_per_company': {'min': 1, 'max': 10, 'default': 10},
    'page_limit_total': {'min': 1, 'max': 10000, 'default': 10000},
    'max_jobs_per_process': {'min': 1, 'max': 20, 'default': 20},
    'max_seconds_per_process': {'min': 1, 'max': 300, 'default': 60},
    'time_limit_hours': {'min': 1, 'max': 72, 'default': 8},
    'candidates_per_chunk': 20,
    'reassess_companies': 20,
    'company_workers': {'min': 1, 'max': 4, 'default': 2},
    'native_inflight': {'min': 1, 'max': 2, 'default': 2},
    'snapshot_every_new_qualified': {'min': 0, 'max': 1000, 'default': 10},
    'snapshot_interval_seconds': {'min': 60, 'max': 86400, 'default': 900},
    'circuit_breaker_failures': {'min': 3, 'max': 20, 'default': 5},
    'checkpoint_characters': 20000,
    'paid_sources': 0,
}

BUSINESS_MODELS = {
    'manufacturer': ('manufacturer', 'manufactures', 'manufacturing', 'producer', 'produces',
                     'producing', 'maker', 'makes', 'fabricator', 'factory'),
    'distributor': ('distributor', 'distributes', 'distribution', 'wholesaler', 'wholesale'),
    'retailer': ('retailer', 'retail', 'shop', 'store', 'stores'),
    'reseller': ('reseller', 'resells', 'value added reseller', 'var'),
    'integrator': ('integrator', 'integration', 'integrates', 'system integrator'),
    'service_provider': ('service provider', 'services provider', 'consultancy', 'consultant',
                         'contractor', 'installation', 'installs'),
    'trading_company': ('trading company', 'trading house', 'import and export', 'importer', 'exporter'),
    'logistics_provider': ('logistics provider', 'freight forwarder', 'forwarding', 'carrier', '3pl'),
    'oem': ('oem', 'original equipment manufacturer', 'private label'),
}

ENTITY_RELATIONSHIPS = ('independent', 'subsidiary', 'branch', 'unknown')

# Each field declares the fact it carries, the evidence a value needs and the
# unknown handling. ``single`` fields cannot legitimately hold two values for
# one enterprise; disagreeing observations become a conflict, not a guess.
FIELDS = {
    'entity.country_code': {
        'label': '经营国家', 'type': 'country', 'single': True,
        'subject': 'entity', 'operators': ['in', 'not_in'],
        'evidence': 'subject_bound_country_observation', 'unknown': 'missing_fact',
    },
    'entity.headquarters_country': {
        'label': '总部国家', 'type': 'country', 'single': True,
        'subject': 'entity', 'operators': ['in', 'not_in'],
        'evidence': 'subject_bound_country_observation', 'unknown': 'missing_fact',
    },
    'entity.registered_country': {
        'label': '注册国家', 'type': 'country', 'single': True,
        'subject': 'entity', 'operators': ['in', 'not_in'],
        'evidence': 'subject_bound_country_observation', 'unknown': 'missing_fact',
    },
    'entity.relationship': {
        'label': '主体关系', 'type': 'enum', 'single': True,
        'values': list(ENTITY_RELATIONSHIPS), 'subject': 'entity',
        'operators': ['equals', 'in', 'not_in'],
        'evidence': 'subject_bound_relationship_observation', 'unknown': 'missing_fact',
    },
    'entity.region': {
        'label': '地区', 'type': 'text', 'single': True, 'subject': 'entity',
        'operators': ['includes_any'],
        'evidence': 'subject_bound_text_observation', 'unknown': 'missing_fact',
    },
    'business.models': {
        'label': '业务模式', 'type': 'enum_list', 'single': False, 'values': sorted(BUSINESS_MODELS),
        'subject': 'entity', 'operators': ['includes', 'includes_any'],
        'evidence': 'subject_bound_model_observation', 'unknown': 'missing_fact',
    },
    'business.type': {
        'label': '企业类型', 'type': 'text_list', 'single': False, 'subject': 'entity',
        'operators': ['includes', 'includes_any'],
        'evidence': 'subject_bound_text_observation', 'unknown': 'missing_fact',
    },
    'offering.products': {
        'label': '自身提供的产品', 'type': 'text_list', 'single': False, 'subject': 'entity',
        'operators': ['includes', 'includes_any', 'provides'],
        'evidence': 'subject_bound_offering_observation', 'unknown': 'missing_fact',
    },
    'offering.services': {
        'label': '自身提供的服务', 'type': 'text_list', 'single': False, 'subject': 'entity',
        'operators': ['includes', 'includes_any', 'provides'],
        'evidence': 'subject_bound_offering_observation', 'unknown': 'missing_fact',
    },
    'customer.industries': {
        'label': '服务的客户行业', 'type': 'text_list', 'single': False, 'subject': 'entity',
        'operators': ['includes', 'includes_any'],
        'evidence': 'subject_bound_customer_observation', 'unknown': 'missing_fact',
    },
    'size.employees': {
        'label': '员工规模', 'type': 'range', 'single': True, 'subject': 'entity',
        'operators': ['within'],
        'evidence': 'profile_range', 'unknown': 'missing_fact',
    },
    'size.revenue': {
        'label': '营收规模', 'type': 'range', 'single': True, 'subject': 'entity',
        'operators': ['within'],
        'evidence': 'profile_range', 'unknown': 'missing_fact',
    },
    'industry.match': {
        'label': '行业匹配', 'type': 'industry', 'single': False, 'subject': 'entity',
        'operators': ['matches'],
        'evidence': 'subject_bound_industry_observation', 'unknown': 'no_hit_is_unknown',
    },
    'evidence.exclude_terms': {
        'label': '排除词', 'type': 'term_list', 'single': False, 'subject': 'entity',
        'operators': ['contains_any'],
        'evidence': 'subject_bound_literal_observation', 'unknown': 'no_hit_is_unknown',
    },
}

OPERATORS = {
    'in': {'label': '属于集合', 'types': ['country'], 'value': 'string_list'},
    'not_in': {'label': '不属于集合', 'types': ['country', 'enum'], 'value': 'string_list'},
    'equals': {'label': '等于', 'types': ['enum'], 'value': 'string'},
    'includes': {'label': '包含', 'types': ['enum_list', 'text_list'], 'value': 'string'},
    'includes_any': {'label': '包含任一', 'types': ['enum_list', 'text_list', 'text'], 'value': 'string_list'},
    'provides': {'label': '自身提供', 'types': ['text_list'], 'value': 'string'},
    'within': {'label': '区间相交', 'types': ['range'], 'value': 'range'},
    'matches': {'label': '行业语义匹配', 'types': ['industry'], 'value': 'string_list'},
    'contains_any': {'label': '字面命中任一', 'types': ['term_list'], 'value': 'string_list'},
}

CHANNEL_TYPES = {
    'business_email': {'label': '公开商务邮箱', 'requires': 'field_observation_with_literal_value'},
    'business_phone': {'label': '公开商务电话', 'requires': 'field_observation_with_literal_value'},
    'contact_form': {'label': '公开商务表单', 'requires': 'observed_form_fields'},
}

CONTACT_MODES = ('any', 'all')
COUNTRY_SCOPES = {
    'operating': 'entity.country_code',
    'headquarters': 'entity.headquarters_country',
    'registration': 'entity.registered_country',
}
DEFAULT_CONTACT_POLICY = {
    'mode': 'any',
    'allowed_types': list(CHANNEL_TYPES),
    'named_person_required': False,
    'required_roles': [],
}
DEFAULT_EVIDENCE_POLICY = {'freshness_days': 180, 'evidence_version': DEFAULT_EVIDENCE_VERSION}
DEFAULT_EXECUTION_POLICY = {
    'company_workers': LIMITS['company_workers']['default'],
    'native_inflight': LIMITS['native_inflight']['default'],
    'snapshot_every_new_qualified': LIMITS['snapshot_every_new_qualified']['default'],
    'snapshot_interval_seconds': LIMITS['snapshot_interval_seconds']['default'],
    'circuit_breaker_failures': LIMITS['circuit_breaker_failures']['default'],
}

FAILURE_CATEGORIES = {
    'input_invalid': '配置或输入证据结构不合法；未开始研究',
    'fact_unknown': '必要事实未知；进入补证而非判定失败',
    'subject_conflict': '主体或事实来源冲突；必须复核',
    'transient_network': '暂时网络故障；预算内有限重试',
    'access_denied': '来源拒绝或 robots 限制；改用允许来源或保留待核',
    'capacity': '工具或模型容量不足；停止重复派发',
    'budget_exhausted': '页面、候选或时间预算耗尽',
    'evidence_unavailable': '快照缺失、过期或校验失败',
    'tool_failure': '未分类的实现或工具错误',
}


def _condition_error(code, message, condition, **extra):
    details = {'condition_id': condition.get('id'), 'field': condition.get('field'),
               'operator': condition.get('operator'), **extra}
    return AppError(code, message, details=details)


def normalize_condition(condition, *, path='required_all'):
    """Validate one condition against the registry and return a canonical copy."""
    if not isinstance(condition, dict):
        raise AppError('UNSUPPORTED_CONDITION', '条件必须是对象', details={'path': path})
    result = deepcopy(condition)
    field, operator = str(result.get('field') or ''), str(result.get('operator') or '')
    spec = FIELDS.get(field)
    if spec is None:
        raise _condition_error('UNSUPPORTED_CONDITION', f'不支持的事实字段: {field or "(空)"}', result)
    if operator not in spec['operators']:
        raise _condition_error('UNSUPPORTED_CONDITION',
            f'字段 {field} 不支持运算符 {operator or "(空)"}', result,
            supported=spec['operators'])
    policy = result.get('unknown_policy', 'review')
    if policy not in {'review', 'allow_unconfirmed'}:
        raise _condition_error('UNSUPPORTED_CONDITION', 'unknown_policy 仅支持 review 或 allow_unconfirmed', result)
    if path == 'required_all' and policy == 'allow_unconfirmed':
        raise _condition_error('UNSUPPORTED_CONDITION', '必要条件不支持 allow_unconfirmed', result)
    value = result.get('value')
    kind = spec['type']
    if kind == 'country':
        values = value if isinstance(value, list) else [value]
        if not values or any(not isinstance(item, str) or len(item) != 2 or not item.isalpha() for item in values) \
                or len(values) > 30:
            raise _condition_error('UNSUPPORTED_CONDITION', '国家条件必须使用 ISO 两位代码列表', result)
        result['value'] = list(dict.fromkeys(item.upper() for item in values))
    elif operator == 'equals' or operator == 'includes' or operator == 'provides':
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise _condition_error('UNSUPPORTED_CONDITION', '该运算符需要 1 至 500 字符的文本值', result)
        result['value'] = value.strip()
    elif operator in {'includes_any', 'matches', 'contains_any', 'in', 'not_in'}:
        if not isinstance(value, list) or not 1 <= len(value) <= 100 \
                or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in value):
            raise _condition_error('UNSUPPORTED_CONDITION', '该运算符需要 1 至 100 项的非空文本列表', result)
        result['value'] = list(dict.fromkeys(item.strip() for item in value))
    elif operator == 'within':
        from app.rules.qualification import range_bounds
        if not isinstance(value, str) or not value.strip() or len(value) > 120 or range_bounds(value) is None:
            raise _condition_error('UNSUPPORTED_CONDITION', '规模条件需要可比较的区间文本，如 50-200 或 2-5 million', result)
        result['value'] = value.strip()
    if kind == 'enum' and operator in {'equals', 'in', 'not_in'}:
        allowed = set(spec.get('values') or [])
        values = result['value'] if isinstance(result['value'], list) else [result['value']]
        if any(item not in allowed for item in values):
            raise _condition_error('UNSUPPORTED_CONDITION', f'字段 {field} 的取值不在能力范围内', result, supported=sorted(allowed))
    if kind == 'enum_list' and operator in {'includes', 'includes_any'}:
        allowed = set(spec.get('values') or [])
        values = result['value'] if isinstance(result['value'], list) else [result['value']]
        if any(item not in allowed for item in values):
            raise _condition_error('UNSUPPORTED_CONDITION', f'字段 {field} 的取值不在能力范围内', result, supported=sorted(allowed))
    if policy == 'allow_unconfirmed' and path != 'exclude_any':
        raise _condition_error('UNSUPPORTED_CONDITION', 'unknown_policy 只适用于排除条件', result)
    result.setdefault('unknown_policy', policy)
    result.setdefault('id', '')
    return result


def capabilities():
    """Return the static capability envelope; never touches the database."""
    from app.persistence import SCHEMA_REVISION
    from app.release import CORE_VERSION, SKILL_VERSION
    return {
        'core_version': CORE_VERSION,
        'skill_version': SKILL_VERSION,
        'schema_revision': SCHEMA_REVISION,
        'rule_version': RULE_VERSION,
        'identity_version': IDENTITY_VERSION,
        'channel_policy_version': CHANNEL_POLICY_VERSION,
        'metrics_version': METRICS_VERSION,
        'spec_versions': list(SPEC_VERSIONS),
        'default_spec_version': 1,
        'evidence_versions': list(EVIDENCE_VERSIONS),
        'default_evidence_version': DEFAULT_EVIDENCE_VERSION,
        'fast_path': {'command': 'advance', 'atomic_admission': True,
                      'replay': 'original_run_chunk_and_input', 'controller_starts_per_chunk': 1,
                      'speed_profile': {'company_workers': 4, 'snapshot_every_new_qualified': 5,
                                        'snapshot_interval_seconds': 300}},
        'condition_model': {
            'shape': ['id', 'field', 'operator', 'value'],
            'sets': ['required_all', 'exclude_any'],
            'nesting': False,
            'unknown_policy': ['review', 'allow_unconfirmed'],
            'truth_rules': ['required_fail_excludes', 'exclude_pass_excludes',
                            'required_unknown_or_conflict_needs_review',
                            'exclude_unknown_needs_review_by_default',
                            'conflict_always_needs_review'],
        },
        'fields': deepcopy(FIELDS),
        'operators': deepcopy(OPERATORS),
        'country_scopes': deepcopy(COUNTRY_SCOPES),
        'channel_policy': {'types': deepcopy(CHANNEL_TYPES), 'modes': list(CONTACT_MODES),
                           'default': deepcopy(DEFAULT_CONTACT_POLICY)},
        'business_models': {name: list(words) for name, words in BUSINESS_MODELS.items()},
        'entity_relationships': list(ENTITY_RELATIONSHIPS),
        'defaults': {'evidence_policy': deepcopy(DEFAULT_EVIDENCE_POLICY),
                     'execution_policy': deepcopy(DEFAULT_EXECUTION_POLICY),
                     'contact_policy': deepcopy(DEFAULT_CONTACT_POLICY),
                     'freshness_days': 180, 'quantity': 200},
        'limits': deepcopy(LIMITS),
        'failure_categories': deepcopy(FAILURE_CATEGORIES),
    }
