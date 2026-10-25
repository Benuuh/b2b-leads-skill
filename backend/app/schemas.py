"""Validated command inputs, independent of transport and persistence."""
import json
import re
from typing import Literal
from urllib.parse import urlsplit

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class SuppressionInput(Strict):
    type: Literal["domain", "company", "email", "phone"]
    value: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="用户排除", max_length=1000)
