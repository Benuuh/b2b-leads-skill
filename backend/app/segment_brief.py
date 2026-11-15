"""Offline segment brief and the confirmation hash shared with create.

``brief`` and ``runs.create`` both call these functions. They read only the
submitted spec and the in-process capability registry: no database, no network,
no clock, and no market conclusion. The confirmation hash proves that a later
create saw the same normalized spec and the same fixed plan values. It does
not prove who confirmed, what they wrote, or when they confirmed.
"""
from __future__ import annotations

from app.capabilities import (
    CHANNEL_TYPES, COUNTRY_SCOPES, EVIDENCE_LIMITS, FIELDS, LIMITS, OPERATORS,
    RULE_VERSION, SEGMENT_BRIEF_VERSION,
)
from app.errors import AppError
from app.schemas import SegmentSpec, SegmentSpecV2, parse_segment


def _hash(value):
    """Same canonical digest ``runs._core._hash`` uses for frozen specs.

    Imported lazily so this module can load without cycling through ``app.runs``.
    """
    from app.runs._core import _hash as canonical
    return canonical(value)

BRIEF_VERSION = SEGMENT_BRIEF_VERSION
V1_RULE_VERSION = 'v1-historical'

# Phrases the summary must keep visible. They describe limits; they are not a
# promise that a run will finish a quantity or a clock time.
EVIDENCE_LIMIT_MESSAGES = (
    '无自动 JS 渲染（automatic_js_rendering=unavailable）。抓取失败不作为负面资格证据，也不能绕过访问控制。',
    '共享目录页面预算合同 shared-page-budget-1 只冻结在新的 v2 批次上，用来统一记账。预算可用不等于目录自动采集可用，也不等于自动 JS 渲染可用。没有该合同的批次拒绝自动共享目录采集；外部工具计数不能代替核心预算。',
    '国家证据仍是旧版字面匹配（ISO 两位码及 pycountry 名称），没有完整别名和结构化地址语义。未命中不等于已经判定不符合。',
    '结构化发现源未覆盖东南亚（structured_discovery_southeast_asia=not_covered）。三源不可用时走 legacy-search，不得把遗留搜索冒充已覆盖。',
)
NON_PROMISE = '本确认不承诺找满目标数量、完成速度、8 小时交付或校准指标。目标数量和初始时限只是冻结的预算口径。'
SUPPRESSION_NOTE = '另适用当前全局抑制名单，运行中可能变化；本次确认不冻结该名单。'
CUSTOMER_FILE_NOTE = (
    '现有客户文件排除未作为独立机制接入。只有已经写入 spec.exclusions 的标识会随本配置生效；'
    '未写入的文件不会因展示或文件哈希而被排除。摘要不展示具体企业、联系人、邮箱或电话。'
)

SCOPE_LABELS = {
    'operating': '经营地',
    'headquarters': '总部',
    'registration': '注册地',
}
UNKNOWN_LABELS = {
    'review': '未知则待核，不视为不符合',
    'allow_unconfirmed': '未知不阻塞交付（仅排除条件可显式使用）',
}
MODE_LABELS = {
    'any': '任一允许渠道即可',
    'all': '每种允许渠道都必须具备',
}
# Empty positive descriptions are「未指定」. Empty restrictions are「不限」
# and must not be read as an exclusion.
UNSPECIFIED_PATHS = {
    'industry', 'industry_codes', 'keywords', 'example_domains', 'notes', 'languages',
}
UNLIMITED_PATHS = {
    'regions', 'employee_range', 'revenue_range', 'company_types', 'roles',
    'exclude_keywords',
}
CONDITION_KEYS = ('id', 'field', 'operator', 'value', 'unknown_policy', 'note')

# Every path the walker can emit needs a label. A new spec or condition field
# without a label fails the brief instead of disappearing from the summary.
LABELS = {
    'name': '任务名称',
    'industry': '行业',
    'industry_codes': '行业代码',
    'countries': '国家',
    'regions': '地区',
    'employee_range': '员工规模',
    'revenue_range': '营收规模',
    'company_types': '企业类型',
    'roles': '岗位要求',
    'keywords': '关键词',
    'exclude_keywords': '排除词',
    'example_domains': '示例企业域名',
    'exclusions': '排除标识',
    'languages': '语言',
    'quantity': '目标数量',
    'freshness_days': '证据有效期（天）',
    'notes': '备注',
    'paid_sources': '付费来源',
    'budget': '付费金额预算',
    'spec_version': '配置版本',
    'qualification_policy': '资格政策',
    'qualification_policy.country_scope': '国家口径',
    'qualification_policy.required_all': '必要条件（全部满足）',
    'qualification_policy.exclude_any': '排除条件（任一命中即排除）',
    'qualification_policy.contact_policy': '渠道政策',
    'qualification_policy.contact_policy.mode': '渠道满足方式',
    'qualification_policy.contact_policy.allowed_types': '允许的渠道类型',
    'qualification_policy.contact_policy.named_person_required': '具名联系人',
    'qualification_policy.contact_policy.required_roles': '要求岗位',
    'qualification_policy.derived_from': '资格政策来源',
    'evidence_policy': '证据政策',
    'evidence_policy.freshness_days': '证据有效期（天）',
    'evidence_policy.evidence_version': '证据版本',
    'execution_policy': '执行参数',
    'execution_policy.company_workers': '公司 worker 数',
    'execution_policy.native_inflight': 'native_inflight 登记值',
    'execution_policy.snapshot_every_new_qualified': '每新增可交付企业的快照间隔',
    'execution_policy.snapshot_interval_seconds': '快照时间间隔（秒）',
    'execution_policy.circuit_breaker_failures': '连续失败熔断阈值',
    'execution_policy.page_limit_per_company': '执行政策中的单企业页上限',
    'fixed_plan.time_limit_seconds': '初始累计研究时限（秒）',
    'fixed_plan.candidate_multiplier': '候选倍数',
    'fixed_plan.candidate_limit': '候选上限',
    'fixed_plan.page_limit_per_company': '批次单企业页上限',
    'fixed_plan.page_limit_total': '批次页面上限',
    'fixed_plan.page_budget_contract': '共享目录页面预算合同',
    'fixed_plan.paid_sources': '付费来源',
    'fixed_plan.budget': '付费金额预算',
    'fixed_plan.execution_mode': '执行模式',
    'fixed_plan.completion_goal': '完成目标',
}
for _key in CONDITION_KEYS:
    LABELS[f'qualification_policy.required_all[].{_key}'] = f'必要条件.{_key}'
    LABELS[f'qualification_policy.exclude_any[].{_key}'] = f'排除条件.{_key}'


def fixed_plan_values(spec, version=1):
    """Plan defaults create actually freezes. Not remembered by the caller."""
    quantity = int(spec['quantity'])
    multiplier = int(LIMITS['candidate_multiplier'])
    return {
        'time_limit_seconds': int(LIMITS['time_limit_hours']['default']) * 3600,
        'candidate_multiplier': multiplier,
        'candidate_limit': quantity * multiplier,
        'page_limit_per_company': int(LIMITS['page_limit_per_company']['default']),
        'page_limit_total': int(LIMITS['page_limit_total']['default']),
        'paid_sources': spec['paid_sources'],
        'budget': spec['budget'],
        'execution_mode': 'skill_research',
        'completion_goal': 'contactable',
        # v1 stays on the historical company counter and cannot open the ledger.
        'page_budget_contract': 'shared-page-budget-1' if version == 2 else None,
    }


def rule_version_for(version):
    """Version token inside the confirmation hash.

    v2 freezes ``b2b-policy-2``. v1 keeps its historical qualifier and must
    not be labeled as that condition model.
    """
    return RULE_VERSION if version == 2 else V1_RULE_VERSION


def confirm_material(spec, version, spec_hash):
    """Canonical confirmation scope. Prose, run id, and the clock are absent."""
    material = {
        'brief_version': BRIEF_VERSION,
        'spec_hash': spec_hash,
        'rule_version': rule_version_for(version),
        'fixed_plan': fixed_plan_values(spec, version),
    }
    if version == 2:
        material['evidence_version'] = spec['evidence_policy']['evidence_version']
        material['execution_policy'] = spec['execution_policy']
    return material


def build_run_plan(spec, version, *, spec_hash, confirm_hash, recorded_at, release):
    """Initial plan written by create. Fixed numbers come from ``fixed_plan_values``."""
    fixed = fixed_plan_values(spec, version)
    plan = {
        'execution_mode': fixed['execution_mode'],
        'completion_goal': fixed['completion_goal'],
        'page_limit_per_company': fixed['page_limit_per_company'],
        'page_limit_total': fixed['page_limit_total'],
        'request_observation_protocol': 'observed-2',
        'lookup_observation_protocol': 'observed-2',
        'timing_protocol': 'observed-1',
        'release': release,
        'spec_version': version,
        'research': {
            'input_open': True,
            'candidate_limit': fixed['candidate_limit'],
            'time_limit_seconds': fixed['time_limit_seconds'],
            'admitted_count': 0,
            'checkpoint': {},
            'external_tool_usage': {},
            'stop_reason': None,
        },
        # Kept apart from plan.research so a later extend-research cannot erase
        # the time limit the user confirmed.
        'initial_limits': {
            'time_limit_seconds': fixed['time_limit_seconds'],
            'candidate_limit': fixed['candidate_limit'],
            'candidate_multiplier': fixed['candidate_multiplier'],
            'page_limit_per_company': fixed['page_limit_per_company'],
            'page_limit_total': fixed['page_limit_total'],
        },
        'page_budget_contract': fixed['page_budget_contract'],
        'segment_brief': {
            'brief_version': BRIEF_VERSION,
            'spec_hash': spec_hash,
            'confirm_hash': confirm_hash,
            'confirmation_recorded_at': recorded_at,
        },
    }
    if version == 2:
        from app.rules.policy import policy_summary
        plan['rules_version'] = policy_summary(spec)['rule_version']
        plan['evidence_version'] = spec['evidence_policy']['evidence_version']
        plan['execution'] = {**spec['execution_policy'], 'circuit': {'open': False}}
    return plan


def historical_plan_matches(stored_plan, spec, version):
    """True when an old run's initial fixed plan still equals this confirmation.

    Runtime circuit state and a later research extension are not part of the
    original confirmation. A mismatch must not be repaired in place.
    """
    if not isinstance(stored_plan, dict):
        return False
    fixed = fixed_plan_values(spec, version)
    if stored_plan.get('spec_version') != version:
        return False
    if stored_plan.get('page_limit_per_company') != fixed['page_limit_per_company']:
        return False
    if stored_plan.get('page_limit_total') != fixed['page_limit_total']:
        return False
    if stored_plan.get('execution_mode') != fixed['execution_mode']:
        return False
    if stored_plan.get('completion_goal') != fixed['completion_goal']:
        return False
    research = stored_plan.get('research') or {}
    if research.get('candidate_limit') != fixed['candidate_limit']:
        return False
    initial = stored_plan.get('initial_limits') or {}
    initial_time = initial.get('time_limit_seconds', research.get('time_limit_seconds'))
    if initial_time != fixed['time_limit_seconds']:
        return False
    if version == 2:
        if stored_plan.get('rules_version') != RULE_VERSION:
            return False
        if stored_plan.get('evidence_version') != spec['evidence_policy']['evidence_version']:
            return False
        stored_exec = {key: value for key, value in (stored_plan.get('execution') or {}).items()
                       if key != 'circuit'}
        if stored_exec != spec['execution_policy']:
            return False
    return True


def inspect_segment(payload):
    """Normalize ``payload`` and return the offline brief. Raises on invalid input."""
    if not isinstance(payload, dict):
        raise AppError('INVALID_INPUT', '客群配置必须是 JSON 对象')
    normalized, version = parse_segment(payload)
    provided = _provided_paths(payload, version)
    defaults = _defaults_applied(normalized, version, provided)
    spec_hash = _hash(normalized)
    material = confirm_material(normalized, version, spec_hash)
    confirm_hash = _hash(material)
    summary = _summary(normalized, version, material, spec_hash, confirm_hash)
    return {
        'spec': normalized,
        'spec_version': version,
        'spec_hash': spec_hash,
        'confirmation_summary': summary,
        'confirm_hash': confirm_hash,
        'defaults_applied': defaults,
        'validation': {'ok': True, 'errors': []},
        'pending_hints': _pending_hints(normalized, version, defaults),
    }


def require_confirmation(payload, confirm_hash):
    """Reject a missing or stale hash. The current hash is returned only for a valid spec."""
    prepared = inspect_segment(payload)
    supplied = confirm_hash if isinstance(confirm_hash, str) else ''
    if supplied != prepared['confirm_hash']:
        raise AppError(
            'SEGMENT_NOT_CONFIRMED',
            '创建前需要与当前配置一致的确认哈希；请先运行 brief，展示摘要并得到明确确认后再创建',
            409,
            details={'confirm_hash': prepared['confirm_hash']},
        )
    return prepared


def frozen_v1_document(customer_spec):
    """A stored v1 spec is a complete SegmentSpec dump with no version field.

    New unversioned tasks omit that full default key set and now normalize as
    v2. The evidence prototype must not promote a historical dump.
    """
    if not isinstance(customer_spec, dict) or 'spec_version' in customer_spec:
        return False
    if 'qualification_policy' in customer_spec or 'evidence_policy' in customer_spec:
        return False
    fields = set(SegmentSpec.model_fields)
    if not fields <= set(customer_spec):
        return False
    try:
        dumped = SegmentSpec.model_validate({key: customer_spec[key] for key in fields}).model_dump()
    except Exception:
        return False
    return {key: customer_spec[key] for key in fields} == dumped


def _provided_paths(payload, version):
    if version == 1:
        body = {key: value for key, value in payload.items() if key != 'spec_version'}
        model = SegmentSpec.model_validate(body)
        return set(model.model_fields_set)
    body = payload if 'spec_version' in payload else {**payload, 'spec_version': 2}
    model = SegmentSpecV2.model_validate(body)
    paths = set(model.model_fields_set)
    if 'spec_version' not in payload:
        paths.discard('spec_version')
    policy = model.qualification_policy
    if 'qualification_policy' in model.model_fields_set and policy is not None:
        paths.update(f'qualification_policy.{name}' for name in policy.model_fields_set)
        if 'contact_policy' in policy.model_fields_set:
            paths.update(f'qualification_policy.contact_policy.{name}'
                         for name in policy.contact_policy.model_fields_set)
        for bucket in ('required_all', 'exclude_any'):
            if bucket not in policy.model_fields_set:
                continue
            for index, condition in enumerate(getattr(policy, bucket)):
                paths.update(f'qualification_policy.{bucket}[{index}].{name}'
                             for name in condition.model_fields_set)
    if 'evidence_policy' in model.model_fields_set and model.evidence_policy is not None:
        paths.update(f'evidence_policy.{name}' for name in model.evidence_policy.model_fields_set)
    if 'execution_policy' in model.model_fields_set and model.execution_policy is not None:
        paths.update(f'execution_policy.{name}' for name in model.execution_policy.model_fields_set)
    return paths


def _defaults_applied(spec, version, provided):
    rows = []
    for path in _walk_paths(spec):
        if '[]' in path or path in provided:
            continue
        rows.append({'path': path, 'value': _redact_value(path, _lookup(spec, path)),
                     'source': '默认值'})
    for key, value in fixed_plan_values(spec, version).items():
        rows.append({'path': 'fixed_plan.' + key, 'value': value, 'source': '固定计划默认值'})
    rows.sort(key=lambda item: item['path'])
    return rows


def _pending_hints(spec, version, defaults):
    hints = []
    if version == 2 and spec['qualification_policy'].get('derived_from') == 'v1_fields':
        hints.append('资格条件由未单列的行业、国家和相关字段派生，请核对是否等于原话。')
    default_paths = [item['path'] for item in defaults if not item['path'].startswith('fixed_plan.')]
    if default_paths:
        hints.append('以下生效值来自核心默认，而不是本次输入：' + '、'.join(default_paths) + '。')
    if not spec.get('exclude_keywords') and not (
            version == 2 and spec['qualification_policy'].get('exclude_any')):
        hints.append('未设置排除条件；未提及不会被当成排除。')
    hints.append(SUPPRESSION_NOTE)
    if spec.get('exclusions'):
        hints.append('spec.exclusions 已计入配置；摘要只给出条数和哈希。' + CUSTOMER_FILE_NOTE)
    else:
        hints.append(CUSTOMER_FILE_NOTE)
    hints.extend(EVIDENCE_LIMIT_MESSAGES)
    hints.append(NON_PROMISE)
    return hints


def _summary(spec, version, material, spec_hash, confirm_hash):
    fields = _fields(spec, version, material)
    covered = {item['path'] for item in fields}
    missing = [path for path in _walk_paths(spec) if path not in covered]
    if missing:
        raise AppError('BRIEF_MAPPING_GAP', '确认摘要缺少字段映射，不能生成确认哈希',
                       details={'fields': missing})
    fixed = material['fixed_plan']
    text = _sections(spec, version, material, fields, spec_hash, confirm_hash)
    return {
        'brief_version': BRIEF_VERSION,
        'spec_version': version,
        'rule_version': material['rule_version'],
        'rule_note': _rule_note(version),
        'spec_hash': spec_hash,
        'confirm_hash': confirm_hash,
        'fields': fields,
        'sections': text,
        'global_suppression': SUPPRESSION_NOTE,
        'customer_file_exclusion': _customer_file(spec),
        'evidence_limits': {
            'codes': dict(EVIDENCE_LIMITS),
            'messages': list(EVIDENCE_LIMIT_MESSAGES),
        },
        'non_promise': NON_PROMISE,
        'effective_budget': {
            'quantity': spec['quantity'],
            'candidate_multiplier': fixed['candidate_multiplier'],
            'candidate_limit': fixed['candidate_limit'],
            'time_limit_seconds': fixed['time_limit_seconds'],
            'time_limit_hours': fixed['time_limit_seconds'] // 3600,
            'page_limit_per_company': fixed['page_limit_per_company'],
            'page_limit_total': fixed['page_limit_total'],
            'paid_sources': fixed['paid_sources'],
            'budget': fixed['budget'],
        },
    }


def _rule_note(version):
    if version == 2:
        return 'v2 批次冻结规则 ' + RULE_VERSION + '。已有 v1 批次保持其冻结语义，不会用本规则重判。'
    return 'v1 历史资格语义（行业词命中、国家、排除词、企业类型和规模）。不是 ' + RULE_VERSION + ' 条件模型，也不会被改写成 v2。'


def _customer_file(spec):
    values = list(spec.get('exclusions') or [])
    return {
        'in_effect': False,
        'message': CUSTOMER_FILE_NOTE,
        'exclusions_count': len(values),
        'exclusions_sha256': _hash(values) if values else None,
    }


def _fields(spec, version, material):
    rows = []
    for path in _walk_paths(spec):
        label = LABELS.get(path)
        if not label:
            raise AppError('BRIEF_MAPPING_GAP', '确认摘要缺少字段映射，不能生成确认哈希',
                           details={'fields': [path]})
        rows.append({'path': path, 'label': label, 'value': _field_value(spec, path),
                     'display': _field_display(spec, path)})
    for key, value in material['fixed_plan'].items():
        path = 'fixed_plan.' + key
        rows.append({'path': path, 'label': LABELS[path], 'value': value,
                     'display': _fixed_display(key, value, spec, version)})
    return rows


def _field_value(spec, path):
    if path == 'exclusions':
        values = list(spec.get('exclusions') or [])
        return {'count': len(values), 'sha256': _hash(values) if values else None}
    if '[]' in path:
        return _condition_column(spec, path)
    return _lookup(spec, path)


def _field_display(spec, path):
    if path in {'qualification_policy', 'qualification_policy.contact_policy',
                'evidence_policy', 'execution_policy'}:
        return '见本对象下列出的各项生效值'
    if path == 'exclusions':
        value = _field_value(spec, path)
        if not value['count']:
            return '无（未写入排除标识，不会因此排除任何企业、联系人、邮箱或电话）'
        return f"{value['count']} 项，列表哈希 {value['sha256']}。不展示具体企业、联系人、邮箱或电话。"
    if path == 'countries':
        return '、'.join(_country_label(code) for code in spec.get('countries') or [])
    if path == 'paid_sources':
        return '不使用付费来源' if spec.get('paid_sources') is False else str(spec.get('paid_sources'))
    if path == 'budget':
        return '0（无付费金额预算）' if spec.get('budget') == 0 else str(spec.get('budget'))
    if path == 'qualification_policy.country_scope':
        value = _lookup(spec, path)
        return SCOPE_LABELS.get(value, str(value)) + f'（{value}，字段 {COUNTRY_SCOPES.get(value, "")}）'
    if path == 'qualification_policy.required_all':
        return _condition_block(spec, 'required_all', '全部满足')
    if path == 'qualification_policy.exclude_any':
        return _condition_block(spec, 'exclude_any', '任一命中即排除')
    if path == 'qualification_policy.contact_policy.mode':
        value = _lookup(spec, path)
        return MODE_LABELS.get(value, str(value))
    if path == 'qualification_policy.contact_policy.allowed_types':
        values = _lookup(spec, path) or []
        if not values:
            return '未指定'
        return '、'.join(CHANNEL_TYPES.get(item, {}).get('label', item) for item in values)
    if path == 'qualification_policy.contact_policy.named_person_required':
        return '需要具名联系人' if _lookup(spec, path) else '不要求具名联系人'
    if path == 'qualification_policy.contact_policy.required_roles':
        value = _lookup(spec, path) or []
        return '不限' if not value else '、'.join(str(item) for item in value)
    if path == 'qualification_policy.derived_from':
        value = _lookup(spec, path)
        if value == 'v1_fields':
            return '由行业、国家等字段派生（derived_from=v1_fields），不是另行写死的条件'
        return '调用方显式写入（derived_from=explicit）'
    if path == 'execution_policy.native_inflight':
        value = _lookup(spec, path)
        return f'{value}（登记上限，不是已经实现的额外并发）'
    if path == 'execution_policy.company_workers':
        value = _lookup(spec, path)
        return f'{value}（上限 {LIMITS["company_workers"]["max"]}，本版本不提高该上限）'
    if path == 'execution_policy.page_limit_per_company':
        value = _lookup(spec, path)
        if value in (None, ''):
            return '未指定。批次生效页上限见固定计划中的 page_limit_per_company，不把未指定当成 0 或排除。'
        return f'{value}。该字段被记录；批次派发仍以固定计划中的 page_limit_per_company 为生效页上限。'
    if path.startswith('qualification_policy.required_all[]') or path.startswith('qualification_policy.exclude_any[]'):
        column = _condition_column(spec, path)
        if not column:
            return '不限' if 'exclude_any' in path else '未指定'
        return '；'.join(str(item) for item in column)
    value = _lookup(spec, path)
    return _plain(path, value)


def _fixed_display(key, value, spec, version):
    if key == 'time_limit_seconds':
        return f'{value} 秒（{value // 3600} 小时）。这是初始累计非暂停时限，不是完成承诺。'
    if key == 'candidate_multiplier':
        return f'{value}（候选上限 = 目标数量 × {value}）'
    if key == 'candidate_limit':
        return f'{value}（目标 {spec["quantity"]} × {LIMITS["candidate_multiplier"]}）'
    if key == 'page_limit_per_company':
        return f'{value}。这是创建时冻结并在派发时生效的单企业页上限。'
    if key == 'page_limit_total':
        return str(value)
    if key == 'page_budget_contract':
        if not value:
            return '不冻结。自动共享目录采集拒绝。JS 渲染不可用。'
        return (str(value) + '。只表示共享目录页面可以按页记账；'
                '不等于目录自动采集可用，也不等于自动 JS 渲染可用。'
                '没有该合同的批次拒绝自动共享目录采集。')
    if key == 'paid_sources':
        return '不使用付费来源' if value is False else str(value)
    if key == 'budget':
        return '0（无付费金额预算）' if value == 0 else str(value)
    if key == 'execution_mode':
        return 'skill_research（由核心执行资格、渠道、预算和交付）'
    if key == 'completion_goal':
        note = '可交付联系渠道' if version == 2 else '可交付联系渠道（v1 历史计数）'
        return f'{value}（{note}）'
    return str(value)


def _sections(spec, version, material, fields, spec_hash, confirm_hash):
    by_path = {item['path']: item for item in fields}
    fixed = material['fixed_plan']

    def line(path):
        item = by_path[path]
        return f"{item['label']}：{item['display']}"

    qualification = [
        f"配置版本：{version}",
        '规则版本：' + material['rule_version'] + '。' + _rule_note(version),
        line('name'), line('industry'), line('industry_codes'), line('countries'),
    ]
    if version == 2:
        qualification.extend([
            line('qualification_policy.country_scope'),
            '必要条件关系：AND。' + line('qualification_policy.required_all'),
            line('qualification_policy.derived_from'),
        ])
    else:
        qualification.append('国家口径：v1 按 countries 列表做历史国家判断，不使用经营地/总部/注册地条件模型。')
    qualification.extend([
        line('company_types'), line('keywords'), line('regions'),
        line('employee_range'), line('revenue_range'), line('example_domains'), line('notes'),
    ])
    exclusions = [
        line('exclude_keywords'), line('exclusions'), line('languages'),
    ]
    if version == 2:
        exclusions.insert(0, '排除条件关系：OR。' + line('qualification_policy.exclude_any'))
        exclusions.append('未知值：必要条件的未知或冲突进入待核；排除条件默认未知待核，只有显式 allow_unconfirmed 才不阻塞。冲突始终待核。')
    else:
        exclusions.append('未知值：v1 证据不足保持不确定，不把缺失写成不符合，也不默认排除未提及的类型。')
    channels = [
        line('quantity'),
        line('freshness_days'),
    ]
    if version == 2:
        channels.extend([
            line('qualification_policy.contact_policy.mode'),
            line('qualification_policy.contact_policy.allowed_types'),
            line('qualification_policy.contact_policy.named_person_required'),
            line('qualification_policy.contact_policy.required_roles'),
            line('evidence_policy.evidence_version'),
            line('evidence_policy.freshness_days'),
        ])
    else:
        channels.append(line('roles') + '。v1 没有 any/all 渠道模型；岗位列表为空表示不限。')
    budget = [
        line('fixed_plan.time_limit_seconds'),
        line('fixed_plan.candidate_multiplier'),
        line('fixed_plan.candidate_limit'),
        line('fixed_plan.page_limit_per_company'),
        line('fixed_plan.page_limit_total'),
        line('fixed_plan.page_budget_contract'),
        line('fixed_plan.paid_sources'),
        line('fixed_plan.budget'),
        NON_PROMISE,
    ]
    execution = [
        line('fixed_plan.execution_mode'),
        line('fixed_plan.completion_goal'),
    ]
    if version == 2:
        execution.extend([
            line('execution_policy.company_workers'),
            line('execution_policy.native_inflight'),
            line('execution_policy.snapshot_every_new_qualified'),
            line('execution_policy.snapshot_interval_seconds'),
            line('execution_policy.circuit_breaker_failures'),
            line('execution_policy.page_limit_per_company'),
        ])
    else:
        execution.append('v1 不冻结 company_workers；处理保持串行。--workers 大于 1 会被拒绝。')
    execution.append(SUPPRESSION_NOTE)
    limits = list(EVIDENCE_LIMIT_MESSAGES) + [CUSTOMER_FILE_NOTE]
    hashes = [
        f'配置哈希 spec_hash：{spec_hash}',
        f'确认范围哈希 confirm_hash：{confirm_hash}',
        '哈希只表示本次提交与这份生效配置一致，不能证明确认人、确认原话或确认发生的时间。直接调用核心可以自行计算哈希，这不构成身份审计。',
        f'确认范围版本：{BRIEF_VERSION}。范围内的有效值一旦变化，旧哈希失效，必须重新展示并确认。',
    ]
    # Touch fixed so a forgotten budget field fails this builder rather than shipping.
    assert fixed['time_limit_seconds'] == material['fixed_plan']['time_limit_seconds']
    return [
        {'id': 'qualification', 'title': '客群与资格', 'lines': qualification},
        {'id': 'exclusions', 'title': '排除与未知值处理', 'lines': exclusions},
        {'id': 'channels', 'title': '数量、渠道、证据要求', 'lines': channels},
        {'id': 'budget', 'title': '初始时间、页面与付费预算', 'lines': budget},
        {'id': 'execution', 'title': '执行参数', 'lines': execution},
        {'id': 'limits', 'title': '取证限制', 'lines': limits},
        {'id': 'hashes', 'title': '哈希', 'lines': hashes},
    ]


def _walk_paths(value, prefix=''):
    paths = []
    if prefix:
        paths.append(prefix)
    if isinstance(value, dict):
        for key in value:
            child = f'{prefix}.{key}' if prefix else key
            nested = value[key]
            if isinstance(nested, dict):
                paths.extend(_walk_paths(nested, child))
            elif isinstance(nested, list) and nested and all(isinstance(item, dict) for item in nested):
                paths.append(child)
                keys = []
                for item in nested:
                    for item_key in item:
                        if item_key not in keys:
                            keys.append(item_key)
                for item_key in keys:
                    paths.append(f'{child}[].{item_key}')
            else:
                paths.append(child)
    return paths


def _lookup(spec, path):
    current = spec
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _condition_column(spec, path):
    bucket, key = path.split('[].')
    rows = _lookup(spec, bucket) or []
    return [row.get(key) for row in rows]


def _condition_block(spec, bucket, relation):
    rows = spec['qualification_policy'].get(bucket) or []
    if not rows:
        return '不限' if bucket == 'exclude_any' else '未指定'
    rendered = [_condition_line(row) for row in rows]
    return relation + '：' + ' | '.join(rendered)


def _condition_line(condition):
    field = condition.get('field')
    field_label = FIELDS.get(field, {}).get('label', field)
    operator = OPERATORS.get(condition.get('operator'), {}).get('label', condition.get('operator'))
    unknown = UNKNOWN_LABELS.get(condition.get('unknown_policy'), condition.get('unknown_policy'))
    note = condition.get('note') or ''
    text = f"{condition.get('id')}：{field_label}（{field}）{operator} {condition.get('value')}；未知值：{unknown}"
    if note:
        text += f'；备注：{note}'
    return text


def _plain(path, value):
    leaf = path.rsplit('.', 1)[-1]
    if value is None:
        return '未指定'
    if path in UNSPECIFIED_PATHS or leaf in UNSPECIFIED_PATHS:
        if value == '' or value == []:
            return '未指定'
    if path in UNLIMITED_PATHS or leaf in UNLIMITED_PATHS:
        if value == '' or value == []:
            return '不限'
    if isinstance(value, list):
        return '、'.join(str(item) for item in value) if value else '未指定'
    if isinstance(value, bool):
        return '是' if value else '否'
    if value == '':
        return '未指定'
    return str(value)


def _redact_value(path, value):
    if path == 'exclusions':
        values = list(value or [])
        return {'count': len(values), 'sha256': _hash(values) if values else None}
    return value


def _country_label(code):
    import pycountry
    record = pycountry.countries.get(alpha_2=str(code or ''))
    name = record.name if record is not None else '未知名称'
    return f'{code}（{name}）'
