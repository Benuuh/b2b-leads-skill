# 客群配置（SegmentSpec）

同一 Skill 与核心通过不同配置研究不同行业。配置在 `create` 时冻结，run 后续只读。

## 版本

- **v1**：无 `spec_version` 的旧输入，字段与语义保持原样；存为 `spec_version: 1`。v1 run 只接受原有候选字段，不接受版本化事实字段。
- **v2**：`spec_version: 2`，新增 `qualification_policy`、`evidence_policy`、`execution_policy`。新能力只由 v2 run 显式启用。
- 其他版本：`UNSUPPORTED_SPEC_VERSION`，不自动降级或剥离条件。

`capabilities` 可离线查询支持的版本、字段、运算符、默认值与限额；不要凭记忆写条件。

## v2 结构

```json
{
  "spec_version": 2,
  "name": "德国可重复使用工业包装制造商",
  "industry": "industrial packaging",
  "countries": ["DE"],
  "quantity": 200,
  "paid_sources": false,
  "budget": 0,
  "qualification_policy": {
    "country_scope": "headquarters",
    "required_all": [
      {"id": "hq", "field": "entity.headquarters_country", "operator": "in", "value": ["DE"]},
      {"id": "maker", "field": "business.models", "operator": "includes", "value": "manufacturer"},
      {"id": "product", "field": "offering.products", "operator": "provides", "value": "reusable industrial packaging"}
    ],
    "exclude_any": [],
    "contact_policy": {"mode": "any", "allowed_types": ["business_email", "business_phone", "contact_form"],
                       "named_person_required": false, "required_roles": []}
  },
  "evidence_policy": {"freshness_days": 180},
  "execution_policy": {"company_workers": 2, "native_inflight": 2,
                       "snapshot_every_new_qualified": 10, "snapshot_interval_seconds": 900}
}
```

`country_scope` 解释 `countries`：`operating`（默认，经营地）、`headquarters`（总部）、`registration`（注册地）。显式国家条件与 `countries` 矛盾时拒绝创建。

提速优先的新任务显式使用 `execution_policy: {"company_workers":4,"native_inflight":2,"snapshot_every_new_qualified":5,"snapshot_interval_seconds":300}`。默认值仍保持兼容；旧批次按原冻结参数执行。先交 5 家、后续 10–20 家一块用 `advance`，不要为了等待凑满 20 家而延迟首批。并发按不同企业重叠，原 host 限流与 robots/429 规则保持。

## 字段与运算符

| 字段 | 类型 | 运算符 |
|---|---|---|
| `entity.country_code` / `entity.headquarters_country` / `entity.registered_country` | ISO 两位国家 | `in` `not_in` |
| `entity.relationship` | `independent` `subsidiary` `branch` `unknown` | `equals` `in` `not_in` |
| `entity.region` | 文本 | `includes_any` |
| `business.models` | `manufacturer` `distributor` `retailer` `reseller` `integrator` `service_provider` `trading_company` `logistics_provider` `oem` | `includes` `includes_any` |
| `business.type` | 文本 | `includes` `includes_any` |
| `offering.products` / `offering.services` | 文本 | `includes` `includes_any` `provides` |
| `customer.industries` | 文本 | `includes` `includes_any` |
| `size.employees` / `size.revenue` | 区间文本，如 `50-200`、`2-5 million` | `within` |
| `industry.match` | 发现词（仅在缺少显式政策时由 `industry`/`keywords` 派生） | `matches` |
| `evidence.exclude_terms` | 排除词 | `contains_any` |

未注册字段或运算符返回 `UNSUPPORTED_CONDITION`，并给出 `field`/`operator`/`supported`。首期不支持任意嵌套表达式、脚本或 SQL。

## 判定真值

- 必要条件出现 `fail` → 不符合（`excluded`）。
- 排除条件出现 `pass` → 排除。
- 必要条件仍为 `unknown`/`conflict`，或排除条件未知且政策为 `review`，或任一 `conflict` → 待核（`uncertain`）。
- 必要条件全部 `pass`、无排除命中、无待核，且渠道政策满足 → 可交付（`qualified`）。

事实必须同时具备：字段值、主体关联（摘录以企业名开头）、该谓词要求的原文依据。仅声明、搜索摘要、置信分数或自由笔记不足以满足条件；无证据不写成 `fail`。

排除条件默认 `unknown_policy: review`（未知则待核）。仅当用户意图是“排除已知某类”时才可显式 `allow_unconfirmed`；此时未知不阻塞交付，但报告保留未知。`conflict` 在两种政策下都必须复核。必要条件不接受 `allow_unconfirmed`。

## 渠道政策

- `mode: any`（默认）：列出的渠道类型中任一合格即可完成。
- `mode: all`：列出的每种类型都必须有合格值。
- `named_person_required` / `required_roles`：需要具名直连渠道；岗位名匹配联系人标题。
- 每个渠道必须有独立的字段观察（URL、采集时间、含实际值的摘录）；表单需观察到邮箱/电话与留言输入。
- 用户明确要求邮箱与电话或指定岗位时不得为提速降级。

## 默认与限额

- 未提供 `qualification_policy` 时，由 v1 字段派生等价政策：国家、企业类型、地区、规模、行业匹配、排除词；`derived_from` 记录为 `v1_fields`。
- 顶层 `freshness_days` 与 `evidence_policy.freshness_days` 冲突时拒绝（`CONFLICTING_CONFIG`）；生效值同步到顶层供旧消费者读取。
- 核心上限不可扩大：`quantity` 50–1000、候选为目标 3 倍、单企业 10 页、单批 10000 页、单次 `process` 20 个任务/300 秒、累计 8 小时、零付费。
- `execution_policy` 只可在核心范围内收窄：`company_workers` 1–4、`native_inflight` 1–2、`freshness_days` 1–730。

## 第二例：阿联酋零售 IT 集成商

```json
{"spec_version": 2, "name": "阿联酋零售业 IT 集成服务商", "industry": "retail IT integration",
 "countries": ["AE"], "quantity": 200,
 "qualification_policy": {"country_scope": "operating",
   "required_all": [
     {"id": "operating", "field": "entity.country_code", "operator": "in", "value": ["AE"]},
     {"id": "integrator", "field": "offering.services", "operator": "provides",
      "value": "POS and store system integration"},
     {"id": "retail", "field": "customer.industries", "operator": "includes", "value": "retail"}],
   "exclude_any": [],
   "contact_policy": {"mode": "any", "allowed_types": ["contact_form", "business_email"],
                      "named_person_required": false}}}
```

总部未知不妨碍允许分支的任务；合作伙伴的能力不转移给候选；合格表单可以是唯一渠道。与德国包装任务使用同一核心，仅配置不同。
