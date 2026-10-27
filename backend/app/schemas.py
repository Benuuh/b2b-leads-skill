"""Validated command inputs, independent of transport and persistence."""
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.capabilities import COUNTRY_SCOPES, normalize_condition


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def domain(value: str) -> str:
    host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").lower().rstrip(".").removeprefix("www.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("域名格式无效")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", host):
        raise ValueError("请输入有效企业域名")
    return host


class SegmentSpec(Strict):
    name: str = Field(min_length=1, max_length=120)
    industry: str = Field(default="", max_length=500)
    industry_codes: list[str] = Field(default_factory=list, max_length=30)
    countries: list[str] = Field(min_length=1, max_length=30)
    regions: list[str] = Field(default_factory=list, max_length=50)
    employee_range: str = Field(default="", max_length=120)
    revenue_range: str = Field(default="", max_length=120)
    company_types: list[str] = Field(default_factory=list, max_length=50)
    roles: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=100)
    example_domains: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=1000)
    languages: list[str] = Field(default_factory=list, max_length=20)
    quantity: int = Field(default=200, ge=50, le=1000)
    freshness_days: int = Field(default=180, ge=1, le=730)
    notes: str = Field(default="", max_length=4000)
    paid_sources: Literal[False] = False
    budget: Literal[0] = 0

    @field_validator("countries")
    @classmethod
    def countries_valid(cls, values):
        values = list(dict.fromkeys(v.upper() for v in values))
        if any(pycountry.countries.get(alpha_2=value) is None for value in values):
            raise ValueError("国家必须使用有效 ISO 两位代码，如 US、DE")
        return values

    @field_validator("industry_codes", "regions", "company_types", "roles", "keywords", "exclude_keywords", "exclusions", "languages")
    @classmethod
    def bounded_strings(cls, values):
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("列表项不能为空或超过500字符")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("example_domains")
    @classmethod
    def domains_valid(cls, values):
        return list(dict.fromkeys(domain(value) for value in values))

    @model_validator(mode="after")
    def sufficient(self):
        if not (self.industry or self.keywords or self.example_domains):
            raise ValueError("至少填写行业、关键词或示例企业中的一项")
        for value in self.exclusions:
            if "@" in value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                raise ValueError("排除名单中的邮箱格式无效")
            if "://" in value:
                domain(value)
        return self


class Condition(Strict):
    id: str = Field(min_length=1, max_length=80)
    field: str = Field(min_length=1, max_length=120)
    operator: str = Field(min_length=1, max_length=40)
    value: str | list[str]
    unknown_policy: Literal["review", "allow_unconfirmed"] = "review"
    note: str = Field(default="", max_length=500)

    @field_validator("id")
    @classmethod
    def identifier(cls, value):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value):
            raise ValueError("条件 id 必须以字母开头，仅包含字母、数字、点、横线和下划线")
        return value


class ContactPolicy(Strict):
    mode: Literal["any", "all"] = "any"
    allowed_types: list[Literal["business_email", "business_phone", "contact_form"]] = Field(
        default_factory=lambda: ["business_email", "business_phone", "contact_form"], min_length=1, max_length=3)
    named_person_required: bool = False
    required_roles: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_types")
    @classmethod
    def unique_types(cls, values):
        return list(dict.fromkeys(values))

    @field_validator("required_roles")
    @classmethod
    def roles_valid(cls, values):
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def named_roles(self):
        if self.required_roles and not self.named_person_required:
            raise ValueError("指定岗位要求 named_person_required 为 true")
        return self


class QualificationPolicy(Strict):
    country_scope: Literal["operating", "headquarters", "registration"] = "operating"
    required_all: list[Condition] = Field(default_factory=list, max_length=50)
    exclude_any: list[Condition] = Field(default_factory=list, max_length=50)
    contact_policy: ContactPolicy = Field(default_factory=ContactPolicy)
    derived_from: Literal["v1_fields", "explicit"] = "explicit"

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [condition.id for condition in [*self.required_all, *self.exclude_any]]
        if len(ids) != len(set(ids)):
            raise ValueError("条件 id 必须唯一")
        return self


class EvidencePolicy(Strict):
    freshness_days: int = Field(default=180, ge=1, le=730)
    evidence_version: Literal["evidence-1", "evidence-2"] = "evidence-2"


class ExecutionPolicy(Strict):
    company_workers: int = Field(default=2, ge=1, le=4)
    native_inflight: int = Field(default=2, ge=1, le=2)
    snapshot_every_new_qualified: int = Field(default=10, ge=0, le=1000)
    snapshot_interval_seconds: int = Field(default=900, ge=60, le=86400)
    circuit_breaker_failures: int = Field(default=5, ge=3, le=20)
    page_limit_per_company: int | None = Field(default=None, ge=1, le=10)


class SegmentSpecV2(SegmentSpec):
    spec_version: Literal[2]
    qualification_policy: QualificationPolicy | None = None
    evidence_policy: EvidencePolicy | None = None
    execution_policy: ExecutionPolicy | None = None

    @model_validator(mode="after")
    def coherent_overrides(self):
        from app.errors import AppError
        fields_set = self.model_fields_set
        # One semantic value per setting: an explicit top-level legacy field and
        # its policy form may not disagree.
        if self.evidence_policy is not None and "freshness_days" in fields_set \
                and self.freshness_days != self.evidence_policy.freshness_days:
            raise AppError('CONFLICTING_CONFIG', 'freshness_days 与 evidence_policy.freshness_days 冲突；只保留一个口径',
                           details={'fields': ['freshness_days', 'evidence_policy.freshness_days']})
        policy = self.qualification_policy
        if policy is not None and self.roles and policy.contact_policy.required_roles \
                and sorted(self.roles) != sorted(policy.contact_policy.required_roles):
            raise AppError('CONFLICTING_CONFIG', 'roles 与 contact_policy.required_roles 冲突；只保留一个口径',
                           details={'fields': ['roles', 'qualification_policy.contact_policy.required_roles']})
        return self


class ResearchInput(Strict):
    candidates: list[dict] = Field(default_factory=list, max_length=20)
    checkpoint: dict | None = None
    external_tool_usage: dict[str, int | None] | None = None
    finalize: bool = False

    @field_validator("external_tool_usage", mode="before")
    @classmethod
    def usage_counts(cls, value):
        if value is not None and (not isinstance(value, dict) or len(value) > 50 or any(
            not isinstance(k, str) or not 1 <= len(k) <= 80 or
            (v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0))
            for k, v in value.items()
        )):
            raise ValueError("外部工具计数必须是非负整数或 null")
        return value

    @field_validator("checkpoint")
    @classmethod
    def checkpoint_size(cls, value):
        if value is not None and len(json.dumps(value, ensure_ascii=False)) > 20000:
            raise ValueError("检查点最多20000字符")
        return value


class SupplementInput(Strict):
    """Incremental evidence for one retained enterprise; never a new candidate."""
    expected_revision: int = Field(ge=1, le=1_000_000)
    candidates: list[dict] = Field(min_length=1, max_length=20)
    external_tool_usage: dict[str, int | None] | None = None

    @field_validator("external_tool_usage", mode="before")
    @classmethod
    def usage_counts(cls, value):
        return ResearchInput.usage_counts(value)


class SuppressionInput(Strict):
    type: Literal["domain", "company", "email", "phone"]
    value: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="用户排除", max_length=1000)


def _default_policy(spec: SegmentSpec) -> dict[str, Any]:
    """Derive the equivalent v1 policy so a v2 run is fully self-describing."""
    required, excluded = [], []
    country_field = COUNTRY_SCOPES['operating']
    required.append({'id': 'country', 'field': country_field, 'operator': 'in',
                     'value': list(spec.countries), 'unknown_policy': 'review'})
    terms = spec.keywords or [w.strip() for w in re.split(r'[/,;，；]', spec.industry) if w.strip()]
    if terms:
        required.append({'id': 'industry', 'field': 'industry.match', 'operator': 'matches',
                         'value': terms, 'unknown_policy': 'review'})
    if spec.company_types:
        required.append({'id': 'company_type', 'field': 'business.type', 'operator': 'includes_any',
                         'value': list(spec.company_types), 'unknown_policy': 'review'})
    if spec.regions:
        required.append({'id': 'region', 'field': 'entity.region', 'operator': 'includes_any',
                         'value': list(spec.regions), 'unknown_policy': 'review'})
    if spec.employee_range:
        required.append({'id': 'employees', 'field': 'size.employees', 'operator': 'within',
                         'value': spec.employee_range, 'unknown_policy': 'review'})
    if spec.revenue_range:
        required.append({'id': 'revenue', 'field': 'size.revenue', 'operator': 'within',
                         'value': spec.revenue_range, 'unknown_policy': 'review'})
    if spec.exclude_keywords:
        excluded.append({'id': 'exclude_terms', 'field': 'evidence.exclude_terms', 'operator': 'contains_any',
                         'value': list(spec.exclude_keywords), 'unknown_policy': 'review'})
    return {
        'country_scope': 'operating', 'required_all': required, 'exclude_any': excluded,
        'contact_policy': {'mode': 'any', 'allowed_types': ['business_email', 'business_phone', 'contact_form'],
                           'named_person_required': bool(spec.roles),
                           'required_roles': list(spec.roles)},
        'derived_from': 'v1_fields',
    }


def _country_values(condition) -> list[str]:
    value = condition.get('value')
    values = value if isinstance(value, list) else [value]
    return [str(item).upper() for item in values if isinstance(item, str)]


def _merge_policy(explicit: dict, spec: SegmentSpec) -> dict:
    """Explicit conditions win; legacy exclusions and country targets still apply."""
    policy = {**explicit}
    required = list(policy.get('required_all') or [])
    excluded = list(policy.get('exclude_any') or [])
    scope_field = COUNTRY_SCOPES[policy.get('country_scope', 'operating')]
    country_conditions = [item for item in required if item.get('field') in {
        'entity.country_code', 'entity.headquarters_country', 'entity.registered_country'}]
    if country_conditions and not set(spec.countries).issubset(set(_country_values(country_conditions[0]))):
        from app.errors import AppError
        raise AppError('CONFLICTING_CONFIG', 'countries 与 qualification_policy 的国家条件矛盾',
                       details={'countries': list(spec.countries), 'condition_id': country_conditions[0].get('id')})
    if not country_conditions:
        required.insert(0, {'id': 'country', 'field': scope_field, 'operator': 'in',
                            'value': list(spec.countries), 'unknown_policy': 'review'})
    if spec.exclude_keywords and not any(item['field'] == 'evidence.exclude_terms' for item in excluded):
        excluded.append({'id': 'exclude_terms', 'field': 'evidence.exclude_terms',
                         'operator': 'contains_any', 'value': list(spec.exclude_keywords),
                         'unknown_policy': 'review'})
    contact = dict(policy.get('contact_policy') or {})
    if spec.roles and not contact.get('required_roles'):
        contact.update(named_person_required=True, required_roles=list(spec.roles))
    policy['required_all'] = required
    policy['exclude_any'] = excluded
    policy['contact_policy'] = contact
    return policy


def _normalize_policy(policy: dict) -> dict:
    result = dict(policy)
    result['required_all'] = [normalize_condition(item, path='required_all')
                              for item in policy.get('required_all') or []]
    result['exclude_any'] = [normalize_condition(item, path='exclude_any')
                             for item in policy.get('exclude_any') or []]
    result.setdefault('contact_policy', {})
    contact = {'mode': result['contact_policy'].get('mode', 'any'),
               'allowed_types': list(result['contact_policy'].get('allowed_types')
                                     or ['business_email', 'business_phone', 'contact_form']),
               'named_person_required': bool(result['contact_policy'].get('named_person_required')),
               'required_roles': list(result['contact_policy'].get('required_roles') or [])}
    if contact['required_roles'] and not contact['named_person_required']:
        raise ValueError("指定岗位要求 named_person_required 为 true")
    result['contact_policy'] = contact
    return result


def parse_segment(payload) -> tuple[dict, int]:
    """Return the frozen normalized configuration and its spec version.

    An unversioned payload keeps the historical v1 normalization byte-for-byte,
    so a retried ``create`` recomputes the same hash for existing runs.
    """
    if not isinstance(payload, dict):
        raise ValueError("客群配置必须是 JSON 对象")
    version = payload.get('spec_version', 1)
    if isinstance(version, bool) or version not in (1, 2):
        from app.errors import AppError
        raise AppError('UNSUPPORTED_SPEC_VERSION', '不支持的 spec_version；本核心仅接受 1 或 2',
                       details={'spec_version': version, 'supported': [1, 2]})
    if version == 1:
        body = {key: value for key, value in payload.items() if key != 'spec_version'}
        return SegmentSpec.model_validate(body).model_dump(), 1
    try:
        spec = SegmentSpecV2.model_validate(payload)
    except Exception:
        raise
    normalized = spec.model_dump()
    explicit = normalized.get('qualification_policy')
    policy = _default_policy(spec) if explicit is None else _merge_policy(explicit, spec)
    try:
        normalized['qualification_policy'] = _normalize_policy(policy)
    except ValueError as error:
        from app.errors import AppError
        raise AppError('CONFLICTING_CONFIG', str(error)) from error
    evidence = dict(normalized.get('evidence_policy') or {})
    evidence.setdefault('freshness_days', normalized['freshness_days'])
    evidence.setdefault('evidence_version', 'evidence-2')
    normalized['evidence_policy'] = evidence
    normalized['freshness_days'] = evidence['freshness_days']
    execution = dict(normalized.get('execution_policy') or {})
    for key, value in {'company_workers': 2, 'native_inflight': 2, 'snapshot_every_new_qualified': 10,
                       'snapshot_interval_seconds': 900, 'circuit_breaker_failures': 5,
                       'page_limit_per_company': None}.items():
        execution.setdefault(key, value)
    normalized['execution_policy'] = execution
    normalized['spec_version'] = 2
    return normalized, 2


def spec_version(spec: dict) -> int:
    """Frozen runs without a version field are v1, never the latest version."""
    value = (spec or {}).get('spec_version', 1)
    return value if value in (1, 2) else 1


def policy_of(spec: dict) -> dict | None:
    return (spec or {}).get('qualification_policy') if spec_version(spec) == 2 else None
